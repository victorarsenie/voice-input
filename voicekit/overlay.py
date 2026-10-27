"""Status pill and tray icon: never take focus, never break dictation."""
import os
import queue
import threading
import time

from .config import LOG_PATH, log


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
        self.tray = None

    def post(self, kind: str, text: str = "") -> None:
        if self.tray is not None:
            self.tray.update(kind, text)
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
class Tray:
    """System-tray icon: shows state, click toggles live dictation."""

    COLORS = {
        "rec": "#e74c3c",
        "live": "#e74c3c",
        "stt": "#f1c40f",
        "done": "#2ecc71",
        "warn": "#e67e22",
        "hide": "#7f8c8d",
    }
    TITLES = {
        "rec": "Recording…",
        "live": "Live dictation",
        "stt": "Transcribing…",
        "done": "Voice dictation — ready",
        "warn": "Voice dictation — attention",
        "hide": "Voice dictation — idle",
    }

    def __init__(self, on_toggle) -> None:
        self.on_toggle = on_toggle
        self.icon = None
        self._lock = threading.Lock()

    @staticmethod
    def _image(color: str):
        from PIL import Image, ImageDraw

        size = 64
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((4, 4, size - 4, size - 4), fill=color)
        # A simple mic glyph: rounded capsule plus stand.
        draw.rounded_rectangle((26, 14, 38, 36), radius=6, fill="#141414")
        draw.arc((20, 22, 44, 44), start=0, end=180, fill="#141414", width=3)
        draw.line((32, 44, 32, 52), fill="#141414", width=3)
        return image

    def start(self) -> bool:
        try:
            import pystray

            menu = pystray.Menu(
                pystray.MenuItem("Start / stop live dictation", lambda: self.on_toggle(), default=True),
                pystray.MenuItem("Open log", lambda: __import__("os").startfile(str(LOG_PATH))),
                pystray.MenuItem("Quit", self._quit),
            )
            self.icon = pystray.Icon("voice-dictation", self._image(self.COLORS["hide"]),
                                     "Voice dictation — idle", menu)
            threading.Thread(target=self.icon.run, daemon=True).start()
            return True
        except Exception as exc:
            log("tray unavailable: %s" % exc)
            return False

    def update(self, kind: str, text: str = "") -> None:
        if self.icon is None:
            return
        color = self.COLORS.get(kind, self.COLORS["hide"])
        title = self.TITLES.get(kind, "Voice dictation")
        if text and kind in ("stt", "rec", "live"):
            title = "%s %s" % (title, text.strip("… ")) if kind == "stt" else title
        try:
            self.icon.icon = self._image(color)
            self.icon.title = title[:120]
        except Exception:
            pass

    def _quit(self) -> None:
        log("tray: quit requested")
        try:
            self.icon.stop()
        finally:
            os._exit(0)
