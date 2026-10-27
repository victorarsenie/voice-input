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


from voicekit import commands, config
from voicekit.config import CFG, LOG_PATH, load_config as _load_config_file, log
from voicekit.audio import LiveListener, Recorder, pick_device
from voicekit.inject import (
    INJECTING,
    _foreground_exe,
    _set_clipboard,
    foreground_elevated,
    insert_text,
    scroll_lock_on,
)
from voicekit.overlay import Indicator, Tray
from voicekit.stt import ensure_model, release_model, transcribe

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


_cfg: dict = {}
_INJECTING = INJECTING


def load_config() -> dict:
    """Load voice.json and point every module at the same dict."""
    cfg = _load_config_file()
    config.configure(cfg)
    return CFG


























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






_compose_state = {"has_text": False, "text": ""}




















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

    # Destination follows focus: inside OpenCode the hold key submits to the
    # active session; anywhere else it types into whatever has the cursor.
    target = (_cfg.get("submit_target") or "OpenCode.exe").lower()
    focused = _foreground_exe().lower()
    mode = (mode or "submit").lower()
    if mode != "compose" and focused != target:
        log("submit redirected: %r is focused, typing instead" % (focused or "unknown"))
        mode = "compose"

    for action, _ in steps:
        if action == "speak":
            speak_last_reply(indicator)
    steps = [step for step in steps if step[0] != "speak"]
    if not steps:
        return

    if mode == "compose":
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




def main() -> int:
    global _cfg
    _cfg = load_config()

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
    ensure_model()  # warm it now; idle unload releases it again later

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
        text = transcribe(ensure_model(), audio, _cfg.get("language", "en"))
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
            partial = transcribe(ensure_model(), audio, _cfg.get("language", "en"))
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

    if _cfg.get("tray", True):
        tray = Tray(on_toggle=lambda: stop_live() if state["live"] else start_live())
        if tray.start():
            indicator.tray = tray
            log("tray: icon started")

    idle_minutes = float(_cfg.get("idle_unload_minutes", 5))

    def idle_watch() -> None:
        """Give the VRAM back to local LLMs when dictation has been idle."""
        while True:
            time.sleep(30)
            if idle_minutes > 0 and _model is not None and (
                time.time() - _model_last_used > idle_minutes * 60
            ):
                release_model("idle %.0f min" % idle_minutes)

    threading.Thread(target=idle_watch, daemon=True).start()

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
