"""
GroqyTalky v0.31 — Windows keyboard listener.

Suppresses Left Win events from reaching the shell when Left Ctrl is held,
preventing the Start Menu from firing while using the hotkey.

On non-Windows, non-macOS platforms (Linux etc.) this falls back to the
standard pynput listener so the app at least runs, even if some features
(winsound, os.startfile) will be absent.

macOS is blocked at import time in config.py with a user-friendly message.
"""

from __future__ import annotations

import ctypes
import sys

from pynput import keyboard

if sys.platform != "win32":

    def make_keyboard_listener(**kwargs):
        return keyboard.Listener(**kwargs)

else:
    from pynput._util import AbstractListener
    from pynput._util.win32 import SystemHook

    class SuppressingKeyboardListener(keyboard.Listener):
        """Extends the Win32 listener to swallow LWin while LCtrl is physically down."""

        VK_LWIN = 0x5B
        VK_LCONTROL = 0xA2

        def _should_suppress_start_menu(self, code: int, msg, lpdata) -> bool:
            if code != SystemHook.HC_ACTION:
                return False
            data = ctypes.cast(lpdata, self._LPKBDLLHOOKSTRUCT).contents
            if int(data.vkCode) != self.VK_LWIN:
                return False
            if not (ctypes.windll.user32.GetAsyncKeyState(self.VK_LCONTROL) & 0x8000):
                return False
            return True

        @AbstractListener._emitter
        def _handler(self, code, msg, lpdata):
            try:
                converted = self._convert(code, msg, lpdata)
                if converted is not None:
                    self._message_loop.post(self._WM_PROCESS, *converted)
                    if self._should_suppress_start_menu(code, msg, lpdata):
                        self.suppress_event()
            except NotImplementedError:
                self._handle_message(code, msg, lpdata)

            if self.suppress:
                self.suppress_event()

    def make_keyboard_listener(**kwargs):
        return SuppressingKeyboardListener(**kwargs)
