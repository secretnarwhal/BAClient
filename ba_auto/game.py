"""Window capture and input for the Blue Archive PC client.

Everything works in a fixed 1920x1080 *reference* coordinate space. viewport.py
finds the game's real picture inside the window, at any size, and converts both
ways. The window itself is never moved or resized. Only the screen is read; the
game process is never opened or touched.
"""
import ctypes
import os
import threading
import time
from ctypes import wintypes

import mss
import numpy as np
import win32gui

from . import inputguard, journal, viewport
from .runtime import Stopped, TitleScreen
from .trail import TRAIL

REF_W, REF_H = 1920, 1080
WS_EX_TRANSPARENT = 0x20

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

user32 = ctypes.windll.user32

INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
KEYEVENTF_KEYUP = 0x0002


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def _send(*inputs):
    if os.environ.get("BA_ACTOR") == "builder":
        raise RuntimeError("BA_ACTOR=builder is read-only: a builder process sends no input")
    arr, size, done = (INPUT * len(inputs))(*inputs), ctypes.sizeof(INPUT), 0
    for _ in range(3):                   # a release that's lost would leave the button down
        done += user32.SendInput(len(inputs) - done, ctypes.byref(arr, done * size), size)
        if done >= len(inputs):
            return
        time.sleep(0.01)
    raise RuntimeError(f"input failed: Windows accepted {done} of {len(inputs)} input events")


class Game:
    TITLE = "Blue Archive"

    def __init__(self, rt=None):
        from .runtime import DEFAULT
        self.rt = rt or DEFAULT
        self._local = threading.local()    # mss handles only work on the thread that made them
        self.hwnd = None
        self.last_view = None
        self.allow_title_once = False      # set only by `press ... --user-approved` and launcher.log_in

    def sleep(self, seconds):
        self.rt.sleep(seconds)

    def log(self, msg):
        self.rt.log(msg)

    # ---- window ----
    def find(self):
        hwnds = []

        def cb(h, _):
            if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h) == self.TITLE:
                hwnds.append(h)
        win32gui.EnumWindows(cb, None)
        if not hwnds:
            raise RuntimeError("Blue Archive window not found - is the game running?")
        self.hwnd = hwnds[0]
        return self.hwnd

    def client_rect(self):
        """Client area in screen pixels: (left, top, width, height)."""
        if not self.hwnd or not win32gui.IsWindow(self.hwnd):
            self.find()
        l, t, r, b = win32gui.GetClientRect(self.hwnd)
        x, y = win32gui.ClientToScreen(self.hwnd, (0, 0))
        return x, y, r - l, b - t

    def read_view(self):
        """A fresh look at where the game's picture is. None if there's no window to read."""
        try:
            if type(self).grab is Game.grab:
                raw, x, y = self._capture()
                self.last_view = viewport.detect(raw, x, y)
                return self.last_view
            frame = self.grab()
            return getattr(frame, "view_info", None) or viewport.from_rect(*self.client_rect())
        except Exception:
            return None

    def window_problem(self):
        """Why the game's picture can't be trusted right now, or None."""
        view = self.read_view()
        return None if view is None or view.confident else view.reason

    def focus(self):
        if not self.hwnd:
            self.find()
        if win32gui.GetForegroundWindow() != self.hwnd:
            try:
                win32gui.SetForegroundWindow(self.hwnd)
            except Exception:
                # Windows refuses focus changes from background processes unless
                # a key event came first; tapping Alt is the standard workaround.
                nudge = getattr(self._local, "focus_nudge", None)
                if nudge:                # under the input guard, Alt would land in the user's window
                    try:
                        nudge()
                        win32gui.SetForegroundWindow(self.hwnd)
                    except Exception as e:
                        raise inputguard.GuardRefused(f"input refused: the game couldn't take the focus ({e})") from e
                else:
                    user32.keybd_event(0x12, 0, 0, 0)
                    user32.keybd_event(0x12, 0, KEYEVENTF_KEYUP, 0)
                    win32gui.SetForegroundWindow(self.hwnd)
            time.sleep(getattr(self._local, "focus_settle", 0.15))    # shorter under the input guard

    # ---- capture ----
    def _capture(self):
        """The raw client area, plus its screen position."""
        sct = getattr(self._local, "sct", None)
        if sct is None:
            sct = self._local.sct = mss.mss()
        x, y, w, h = self.client_rect()
        shot = np.asarray(sct.grab({"left": x, "top": y, "width": w, "height": h}))[:, :, :3]
        return np.ascontiguousarray(shot), x, y

    def grab(self):
        """Current game frame as BGR in 1920x1080 reference space, at any window size."""
        self.rt.check()
        raw, x, y = self._capture()
        self.last_view = viewport.detect(raw, x, y)
        return self.last_view.compose(raw)

    # ---- input ----
    def _point_is_game(self, sx, sy):
        """True if a click at this screen point would land on the game, not a window over it."""
        hit = win32gui.WindowFromPoint((sx, sy))
        root = user32.GetAncestor(hit, 2) if hit else 0
        if root == self.hwnd:
            return True
        # Our own click-through overlay sits above the game and passes clicks on.
        if root and win32gui.GetWindowLong(root, -20) & WS_EX_TRANSPARENT:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(root, ctypes.byref(pid))
            return pid.value == os.getpid()
        return False

    def _check_point(self, view, sx, sy):
        if not view.contains_screen(sx, sy):
            raise RuntimeError(f"input refused: ({sx}, {sy}) is outside the game's picture {view.info()['view']}")
        if not self._point_is_game(sx, sy):
            raise RuntimeError(f"input refused: another window is over the game at ({sx}, {sy})")

    def _aim(self, frame, view, rx, ry):
        """Where a reference point really is. Outside 16:9 it must be matched in this frame."""
        manual = getattr(self._local, "manual", None)
        if manual is not None:           # the player's checked click: see manual.py
            from . import manual as manual_mod
            manual_mod.verify(manual[0], frame, manual[1], int(rx), int(ry))
            return view.to_screen(rx, ry), "manual"
        if view.native:
            return view.to_screen(rx, ry), None
        hit = viewport.TARGETS.confirm(frame, rx, ry)
        if hit is None:
            raise RuntimeError(f"input refused: ({int(rx)}, {int(ry)}) isn't a target matched in the current "
                               "frame, and this window shape isn't verified for fixed positions")
        name, shift = hit
        return view.to_screen(rx, ry, shift), name

    def _before_input(self, kind, **fields):
        """Every input passes here: refuse it for read-only actors, then journal it."""
        if journal.read_only():
            raise RuntimeError("BA_ACTOR=builder is read-only: the player session owns game input")
        from . import watchdog
        halt = watchdog.halted()
        if halt:
            raise RuntimeError(f"input is halted: {halt.get('reason')} (a person reviews it, "
                               "then: python -m ba_auto ctl clear-halt)")
        from .runtime import DEFAULT
        if self.rt is DEFAULT:         # a command-line process, not the client's own worker
            from . import control
            active = control.active_run()
            if active:
                raise RuntimeError(f"the client is running '{active}': one mouse at a time "
                                   "(python -m ba_auto ctl stop, or wait for it)")
        self.rt.check()
        try:
            frame = self.grab()
        except Stopped:
            raise
        except Exception as e:
            raise RuntimeError(f"input refused: can't capture the game ({e})") from e
        view = getattr(frame, "view_info", None) or viewport.from_rect(*self.client_rect())
        if not view.confident:
            raise RuntimeError(f"input refused: {view.reason}")
        blocked = watchdog.blocking_popup(frame)
        if blocked:
            watchdog.halt(blocked, input=kind)
            raise RuntimeError(f"input refused: {blocked} (the user decides; then ctl clear-halt)")
        if watchdog.unreadable_notice(frame):
            raise RuntimeError("input refused: a Notice is open and its text can't be read")
        reconnecting = getattr(self._local, "reconnecting", False)
        if watchdog.network_notice(frame):   # only its Reconnect may be pressed
            spot = watchdog.network_reconnect(frame) if kind == "click" else None
            if spot is None or abs(fields["x"] - spot[0]) > 60 or abs(fields["y"] - spot[1]) > 30:
                if reconnecting:
                    raise inputguard.NotSent("input not sent: Reconnect moved before it could be pressed")
                from . import reconnect
                reconnect.answer(self, frame)
                raise inputguard.NotSent("input not sent: the network Notice was answered with Reconnect")
        elif reconnecting:
            raise inputguard.NotSent("input not sent: the network Notice closed before Reconnect was pressed")
        if watchdog.date_notice(frame):      # the daily reset: only its own Confirm may be pressed
            spot = watchdog.date_confirm(frame) if kind == "click" else None
            if spot is None or abs(fields["x"] - spot[0]) > 60 or abs(fields["y"] - spot[1]) > 30:
                raise TitleScreen("input refused: this is the title-bound date-change notice. "
                                  "Logging in goes through `ctl run login`")
        elif watchdog.on_title(frame):
            if not self.allow_title_once:
                raise TitleScreen("input refused: this is the title screen. Logging in goes through "
                                  "`ctl run login` or `press click_to_start --user-approved`")
            self.allow_title_once = False
        aim, target = None, None
        if kind == "click":
            aim, target = self._aim(frame, view, fields["x"], fields["y"])
        elif kind == "drag" and not view.native:
            raise RuntimeError("input refused: drags use fixed positions, and this window shape isn't verified")
        self._local.view, self._local.aim = view, aim
        screen = TRAIL.name(frame)
        return journal.record(kind, frame=frame, task=self.rt.task, screen=screen, layout=view.info(),
                              target=target, screen_xy=list(aim) if aim else None, **fields)

    def _not_yet(self, event, error, late, allow_title):
        """This attempt sent nothing. The next one waits for the user and runs every check again."""
        if event:
            journal.record("not_sent", of=event, reason=str(error))
        self.allow_title_once = allow_title
        late += isinstance(error, inputguard.Late)
        if late >= inputguard.LATE_TRIES:
            raise inputguard.NotSent(str(error)) from error
        return late

    def click(self, rx, ry, hold=0.06):
        """Sends one guarded click, or raises. NotSent means nothing was sent and the screen should be read again."""
        allow_title, late = self.allow_title_once, 0
        while True:
            guard = inputguard.shield(self, "click", [(rx, ry)])     # every automated input, with no off switch
            guard.wait_turn()
            event = self._before_input("click", x=int(rx), y=int(ry))
            view, (sx, sy) = self._local.view, self._local.aim
            self.rt.last_click = ((int(rx), int(ry)), time.time(), (sx - view.client[0], sy - view.client[1]))
            try:
                with guard:
                    self.focus()
                    guard.ready(view)
                    self._check_point(view, sx, sy)      # after focus: nothing may have moved over the game
                    vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)   # virtual desktop origin
                    vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
                    ax = int((sx - vx) * 65535 / (vw - 1))
                    ay = int((sy - vy) * 65535 / (vh - 1))
                    flags = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
                    mv = INPUT(INPUT_MOUSE, _U(mi=MOUSEINPUT(ax, ay, 0, flags | MOUSEEVENTF_MOVE, 0, 0)))
                    dn = INPUT(INPUT_MOUSE, _U(mi=MOUSEINPUT(ax, ay, 0, flags | MOUSEEVENTF_LEFTDOWN, 0, 0)))
                    up = INPUT(INPUT_MOUSE, _U(mi=MOUSEINPUT(ax, ay, 0, flags | MOUSEEVENTF_LEFTUP, 0, 0)))
                    _send(mv)
                    time.sleep(0.05)
                    guard.check(hold)
                    _send(dn)
                    guard.pressed()
                    try:
                        time.sleep(hold)
                    finally:
                        _send(up)
            except inputguard.Busy as e:
                late = self._not_yet(event, e, late, allow_title)
                continue
            TRAIL.after(self, event)
            return

    def press_reconnect(self, x, y):
        """Only reconnect.py calls this. The click goes out only while that exact Notice is still open."""
        self._local.reconnecting = True
        try:
            self.click(x, y)
        finally:
            self._local.reconnecting = False

    def click_manual(self, saved, vision, rx, ry):
        """One click at a point the player read from a saved frame. Only manual.py calls this."""
        self._local.manual = (saved, vision)
        try:
            self.click(rx, ry)
        finally:
            self._local.manual = None

    def drag(self, x1, y1, x2, y2, steps=18):
        """Press at (x1, y1), move to (x2, y2), hold still, release. Used to scroll lists.
        Sends one guarded drag, or raises. NotSent means nothing was sent and the screen should be read again."""
        allow_title, late = self.allow_title_once, 0
        while True:
            guard = inputguard.shield(self, "drag", [(x1, y1), (x2, y2)])    # every automated input, with no off switch
            guard.wait_turn()
            journal_event = self._before_input("drag", x1=int(x1), y1=int(y1), x2=int(x2), y2=int(y2))
            view = self._local.view

            def event(rx, ry, flag):
                sx, sy = view.to_screen(rx, ry)
                vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
                vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
                ax, ay = int((sx - vx) * 65535 / (vw - 1)), int((sy - vy) * 65535 / (vh - 1))
                flags = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK | flag
                return INPUT(INPUT_MOUSE, _U(mi=MOUSEINPUT(ax, ay, 0, flags, 0, 0)))

            try:
                with guard:
                    self.focus()
                    guard.ready(view)
                    self._check_point(view, *view.to_screen(x1, y1))
                    self._check_point(view, *view.to_screen(x2, y2))
                    _send(event(x1, y1, MOUSEEVENTF_MOVE))
                    time.sleep(0.08)
                    guard.check(0.08 + steps * 0.025 + 0.25)
                    _send(event(x1, y1, MOUSEEVENTF_LEFTDOWN))
                    guard.pressed()
                    at = (x1, y1)      # stopped early, the button comes up here, with no jump that flings the list
                    try:
                        time.sleep(0.08)
                        for i in range(1, steps + 1):
                            guard.check((steps - i + 1) * 0.025 + 0.25)      # the rest of the drag and its hold
                            at = (x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps)
                            _send(event(*at, MOUSEEVENTF_MOVE))
                            time.sleep(0.025)
                        time.sleep(0.25)   # no fling: the list stops where the drag stopped
                    finally:
                        _send(event(*at, MOUSEEVENTF_LEFTUP))
            except inputguard.Busy as e:
                late = self._not_yet(journal_event, e, late, allow_title)
                continue
            TRAIL.after(self, journal_event)
            return

    def key(self, vk):
        """Blue Archive ignored every synthetic key when tested, so no task may
        depend on this. Use the on-screen buttons. It is kept for a future live re-test."""
        allow_title, late = self.allow_title_once, 0
        while True:
            guard = inputguard.shield(self, "key")               # every automated input, with no off switch
            guard.wait_turn()
            event = self._before_input("key", vk=int(vk))
            try:
                with guard:
                    self.focus()
                    guard.ready(self._local.view)
                    guard.check(0.05)
                    _send(INPUT(1, _U(ki=KEYBDINPUT(vk, 0, 0, 0, 0))))
                    guard.pressed()
                    try:
                        time.sleep(0.05)
                    finally:
                        _send(INPUT(1, _U(ki=KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, 0))))
            except inputguard.Busy as e:
                late = self._not_yet(event, e, late, allow_title)
                continue
            TRAIL.after(self, event)
            return
