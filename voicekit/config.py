"""Configuration, paths and the shared log file."""
import json
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "voice.json"
LOG_DIR = ROOT / "logs"
LOG_PATH = LOG_DIR / "voice.log"

CFG: dict = {}
_log_lock = threading.Lock()

# Set while text-to-speech is playing, so the microphone never transcribes the
# voice it just produced. Audio modules drop frames while this is set.
SPEAKING = threading.Event()


def configure(cfg: dict) -> None:
    """Point every module at the live config dict (same object, so updates show)."""
    CFG.clear()
    CFG.update(cfg)


def load_config() -> dict:
    # utf-8-sig: PowerShell's Set-Content writes a BOM, which json.load rejects.
    with CONFIG_PATH.open(encoding="utf-8-sig") as fh:
        return json.load(fh)


def log(message: str) -> None:
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
    LOG_DIR.mkdir(exist_ok=True)
    with _log_lock:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    print(line, flush=True)
