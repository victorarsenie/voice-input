"""Windows input: SendInput typing, clipboard fallback, focus and UIPI checks."""
import ctypes
import threading
import time
from ctypes import wintypes

import keyboard

from .config import CFG, log

# Set while we inject, so our own keystrokes never re-trigger the hotkeys.
INJECTING = threading.Event()


def scroll_lock_on() -> bool:
    """The key's own toggle state, which the keyboard LED shows."""
    import ctypes

    return bool(ctypes.windll.user32.GetKeyState(0x91) & 1)
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
    mode = (CFG.get("injection") or "auto").lower()
    limit = int(CFG.get("paste_threshold_chars", 200))
    use_paste = mode == "paste" or (mode == "auto" and len(text) > limit)

    INJECTING.set()
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
        INJECTING.clear()
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
