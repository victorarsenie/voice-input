"""SendInput round-trip: type into a self-owned Tk window and read it back.

This is the injection path used for real dictation, exercised against a window
we control, so a failure can never type into someone else's app.
"""
import sys
import time
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ctypes

import voice

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

root = tk.Tk()
root.title("voice-injection-test")
root.geometry("640x220+200+200")
text = tk.Text(root, wrap="word")
text.pack(fill="both", expand=True)
root.update()

HWND = ctypes.c_void_p(user32.GetAncestor(root.winfo_id(), 2))


def force_foreground() -> None:
    """SetForegroundWindow is refused from a background process; attach first."""
    fg = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None)
    this_thread = kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(fg_thread, this_thread, True)
    try:
        user32.SetForegroundWindow(HWND)
    finally:
        user32.AttachThreadInput(fg_thread, this_thread, False)
    root.update()


force_foreground()
time.sleep(0.3)
text.focus_set()
root.update()

sample = "Hello from SendInput: 42, fifty — naïve ✓"
voice._cfg = voice.load_config()
voice._INJECTING.set()  # keep the real listener from reacting to these events
try:
    voice._type_text(sample)
finally:
    voice._INJECTING.clear()
root.update()
time.sleep(0.2)
root.update()

produced = text.get("1.0", "end-1c")
root.destroy()

if produced != sample:
    print("FAILED")
    print("  wanted:", repr(sample))
    print("  got:   ", repr(produced))
    sys.exit(1)
print("injection tests: %d characters typed and read back intact" % len(sample))
