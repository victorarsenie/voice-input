"""Microphone capture: device choice, native-rate fallback, pause segmentation."""
import queue
import threading
import time

import numpy as np
import sounddevice as sd

from .config import SPEAKING, log


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
        if SPEAKING.is_set():
            return  # never record our own speech
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
        if SPEAKING.is_set():
            # Drop frames (and any half-captured phrase) while the reply is read.
            with self._lock:
                self._buf = []
                self._speech = 0
                self._silence = 0
            return
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
