"""Push-to-talk voice input for OpenCode.

Hold the key, speak, release. The clip is transcribed locally with
faster-whisper and the text is submitted as a prompt to a dedicated OpenCode
session, so it runs in the same conversation tree the app shows.

Design and landmines: DESIGN.md in this repo.
No cloud, no keys, nothing runs between key-down and key-up except the recorder.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
import keyboard


def _register_cuda_dlls() -> None:
    """Make pip's cuBLAS/cuDNN DLLs findable by ctranslate2.

    The NVIDIA wheels drop their DLLs inside site-packages and nothing adds them
    to the search path, so CUDA silently falls back to CPU without this.
    """
    base = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    for sub in ("cublas", "cudnn"):
        folder = base / sub / "bin"
        if folder.is_dir():
            try:
                os.add_dll_directory(str(folder))
            except Exception:
                pass
            os.environ["PATH"] = str(folder) + os.pathsep + os.environ.get("PATH", "")


_register_cuda_dlls()
from faster_whisper import WhisperModel  # noqa: E402  (needs the DLL paths first)

from voicekit import commands
from voicekit.commands import (
    apply_dictionary,
    classify,
    format_words,
    parse_edit,
    parse_format,
    plan_transcript,
    _clean_spacing,
    _last_sentence_words,
    _tokens_for_words,
    _trim_tokens,
)


def load_phrases() -> None:
    """Configure the command module with the live config, then load phrases."""
    commands.configure(_cfg)
    commands.load_phrases()


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "voice.json"
LOG_DIR = ROOT / "logs"
LOG_PATH = LOG_DIR / "voice.log"

_cfg: dict = {}
_log_lock = threading.Lock()


def log(message: str) -> None:
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
    LOG_DIR.mkdir(exist_ok=True)
    with _log_lock:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    print(line, flush=True)


def load_config() -> dict:
    # utf-8-sig: PowerShell's Set-Content writes a BOM, which json.load rejects.
    with CONFIG_PATH.open(encoding="utf-8-sig") as fh:
        return json.load(fh)


def _resample(audio: np.ndarray, source: int, target: int) -> np.ndarray:
    if source == target or audio.size == 0:
        return audio
    count = int(round(audio.size * target / source))
    old_axis = np.linspace(0.0, 1.0, audio.size, endpoint=False)
    new_axis = np.linspace(0.0, 1.0, count, endpoint=False)
    return np.interp(new_axis, old_axis, audio).astype(np.float32)


def default_input() -> int | None:
    """Re-read the Windows default input. PortAudio caches devices at init, so
    re-init first; it costs ~30 ms and makes headset swaps take effect."""
    try:
        sd._terminate()
        sd._initialize()
    except Exception as exc:
        log("audio re-init failed: %s" % exc)
    # Prefer WASAPI: MME truncates names to 31 characters and often reports a
    # different default than the one Windows actually uses.
    try:
        for api in sd.query_hostapis():
            if "WASAPI" in api["name"]:
                index = api["default_input_device"]
                if index is not None and int(index) >= 0:
                    return int(index)
    except Exception as exc:
        log("WASAPI default lookup failed: %s" % exc)
    try:
        index = sd.default.device[0]
        if index is not None and int(index) >= 0:
            return int(index)
    except Exception as exc:
        log("no default input: %s" % exc)
    return None


def pick_device(name: str | None) -> int | None:
    """A named device wins; otherwise follow the default input."""
    devices = sd.query_devices()
    if name:
        for index, dev in enumerate(devices):
            if dev["max_input_channels"] > 0 and name.lower() in dev["name"].lower():
                return index
        log("mic %r not found, following the default input" % name)

    index = default_input()
    if index is not None:
        return index
    for index, dev in enumerate(devices):
        if dev["max_input_channels"] > 0:
            return index
    raise RuntimeError("no input device available")


class Indicator:
    """Borderless, always-on-top status pill plus optional beeps.

    Runs the Tk loop in the main thread; key callbacks only post to a queue,
    because Tk is not thread-safe.
    """

    COLORS = {"rec": "#ff5555", "stt": "#ffcc66", "done": "#66cc66", "warn": "#ff8844", "live": "#ff5555"}

    def __init__(self) -> None:
        self.queue: queue.Queue = queue.Queue()
        self.failed = False
        self.hide_at = 0.0

    def post(self, kind: str, text: str = "") -> None:
        if self.failed:
            return  # headless: hooks and dictation keep working without the pill
        self.queue.put((kind, text))

    def run(self) -> None:
        import ctypes
        import tkinter as tk

        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.9)
        root.configure(bg="#141414")
        label = tk.Label(
            root,
            text="",
            fg="#ffffff",
            bg="#141414",
            font=("Segoe UI", 11, "bold"),
            padx=16,
            pady=7,
            wraplength=720,
            justify="left",
        )
        label.pack()

        # Never take focus from the app being dictated into. Tk's deiconify()
        # force-activates even with WS_EX_NOACTIVATE, so visibility is driven at
        # the Win32 level (ShowWindow) and Tk's map state is left alone.
        user32 = ctypes.windll.user32
        GWL_EXSTYLE = -20
        WS_EX_NOACTIVATE = 0x08000000
        WS_EX_TOOLWINDOW = 0x00000080
        GA_ROOT = 2
        SW_HIDE = 0
        SW_SHOWNOACTIVATE = 4
        LONG_PTR = ctypes.c_ssize_t
        # Without explicit prototypes ctypes truncates LONG_PTR to int and the
        # style write corrupts the window instead of adding the flags.
        user32.GetAncestor.restype = ctypes.c_void_p
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetWindowLongPtrW.restype = LONG_PTR
        user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetWindowLongPtrW.restype = LONG_PTR
        user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, LONG_PTR]
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.ShowWindow.restype = ctypes.c_bool
        hwnd = ctypes.c_void_p(user32.GetAncestor(root.winfo_id(), GA_ROOT))
        style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongPtrW(
            hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)

        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        visible = {"on": False}

        def place(offscreen: bool = False) -> None:
            root.update_idletasks()
            width = max(root.winfo_reqwidth(), 190)
            height = root.winfo_reqheight()
            if offscreen:
                x, y = -32000, -32000
            else:
                x = (screen_w - width) // 2
                y = screen_h - height - 96
            root.geometry("%dx%d+%d+%d" % (width, height, x, y))

        def show() -> None:
            place()
            if not visible["on"]:
                root.deiconify()
                root.lift()
                visible["on"] = True

        def hide() -> None:
            if visible["on"]:
                root.withdraw()
                visible["on"] = False

        # Map it once, then apply NOACTIVATE while it is a real, mapped window.
        root.update()
        root.withdraw()
        style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongPtrW(
            hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)

        def poll() -> None:
            try:
                while True:
                    kind, text = self.queue.get_nowait()
                    if kind == "hide":
                        hide()
                        self.hide_at = 0.0
                        continue
                    label.config(text=text, fg=self.COLORS.get(kind, "#ffffff"))
                    show()
                    self.hide_at = 0.0 if kind in ("rec", "stt", "live") else time.time() + 1.3
            except Exception:
                pass
            if self.hide_at and time.time() > self.hide_at:
                hide()
                self.hide_at = 0.0
            root.after(40, poll)

        root.after(40, poll)
        try:
            root.mainloop()
        except Exception as exc:
            self.failed = True
            log("overlay failed, continuing headless: %s" % exc)
        finally:
            self.failed = True


class Recorder:
    """One stream per utterance, opened on key-down and closed on key-up."""

    def __init__(self, rate: int, max_seconds: int) -> None:
        self.rate = rate
        self.stream_rate = rate
        self.max_frames = int(rate * max_seconds)
        self._stream: sd.InputStream | None = None
        self._chunks: list[np.ndarray] = []
        self._frames = 0
        self._lock = threading.Lock()

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            log("audio status: %s" % status)
        with self._lock:
            if self._frames >= self.max_frames:
                return
            self._chunks.append(indata.copy())
            self._frames += frames

    def start(self, device: int | None) -> bool:
        with self._lock:
            self._chunks = []
            self._frames = 0
        native = self.rate
        try:
            native = int(sd.query_devices(device)["default_samplerate"])
        except Exception:
            pass
        for rate in (self.rate, native):
            try:
                stream = sd.InputStream(
                    device=device,
                    channels=1,
                    samplerate=rate,
                    dtype="float32",
                    callback=self._callback,
                )
                stream.start()
                self._stream = stream
                self.stream_rate = rate
                if rate != self.rate:
                    log("mic opened at %d Hz, resampling to %d" % (rate, self.rate))
                return True
            except Exception as exc:
                log("could not open mic at %d Hz: %s" % (rate, exc))
        self._stream = None
        return False

    def stop(self) -> np.ndarray:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as exc:
                log("error closing stream: %s" % exc)
        with self._lock:
            chunks = self._chunks
            self._chunks = []
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return _resample(np.concatenate(chunks).reshape(-1), self.stream_rate, self.rate)


class LiveListener:
    """Continuous capture for toggle mode: closes a segment on a pause and
    hands each phrase to a worker, so speech keeps flowing while whisper runs."""

    def __init__(self, rate: int, on_segment, threshold: float = 0.006,
                 hangover: float = 0.7, min_speech: float = 0.35,
                 max_segment: float = 20.0, on_partial=None) -> None:
        self.rate = rate
        self.stream_rate = rate
        self.on_segment = on_segment
        self.on_partial = on_partial
        self._partial_mark = 0
        self.threshold = threshold
        self.hangover = int(hangover * rate)
        self.min_speech = int(min_speech * rate)
        self.max_segment = int(max_segment * rate)
        self._stream: sd.InputStream | None = None
        self._buf: list[np.ndarray] = []
        self._speech = 0
        self._silence = 0
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._running = False

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            log("audio status: %s" % status)
        mono = indata[:, 0].copy()
        if self.stream_rate != self.rate:
            mono = _resample(mono, self.stream_rate, self.rate)
        rms = float(np.sqrt(np.mean(mono.astype(np.float64) ** 2)))
        with self._lock:
            self._buf.append(mono)
            if rms >= self.threshold:
                self._speech += frames
                self._silence = 0
            else:
                self._silence += frames
            if self._speech >= self.min_speech and self._silence >= self.hangover:
                self._emit()
            elif len(self._buf) * frames >= self.max_segment and self._speech > 0:
                self._emit()

    def _emit(self) -> None:
        if not self._buf:
            return
        audio = np.concatenate(self._buf).reshape(-1)
        self._buf = []
        self._speech = 0
        self._silence = 0
        self._partial_mark = 0
        if audio.size:
            self._queue.put(audio)

    def start(self, device: int | None) -> bool:
        native = self.rate
        try:
            native = int(sd.query_devices(device)["default_samplerate"])
        except Exception:
            pass
        for rate in (self.rate, native):
            try:
                stream = sd.InputStream(
                    device=device, channels=1, samplerate=rate,
                    dtype="float32", callback=self._callback,
                )
                stream.start()
                self._stream = stream
                self.stream_rate = rate
                if rate != self.rate:
                    log("live: mic opened at %d Hz, resampling to %d" % (rate, self.rate))
                break
            except Exception as exc:
                log("live: could not open mic at %d Hz: %s" % (rate, exc))
        else:
            self._stream = None
            return False
        self._running = True
        self._worker = threading.Thread(target=self._work, daemon=True)
        self._worker.start()
        return True

    def _work(self) -> None:
        while self._running or not self._queue.empty():
            try:
                audio = self._queue.get(timeout=0.3)
            except queue.Empty:
                # Nothing to finalise: give a live caption on what is being said.
                if self.on_partial is not None:
                    snapshot = self._snapshot()
                    if snapshot is not None:
                        try:
                            self.on_partial(snapshot)
                        except Exception as exc:
                            log("live caption error: %s" % exc)
                continue
            try:
                self.on_segment(audio)
            except Exception as exc:
                log("live segment error: %s" % exc)

    def _snapshot(self):
        """Copy of the in-progress phrase, or None if there is nothing new."""
        with self._lock:
            if self._speech < self.min_speech or not self._buf:
                return None
            frames = sum(chunk.size for chunk in self._buf)
            if frames - self._partial_mark < int(self.rate * 0.6):
                return None
            self._partial_mark = frames
            return np.concatenate(self._buf).reshape(-1)

    def stop(self) -> None:
        self._running = False
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as exc:
                log("live: error closing stream: %s" % exc)
        with self._lock:
            # Only flush on stop if there was actual speech, otherwise room noise
            # gets transcribed as an empty phrase.
            if self._speech >= self.min_speech:
                self._emit()
            else:
                self._buf = []
                self._speech = 0
                self._silence = 0
        if self._worker is not None:
            self._worker.join(timeout=10)
            self._worker = None


def scroll_lock_on() -> bool:
    """The key's own toggle state, which the keyboard LED shows."""
    import ctypes

    return bool(ctypes.windll.user32.GetKeyState(0x91) & 1)


def transcribe(model: WhisperModel, audio: np.ndarray, language: str) -> str:
    segments, info = model.transcribe(
        audio,
        language=language,
        beam_size=1,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    # Whisper invents phrases on marginal audio (room noise, a fan, a keyboard).
    # Its own no-speech probability is the cheapest reliable gate.
    kept = []
    for segment in segments:
        no_speech = float(getattr(segment, "no_speech_prob", 0.0) or 0.0)
        logprob = float(getattr(segment, "avg_logprob", 0.0) or 0.0)
        if no_speech > 0.5 and logprob < -0.5:
            continue
        kept.append(segment.text.strip())
    return " ".join(part for part in kept if part).strip()


SERVICE_PATH = Path.home() / ".local" / "state" / "opencode" / "service.json"
_service_cache: dict | None = None


def service() -> dict:
    """Read the running server's url/password; cached until a call fails."""
    global _service_cache
    if _service_cache is None:
        with SERVICE_PATH.open(encoding="utf-8") as fh:
            raw = json.load(fh)
        _service_cache = {
            "url": raw["url"].rstrip("/"),
            "user": "opencode",
            "password": raw.get("password") or "",
        }
    return _service_cache


def api(method: str, path: str, body: dict | None = None):
    """Call the OpenCode HTTP API directly: no subprocess, no console window."""
    import base64
    import urllib.error
    import urllib.request

    payload = json.dumps(body).encode("utf-8") if body is not None else None
    for attempt in range(2):
        info = service()
        request = urllib.request.Request(info["url"] + path, data=payload, method=method)
        token = "%s:%s" % (info["user"], info["password"])
        request.add_header("Authorization", "Basic " + base64.b64encode(token.encode()).decode())
        if payload is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise RuntimeError("%s %s -> %s %s" % (method, path, exc.code, detail[:400]))
        except Exception as exc:
            if attempt == 0:
                # The service may have restarted on a new port; re-read and retry.
                global _service_cache
                _service_cache = None
                continue
            raise RuntimeError("%s %s -> %s" % (method, path, exc))


def _create_body(title: str) -> dict:
    body: dict = {"title": title, "agent": _cfg.get("agent") or "assistant"}
    model = _cfg.get("model_ref")
    if isinstance(model, dict):
        # Model.Ref wants id + providerID (a "modelID" key is rejected).
        model_id = model.get("id") or model.get("modelID")
        if model_id and model.get("providerID"):
            ref = {"id": model_id, "providerID": model["providerID"]}
            if model.get("variant"):
                ref["variant"] = model["variant"]
            body["model"] = ref
    return body


def resolve_session() -> str:
    """Configured id, else the session titled in config, else create it."""
    configured = _cfg.get("session_id")
    if configured:
        return configured

    target = (_cfg.get("target") or "active").lower()
    data = api("GET", "/api/session?limit=100&order=desc") or {}
    sessions = [s for s in (data.get("data") or []) if isinstance(s, dict) and s.get("id")]

    if target == "active":
        # The app records when a session was last viewed: that is the one on screen.
        viewed = [s for s in sessions if (s.get("time") or {}).get("viewed")]
        if viewed:
            return max(viewed, key=lambda s: s["time"]["viewed"])["id"]

    if target in ("active", "latest"):
        if sessions:
            return sessions[0]["id"]

    title = _cfg.get("session_title") or "voice"
    for session in sessions:
        if session.get("title") == title:
            return session["id"]

    created = api("POST", "/api/session", _create_body(title)) or {}
    session = created.get("data", created)
    session_id = session.get("id") if isinstance(session, dict) else None
    if not session_id:
        raise RuntimeError("could not create the %r session: %s" % (title, created))
    log("created session %s (%r)" % (session_id, title))
    return session_id


def _foreground_exe() -> str:
    """Basename of the process owning the foreground window."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    hwnd = user32.GetForegroundWindow()
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _set_clipboard(text: str) -> bool:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # 64-bit handles must be declared, or ctypes truncates them to int.
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.restype = wintypes.BOOL

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    data = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(data)

    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
    if not handle:
        return False
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        kernel32.GlobalFree(handle)
        return False
    try:
        ctypes.memmove(pointer, data, size)
    finally:
        kernel32.GlobalUnlock(handle)

    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(handle)
        return False
    try:
        user32.EmptyClipboard()
        return bool(user32.SetClipboardData(CF_UNICODETEXT, handle))
    finally:
        user32.CloseClipboard()


_INJECTING = threading.Event()

_compose_state = {"has_text": False, "text": ""}












def _clipboard_get_text() -> str | None:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]

    CF_UNICODETEXT = 13
    if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
        return None
    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _type_text(text: str) -> None:
    """SendInput with KEYEVENTF_UNICODE: no clipboard, surrogate-pair aware."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004
    VK_RETURN = 0x0D
    ULONG_PTR = ctypes.c_size_t

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class _Union(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("padding", ctypes.c_byte * 32)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("union", _Union)]

    def event(vk: int = 0, scan: int = 0, flags: int = 0):
        item = INPUT()
        item.type = INPUT_KEYBOARD
        item.union.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
        return item

    events = []
    for char in text.replace("\r\n", "\n"):
        if char == "\n":
            events.append(event(vk=VK_RETURN))
            events.append(event(vk=VK_RETURN, flags=KEYEVENTF_KEYUP))
            continue
        for byte_index in range(0, len(char.encode("utf-16-le")), 2):
            unit = int.from_bytes(char.encode("utf-16-le")[byte_index:byte_index + 2], "little")
            events.append(event(scan=unit, flags=KEYEVENTF_UNICODE))
            events.append(event(scan=unit, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))

    batch = 32
    for start in range(0, len(events), batch):
        chunk = events[start:start + batch]
        array = (INPUT * len(chunk))(*chunk)
        sent = user32.SendInput(len(chunk), array, ctypes.sizeof(INPUT))
        if sent != len(chunk):
            raise OSError("SendInput injected %d/%d events (err %d)"
                          % (sent, len(chunk), kernel32.GetLastError()))
        time.sleep(0.005)


def insert_text(text: str, indicator: "Indicator") -> None:
    """Type by default; paste for long text (typing thousands of chars is slow)."""
    mode = (_cfg.get("injection") or "auto").lower()
    limit = int(_cfg.get("paste_threshold_chars", 200))
    use_paste = mode == "paste" or (mode == "auto" and len(text) > limit)

    _INJECTING.set()
    try:
        if use_paste:
            import keyboard as kb

            previous = _clipboard_get_text()
            if not _set_clipboard(text):
                raise OSError("could not set the clipboard")
            kb.send("left ctrl+v")
            time.sleep(0.08)
            if previous is not None:
                time.sleep(0.25)  # let the target read it before restoring
                _set_clipboard(previous)
        else:
            _type_text(text)
    finally:
        _INJECTING.clear()


def foreground_elevated() -> bool:
    """True when the foreground process runs elevated (UIPI blocks our input)."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetTokenInformation.restype = wintypes.BOOL

    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    token = wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(handle, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            return False
        elevation = wintypes.DWORD()
        size = wintypes.DWORD()
        ok = advapi32.GetTokenInformation(
            token, 20, ctypes.byref(elevation), ctypes.sizeof(elevation), ctypes.byref(size))
        return bool(ok and elevation.value)
    finally:
        if token:
            kernel32.CloseHandle(token)
        kernel32.CloseHandle(handle)


def compose(text: str, action: str, indicator: "Indicator") -> None:
    """Type into the focused composer instead of submitting."""
    import keyboard as kb

    target = (_cfg.get("compose_target") or "*").lower()
    front = _foreground_exe().lower()
    deny = [name.lower() for name in (_cfg.get("compose_deny") or [])]
    if front in deny:
        indicator.post("warn", "%s blocked" % front)
        log("compose skipped: %s is on the deny list" % front)
        return
    if target != "*" and front != target:
        indicator.post("warn", "focus the chat box")
        log("compose skipped: foreground is %r, not %s" % (front or "unknown", target))
        return

    if foreground_elevated():
        # UIPI: an elevated window ignores our synthetic input entirely.
        indicator.post("warn", "app is elevated")
        log("compose blocked: the foreground app runs elevated (UIPI)")
        if text:
            _set_clipboard(text)
        return

    if action == "send":
        _INJECTING.set()
        try:
            kb.send("enter")
        finally:
            _INJECTING.clear()
        _compose_state["has_text"] = False
        indicator.post("done", "✓ sent")
        log("compose: sent")
        return

    if action == "clear":
        _INJECTING.set()
        try:
            kb.send("left ctrl+a")
            time.sleep(0.05)
            kb.send("delete")
        finally:
            _INJECTING.clear()
        _compose_state["has_text"] = False
        indicator.post("warn", "cleared")
        log("compose: cleared the prompt")
        return

    if action == "delete_words":
        count = int(text or 0)
        mirror = _compose_state["text"].rstrip()
        # Count tokens, not words: a stray "." sitting on its own must go too.
        span = _tokens_for_words(mirror, count) if mirror else count
        _INJECTING.set()
        try:
            for _ in range(max(span, 0)):
                kb.send("left ctrl+left shift+left")
                time.sleep(0.02)
            kb.send("backspace")
        finally:
            _INJECTING.clear()
        _compose_state["text"] = _trim_tokens(mirror, span)
        indicator.post("warn", "deleted %d" % count)
        log("compose: deleted last %d word(s) (%d tokens)" % (count, span))
        return

    if action == "delete_sentence":
        # Count the words of the last sentence we inserted, then delete exactly
        # that many: no reliable way to select a sentence back in a textarea.
        mirror = _compose_state["text"].rstrip()
        parts = [part for part in re.split(r"(?<=[.!?])\s+", mirror) if part.strip()]
        if not parts:
            indicator.post("warn", "nothing to remove")
            log("compose: delete sentence ignored (no tracked text)")
            return
        count = _last_sentence_words(mirror)
        span = _tokens_for_words(mirror, count)
        _INJECTING.set()
        try:
            for _ in range(span):
                kb.send("left ctrl+left shift+left")
                time.sleep(0.02)
            kb.send("backspace")
        finally:
            _INJECTING.clear()
        _compose_state["text"] = _trim_tokens(mirror, span)
        indicator.post("warn", "deleted sentence")
        log("compose: deleted last sentence (%d words, %d tokens)" % (count, span))
        return

    if action.startswith("format:"):
        style = action.split(":", 1)[1]
        payload = format_words(text, style)
        if not payload:
            indicator.post("warn", "nothing to format")
            return
        if _compose_state["has_text"] and payload[:1] not in (" ", "\n"):
            payload = " " + payload
        insert_text(payload, indicator)
        _compose_state["has_text"] = True
        mirror = _compose_state["text"].rstrip()
        _compose_state["text"] = (mirror + " " + payload.strip()) if mirror else payload.strip()
        indicator.post("done", "✓ %s" % style)
        log("compose: formatted %s" % style)
        return

    if action.startswith("punct:"):
        char = action.split(":", 1)[1]
        insert_text(char, indicator)
        _compose_state["has_text"] = True
        _compose_state["text"] = _compose_state["text"].rstrip() + char
        indicator.post("done", "✓ %s" % char)
        log("compose: inserted punctuation %r" % char)
        return

    if action in ("undo", "cancel"):
        _INJECTING.set()
        try:
            kb.send("left ctrl+z")
        finally:
            _INJECTING.clear()
        indicator.post("warn", "undone")
        log("compose: %s (ctrl+z)" % action)
        return

    if action == "redo":
        _INJECTING.set()
        try:
            kb.send("left ctrl+y")
        finally:
            _INJECTING.clear()
        indicator.post("done", "redone")
        log("compose: redo (ctrl+y)")
        return

    if action == "copy":
        _INJECTING.set()
        try:
            kb.send("left ctrl+c")
        finally:
            _INJECTING.clear()
        indicator.post("done", "copied")
        log("compose: copy (ctrl+c)")
        return

    if action == "paste":
        _INJECTING.set()
        try:
            kb.send("left ctrl+v")
        finally:
            _INJECTING.clear()
        indicator.post("done", "pasted")
        log("compose: paste (ctrl+v)")
        return

    if action == "new_paragraph":
        insert_text("\n\n", indicator)
        _compose_state["text"] = _compose_state["text"].rstrip() + "\n\n"
        indicator.post("done", "¶")
        log("compose: new paragraph")
        return

    payload = "\n" if action == "newline" else _clean_spacing(text)
    if action == "add" and _compose_state["has_text"] and payload[:1] not in (" ", "\n"):
        payload = " " + payload
    insert_text(payload, indicator)
    if action in ("add", "newline"):
        _compose_state["has_text"] = True
        mirror = _compose_state["text"].rstrip()
        _compose_state["text"] = (mirror + " " + payload.strip()) if mirror else payload.strip()
    indicator.post("done", "✓ added")
    log("compose: added %d chars" % len(payload))




































def _part_text(parts) -> str:
    chunks = []
    for part in parts or []:
        if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
            chunks.append(part["text"])
    return "\n".join(chunks).strip()


def last_reply_text() -> str:
    """Newest assistant message that actually contains text."""
    session_id = resolve_session()
    data = api("GET", "/api/session/%s/message?limit=20" % session_id) or {}
    for message in data.get("data", []):
        if message.get("type") == "assistant":
            text = _part_text(message.get("content"))
            if text:
                return text
    return ""


def sanitise_for_speech(text: str, limit: int = 700) -> str:
    """Strip what should not be read aloud: code, paths, urls, markup."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`[^`]*`", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[A-Za-z]:\\[^\s]+", " ", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*\|.*\|\s*$", " ", text, flags=re.M)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.M)
    text = re.sub(r"[*_~]{1,3}", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        cut = text[:limit]
        stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        text = cut[: stop + 1] if stop > 200 else cut
    return text


def speak_text(text: str, indicator: "Indicator") -> None:
    """Local Piper, then play it. Runs in a thread so recording stays responsive."""
    path = _cfg.get("tts_voice")
    if not path or not Path(path).exists():
        indicator.post("warn", "no tts voice")
        log("tts: voice file missing: %r" % path)
        return
    if not text:
        indicator.post("warn", "nothing to say")
        return

    def run() -> None:
        import tempfile
        import wave

        try:
            from piper import PiperVoice

            voice = PiperVoice.load(path)
            wav_path = Path(tempfile.gettempdir()) / "opencode-voice-tts.wav"
            # piper >= 1.8 yields AudioChunk objects; older builds write a wav.
            chunks = list(voice.synthesize(text))
            if not chunks:
                raise RuntimeError("piper returned no audio")
            with wave.open(str(wav_path), "wb") as handle:
                handle.setnchannels(chunks[0].sample_channels)
                handle.setsampwidth(chunks[0].sample_width)
                handle.setframerate(chunks[0].sample_rate)
                for chunk in chunks:
                    handle.writeframes(chunk.audio_int16_bytes)
            import winsound

            winsound.PlaySound(str(wav_path), winsound.SND_FILENAME)
        except Exception as exc:
            log("tts failed: %s" % exc)

    indicator.post("done", "♪ speaking")
    threading.Thread(target=run, daemon=True).start()
    log("tts: speaking %d chars" % len(text))


def speak_last_reply(indicator: "Indicator") -> None:
    try:
        raw = last_reply_text()
    except Exception as exc:
        indicator.post("warn", "no reply")
        log("tts: could not read the reply: %s" % exc)
        return
    speak_text(sanitise_for_speech(raw), indicator)


def deliver(text: str, mode: str, indicator: "Indicator") -> None:
    """Route a transcript: speak, submit it, or type it into the focused composer."""
    steps = plan_transcript(text)
    if not steps:
        return

    for action, _ in steps:
        if action == "speak":
            speak_last_reply(indicator)
    steps = [step for step in steps if step[0] != "speak"]
    if not steps:
        return

    if (mode or "submit").lower() == "compose":
        for action, payload in steps:
            compose(payload, action, indicator)
        if all(action == "add" for action, _ in steps):
            indicator.post("done", "✓ added")
        return

    spoken = " ".join(payload for action, payload in steps if action == "add").strip()
    if not spoken:
        return
    submit(spoken, resolve_session())
    indicator.post("done", "✓ sent")


def submit(text: str, session_id: str) -> None:
    try:
        api("POST", "/api/session/%s/prompt" % session_id, {"text": text, "delivery": "queue"})
        log("submitted %d chars to %s" % (len(text), session_id))
    except Exception as exc:
        log("submit failed: %s" % exc)
        return

    # Confirm it actually landed: a busy session can accept and drop a prompt.
    for _ in range(6):
        time.sleep(0.5)
        try:
            data = api("GET", "/api/session/%s/message?limit=10" % session_id) or {}
        except Exception:
            continue
        rows = data.get("data", [])
        for row in rows:
            if row.get("type") == "user" and (row.get("text") or "").strip() == text.strip():
                log("confirmed in session %s" % session_id)
                return
    log("prompt not visible in %s after 3s (session busy?)" % session_id)
    try:
        inbox = api("GET", "/api/session/%s/inbox" % session_id) or {}
        queued = [row for row in inbox.get("data", []) if (row.get("payload") or {}).get("text") == text.strip()]
        if queued:
            log("confirmed queued in session %s (waits for the current turn)" % session_id)
    except Exception:
        pass


def beep(freq: int, ms: int) -> None:
    if not _cfg.get("beep", True):
        return

    def play() -> None:
        try:
            import winsound

            winsound.Beep(freq, ms)
        except Exception:
            pass

    threading.Thread(target=play, daemon=True).start()


def load_model() -> WhisperModel:
    """CUDA when it works, CPU int8 otherwise."""
    size = _cfg.get("model", "base")
    mode = (_cfg.get("device") or "auto").lower()
    attempts = []
    if mode in ("auto", "cuda"):
        attempts.append(("cuda", "int8_float16"))
    if mode in ("auto", "cpu"):
        attempts.append(("cpu", "int8"))
    for device, compute in attempts:
        try:
            model = WhisperModel(size, device=device, compute_type=compute)
            log("whisper %s loaded on %s (%s)" % (size, device, compute))
            return model
        except Exception as exc:
            log("whisper on %s failed: %s" % (device, exc))
    raise RuntimeError("whisper could not load on any device")


def main() -> int:
    global _cfg
    _cfg = load_config()
    LOG_DIR.mkdir(exist_ok=True)

    # Single instance: two listeners would fire two prompts per key press.
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW(None, False, "Global\\opencode_voice_ptt")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        log("another listener is already running, exiting")
        return 0

    log("starting: hold=%r toggle=%r model=%r mic=%r" % (
        _cfg.get("hold_key") or _cfg.get("key"),
        _cfg.get("toggle_key") or "scroll lock",
        _cfg.get("model"),
        _cfg.get("mic_device"),
    ))

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    load_phrases()
    model = load_model()
    log("whisper model loaded")

    device = pick_device(_cfg.get("mic_device"))
    log("mic: %s (follows the default input)" % sd.query_devices(device)["name"])

    recorder = Recorder(int(_cfg.get("sample_rate", 16000)), int(_cfg.get("max_seconds", 60)))
    indicator = Indicator()
    rate = int(_cfg.get("sample_rate", 16000))
    hold_mode = (_cfg.get("mode") or "submit").lower()
    toggle_mode = (_cfg.get("toggle_mode") or "compose").lower()
    hold_key = (_cfg.get("hold_key") or _cfg.get("key") or "right ctrl").lower()
    toggle_key = (_cfg.get("toggle_key") or "scroll lock").lower()
    state = {"held": False, "busy": False, "live": False, "toggle_down": False}
    guard = threading.Lock()

    min_samples = int(float(_cfg.get("min_seconds", 0.35)) * rate)
    silence_peak = float(_cfg.get("silence_peak", 0.002))

    def handle_audio(audio: np.ndarray, mode: str, source: str) -> None:
        if audio.size < min_samples:
            indicator.post("warn", "too short")
            log("%s: clip too short (%.2fs), ignored" % (source, audio.size / rate))
            return
        peak = float(np.abs(audio).max()) if audio.size else 0.0
        if peak < silence_peak:
            indicator.post("warn", "mic muted?")
            log("%s: silent clip (peak %.4f) - mic muted?, ignored" % (source, peak))
            return
        seconds = audio.size / rate
        started = time.perf_counter()
        text = transcribe(model, audio, _cfg.get("language", "en"))
        took = time.perf_counter() - started
        text = apply_dictionary(text)
        if not text:
            indicator.post("warn", "nothing heard")
            log("%s: empty transcript (%.2fs clip, %.2fs stt)" % (source, seconds, took))
            return
        log("%s heard (%.2fs clip, %.2fs stt): %s" % (source, seconds, took, text))
        deliver(text, mode, indicator)

    def on_segment(audio: np.ndarray) -> None:
        indicator.post("stt", "… transcribing")
        try:
            handle_audio(audio, toggle_mode, "live")
        except Exception as exc:
            indicator.post("warn", "error")
            log("live error: %s" % exc)
        if state["live"]:
            indicator.post("live", "● live — %s to stop" % toggle_key)

    def on_partial(audio: np.ndarray) -> None:
        if not state["live"]:
            return
        try:
            partial = transcribe(model, audio, _cfg.get("language", "en"))
        except Exception as exc:
            log("live caption failed: %s" % exc)
            return
        if partial:
            indicator.post("stt", "… " + partial[-48:])

    live = LiveListener(
        rate,
        on_segment,
        threshold=float(_cfg.get("vad_threshold", 0.006)),
        hangover=float(_cfg.get("vad_hangover", 0.7)),
        on_partial=on_partial if _cfg.get("live_caption", True) else None,
    )

    def stop_live() -> None:
        if not state["live"]:
            return
        state["live"] = False
        live.stop()
        indicator.post("hide", "")
        beep(600, 30)
        log("live: stopped")

    def start_live() -> None:
        if state["live"]:
            return
        if state["busy"] or state["held"]:
            indicator.post("warn", "busy")
            return
        device = pick_device(_cfg.get("mic_device"))
        if live.start(device):
            state["live"] = True
            _compose_state["has_text"] = True  # assume the box may already hold text
            indicator.post("live", "● live — %s to stop" % toggle_key)
            beep(880, 40)
            log("live: listening on %s" % sd.query_devices(device)["name"])

    def sync_live() -> None:
        """Match the key's own state, so the LED can never disagree with us."""
        if scroll_lock_on():
            start_live()
        else:
            stop_live()

    def toggle_live() -> None:
        # The key has already flipped its own state; match it rather than guess,
        # and read it slightly later so the OS has applied the toggle.
        threading.Timer(0.08, sync_live).start()

    def on_press(event) -> None:
        with guard:
            if state["held"] or state["busy"]:
                return
            state["held"] = True
        device = pick_device(_cfg.get("mic_device"))
        state["device"] = sd.query_devices(device)["name"]
        if recorder.start(device):
            indicator.post("rec", "● recording")
            beep(880, 40)
            log("recording on %s" % state["device"])

    def on_release(event) -> None:
        with guard:
            if not state["held"]:
                return
            state["held"] = False
            cancelled = state.pop("cancel", False)
            state["busy"] = True
        audio = recorder.stop()
        if cancelled:
            indicator.post("warn", "cancelled")
            log("hold: cancelled, clip discarded (%.2fs)" % (audio.size / rate))
            with guard:
                state["busy"] = False
            return
        indicator.post("stt", "… transcribing")
        beep(600, 30)
        try:
            handle_audio(audio, hold_mode, "hold")
        except Exception as exc:
            indicator.post("warn", "error")
            log("error: %s" % exc)
        finally:
            with guard:
                state["busy"] = False

    def handle(event) -> None:
        # Both ctrls (and both shifts) report the same scan code; only the event
        # name separates left from right, so match on the name.
        if _INJECTING.is_set():
            return  # our own keystrokes must never re-trigger the listener
        name = (event.name or "").lower()
        down = event.event_type == "down"

        if name in ("esc", "escape"):
            if down and state["held"]:
                state["cancel"] = True
                indicator.post("warn", "cancelling")
                log("hold: cancel requested")
            return

        if name == hold_key:
            if state["live"]:
                return
            if down:
                on_press(event)
            else:
                on_release(event)
        elif name == toggle_key:
            if down:
                if state["toggle_down"]:
                    return
                state["toggle_down"] = True
                toggle_live()
            else:
                state["toggle_down"] = False

    keyboard.hook(handle)
    indicator.post("hide", "")
    log("ready: hold %s (%s) | tap %s (%s)" % (hold_key, hold_mode, toggle_key, toggle_mode))

    # Adopt the key's current state at startup, so a lit LED resumes dictation
    # instead of silently disagreeing with the pill.
    threading.Thread(target=sync_live, daemon=True).start()

    # Tk owns the main thread; the hook needs its own thread to stay pumped.
    threading.Thread(target=keyboard.wait, daemon=True).start()
    try:
        indicator.run()
    except KeyboardInterrupt:
        pass
    finally:
        if state["live"]:
            live.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
