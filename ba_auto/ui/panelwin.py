"""Find the panel's Chrome windows and keep them off the game's picture.

A title can't identify the panel: the click-through overlay is also a visible window called BAClient.
Chrome keeps one hidden message window per profile, named after the profile folder, so that gives the
browser process for exactly our profile without opening any process. Only that process's normal
top-level windows count, so menus, dropdowns, the overlay and the user's own Chrome are never touched.
"""
import ctypes
import ctypes.wintypes as wt
import time

import win32con
import win32gui
import win32process

from .. import panelguard

MESSAGE_CLASS = "Chrome_MessageWindow"
WINDOW_CLASS = "Chrome_WidgetWin_1"
HWND_MESSAGE = -3
MIN_SIZE = 120                  # Chrome keeps small helper windows too
PANEL_TITLE = "BAClient"
DWMWA_CLOAKED = 14
SHOWN, MINIMIZED, ELSEWHERE = "shown", "minimized", "elsewhere"
OPENING_SECONDS = 12.0          # Chrome can take a while to show a window, and a second launch would open a second panel


def profile_pid(profile):
    """The browser process running this Chrome profile, or None."""
    try:
        hwnd = win32gui.FindWindowEx(HWND_MESSAGE, 0, MESSAGE_CLASS, str(profile))
        return win32process.GetWindowThreadProcessId(hwnd)[1] if hwnd else None
    except Exception:
        return None


def is_panel_window(hwnd, pid):
    try:
        if win32gui.GetClassName(hwnd) != WINDOW_CLASS or not win32gui.IsWindowVisible(hwnd):
            return False
        if win32process.GetWindowThreadProcessId(hwnd)[1] != pid:
            return False
        if win32gui.GetWindow(hwnd, win32con.GW_OWNER):
            return False
        if win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOOLWINDOW:
            return False
        x1, y1, x2, y2 = win32gui.GetWindowRect(hwnd)
        return x2 - x1 >= MIN_SIZE and y2 - y1 >= MIN_SIZE
    except Exception:
        return False


def panel_windows(profile):
    """Every open panel window for this Chrome profile. Reopening the panel can leave more than one."""
    pid = profile_pid(profile)
    if pid is None:
        return []
    found = []

    def visit(hwnd, _):
        if is_panel_window(hwnd, pid):
            found.append(hwnd)
        return True

    try:
        win32gui.EnumWindows(visit, None)
    except Exception:
        return []
    return found


def cloaked(hwnd):
    """Whether the window is hidden by the shell, which is how tiling window managers hide other workspaces."""
    value = wt.DWORD(0)
    try:
        failed = ctypes.windll.dwmapi.DwmGetWindowAttribute(wt.HWND(hwnd), DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value))
    except OSError:
        return False
    return not failed and bool(value.value)


def panel_state(hwnd, pid):
    """SHOWN, MINIMIZED, ELSEWHERE (on another workspace), or None if this isn't a panel window."""
    try:
        if win32gui.GetClassName(hwnd) != WINDOW_CLASS or win32process.GetWindowThreadProcessId(hwnd)[1] != pid:
            return None
        if win32gui.GetWindow(hwnd, win32con.GW_OWNER) or win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOOLWINDOW:
            return None
        minimized = win32gui.IsIconic(hwnd)
        x1, y1, x2, y2 = win32gui.GetWindowPlacement(hwnd)[4] if minimized else win32gui.GetWindowRect(hwnd)
        if x2 - x1 < MIN_SIZE or y2 - y1 < MIN_SIZE:
            return None
        if not win32gui.IsWindowVisible(hwnd):      # Chrome's hidden helper windows have no title
            return ELSEWHERE if win32gui.GetWindowText(hwnd) == PANEL_TITLE else None
        if cloaked(hwnd):
            return ELSEWHERE
        return MINIMIZED if minimized else SHOWN
    except Exception:
        return None


def panel_states(profile):
    """[(hwnd, state)] for every panel window of this Chrome profile, wherever it is."""
    pid = profile_pid(profile)
    if pid is None:
        return []
    found = []

    def visit(hwnd, _):
        state = panel_state(hwnd, pid)
        if state:
            found.append((hwnd, state))
        return True

    try:
        win32gui.EnumWindows(visit, None)
    except Exception:
        return []
    return found


class RestoreDesktop(panelguard.Desktop):
    """The window calls for a tray click. Focus is only ever taken here, right after the user's click."""

    def normal_rect(self, hwnd):
        return tuple(win32gui.GetWindowPlacement(hwnd)[4]) if win32gui.IsIconic(hwnd) else self.rect(hwnd)

    def restore(self, hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)

    def foreground(self, hwnd):
        try:
            win32gui.SetForegroundWindow(hwnd)
            return True
        except Exception:
            return False

    def lift(self, hwnd):
        """In front of other windows without focus, if the shell didn't allow focus."""
        flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, flags)
        win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, flags)


class PanelRestorer:
    """What a tray click does. Never opens a second panel, and never touches a panel on another workspace."""

    def __init__(self, find, open_new, picture, desktop=None, clock=time.monotonic):
        self.find, self.open_new, self.picture, self.clock = find, open_new, picture, clock
        self.desktop = desktop or RestoreDesktop()
        self.opening_until = None

    def bring_back(self):
        """"raised", "opened", "opening", "elsewhere", "no_room" or "failed"."""
        windows = list(self.find())
        if windows:
            self.opening_until = None
            order = {SHOWN: 0, MINIMIZED: 1, ELSEWHERE: 2}
            hwnd, state = min(windows, key=lambda w: order[w[1]])
            return "elsewhere" if state == ELSEWHERE else self._raise(hwnd, state)
        if self.opening_until is not None and self.clock() < self.opening_until:
            return "opening"
        if not self.open_new():
            return "failed"
        self.opening_until = self.clock() + OPENING_SECONDS
        return "opened"

    def _raise(self, hwnd, state):
        d = self.desktop
        rect = d.normal_rect(hwnd)
        spot = None
        try:
            _, picture = self.picture()
        except Exception:
            picture = None
        if picture is not None and panelguard.overlaps(rect, picture):       # never bring it up over the game
            spot = panelguard.free_spot(d.monitors(), picture, rect[2] - rect[0], rect[3] - rect[1])
            if spot is None:
                return "no_room"
        if state == MINIMIZED:
            d.restore(hwnd)
        if spot:
            d.move(hwnd, *spot)
        if not d.foreground(hwnd):
            d.lift(hwnd)
        return "raised"


class ChromeGuards:
    """One of Core's PanelGuards per open panel window, behind the same check() call."""

    def __init__(self, find, log, desktop=None, is_cloaked=cloaked):
        self.find, self.log = find, log
        self.desktop = desktop or panelguard.Desktop()
        self.is_cloaked = is_cloaked
        self.guards = {}

    def check(self, picture, game_hwnd):
        open_now = list(self.find())
        for hwnd in [h for h in self.guards if h not in open_now]:
            del self.guards[hwnd]
        notes = []
        for hwnd in open_now:
            guard = self.guards.get(hwnd)
            if guard is None:
                guard = self.guards[hwnd] = panelguard.PanelGuard(lambda h=hwnd: h, self.log, desktop=self.desktop,
                                                                  keep_on_top=False)
            if self.is_cloaked(hwnd):   # on another workspace it covers nothing, and the window manager owns its position
                continue
            notes.append(guard.check(picture, game_hwnd))
        return next((n for n in notes if n), None)
