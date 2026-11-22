"""Keep BAClient's own panel off the game's picture.

While the panel overlaps the picture it is dropped behind the game, which moves nothing. If another
monitor is free of the game, the panel then tries to move there, a limited number of times, because a
window manager may put it back. If that can't be confirmed it stays behind the game and says so.

This only ever changes the panel window. It never moves, resizes, focuses or reorders the game, never
activates a window, never moves the cursor and never talks to the window manager.
"""
import time

import win32api
import win32con
import win32gui

GAME_TITLE = "Blue Archive"
MAX_MOVES = 2                    # a window manager that keeps putting it back wins
MOVE_GAP = 30.0                  # seconds between tries
EDGE = 24                        # gap from the target monitor's edge
TOLERANCE = 8                    # window frames include an invisible border
QUIET = win32con.SWP_NOACTIVATE | win32con.SWP_NOOWNERZORDER


class Desktop:
    """The real windows. Every call that changes something acts on the panel's handle only."""

    def game(self):
        hwnd = win32gui.FindWindow(None, GAME_TITLE)
        return hwnd if hwnd and win32gui.IsWindowVisible(hwnd) and not win32gui.IsIconic(hwnd) else None

    def shown(self, hwnd):
        return bool(win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd) and not win32gui.IsIconic(hwnd))

    def rect(self, hwnd):
        return tuple(win32gui.GetWindowRect(hwnd))

    def topmost(self, hwnd):
        return bool(win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOPMOST)

    def above(self, hwnd, other):
        """Whether hwnd is higher in the window order than `other`."""
        current = other
        for _ in range(4000):
            current = win32gui.GetWindow(current, win32con.GW_HWNDPREV)
            if not current:
                return False
            if current == hwnd:
                return True
        return False

    def monitors(self):
        return [tuple(win32api.GetMonitorInfo(m[0])["Work"]) for m in win32api.EnumDisplayMonitors()]

    def button_down(self):
        """The user may be dragging the panel: leave it alone until they let go."""
        return bool(win32api.GetAsyncKeyState(win32con.VK_LBUTTON) & 0x8000)

    def behind(self, hwnd, other):
        win32gui.SetWindowPos(hwnd, other, 0, 0, 0, 0, win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | QUIET)

    def set_topmost(self, hwnd):
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | QUIET)

    def move(self, hwnd, x, y, w, h):
        win32gui.SetWindowPos(hwnd, 0, x, y, w, h, win32con.SWP_NOZORDER | QUIET)


def overlaps(a, b, tolerance=TOLERANCE):
    """Whether two (x1, y1, x2, y2) rects share more than the invisible border."""
    return min(a[2], b[2]) - max(a[0], b[0]) > tolerance and min(a[3], b[3]) - max(a[1], b[1]) > tolerance


def free_spot(monitors, picture, width, height):
    """(x, y, w, h) on the largest monitor the picture isn't on, or None."""
    free = [m for m in monitors if not overlaps(m, picture, 0)]
    if not free:
        return None
    x1, y1, x2, y2 = max(free, key=lambda m: (m[2] - m[0]) * (m[3] - m[1]))
    w, h = min(width, x2 - x1 - 2 * EDGE), min(height, y2 - y1 - 2 * EDGE)
    return (x1 + EDGE, y1 + EDGE, w, h) if w > 0 and h > 0 else None


class PanelGuard:
    """Call check() about twice a second. keep_on_top is for a panel that is normally always on top."""

    def __init__(self, get_hwnd, log, desktop=None, keep_on_top=True, clock=time.monotonic):
        self.get_hwnd, self.log, self.desktop = get_hwnd, log, desktop or Desktop()
        self.keep_on_top, self.clock = keep_on_top, clock
        self.moves = 0
        self.last_move = None
        self.note = None

    def _say(self, note):
        if note != self.note:
            self.note = note
            if note:
                self.log(f"panel: {note}")
        return note

    def check(self, picture, game_hwnd):
        """The panel's status, or None when it is simply clear of the game."""
        d = self.desktop
        hwnd = self.get_hwnd()
        if not hwnd or not d.shown(hwnd) or d.button_down():
            return self.note
        rect = d.rect(hwnd)
        if picture is None or game_hwnd is None or not overlaps(rect, picture):
            if self.keep_on_top and not d.topmost(hwnd):
                d.set_topmost(hwnd)
            return self._say(None)
        if d.topmost(hwnd) or d.above(hwnd, game_hwnd):      # first, stop covering the game
            d.behind(hwnd, game_hwnd)
        spot = free_spot(d.monitors(), picture, rect[2] - rect[0], rect[3] - rect[1])
        if spot is None:
            return self._say("behind the game: there's no other monitor to move to")
        now = self.clock()
        if self.moves < MAX_MOVES and (self.last_move is None or now - self.last_move >= MOVE_GAP):
            self.moves += 1
            self.last_move = now
            d.move(hwnd, *spot)
            return self._say("behind the game, moving to the other monitor")
        if self.moves >= MAX_MOVES:
            return self._say("behind the game: it couldn't be kept on the other monitor")
        return self.note
