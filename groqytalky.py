"""
GroqyTalky v0.41 — tray, HUD, keyboard listener, Groq pipeline, and Setup Wizard.

Startup flow:
  1. config.py runs the macOS guard on import.
  2. If no valid GROQ_API_KEY exists, the Setup Wizard runs before anything else.
  3. Once a key is confirmed, background services start (worker thread, listener, tray).
  4. The "Settings / Change Hotkey" tray item re-opens the wizard at any time.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import time
import tkinter as tk
import tkinter.messagebox as messagebox
import winsound
from typing import Callable

import pystray
from PIL import Image

import config
import core
from win_listener import make_keyboard_listener

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger("voice_assistant")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _beep_async(frequency: int, duration_ms: int) -> None:
    def _run() -> None:
        try:
            winsound.Beep(frequency, duration_ms)
        except (RuntimeError, OSError):
            pass
    threading.Thread(target=_run, daemon=True).start()


def _solid_icon(rgb: tuple[int, int, int]) -> Image.Image:
    return Image.new("RGB", (64, 64), rgb)


def _load_tray_icon(filename: str, fallback_rgb: tuple[int, int, int]) -> Image.Image:
    """Load a .ico file from the Icons folder; fall back to a solid colour if missing."""
    try:
        path = config.icons_dir() / filename
        img = Image.open(path).convert("RGBA").resize((64, 64), Image.LANCZOS)
        return img
    except Exception:
        return _solid_icon(fallback_rgb)


# ---------------------------------------------------------------------------
# Hotkey recording widget
# ---------------------------------------------------------------------------

_KEY_NAME_OVERRIDES: dict = {
    "Key.ctrl_l": "Left Ctrl",
    "Key.ctrl_r": "Right Ctrl",
    "Key.shift_l": "Left Shift",
    "Key.shift_r": "Right Shift",
    "Key.alt_l": "Left Alt",
    "Key.alt_r": "Right Alt",
    "Key.cmd_l": "Left Win",
    "Key.cmd_r": "Right Win",
    "Key.caps_lock": "Caps Lock",
    "Key.tab": "Tab",
    "Key.space": "Space",
    "Key.enter": "Enter",
    "Key.backspace": "Backspace",
    "Key.delete": "Delete",
    "Key.esc": "Escape",
    "Key.home": "Home",
    "Key.end": "End",
    "Key.page_up": "Page Up",
    "Key.page_down": "Page Down",
    "Key.up": "↑",
    "Key.down": "↓",
    "Key.left": "←",
    "Key.right": "→",
    "Key.insert": "Insert",
    "Key.print_screen": "Print Screen",
    "Key.scroll_lock": "Scroll Lock",
    "Key.pause": "Pause",
    "Key.num_lock": "Num Lock",
    "Key.f1": "F1", "Key.f2": "F2", "Key.f3": "F3", "Key.f4": "F4",
    "Key.f5": "F5", "Key.f6": "F6", "Key.f7": "F7", "Key.f8": "F8",
    "Key.f9": "F9", "Key.f10": "F10", "Key.f11": "F11", "Key.f12": "F12",
}


def _key_display_name(key) -> str:
    """Return a human-readable name for a pynput key."""
    from pynput.keyboard import Key, KeyCode
    s = str(key)
    if s in _KEY_NAME_OVERRIDES:
        return _KEY_NAME_OVERRIDES[s]
    if isinstance(key, Key):
        return key.name.replace("_", " ").title()
    if isinstance(key, KeyCode):
        if key.char:
            return key.char.upper()
        return f"[vk={key.vk}]"
    return str(key)


def _key_to_config_name(key) -> str:
    """Return the string that config._parse_key() can round-trip."""
    from pynput.keyboard import Key, KeyCode
    if isinstance(key, Key):
        return key.name           # e.g. "ctrl_l", "f8"
    if isinstance(key, KeyCode):
        if key.char:
            return key.char       # single character
        return f"vk_{key.vk}"
    return str(key)


# Modifier keys whose left/right variants should be grouped together.
_MODIFIER_PAIRS: dict[str, str] = {
    "ctrl_l": "ctrl_r",
    "ctrl_r": "ctrl_l",
    "shift_l": "shift_r",
    "shift_r": "shift_l",
    "alt_l": "alt_r",
    "alt_r": "alt_l",
    "cmd_l": "cmd_r",
    "cmd_r": "cmd_l",
}


def _build_hotkey_components(keys: list) -> list[list[str]]:
    """
    Convert a list of pynput keys held simultaneously into the
    HOTKEY_COMPONENTS format: each group is a list of equivalent keys.

    Modifier keys are grouped with their left/right counterpart so that
    e.g. "Left Ctrl + F8" will also fire if the user presses Right Ctrl + F8.
    Non-modifier (non-paired) keys form a singleton group.
    """
    seen: set[str] = set()
    components: list[list[str]] = []
    for key in keys:
        name = _key_to_config_name(key)
        if name in seen:
            continue
        seen.add(name)
        pair = _MODIFIER_PAIRS.get(name)
        if pair:
            seen.add(pair)
            components.append([name, pair])
        else:
            components.append([name])
    return components


class HotkeyRecorder:
    """
    Embeddable Tkinter frame that records a key combo via pynput.

    Usage:
        recorder = HotkeyRecorder(parent_frame, on_change=my_callback)
        recorder.pack(...)

    on_change(components: list[list[str]]) is called when a new valid combo
    is confirmed (released after ≥300 ms hold).
    """

    MIN_HOLD_MS = 300

    def __init__(
        self,
        master: tk.Widget,
        on_change: Callable[[list[list[str]]], None] | None = None,
        initial_components: list[list[str]] | None = None,
    ) -> None:
        self._master = master
        self._on_change = on_change
        self._listener = None
        self._listening = False
        self._held_keys: dict = {}          # key → press time (monotonic)
        self._confirmed_components: list[list[str]] = initial_components or []
        self._listener_lock = threading.Lock()

        # --- UI ---
        self._frame = tk.Frame(master, bg="#1e1e2e")
        self._display_var = tk.StringVar()
        self._display_var.set(
            self._components_to_display(self._confirmed_components)
            if self._confirmed_components
            else "Not set"
        )

        tk.Label(
            self._frame,
            text="Current hotkey:",
            bg="#1e1e2e",
            fg="#a0a0b0",
            font=("Segoe UI", 10),
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))

        self._display_label = tk.Label(
            self._frame,
            textvariable=self._display_var,
            bg="#2a2a3e",
            fg="#e0e0ff",
            font=("Segoe UI", 11, "bold"),
            relief="flat",
            padx=10,
            pady=4,
            width=28,
            anchor="center",
        )
        self._display_label.grid(row=0, column=1, padx=(0, 8))

        self._btn = tk.Button(
            self._frame,
            text="Click to Assign Hotkey",
            command=self._start_listening,
            bg="#4a4a6a",
            fg="#ffffff",
            font=("Segoe UI", 10),
            relief="flat",
            padx=10,
            pady=5,
            cursor="hand2",
            activebackground="#6a6a9a",
            activeforeground="#ffffff",
        )
        self._btn.grid(row=0, column=2)

        self._status_label = tk.Label(
            self._frame,
            text="",
            bg="#1e1e2e",
            fg="#ffcc44",
            font=("Segoe UI", 9, "italic"),
        )
        self._status_label.grid(row=1, column=0, columnspan=3, pady=(4, 0), sticky="w")

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def pack(self, **kwargs) -> None:
        self._frame.pack(**kwargs)

    def grid(self, **kwargs) -> None:
        self._frame.grid(**kwargs)

    def get_components(self) -> list[list[str]]:
        return self._confirmed_components

    def stop_listener(self) -> None:
        self._stop_listener()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _components_to_display(self, components: list[list[str]]) -> str:
        if not components:
            return "Not set"
        from pynput.keyboard import Key
        parts = []
        for group in components:
            # Display the first key in each group (the one the user actually pressed)
            parts.append(_key_display_name(config._parse_key(group[0])))
        return " + ".join(parts)

    def _start_listening(self) -> None:
        with self._listener_lock:
            if self._listening:
                return
            self._listening = True
            self._held_keys.clear()

        self._btn.config(
            text="Listening… hold your combo, then release",
            bg="#6a2a2a",
            state="disabled",
        )
        self._status_label.config(text="Hold all keys together for ≥ 0.3 s, then release.")
        self._display_var.set("…")

        self._listener = make_keyboard_listener(
            on_press=self._on_key_press,
            on_release=self._on_key_release,
        )
        self._listener.start()

    def _stop_listener(self) -> None:
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                pass

    def _on_key_press(self, key) -> None:
        with self._listener_lock:
            if key not in self._held_keys:
                self._held_keys[key] = time.monotonic()
            keys_so_far = list(self._held_keys.keys())
        self._master.after(0, lambda k=keys_so_far: self._update_live_display(k))

    def _update_live_display(self, keys: list) -> None:
        if not self._listening:
            return
        display = " + ".join(_key_display_name(k) for k in keys) if keys else "…"
        self._display_var.set(display)

    def _on_key_release(self, key) -> None:
        with self._listener_lock:
            if not self._listening:
                return
            # All keys must have been released for combo to be considered done.
            # We fire on the *first* release so the user doesn't have to release all at once.
            held = dict(self._held_keys)
            if not held:
                return
            now = time.monotonic()
            # Check the key that has been held longest
            press_time = min(held.values())
            hold_duration_ms = (now - press_time) * 1000

            if hold_duration_ms < self.MIN_HOLD_MS:
                # Too brief — reset and wait for another attempt
                self._held_keys.clear()
                self._master.after(0, self._show_too_short)
                return

            # Valid combo captured
            keys_captured = list(held.keys())
            self._listening = False
            self._held_keys.clear()

        self._stop_listener()
        components = _build_hotkey_components(keys_captured)
        self._confirmed_components = components
        display = self._components_to_display(components)
        self._master.after(0, lambda: self._show_confirmed(display))
        if self._on_change:
            self._on_change(components)

    def _show_too_short(self) -> None:
        self._status_label.config(
            text=f"Hold too brief (< {self.MIN_HOLD_MS} ms). Try again.",
            fg="#ff6644",
        )
        self._display_var.set(
            self._components_to_display(self._confirmed_components)
            if self._confirmed_components else "Not set"
        )
        self._btn.config(
            text="Click to Assign Hotkey",
            bg="#4a4a6a",
            state="normal",
        )

    def _show_confirmed(self, display: str) -> None:
        self._display_var.set(display)
        self._status_label.config(
            text=f"✓ Recorded: {display}  — looks right? Hit Save & Start.",
            fg="#66dd88",
        )
        self._btn.config(
            text="Re-assign Hotkey",
            bg="#4a4a6a",
            state="normal",
        )


# ---------------------------------------------------------------------------
# Setup Wizard window
# ---------------------------------------------------------------------------

class SetupWizard:
    """
    Modal-ish setup window.  Call .run_modal() to block until the user saves
    (returns True) or closes without saving (returns False).

    Also usable non-modally from the tray via .open_non_modal(on_saved).
    """

    def __init__(self, root: tk.Tk, *, modal: bool = True) -> None:
        self._root = root
        self._modal = modal
        self._saved = False
        self._on_saved_cb: Callable[[], None] | None = None
        self._win: tk.Toplevel | None = None
        self._hotkey_recorder: HotkeyRecorder | None = None
        self._api_key_var = tk.StringVar()
        self._current_components: list[list[str]] = []
        self._current_media_ducking: bool = True
        self._media_ducking_var: tk.BooleanVar | None = None

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def run_modal(self) -> bool:
        """Build the window, block until closed, return True if user saved."""
        self._build()
        if self._win is None:
            return False
        self._win.wait_window()
        return self._saved

    def open_non_modal(self, on_saved: Callable[[], None] | None = None) -> None:
        """Open the window without blocking (used from tray menu)."""
        # If already open, just lift it
        if self._win is not None and self._win.winfo_exists():
            self._win.lift()
            self._win.focus_force()
            return
        self._on_saved_cb = on_saved
        self._build()

    # ------------------------------------------------------------------
    # Window construction
    # ------------------------------------------------------------------

    def _build(self) -> None:
        self._load_current_settings()

        win = tk.Toplevel(self._root)
        self._win = win
        win.title("GroqyTalky Setup")
        win.configure(bg="#1e1e2e")
        win.resizable(True, True)
        win.protocol("WM_DELETE_WINDOW", self._on_close)

        if self._modal:
            win.grab_set()
            win.focus_force()

        # Header always visible above the scroll area
        self._build_header(win)

        # Scrollable middle — all section builders pack into scroll_frame
        scroll_frame = self._build_scroll_area(win)
        self._build_api_section(scroll_frame)
        self._build_hotkey_section(scroll_frame)
        self._build_preferences_section(scroll_frame)
        self._build_tray_tip_section(scroll_frame)

        # Footer always visible below the scroll area
        self._build_footer(win)

        # Center on screen; derive width from inner scroll frame content, cap height at 88%
        win.update_idletasks()
        content_w = scroll_frame.winfo_reqwidth() + 40  # 40px: scrollbar + chrome
        req_h = win.winfo_reqheight()
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        final_w = max(content_w, 480)
        max_h = int(sh * 0.88)
        final_h = min(req_h, max_h)
        win.geometry(f"{final_w}x{final_h}+{(sw - final_w) // 2}+{(sh - final_h) // 2}")

    def _build_scroll_area(self, win: tk.Toplevel) -> tk.Frame:
        """Wrap a scrollable canvas between the header and footer; return the inner Frame."""
        container = tk.Frame(win, bg="#1e1e2e")
        container.pack(fill="both", expand=True)

        vbar = tk.Scrollbar(container, orient="vertical")
        vbar.pack(side="right", fill="y")

        canvas = tk.Canvas(container, bg="#1e1e2e", highlightthickness=0,
                           yscrollcommand=vbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        vbar.config(command=canvas.yview)

        inner = tk.Frame(canvas, bg="#1e1e2e")
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _resize_inner(event):
            canvas.itemconfig(win_id, width=event.width)
        canvas.bind("<Configure>", _resize_inner)

        def _update_scrollregion(event):
            canvas.configure(scrollregion=canvas.bbox("all"))
        inner.bind("<Configure>", _update_scrollregion)

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        return inner

    def _load_current_settings(self) -> None:
        self._api_key_var.set(config.get_groq_api_key())
        settings = config.load_settings()
        self._current_components = settings.get(
            "hotkey_components", config._DEFAULT_HOTKEY_COMPONENTS
        )
        self._current_media_ducking = bool(settings.get("media_ducking", True))

    def _section_label(self, parent: tk.Widget, text: str) -> tk.Label:
        lbl = tk.Label(
            parent,
            text=text,
            bg="#1e1e2e",
            fg="#8888bb",
            font=("Segoe UI", 9, "bold"),
            anchor="w",
        )
        lbl.pack(fill="x", padx=24, pady=(14, 2))
        return lbl

    def _build_header(self, win: tk.Toplevel) -> None:
        hdr = tk.Frame(win, bg="#13132a", pady=16)
        hdr.pack(fill="x")
        tk.Label(
            hdr,
            text="GroqyTalky",
            bg="#13132a",
            fg="#ccccff",
            font=("Segoe UI", 22, "bold"),
        ).pack()
        tk.Label(
            hdr,
            text=f"v{config.APP_VERSION}  ·  First-time setup",
            bg="#13132a",
            fg="#6666aa",
            font=("Segoe UI", 10),
        ).pack()

    def _build_api_section(self, win: tk.Toplevel) -> None:
        self._section_label(win, "STEP 1 — GET YOUR FREE GROQ API KEY")

        # Step-by-step card
        card = tk.Frame(win, bg="#252538")
        card.pack(fill="x", padx=24, pady=(0, 8))

        tk.Label(
            card,
            text="Follow these steps — it only takes about a minute:",
            bg="#252538",
            fg="#a0a0b0",
            font=("Segoe UI", 9, "italic"),
            anchor="w",
        ).pack(fill="x", padx=14, pady=(10, 6))

        steps = [
            "Click the button below to open the Groq website in your browser",
            'Create a free account (or sign in if you already have one)',
            'Click "API Keys" in the top navigation bar',
            'Click "Create API Key"',
            'Give it any name — it does not matter  (e.g. "GroqyTalky key")',
            "Click Submit, then copy the key that appears",
            "Paste your key into the field at the bottom of this card",
        ]

        for i, text in enumerate(steps, 1):
            row = tk.Frame(card, bg="#252538")
            row.pack(fill="x", padx=14, pady=2)
            tk.Label(
                row, text=f"{i}.", bg="#252538", fg="#5599ff",
                font=("Segoe UI", 10, "bold"), width=3, anchor="e",
            ).pack(side="left")
            tk.Label(
                row, text=text, bg="#252538", fg="#c8c8e0",
                font=("Segoe UI", 10), anchor="w", justify="left",
            ).pack(side="left", padx=(8, 0), fill="x", expand=True)

        link_row = tk.Frame(card, bg="#252538")
        link_row.pack(fill="x", padx=14, pady=(8, 12))
        link_btn = tk.Button(
            link_row,
            text="  Open console.groq.com in browser  →",
            command=lambda: os.startfile("https://console.groq.com"),  # type: ignore[attr-defined]
            bg="#1e3a6e",
            fg="#88bbff",
            font=("Segoe UI", 10),
            relief="flat",
            padx=10,
            pady=5,
            cursor="hand2",
            activebackground="#2a4a8e",
            activeforeground="#aaccff",
        )
        link_btn.pack(anchor="w")

        # Separator
        tk.Frame(card, bg="#3a3a5a", height=1).pack(fill="x", padx=14, pady=(0, 10))

        paste_row = tk.Frame(card, bg="#252538")
        paste_row.pack(fill="x", padx=14, pady=(0, 12))

        tk.Label(
            paste_row, text="Paste your key here:",
            bg="#252538", fg="#a0a0b0", font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(0, 4))

        entry_frame = tk.Frame(paste_row, bg="#252538")
        entry_frame.pack(fill="x")

        self._key_entry = tk.Entry(
            entry_frame,
            textvariable=self._api_key_var,
            show="•",
            font=("Segoe UI", 11),
            bg="#2a2a3e",
            fg="#e0e0ff",
            insertbackground="#e0e0ff",
            relief="flat",
            width=48,
        )
        self._key_entry.pack(side="left", ipady=6, padx=(0, 8))

        self._show_key_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            entry_frame,
            text="Show",
            variable=self._show_key_var,
            command=self._toggle_key_visibility,
            bg="#252538",
            fg="#a0a0b0",
            selectcolor="#2a2a3e",
            activebackground="#252538",
            font=("Segoe UI", 9),
        ).pack(side="left")

    def _toggle_key_visibility(self) -> None:
        self._key_entry.config(show="" if self._show_key_var.get() else "•")

    def _build_hotkey_section(self, win: tk.Toplevel) -> None:
        self._section_label(win, "STEP 2 — RECORDING HOTKEY")

        tk.Label(
            win,
            text="Hold your preferred key combination, then release.",
            bg="#1e1e2e",
            fg="#a0a0b0",
            font=("Segoe UI", 9),
            justify="left",
        ).pack(fill="x", padx=24, pady=(0, 6))

        recorder_frame = tk.Frame(win, bg="#1e1e2e")
        recorder_frame.pack(fill="x", padx=24)

        self._hotkey_recorder = HotkeyRecorder(
            recorder_frame,
            on_change=self._on_hotkey_changed,
            initial_components=self._current_components,
        )
        self._hotkey_recorder.pack(fill="x")

    def _on_hotkey_changed(self, components: list[list[str]]) -> None:
        self._current_components = components

    def _build_preferences_section(self, win: tk.Toplevel) -> None:
        self._section_label(win, "PREFERENCES")

        pref_frame = tk.Frame(win, bg="#1e1e2e")
        pref_frame.pack(fill="x", padx=24, pady=(0, 4))

        self._media_ducking_var = tk.BooleanVar(value=self._current_media_ducking)
        tk.Checkbutton(
            pref_frame,
            text="Media ducking  —  lower other app volumes while recording",
            variable=self._media_ducking_var,
            bg="#1e1e2e",
            fg="#c8c8e0",
            selectcolor="#2a2a3e",
            activebackground="#1e1e2e",
            activeforeground="#e0e0ff",
            font=("Segoe UI", 10),
        ).pack(anchor="w")

        tk.Label(
            pref_frame,
            text="    Restores volumes automatically when recording stops.",
            bg="#1e1e2e",
            fg="#6666aa",
            font=("Segoe UI", 9, "italic"),
        ).pack(anchor="w")

    def _build_tray_tip_section(self, win: tk.Toplevel) -> None:
        self._section_label(win, "STEP 3 — FIND THE TRAY ICON")

        tip_frame = tk.Frame(win, bg="#1e1e2e")
        tip_frame.pack(fill="x", padx=24, pady=(0, 4))

        tk.Label(
            tip_frame,
            text="The status light lives in your system tray (bottom-right).",
            bg="#1e1e2e",
            fg="#a0a0b0",
            font=("Segoe UI", 10),
        ).pack(anchor="w")

        tk.Button(
            tip_frame,
            text="Icon Hidden?  Click Here",
            command=self._show_tray_help,
            bg="#2a3a2a",
            fg="#88dd88",
            font=("Segoe UI", 10),
            relief="flat",
            padx=10,
            pady=4,
            cursor="hand2",
            activebackground="#3a5a3a",
            activeforeground="#aaffaa",
        ).pack(anchor="w", pady=(6, 0))

    def _show_tray_help(self) -> None:
        messagebox.showinfo(
            "Finding the GroqyTalky tray icon",
            "Windows often hides new tray icons.\n\n"
            "1.  Look for a small '^' arrow near your clock (bottom-right).\n"
            "2.  Click it to expand the hidden icons panel.\n"
            "3.  Find the grey circle — that's GroqyTalky.\n"
            "4.  Drag it out onto the main taskbar so you can see the\n"
            "     Red (recording) and Yellow (processing) status lights.\n\n"
            "You can also right-click the icon for Settings and Usage Stats.",
            parent=self._win,
        )

    def _build_footer(self, win: tk.Toplevel) -> None:
        sep = tk.Frame(win, bg="#3a3a5a", height=1)
        sep.pack(fill="x", padx=0, pady=(16, 0))

        footer = tk.Frame(win, bg="#13132a", pady=12)
        footer.pack(fill="x")

        self._error_label = tk.Label(
            footer,
            text="",
            bg="#13132a",
            fg="#ff6644",
            font=("Segoe UI", 9),
        )
        self._error_label.pack(pady=(0, 8))

        btn_row = tk.Frame(footer, bg="#13132a")
        btn_row.pack()

        tk.Button(
            btn_row,
            text="Cancel",
            command=self._on_close,
            bg="#2a2a3e",
            fg="#a0a0b0",
            font=("Segoe UI", 11),
            relief="flat",
            padx=18,
            pady=7,
            cursor="hand2",
            activebackground="#3a3a5e",
            activeforeground="#ccccff",
        ).pack(side="left", padx=(0, 12))

        tk.Button(
            btn_row,
            text="Save & Start",
            command=self._on_save,
            bg="#3366cc",
            fg="#ffffff",
            font=("Segoe UI", 11, "bold"),
            relief="flat",
            padx=18,
            pady=7,
            cursor="hand2",
            activebackground="#4477ee",
            activeforeground="#ffffff",
        ).pack(side="left")

    # ------------------------------------------------------------------
    # Save / close
    # ------------------------------------------------------------------

    def _on_save(self) -> None:
        api_key = self._api_key_var.get().strip()
        if not api_key:
            self._error_label.config(text="API key is required.")
            return
        if not api_key.startswith("gsk_"):
            self._error_label.config(
                text="That doesn't look like a Groq key (should start with 'gsk_')."
            )
            return

        components = self._hotkey_recorder.get_components() if self._hotkey_recorder else []
        if not components:
            self._error_label.config(text="Please assign a hotkey before saving.")
            return

        # Save secrets to .env
        config.save_groq_api_key(api_key)

        # Save settings to config.json
        settings = config.load_settings()
        settings["hotkey_components"] = components
        if self._media_ducking_var is not None:
            settings["media_ducking"] = self._media_ducking_var.get()
        config.save_settings(settings)

        # Apply immediately to the running process
        config._apply_settings(settings)

        self._saved = True
        if self._hotkey_recorder:
            self._hotkey_recorder.stop_listener()

        self._close_window()

        if self._on_saved_cb:
            self._on_saved_cb()

    def _on_close(self) -> None:
        if self._hotkey_recorder:
            self._hotkey_recorder.stop_listener()
        self._close_window()

    def _close_window(self) -> None:
        win = self._win
        self._win = None
        if win is not None:
            try:
                if self._modal:
                    win.grab_release()
                win.destroy()
            except tk.TclError:
                pass


# ---------------------------------------------------------------------------
# System Prompt Editor  (launched from tray, not part of wizard)
# ---------------------------------------------------------------------------

class SystemPromptEditor:
    """Standalone window for editing the AI system prompt, opened from the tray menu."""

    def __init__(self, root: tk.Tk) -> None:
        self._root = root

    def open(self) -> None:
        # Bring to front if already open
        for child in self._root.winfo_children():
            if isinstance(child, tk.Toplevel) and child.title() == "GroqyTalky — System Prompt":
                child.lift()
                child.focus_force()
                return

        win = tk.Toplevel(self._root)
        win.title("GroqyTalky — System Prompt")
        win.configure(bg="#1e1e2e")
        win.resizable(True, True)
        win.minsize(480, 320)

        # Header
        hdr = tk.Frame(win, bg="#13132a", pady=14)
        hdr.pack(fill="x")
        tk.Label(hdr, text="AI System Prompt / Instructions",
                 bg="#13132a", fg="#ccccff",
                 font=("Segoe UI", 14, "bold")).pack()
        tk.Label(hdr,
                 text="This controls how the AI cleans up your speech transcription.\n"
                      "Advanced users only — leave it as-is for normal use.",
                 bg="#13132a", fg="#6666aa", font=("Segoe UI", 9)).pack()

        # Text area
        text_frame = tk.Frame(win, bg="#1e1e2e")
        text_frame.pack(fill="both", expand=True, padx=16, pady=12)

        vbar = tk.Scrollbar(text_frame)
        vbar.pack(side="right", fill="y")

        text = tk.Text(
            text_frame,
            font=("Segoe UI", 10),
            bg="#2a2a3e",
            fg="#e0e0ff",
            insertbackground="#e0e0ff",
            relief="flat",
            wrap="word",
            yscrollcommand=vbar.set,
        )
        text.pack(side="left", fill="both", expand=True)
        vbar.config(command=text.yview)

        settings = config.load_settings()
        text.insert("1.0", settings.get("system_prompt", config._DEFAULT_SYSTEM_PROMPT))

        # Footer
        tk.Frame(win, bg="#3a3a5a", height=1).pack(fill="x")
        btn_frame = tk.Frame(win, bg="#13132a", pady=10)
        btn_frame.pack(fill="x")

        def _reset() -> None:
            text.delete("1.0", "end")
            text.insert("1.0", config._DEFAULT_SYSTEM_PROMPT)

        def _save() -> None:
            prompt = text.get("1.0", "end-1c").strip()
            settings["system_prompt"] = prompt or config._DEFAULT_SYSTEM_PROMPT
            config.save_settings(settings)
            config._apply_settings(settings)
            win.destroy()

        tk.Button(btn_frame, text="Reset to Default", command=_reset,
                  bg="#2a2a3e", fg="#a0a0b0", font=("Segoe UI", 10), relief="flat",
                  padx=10, pady=5, cursor="hand2",
                  activebackground="#3a3a5e", activeforeground="#ccccff",
                  ).pack(side="left", padx=16)

        tk.Button(btn_frame, text="Cancel", command=win.destroy,
                  bg="#2a2a3e", fg="#a0a0b0", font=("Segoe UI", 11), relief="flat",
                  padx=14, pady=7, cursor="hand2",
                  activebackground="#3a3a5e", activeforeground="#ccccff",
                  ).pack(side="right", padx=(0, 8))

        tk.Button(btn_frame, text="Save", command=_save,
                  bg="#3366cc", fg="#ffffff", font=("Segoe UI", 11, "bold"), relief="flat",
                  padx=14, pady=7, cursor="hand2",
                  activebackground="#4477ee", activeforeground="#ffffff",
                  ).pack(side="right", padx=(0, 8))

        # Size and center
        w, h = 640, 480
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        win.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")


# ---------------------------------------------------------------------------
# HUD
# ---------------------------------------------------------------------------

class RecordingHud:
    """Top-right always-on-top recording indicator; click-through on Windows."""

    def __init__(self, master: tk.Tk) -> None:
        self._root = master
        self._win = tk.Toplevel(master)
        self._win.overrideredirect(True)
        self._win.attributes("-topmost", True)
        self._win.attributes("-alpha", config.HUD_ALPHA)
        self._label = tk.Label(
            self._win,
            text=config.HUD_LABEL,
            fg="#ff2222",
            bg="#1a1a1a",
            font=config.HUD_FONT,
        )
        self._label.pack(padx=6, pady=4)
        self._win.withdraw()
        self._win.update_idletasks()
        self._clickthrough_applied = False

    def _apply_clickthrough(self) -> None:
        if sys.platform != "win32":
            return
        if self._clickthrough_applied:
            return
        try:
            hwnd = int(self._win.winfo_id())
        except tk.TclError:
            return
        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x80000
        WS_EX_TRANSPARENT = 0x20
        user32 = ctypes.windll.user32
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED | WS_EX_TRANSPARENT)
        self._clickthrough_applied = True

    def _position(self) -> None:
        self._win.update_idletasks()
        w = self._win.winfo_reqwidth()
        h = self._win.winfo_reqheight()
        sw = self._win.winfo_screenwidth()
        x = sw - w - config.HUD_MARGIN_X
        y = config.HUD_MARGIN_Y
        self._win.geometry(f"+{x}+{y}")

    def show(self) -> None:
        def _() -> None:
            self._position()
            self._win.deiconify()
            self._win.lift()
            self._root.after(80, self._apply_clickthrough)
        self._root.after(0, _)

    def hide(self) -> None:
        self._root.after(0, lambda: self._win.withdraw())


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class VoiceAssistantApp:
    TRAY_IDLE = "idle"
    TRAY_RECORDING = "recording"
    TRAY_PROCESSING = "processing"

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._shutdown_done = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._listener = None
        self._tray_icon: pystray.Icon | None = None
        self._tray_lock = threading.Lock()
        self._services_running = False

        self._root = tk.Tk()
        self._root.withdraw()
        self._root.title(f"GroqyTalky {config.APP_VERSION}")

        self._hud = RecordingHud(self._root)

        self._images = {
            self.TRAY_IDLE: _load_tray_icon("groqytalky.ico", config.TRAY_COLOR_IDLE),
            self.TRAY_RECORDING: _load_tray_icon("groqytalky_recording.ico", config.TRAY_COLOR_RECORDING),
            self.TRAY_PROCESSING: _load_tray_icon("groqytalky_processing.ico", config.TRAY_COLOR_PROCESSING),
        }

    # ------------------------------------------------------------------
    # Tray visual state
    # ------------------------------------------------------------------

    def _set_tray_visual_state(self, state: str) -> None:
        icon = self._tray_icon
        if icon is None:
            return
        img = self._images.get(state) or self._images[self.TRAY_IDLE]
        with self._tray_lock:
            try:
                icon.icon = img
            except Exception:
                log.exception("Tray icon update failed")

    def _schedule(self, fn) -> None:
        try:
            self._root.after(0, fn)
        except tk.TclError:
            pass

    def _show_message(self, title: str, body: str, *, kind: str = "info") -> None:
        def _run() -> None:
            if kind == "error":
                messagebox.showerror(title, body, parent=self._root)
            else:
                messagebox.showinfo(title, body, parent=self._root)
        self._schedule(_run)

    def _set_tray_state(self, state: str) -> None:
        self._schedule(lambda s=state: self._set_tray_visual_state(s))

    # ------------------------------------------------------------------
    # Pipeline callbacks
    # ------------------------------------------------------------------

    def _on_recording_start(self) -> None:
        if config.MEDIA_DUCKING:
            core.duck_media()
        self._set_tray_state(self.TRAY_RECORDING)
        self._schedule(self._hud.show)
        self._schedule(
            lambda: _beep_async(config.BEEP_RECORD_START_HZ, config.BEEP_RECORD_START_MS)
        )

    def _on_recording_stop(self) -> None:
        if config.MEDIA_DUCKING:
            core.unduck_media()
        self._set_tray_state(self.TRAY_IDLE)
        self._schedule(self._hud.hide)
        self._schedule(
            lambda: _beep_async(config.BEEP_RECORD_STOP_HZ, config.BEEP_RECORD_STOP_MS)
        )

    def _on_processing_begin(self) -> None:
        self._set_tray_state(self.TRAY_PROCESSING)

    def _on_processing_end(self) -> None:
        self._set_tray_state(self.TRAY_IDLE)

    def _on_transcription_failed(self) -> None:
        self._show_message(
            "GroqyTalky — Transcription Failed",
            "Transcription failed (network issue or API error).\n\n"
            "Your recording was saved automatically.\n"
            "Right-click the tray icon and choose\n"
            "\"Retry Last Recording\" when you're ready to try again.",
            kind="error",
        )

    # ------------------------------------------------------------------
    # Tray menu actions
    # ------------------------------------------------------------------

    def _tray_view_readme(self, icon, item) -> None:
        path = config.readme_path()
        if not path.is_file():
            self._show_message(
                "GroqyTalky",
                "readme.htm was not found in the install folder.\n" + str(path),
            )
            return
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except OSError as e:
            self._show_message("GroqyTalky", f"Could not open readme:\n{e}", kind="error")

    def _tray_view_logs(self, icon, item) -> None:
        path = config.log_path()
        if not path.is_file():
            self._show_message(
                "GroqyTalky",
                "Log file does not exist yet.\n" + str(path),
            )
            return
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except OSError as e:
            self._show_message("GroqyTalky", f"Could not open log:\n{e}", kind="error")

    def _tray_usage_stats(self, icon, item) -> None:
        data = core.load_usage_stats()
        session_n = core.get_session_transcription_count()
        body = (
            f"Session transcriptions: {session_n}\n"
            f"Lifetime transcriptions: {data.get('lifetime_transcriptions', 0)}\n"
            f"This month ({data.get('current_month', '')}): {data.get('month_transcriptions', 0)}"
        )
        self._show_message("GroqyTalky — Usage", body)

    def _tray_open_settings(self, icon, item) -> None:
        """Open the Setup Wizard from the tray (non-modal, pre-filled)."""
        def _open() -> None:
            wizard = SetupWizard(self._root, modal=False)
            wizard.open_non_modal(on_saved=self._on_settings_saved)
        self._schedule(_open)

    def _tray_open_system_prompt(self, icon, item) -> None:
        self._schedule(lambda: SystemPromptEditor(self._root).open())

    def _on_settings_saved(self) -> None:
        """Called after the user saves new settings from the tray wizard."""
        # config._apply_settings() was already called inside SetupWizard._on_save.
        # Restart the keyboard listener so it picks up the new hotkey.
        self._restart_listener()

    def _tray_open_config(self, icon, item) -> None:
        path = config.config_file_path()
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except OSError as e:
            self._show_message("GroqyTalky", f"Could not open config:\n{e}", kind="error")

    def _tray_retry_last(self, icon, item) -> None:
        if core.retry_last_recording():
            self._show_message(
                "GroqyTalky",
                "Last recording re-queued for transcription.",
            )
        else:
            self._show_message(
                "GroqyTalky",
                "No saved recording found to retry.\n"
                f"(Expected: {config.last_recording_path()})",
            )

    def _tray_quit(self, icon, item) -> None:
        self._schedule(self._shutdown)

    def _build_tray_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem("Settings / Change Hotkey", self._tray_open_settings),
            pystray.MenuItem("Edit System Prompt…", self._tray_open_system_prompt),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Retry Last Recording", self._tray_retry_last),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("View Readme / Help", self._tray_view_readme),
            pystray.MenuItem("View Logs", self._tray_view_logs),
            pystray.MenuItem("Usage Stats", self._tray_usage_stats),
            pystray.MenuItem("Open config.json", self._tray_open_config),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._tray_quit),
        )

    def _run_tray(self) -> None:
        menu = self._build_tray_menu()
        self._tray_icon = pystray.Icon(
            "grogy_talky",
            self._images[self.TRAY_IDLE],
            f"GroqyTalky {config.APP_VERSION}",
            menu,
        )
        self._tray_icon.run()

    # ------------------------------------------------------------------
    # Background services
    # ------------------------------------------------------------------

    def _start_services(self) -> None:
        """Start worker thread, keyboard listener, and tray (idempotent)."""
        if self._services_running:
            return
        self._services_running = True

        core.set_pipeline_callbacks(
            on_recording_start=self._on_recording_start,
            on_recording_stop=self._on_recording_stop,
            on_processing_begin=self._on_processing_begin,
            on_processing_end=self._on_processing_end,
            on_transcription_failed=self._on_transcription_failed,
        )

        self._worker_thread = threading.Thread(
            target=core.session_worker,
            args=(self._stop_event,),
            daemon=True,
        )
        self._worker_thread.start()

        self._listener = make_keyboard_listener(
            on_press=core.on_press,
            on_release=core.on_release,
        )
        self._listener.start()

        threading.Thread(target=self._run_tray, daemon=True).start()

    def _restart_listener(self) -> None:
        """Stop and restart the keyboard listener (called after hotkey change)."""
        old = self._listener
        self._listener = None
        if old is not None:
            try:
                old.stop()
            except Exception:
                pass
        core.shutdown_recording()
        self._listener = make_keyboard_listener(
            on_press=core.on_press,
            on_release=core.on_release,
        )
        self._listener.start()
        log.info("Keyboard listener restarted with new hotkey.")

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def _shutdown(self) -> None:
        if self._shutdown_done.is_set():
            return
        self._shutdown_done.set()
        self._stop_event.set()

        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                log.exception("Listener stop")
            self._listener = None

        core.shutdown_recording()
        try:
            core.enqueue_sentinel()
        except Exception:
            pass

        if self._worker_thread is not None:
            self._worker_thread.join(timeout=6.0)
            self._worker_thread = None

        if self._tray_icon is not None:
            try:
                self._tray_icon.stop()
            except Exception:
                log.exception("Tray stop")
            self._tray_icon = None

        try:
            self._root.quit()
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        # --- First-run wizard (blocks until saved or dismissed) ---
        if config.needs_first_run_wizard():
            wizard = SetupWizard(self._root, modal=True)
            saved = wizard.run_modal()
            if not saved:
                # User closed without saving — can't run without an API key.
                messagebox.showerror(
                    "GroqyTalky",
                    "No API key was saved. GroqyTalky cannot start.\n\n"
                    "Re-launch the app to try again.",
                    parent=self._root,
                )
                sys.exit(1)

        # --- Start background services ---
        self._start_services()

        try:
            self._root.mainloop()
        finally:
            if not self._shutdown_done.is_set():
                self._shutdown()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    VoiceAssistantApp().run()


if __name__ == "__main__":
    main()
