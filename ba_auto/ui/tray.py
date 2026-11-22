"""A tray icon that brings the panel back while the client keeps running. Windows only.

One hidden window and one icon id, for the life of the client process. A custom tray host can be strict: it
makes a popup entry for every new window and id it sees, even from failed calls, so this never
probes: it adds once, modifies if the icon already exists, and adds again only when the shell says
it restarted (`TaskbarCreated`). That broadcast needs a real top-level window, so the window is a
hidden tool window, which tiling window managers ignore.

Everything runs on this module's own thread, and a failure here only costs the icon.
"""
import ctypes
import ctypes.wintypes as wt
import math
import threading
import time

import pywintypes
import win32api
import win32con
import win32gui

TIP = "BAClient"
UID = 1
CLASS_NAME = "BAClientTray"
WM_TRAY = win32con.WM_APP + 41
NIN_SELECT = win32con.WM_USER
NIN_KEYSELECT = win32con.WM_USER + 1
WM_CONTEXTMENU = 0x007B
CLICKS = {win32con.WM_LBUTTONUP, win32con.WM_RBUTTONUP, WM_CONTEXTMENU, NIN_SELECT, NIN_KEYSELECT}
DEBOUNCE = 0.45                 # one click can arrive as several of the messages above
ICON_SIZE = 32


def icon_pixels(size=ICON_SIZE):
    """A blue disc with a white halo, as top-down BGRA. Drawn here so no game art is involved."""
    out = bytearray(size * size * 4)
    samples = 3
    centre, radius = size / 2, size / 2 - 0.5
    cos, sin = math.cos(math.radians(-18)), math.sin(math.radians(-18))
    for y in range(size):
        for x in range(size):
            inside = ring = 0
            for sy in range(samples):
                for sx in range(samples):
                    px = x + (sx + .5) / samples - centre
                    py = y + (sy + .5) / samples - centre
                    if px * px + py * py > radius * radius:
                        continue
                    inside += 1
                    u = (px * cos - py * sin) / (radius * .78)
                    v = (px * sin + py * cos) / (radius * .42)
                    ring += .42 <= u * u + v * v <= 1
            if not inside:
                continue
            white = ring / inside
            shade = y / max(1, size - 1)
            base = (0xf4 - 0x28 * shade, 0xb0 - 0x2d * shade, 0x2f - 0x1a * shade)    # BGR
            i = (y * size + x) * 4
            out[i:i + 3] = bytes(round(c * (1 - white) + 255 * white) for c in base)
            out[i + 3] = round(255 * inside / samples ** 2)
    return bytes(out)


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
                ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class _IconInfo(ctypes.Structure):
    _fields_ = [("fIcon", wt.BOOL), ("xHotspot", wt.DWORD), ("yHotspot", wt.DWORD), ("hbmMask", wt.HBITMAP), ("hbmColor", wt.HBITMAP)]


def make_icon(size=ICON_SIZE):
    """An HICON built from icon_pixels."""
    gdi = ctypes.WinDLL("gdi32", use_last_error=True)
    user = ctypes.WinDLL("user32", use_last_error=True)
    gdi.CreateDIBSection.restype = wt.HBITMAP
    gdi.CreateDIBSection.argtypes = (wt.HDC, ctypes.POINTER(_BitmapInfo), wt.UINT, ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD)
    gdi.CreateBitmap.restype = wt.HBITMAP
    gdi.CreateBitmap.argtypes = (ctypes.c_int, ctypes.c_int, wt.UINT, wt.UINT, ctypes.c_void_p)
    gdi.DeleteObject.argtypes = (wt.HGDIOBJ,)
    user.CreateIconIndirect.restype = wt.HICON
    user.CreateIconIndirect.argtypes = (ctypes.POINTER(_IconInfo),)
    header = _BitmapInfo(biSize=ctypes.sizeof(_BitmapInfo), biWidth=size, biHeight=-size, biPlanes=1, biBitCount=32)
    bits = ctypes.c_void_p()
    color = gdi.CreateDIBSection(None, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
    if not color:
        raise ctypes.WinError(ctypes.get_last_error())
    mask = gdi.CreateBitmap(size, size, 1, 1, None)
    try:
        ctypes.memmove(bits, icon_pixels(size), size * size * 4)
        icon = user.CreateIconIndirect(ctypes.byref(_IconInfo(True, 0, 0, mask, color)))
        if not icon:
            raise ctypes.WinError(ctypes.get_last_error())
        return icon
    finally:
        gdi.DeleteObject(color)
        if mask:
            gdi.DeleteObject(mask)


class WinShell:
    """Every call that reaches the real shell. Tests replace `notify`, so they can't create a tray icon."""

    def __init__(self):
        self.atom = None
        self.instance = win32api.GetModuleHandle(None)

    def taskbar_message(self):
        return win32gui.RegisterWindowMessage("TaskbarCreated")

    def create_window(self, handlers):
        wc = win32gui.WNDCLASS()
        wc.hInstance = self.instance
        wc.lpszClassName = CLASS_NAME
        wc.lpfnWndProc = handlers
        self.atom = win32gui.RegisterClass(wc)
        return win32gui.CreateWindowEx(win32con.WS_EX_TOOLWINDOW | win32con.WS_EX_NOACTIVATE, self.atom, "", win32con.WS_POPUP,
                                       0, 0, 0, 0, 0, 0, self.instance, None)     # never shown

    def make_icon(self):
        return make_icon()

    def notify(self, op, hwnd, icon):
        data = (hwnd, UID) if op == win32gui.NIM_DELETE else (
            hwnd, UID, win32gui.NIF_MESSAGE | win32gui.NIF_ICON | win32gui.NIF_TIP, WM_TRAY, icon, TIP)
        try:
            win32gui.Shell_NotifyIcon(op, data)
            return True
        except pywintypes.error:
            return False

    def pump(self):
        win32gui.PumpMessages()

    def close(self, hwnd):
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)

    def destroy_window(self, hwnd):
        win32gui.DestroyWindow(hwnd)

    def quit_pump(self):
        win32gui.PostQuitMessage(0)

    def release(self, icon):
        if icon:
            win32gui.DestroyIcon(icon)
        if self.atom:
            win32gui.UnregisterClass(self.atom, self.instance)
            self.atom = None


class TrayIcon:
    """`on_click` runs on the tray thread, right after the user's click."""

    def __init__(self, on_click, log, shell=None, clock=time.monotonic):
        self.on_click, self.log, self.clock = on_click, log, clock
        self.shell = shell or WinShell()
        self.hwnd = self.icon = None
        self.added = False
        self.ready = threading.Event()
        self._last_click = None
        self._warned = False
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="ui-tray")
        self._thread.start()
        return self

    def stop(self, timeout=2.0):
        """Removes the icon. Safe to call if the tray never started."""
        thread, hwnd = self._thread, self.hwnd
        if thread is None:
            return
        self.ready.wait(timeout)
        hwnd = self.hwnd or hwnd
        if hwnd and thread.is_alive():
            try:
                self.shell.close(hwnd)
            except Exception as e:
                self.log(f"tray: close failed: {e!r}")
        thread.join(timeout)

    # ---- the tray thread ----
    def _run(self):
        try:
            taskbar = self.shell.taskbar_message()
            self.hwnd = self.shell.create_window({WM_TRAY: self._on_tray, taskbar: self._on_taskbar,
                                                  win32con.WM_CLOSE: self._on_close, win32con.WM_DESTROY: self._on_destroy})
            self.icon = self.shell.make_icon()
            self._show()
            self.ready.set()
            self.shell.pump()
        except Exception as e:
            self.log(f"tray: off: {e!r}")
        finally:
            self.ready.set()
            self._remove()
            try:
                self.shell.release(self.icon)
            except Exception:
                pass
            self.hwnd = self.icon = None

    def _show(self):
        """Add once. If that fails because the icon exists, update it. Never add twice."""
        self.added = self.shell.notify(win32gui.NIM_ADD, self.hwnd, self.icon) or \
            self.shell.notify(win32gui.NIM_MODIFY, self.hwnd, self.icon)
        if self.added:
            self._warned = False
        elif not self._warned:
            self._warned = True
            self.log("tray: the shell refused the icon. It will try again when the tray restarts")

    def _remove(self):
        if self.added and self.hwnd:
            self.added = False
            try:
                self.shell.notify(win32gui.NIM_DELETE, self.hwnd, self.icon)
            except Exception:
                pass

    def _on_taskbar(self, hwnd, msg, wparam, lparam):
        try:
            self._show()
        except Exception as e:
            self.log(f"tray: {e!r}")
        return 0

    def _on_tray(self, hwnd, msg, wparam, lparam):
        if (lparam & 0xFFFF) not in CLICKS:
            return 0
        now = self.clock()
        if self._last_click is not None and now - self._last_click < DEBOUNCE:
            return 0
        self._last_click = now
        try:
            self.on_click()
        except Exception as e:
            self.log(f"tray: click failed: {e!r}")
        return 0

    def _on_close(self, hwnd, msg, wparam, lparam):
        self._remove()
        self.shell.destroy_window(hwnd)
        return 0

    def _on_destroy(self, hwnd, msg, wparam, lparam):
        self.shell.quit_pump()
        return 0
