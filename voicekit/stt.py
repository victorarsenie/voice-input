"""Whisper: CUDA DLL registration, on-demand model, transcription."""
import gc
import os
import sys
import threading
import time
from pathlib import Path

import numpy as np

from .config import CFG, log


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


_model = None
_model_lock = threading.Lock()
_model_last_used = 0.0
def ensure_model() -> WhisperModel:
    """Load on demand and remember when it was last needed."""
    global _model, _model_last_used
    with _model_lock:
        if _model is None:
            _model = load_model()
        _model_last_used = time.time()
        return _model
def release_model(reason: str) -> None:
    """Free the CUDA context and weights so a local LLM can have the VRAM."""
    global _model
    with _model_lock:
        if _model is None:
            return
        _model = None
    import gc

    gc.collect()
    log("model released (%s)" % reason)
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
def load_model() -> WhisperModel:
    """CUDA when it works, CPU int8 otherwise."""
    size = CFG.get("model", "base")
    mode = (CFG.get("device") or "auto").lower()
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
