"""Manual SendInput check: cannot type into the wrong app.

Run it, click the window it opens, then press Enter. Text is only injected after
you confirm the window has focus, so a focus race cannot spray characters into
another application. Exits 1 on mismatch.
"""
import ctypes
import sys
import time
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import voice

SAMPLE = "Hello from SendInput: 42, fifty — naïve ✓"

root = tk.Tk()
root.title("voice injection test — click here, then press Enter")
root.geometry("720x240+220+220")
box = tk.Text(root, wrap="word")
box.pack(fill="both", expand=True)

status = tk.Label(root, text="Click this window, then press Enter to type.", fg="#333")
status.pack()

user32 = ctypes.windll.user32
result = {"ok": None}


def run_test(_event=None) -> None:
    foreground = ctypes.c_void_p(user32.GetForegroundWindow())
    ours = ctypes.c_void_p(user32.GetAncestor(root.winfo_id(), 2))
    if foreground.value != ours.value:
        status.config(text="This window does not have focus — click it and press Enter again.")
        return
    box.delete("1.0", "end")
    root.update()
    voice._cfg = voice.load_config()
    voice._INJECTING.set()
    try:
        voice._type_text(SAMPLE)
    finally:
        voice._INJECTING.clear()
    root.update()
    time.sleep(0.2)
    root.update()
    got = box.get("1.0", "end-1c")
    result["ok"] = got == SAMPLE
    if result["ok"]:
        status.config(text="PASS — %d characters typed and read back intact." % len(SAMPLE))
    else:
        status.config(text="FAIL\nwanted: %r\ngot:    %r" % (SAMPLE, got))


root.bind("<Return>", run_test)
root.mainloop()

if result["ok"] is None:
    print("no test run (window closed first)")
    sys.exit(0)
print("injection manual test:", "PASS" if result["ok"] else "FAIL")
sys.exit(0 if result["ok"] else 1)
