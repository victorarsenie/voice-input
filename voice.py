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
from faster_whisper import WhisperModel

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


def default_input() -> int | None:
    """Re-read the Windows default input. PortAudio caches devices at init, so
    re-init first; it costs ~30 ms and makes headset swaps take effect."""
    try:
        sd._terminate()
        sd._initialize()
    except Exception as exc:
        log("audio re-init failed: %s" % exc)
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
        import queue

        self.queue: queue.Queue = queue.Queue()
        self.hide_at = 0.0

    def post(self, kind: str, text: str = "") -> None:
        self.queue.put((kind, text))

    def run(self) -> None:
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
        )
        label.pack()
        width, height = 190, 42
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        root.geometry("%dx%d+%d+%d" % (width, height, (screen_w - width) // 2, screen_h - height - 96))
        root.withdraw()

        def poll() -> None:
            try:
                while True:
                    kind, text = self.queue.get_nowait()
                    if kind == "hide":
                        root.withdraw()
                        self.hide_at = 0.0
                        continue
                    label.config(text=text, fg=self.COLORS.get(kind, "#ffffff"))
                    root.deiconify()
                    root.lift()
                    self.hide_at = 0.0 if kind in ("rec", "stt", "live") else time.time() + 1.3
            except Exception:
                pass
            if self.hide_at and time.time() > self.hide_at:
                root.withdraw()
                self.hide_at = 0.0
            root.after(40, poll)

        root.after(40, poll)
        root.mainloop()


class Recorder:
    """One stream per utterance, opened on key-down and closed on key-up."""

    def __init__(self, rate: int, max_seconds: int) -> None:
        self.rate = rate
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
        try:
            self._stream = sd.InputStream(
                device=device,
                channels=1,
                samplerate=self.rate,
                dtype="float32",
                callback=self._callback,
            )
            self._stream.start()
            return True
        except Exception as exc:
            log("could not open mic: %s" % exc)
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
        return np.concatenate(chunks).reshape(-1)


class LiveListener:
    """Continuous capture for toggle mode: closes a segment on a pause and
    hands each phrase to a worker, so speech keeps flowing while whisper runs."""

    def __init__(self, rate: int, on_segment, threshold: float = 0.006,
                 hangover: float = 0.7, min_speech: float = 0.35,
                 max_segment: float = 20.0) -> None:
        self.rate = rate
        self.on_segment = on_segment
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
        if audio.size:
            self._queue.put(audio)

    def start(self, device: int | None) -> bool:
        try:
            self._stream = sd.InputStream(
                device=device, channels=1, samplerate=self.rate,
                dtype="float32", callback=self._callback,
            )
            self._stream.start()
        except Exception as exc:
            log("live: could not open mic: %s" % exc)
            self._stream = None
            return False
        self._running = True
        self._worker = threading.Thread(target=self._work, daemon=True)
        self._worker.start()
        return True

    def _work(self) -> None:
        while self._running or not self._queue.empty():
            try:
                audio = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.on_segment(audio)
            except Exception as exc:
                log("live segment error: %s" % exc)

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


def transcribe(model: WhisperModel, audio: np.ndarray, language: str) -> str:
    segments, info = model.transcribe(
        audio,
        language=language,
        beam_size=1,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    text = " ".join(seg.text.strip() for seg in segments).strip()
    return text


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


def _last_sentence_words(text: str) -> int:
    parts = [part for part in re.split(r"(?<=[.!?])\s+", text.rstrip()) if part.strip()]
    return len(parts[-1].split()) if parts else 0


def _clean_spacing(text: str) -> str:
    """Whisper sometimes emits "word ." - pull punctuation back onto the word."""
    text = re.sub(r"\s+([,.;:!?%)\]}])", r"\1", text)
    text = re.sub(r"([(\[{])\s+", r"\1", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _is_word(token: str) -> bool:
    return any(ch.isalnum() for ch in token)


def _tokens_for_words(text: str, words: int) -> int:
    """How many whitespace tokens the last `words` words plus trailing punctuation span."""
    if words <= 0:
        return 0
    span = 0
    seen = 0
    for token in reversed(text.rstrip().split()):
        span += 1
        if _is_word(token):
            seen += 1
            if seen >= words:
                break
    return span if seen else 0


def _trim_tokens(text: str, span: int) -> str:
    tokens = text.rstrip().split()
    if span >= len(tokens):
        return ""
    return " ".join(tokens[: len(tokens) - span])


def compose(text: str, action: str, indicator: "Indicator") -> None:
    """Type into the focused composer instead of submitting."""
    import keyboard as kb

    target = (_cfg.get("compose_target") or "OpenCode.exe").lower()
    front = _foreground_exe().lower()
    if front != target:
        indicator.post("warn", "focus the chat box")
        log("compose skipped: foreground is %r, not %s" % (front or "unknown", target))
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

    if action.startswith("punct:"):
        char = action.split(":", 1)[1]
        _INJECTING.set()
        try:
            if _set_clipboard(char):
                kb.send("left ctrl+v")
                time.sleep(0.05)
            else:
                kb.write(char)
        finally:
            _INJECTING.clear()
        _compose_state["has_text"] = True
        _compose_state["text"] = _compose_state["text"].rstrip() + char
        indicator.post("done", "✓ %s" % char)
        log("compose: inserted punctuation %r" % char)
        return

    payload = "\n" if action == "newline" else _clean_spacing(text)
    if action == "add" and _compose_state["has_text"] and payload[:1] not in (" ", "\n"):
        payload = " " + payload
    _INJECTING.set()
    try:
        if _set_clipboard(payload):
            kb.send("left ctrl+v")
            time.sleep(0.05)
        else:
            kb.write(payload)
    finally:
        _INJECTING.clear()
    if action in ("add", "newline"):
        _compose_state["has_text"] = True
        mirror = _compose_state["text"].rstrip()
        _compose_state["text"] = (mirror + " " + payload.strip()) if mirror else payload.strip()
    indicator.post("done", "✓ added")
    log("compose: added %d chars" % len(payload))


def classify(text: str) -> str | None:
    """Map a spoken phrase to a command action, if the whole phrase is one."""
    return _PHRASES.get(_normalise(text))


def split_sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part.strip()]


NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

_EDIT_VERBS = r"(?:delete|remove|undo|cut)"
_EDIT_NUM_WORDS = re.compile(
    _EDIT_VERBS + r"\s+(?:the\s+)?(?:last\s+)?(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s*(?:words?)?"
)
_EDIT_LAST_WORD = re.compile(_EDIT_VERBS + r"\s+(?:the\s+)?last\s+word")
_EDIT_LAST_SENTENCE = re.compile(_EDIT_VERBS + r"\s+(?:the\s+)?last\s+sentence")

_PHRASES: dict[str, str] = {}


DEFAULT_PUNCTUATION = {
    ",": ["comma", "add comma", "punctuation comma"],
    ".": ["period", "full stop", "add period", "punctuation period"],
    ";": ["semicolon", "add semicolon", "punctuation semicolon"],
    ":": ["colon", "add colon", "punctuation colon"],
    "?": ["question mark", "add question mark"],
    "!": ["exclamation mark", "exclamation point", "add exclamation mark"],
    "-": ["dash", "hyphen", "add dash"],
    "(": ["open bracket", "open parenthesis"],
    ")": ["close bracket", "close parenthesis"],
    "'": ["apostrophe", "single quote"],
    '"': ["quote", "double quote"],
}


def load_phrases() -> None:
    """Flatten compose_commands and punctuation into phrase -> action, longest first."""
    _PHRASES.clear()
    table = _cfg.get("compose_commands") or {}
    for action, phrases in table.items():
        for phrase in phrases:
            normalised = _normalise(phrase)
            if normalised:
                _PHRASES[normalised] = action

    punctuation = dict(DEFAULT_PUNCTUATION)
    for char, phrases in (_cfg.get("punctuation_commands") or {}).items():
        punctuation[char] = phrases
    for char, phrases in punctuation.items():
        for phrase in phrases:
            normalised = _normalise(phrase)
            if normalised:
                _PHRASES.setdefault(normalised, "punct:" + char)


def _normalise(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split())


def _collapse(actions: list[str]) -> list[str]:
    out: list[str] = []
    for action in actions:
        if not out or out[-1] != action:
            out.append(action)
    return out


def _consume_all(normalised: str) -> list[str] | None:
    """True only if the whole utterance is a sequence of command phrases."""
    tokens = normalised.split()
    ordered = sorted(_PHRASES, key=lambda p: len(p.split()), reverse=True)
    actions: list[str] = []
    index = 0
    while index < len(tokens):
        for phrase in ordered:
            words = phrase.split()
            if tokens[index:index + len(words)] == words:
                actions.append(_PHRASES[phrase])
                index += len(words)
                break
        else:
            return None
    return actions or None


def _trailing_command(text: str) -> tuple[str, str] | None:
    """A command phrase at the very end of a longer utterance."""
    ordered = sorted(_PHRASES, key=len, reverse=True)
    for phrase in ordered:
        match = re.search(r"(?:^|\s)" + re.escape(phrase) + r"[\s.!?]*$", text, re.I)
        if match:
            return text[:match.start()].strip(), _PHRASES[phrase]
    return None


def parse_edit(text: str) -> tuple[str, str] | None:
    """Whole-utterance edit commands, tolerant of punctuation and missing words."""
    norm = _normalise(text)
    if _EDIT_LAST_SENTENCE.fullmatch(norm):
        return ("delete_sentence", "")
    if _EDIT_LAST_WORD.fullmatch(norm):
        return ("delete_words", "1")
    match = _EDIT_NUM_WORDS.fullmatch(norm)
    if match:
        token = match.group(1)
        count = int(token) if token.isdigit() else NUMBERS.get(token, 0)
        if count:
            return ("delete_words", str(count))
    return None


def plan_transcript(text: str) -> list[tuple[str, str]]:
    """Split into ordered steps: prose runs, spoken commands, word deletions."""
    whole = _consume_all(_normalise(text))
    if whole:
        return [(action, "") for action in _collapse(whole)]

    steps: list[tuple[str, str]] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            steps.append(("add", " ".join(buffer)))
            buffer.clear()

    for sentence in split_sentences(text):
        edit = parse_edit(sentence)
        action = None if edit else classify(sentence)
        if edit:
            flush()
            steps.append(edit)
        elif action:
            flush()
            if not steps or steps[-1][0] != action:
                steps.append((action, ""))
        else:
            buffer.append(sentence)

    if buffer:
        joined = " ".join(buffer)
        edit = parse_edit(joined)
        trailing = None if edit else _trailing_command(joined)
        if edit:
            steps.append(edit)
        elif trailing:
            head, action = trailing
            if head:
                steps.append(("add", head))
            if not steps or steps[-1][0] != action:
                steps.append((action, ""))
        else:
            steps.append(("add", joined))
    return steps


def deliver(text: str, mode: str, indicator: "Indicator") -> None:
    """Route a transcript: submit it, or type it into the focused composer."""
    if (mode or "submit").lower() == "compose":
        steps = plan_transcript(text)
        if not steps:
            return
        for action, payload in steps:
            compose(payload, action, indicator)
        if all(action == "add" for action, _ in steps):
            indicator.post("done", "✓ added")
        return
    submit(text, resolve_session())
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
        _cfg.get("toggle_key") or "right shift",
        _cfg.get("model"),
        _cfg.get("mic_device"),
    ))

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    load_phrases()
    model = WhisperModel(_cfg.get("model", "base"), device="cpu", compute_type="int8")
    log("whisper model loaded")

    device = pick_device(_cfg.get("mic_device"))
    log("mic: %s (follows the default input)" % sd.query_devices(device)["name"])

    recorder = Recorder(int(_cfg.get("sample_rate", 16000)), int(_cfg.get("max_seconds", 60)))
    indicator = Indicator()
    rate = int(_cfg.get("sample_rate", 16000))
    hold_mode = (_cfg.get("mode") or "submit").lower()
    toggle_mode = (_cfg.get("toggle_mode") or "compose").lower()
    hold_key = (_cfg.get("hold_key") or _cfg.get("key") or "right ctrl").lower()
    toggle_key = (_cfg.get("toggle_key") or "right shift").lower()
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
            indicator.post("live", "● live — right shift to stop")

    live = LiveListener(
        rate,
        on_segment,
        threshold=float(_cfg.get("vad_threshold", 0.006)),
        hangover=float(_cfg.get("vad_hangover", 0.7)),
    )

    def toggle_live() -> None:
        if state["live"]:
            state["live"] = False
            live.stop()
            indicator.post("hide", "")
            beep(600, 30)
            log("live: stopped")
            return
        if state["busy"] or state["held"]:
            indicator.post("warn", "busy")
            return
        device = pick_device(_cfg.get("mic_device"))
        if live.start(device):
            state["live"] = True
            _compose_state["has_text"] = True  # assume the box may already hold text
            indicator.post("live", "● live — right shift to stop")
            beep(880, 40)
            log("live: listening on %s" % sd.query_devices(device)["name"])

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
            state["busy"] = True
        audio = recorder.stop()
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
