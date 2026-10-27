"""Write assets/voice.ico from the same glyph the tray uses.

Run after changing icon_image() so the shortcut and the tray never diverge:
    .venv\\Scripts\\python.exe make-icon.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from voicekit.overlay import icon_image

OUT = ROOT / "assets" / "voice.ico"
SIZES = [16, 20, 24, 32, 48, 64, 128, 256]

OUT.parent.mkdir(exist_ok=True)
base = icon_image("#7f8c8d", 256)
base.save(OUT, format="ICO", sizes=[(size, size) for size in SIZES])
print("wrote %s (%d bytes, sizes %s)" % (OUT, OUT.stat().st_size, SIZES))
