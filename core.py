"""
GroqyTalky v0.31 — audio capture, Groq pipeline, paste, logging, usage counters.

The only structural change from v0.2: hotkey_active() reads from config at
call-time so that a hotkey change in the wizard takes effect without a restart.
"""

from __future__ import annotations

import io
import json
import logging
import os
import queue
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone

import numpy as np
import pyautogui
import pyperclip
import sounddevice as sd
from groq import Groq
from scipy.io.wavfile import write as wav_write

try:
    from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume as _ISimpleAudioVolume
    _PYCAW_OK = True
except Exception:
    _PYCAW_OK = False

import config

log = logging.getLogger(__name__)

pyautogui.FAILSAFE = False

_stats_lock = threading.Lock()
_session_transcriptions = 0

# --- Keyboard / recording mode state ---
_pressed: set = set()
_hotkey_was_active = False
_recording = False
_latched = False
_hold_recording = False
_processing = False
_activation_times: list[float] = []
_hold_timer: threading.Timer | None = None
_audio_chunks: list[np.ndarray] = []
_audio_lock = threading.Lock()
_rec_state_lock = threading.Lock()
_stream: sd.InputStream | None = None
_session_queue: queue.Queue[bytes | None] = queue.Queue()

_on_recording_start: Callable[[], None] | None = None
_on_recording_stop: Callable[[], None] | None = None
_on_processing_begin: Callable[[], None] | None = None
_on_processing_end: Callable[[], None] | None = None
_on_transcription_failed: Callable[[], None] | None = None

_last_wav: bytes | None = None


# ---------------------------------------------------------------------------
# Usage stats
# ---------------------------------------------------------------------------

def get_session_transcription_count() -> int:
    return _session_transcriptions


def _month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def load_usage_stats() -> dict:
    path = config.usage_stats_path()
    if not path.is_file():
        return {
            "lifetime_transcriptions": 0,
            "current_month": _month_key(),
            "month_transcriptions": 0,
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {
            "lifetime_transcriptions": 0,
            "current_month": _month_key(),
            "month_transcriptions": 0,
        }
    now_m = _month_key()
    if data.get("current_month") != now_m:
        data["current_month"] = now_m
        data["month_transcriptions"] = 0
        try:
            save_usage_stats(data)
        except OSError:
            pass
    return data


def save_usage_stats(data: dict) -> None:
    path = config.usage_stats_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def increment_transcription_stats() -> None:
    global _session_transcriptions
    with _stats_lock:
        _session_transcriptions += 1
        data = load_usage_stats()
        data["lifetime_transcriptions"] = int(data.get("lifetime_transcriptions", 0)) + 1
        now_m = _month_key()
        if data.get("current_month") != now_m:
            data["current_month"] = now_m
            data["month_transcriptions"] = 0
        data["month_transcriptions"] = int(data.get("month_transcriptions", 0)) + 1
        save_usage_stats(data)


# ---------------------------------------------------------------------------
# Pipeline callbacks
# ---------------------------------------------------------------------------

def set_pipeline_callbacks(
    *,
    on_recording_start: Callable[[], None] | None = None,
    on_recording_stop: Callable[[], None] | None = None,
    on_processing_begin: Callable[[], None] | None = None,
    on_processing_end: Callable[[], None] | None = None,
    on_transcription_failed: Callable[[], None] | None = None,
) -> None:
    global _on_recording_start, _on_recording_stop, _on_processing_begin, _on_processing_end
    global _on_transcription_failed
    _on_recording_start = on_recording_start
    _on_recording_stop = on_recording_stop
    _on_processing_begin = on_processing_begin
    _on_processing_end = on_processing_end
    _on_transcription_failed = on_transcription_failed


# ---------------------------------------------------------------------------
# Hotkey helpers  (read config at call-time so wizard changes apply live)
# ---------------------------------------------------------------------------

def hotkey_active() -> bool:
    """True when at least one key from every HOTKEY_COMPONENTS group is pressed."""
    for group in config.HOTKEY_COMPONENTS:
        if not any(k in _pressed for k in group):
            return False
    return True


def _double_tap_window_s() -> float:
    return config.DOUBLE_TAP_WINDOW_MS / 1000.0


def _hold_threshold_s() -> float:
    return config.HOLD_TO_TALK_THRESHOLD_MS / 1000.0


# ---------------------------------------------------------------------------
# Hold timer
# ---------------------------------------------------------------------------

def _cancel_hold_timer() -> None:
    global _hold_timer
    if _hold_timer is not None:
        _hold_timer.cancel()
        _hold_timer = None


def _prune_activation_times(now: float) -> None:
    global _activation_times
    window = _double_tap_window_s()
    _activation_times = [t for t in _activation_times if now - t <= window]


# ---------------------------------------------------------------------------
# Audio stream
# ---------------------------------------------------------------------------

def _append_audio(indata: np.ndarray) -> None:
    with _audio_lock:
        if _recording:
            _audio_chunks.append(indata.copy())


def _audio_callback(indata, frames, t, status) -> None:
    if status:
        log.debug("sounddevice status: %s", status)
    _append_audio(indata)


def _start_stream() -> None:
    global _stream
    if _stream is not None:
        return
    _stream = sd.InputStream(
        samplerate=config.SAMPLE_RATE,
        channels=config.CHANNELS,
        dtype=np.float32,
        callback=_audio_callback,
        blocksize=1024,
    )
    _stream.start()


def _stop_stream() -> None:
    global _stream
    if _stream is not None:
        _stream.stop()
        _stream.close()
        _stream = None


def _concat_audio() -> np.ndarray | None:
    with _audio_lock:
        if not _audio_chunks:
            return None
        return np.concatenate(_audio_chunks, axis=0)


# ---------------------------------------------------------------------------
# Recording state machine
# ---------------------------------------------------------------------------

def _begin_recording() -> None:
    global _recording, _audio_chunks
    if _recording:
        return
    with _audio_lock:
        _audio_chunks = []
    _recording = True
    _start_stream()
    log.info("Recording started (latched=%s, hold=%s)", _latched, _hold_recording)
    if _on_recording_start:
        _on_recording_start()


def _finish_recording() -> bytes | None:
    """Stop capture and return WAV bytes, or None if too short."""
    global _recording, _audio_chunks, _latched, _hold_recording
    if not _recording:
        _latched = False
        _hold_recording = False
        return None

    _stop_stream()
    _recording = False
    was_latched = _latched
    was_hold = _hold_recording
    _latched = False
    _hold_recording = False

    if _on_recording_stop:
        _on_recording_stop()

    audio = _concat_audio()
    with _audio_lock:
        _audio_chunks = []

    min_samples = int(config.SAMPLE_RATE * config.MIN_RECORDING_SECONDS)
    if audio is None or audio.size < min_samples:
        log.info(
            "Recording too short, discarding (latched=%s, hold=%s)",
            was_latched,
            was_hold,
        )
        return None

    wav = wav_bytes(audio)
    log.info("Recording stopped, bytes=%s", len(wav))
    return wav


def _on_hold_timer_fired() -> None:
    global _hold_timer
    _hold_timer = None
    with _rec_state_lock:
        if _processing or _latched or _recording:
            return
        if not hotkey_active():
            return
        if not config.ENABLE_HOLD_TO_TALK:
            return
        _begin_hold_recording()


def _arm_hold_timer() -> None:
    global _hold_timer
    if not config.ENABLE_HOLD_TO_TALK:
        return
    _cancel_hold_timer()

    def _fire() -> None:
        _on_hold_timer_fired()

    _hold_timer = threading.Timer(_hold_threshold_s(), _fire)
    _hold_timer.daemon = True
    _hold_timer.start()


def _begin_hold_recording() -> None:
    global _hold_recording
    _hold_recording = True
    _begin_recording()


def _begin_latched_recording() -> None:
    global _latched, _activation_times
    _activation_times.clear()
    _latched = True
    _begin_recording()


def _on_combo_active_edge() -> None:
    global _activation_times

    if _latched and _recording:
        wav = _finish_recording()
        if wav is not None:
            enqueue_session(wav)
        return

    if _processing:
        return

    now = time.monotonic()

    if not config.ENABLE_DOUBLE_TAP_LATCH:
        if config.ENABLE_HOLD_TO_TALK:
            _arm_hold_timer()
        return

    _prune_activation_times(now)
    _activation_times.append(now)

    if len(_activation_times) >= 2:
        _cancel_hold_timer()
        _activation_times.clear()
        _begin_latched_recording()
        log.info("Latched recording started (double-tap)")
        return

    _arm_hold_timer()


def _on_combo_inactive_edge() -> None:
    _cancel_hold_timer()

    if _hold_recording and _recording and not _latched:
        wav = _finish_recording()
        if wav is not None:
            enqueue_session(wav)


def _update_hotkey_edges() -> None:
    global _hotkey_was_active
    active = hotkey_active()
    with _rec_state_lock:
        if active and not _hotkey_was_active:
            _on_combo_active_edge()
        elif not active and _hotkey_was_active:
            _on_combo_inactive_edge()
        _hotkey_was_active = active


# ---------------------------------------------------------------------------
# Audio utilities
# ---------------------------------------------------------------------------

def wav_bytes(mono_float: np.ndarray) -> bytes:
    mono = mono_float.reshape(-1)
    mono = np.clip(mono, -1.0, 1.0)
    pcm = (mono * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    wav_write(buf, config.SAMPLE_RATE, pcm)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Groq pipeline
# ---------------------------------------------------------------------------

def transcribe(client: Groq, wav_data: bytes) -> str:
    tr = client.audio.transcriptions.create(
        file=("recording.wav", wav_data),
        model=config.WHISPER_MODEL,
        language=config.WHISPER_LANGUAGE,
    )
    return (tr.text or "").strip()


_TRANSCRIPT_TAG_RE = re.compile(
    r"</?\s*transcript\s*>",
    re.IGNORECASE,
)


def _strip_transcript_markup(text: str) -> str:
    cleaned = _TRANSCRIPT_TAG_RE.sub("", text)
    return cleaned.strip()


def cleanup_text(client: Groq, raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    user_message_content = f"<transcript>\n{raw}\n</transcript>"
    completion = client.chat.completions.create(
        model=config.CLEANUP_MODEL,
        messages=[
            {"role": "system", "content": config.SYSTEM_PROMPT},
            {"role": "user", "content": user_message_content},
        ],
        temperature=0.2,
    )
    msg = completion.choices[0].message
    return _strip_transcript_markup(msg.content or "")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log_field(text: str) -> str:
    return text.replace("\r", " ").replace("\n", " ").replace("\t", " ")


def append_log(raw: str, cleaned: str) -> None:
    path = config.log_path()
    ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    new_line = f"{ts}\t{_log_field(raw)}\t{_log_field(cleaned)}\n".encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = b""
    if path.is_file():
        try:
            existing = path.read_bytes()
        except OSError:
            pass
    path.write_bytes(new_line + existing)


# ---------------------------------------------------------------------------
# Paste
# ---------------------------------------------------------------------------

def paste_text(text: str) -> None:
    if not text:
        return
    pyperclip.copy(text)
    time.sleep(config.CLIPBOARD_PASTE_DELAY)
    pyautogui.hotkey("ctrl", "v")


# ---------------------------------------------------------------------------
# Session worker
# ---------------------------------------------------------------------------

def process_session(wav_data: bytes) -> bool:
    api_key = config.get_groq_api_key()
    if not api_key:
        log.error("Missing GROQ_API_KEY in environment / .env")
        return False
    client = Groq(api_key=api_key)
    try:
        raw = transcribe(client, wav_data)
    except Exception:
        log.exception("Transcription failed")
        return False
    try:
        cleaned = cleanup_text(client, raw)
    except Exception:
        log.exception("Cleanup failed")
        cleaned = raw
    try:
        append_log(raw, cleaned)
    except Exception as e:
        log.warning("Log write failed: %s", e)
    paste_text(cleaned)
    increment_transcription_stats()
    return True


# ---------------------------------------------------------------------------
# Media ducking
# ---------------------------------------------------------------------------

_duck_restore: list[tuple] = []   # [(ISimpleAudioVolume, original_level), ...]
_duck_lock = threading.Lock()


def duck_media() -> None:
    """Lower all non-GroqyTalky audio sessions to 10 % and save their levels."""
    if not _PYCAW_OK or not config.MEDIA_DUCKING:
        return
    own_pid = os.getpid()
    captured: list[tuple] = []
    try:
        for session in AudioUtilities.GetAllSessions():
            try:
                if session.Process is None:
                    continue
                if session.Process.pid == own_pid:
                    continue
                vc = session._ctl.QueryInterface(_ISimpleAudioVolume)
                level = vc.GetMasterVolume()
                if level < 0.02:        # already near-silent — leave untouched
                    continue
                captured.append((vc, level))
                vc.SetMasterVolume(level * 0.10, None)
            except Exception:
                pass
    except Exception as e:
        log.warning("duck_media: %s", e)
    with _duck_lock:
        _duck_restore[:] = captured


def unduck_media() -> None:
    """Restore all previously ducked sessions to their original levels."""
    with _duck_lock:
        saved = list(_duck_restore)
        _duck_restore.clear()
    if not saved:
        return
    for vc, level in saved:
        try:
            vc.SetMasterVolume(level, None)
        except Exception:
            pass


def _save_last_recording(wav_data: bytes) -> None:
    global _last_wav
    _last_wav = wav_data
    try:
        config.last_recording_path().write_bytes(wav_data)
    except OSError as e:
        log.warning("Could not save last recording to disk: %s", e)


def load_last_recording() -> bytes | None:
    if _last_wav is not None:
        return _last_wav
    path = config.last_recording_path()
    if path.is_file():
        try:
            data = path.read_bytes()
            return data or None
        except OSError:
            return None
    return None


def retry_last_recording() -> bool:
    wav = load_last_recording()
    if wav is None:
        return False
    _session_queue.put(wav)
    return True


def enqueue_session(wav_data: bytes) -> None:
    _save_last_recording(wav_data)
    _session_queue.put(wav_data)


def session_worker(stop_event: threading.Event) -> None:
    global _processing
    while not stop_event.is_set():
        try:
            item = _session_queue.get(timeout=0.25)
        except queue.Empty:
            continue
        if item is None:
            _session_queue.task_done()
            break
        try:
            _processing = True
            if _on_processing_begin:
                _on_processing_begin()
            success = process_session(item)
            if not success and _on_transcription_failed:
                _on_transcription_failed()
        finally:
            _processing = False
            if _on_processing_end:
                _on_processing_end()
            _session_queue.task_done()


# ---------------------------------------------------------------------------
# Keyboard event entry points (called by win_listener)
# ---------------------------------------------------------------------------

def on_press(key) -> None:
    _pressed.add(key)
    _update_hotkey_edges()


def on_release(key) -> None:
    _pressed.discard(key)
    _update_hotkey_edges()


# ---------------------------------------------------------------------------
# Shutdown helpers
# ---------------------------------------------------------------------------

def drain_session_queue() -> None:
    while True:
        try:
            _session_queue.get_nowait()
        except queue.Empty:
            break


def enqueue_sentinel() -> None:
    _session_queue.put(None)


def shutdown_recording() -> None:
    global _recording, _latched, _hold_recording, _hotkey_was_active, _activation_times
    _cancel_hold_timer()
    with _rec_state_lock:
        if _recording:
            _finish_recording()
        _hotkey_was_active = hotkey_active()
        _activation_times.clear()
