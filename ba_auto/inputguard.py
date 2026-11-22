"""Protect every automated input from the user's own input, then hand everything back.

It runs for every automated input, with no off switch. If it can't run,
that input isn't sent. The user's own play is never touched: with no automated action in progress there is
no hook, and nothing of theirs is delayed or held.

For one automated action:
  1. Wait for a free moment: no physical mouse button, modifier or game-shortcut key held, and no other game
     open. Using the PC never fails a run. Game runs all its checks after that wait. A target that changed
     raises NotSent, so no task can mistake it for a click that landed.
  2. Install low-level keyboard and mouse hooks on their own thread. They are never left in place: the
     thread removes them by itself at the time cap, even if the rest of the client is stuck.
  3. Hold the user's keys and mouse buttons in one ordered buffer, in memory only, and add up their pointer
     motion.
  4. Afterwards the cursor goes to where the user was aiming, then the focus returns, then the buffer is
     replayed in order. A key or button still held is pressed and its real release passes through.
     Nothing is ever replayed into Blue Archive, which has physical shortcuts that spend (F, Space).

F8 always passes through and stops the action. The user's input is never logged or journaled.

Why the cursor goes first: a desktop helper's hover focus is off only while a game is in front. Moving the cursor
after the focus has left the game could hand the focus to whatever is under the pointer.
"""
import ctypes
import os
import threading
import time
from ctypes import wintypes

from .runtime import Stopped

# 80 ms and 50 ms lost about a third of clicks in live testing, against about 1% without the guard.
CLICK_CAP = 0.70                # the longest input is held for one click, cleanup included
DRAG_CAP = 1.60                 # a drag keeps its live-proven timing
FOCUS_SETTLE = 0.20             # after the game takes focus, before anything is pressed
RELEASE_WAIT = 0.12             # lets the game finish the click before the focus leaves it
HELD_POLL = 0.02
LATE_TRIES = 3                  # attempts when timing or the window, not the user, stopped an input
LOCK_POLL_MS = 50
FOCUS_TRIES = 3                 # Windows can refuse a focus change once, with the user's window still open
FOCUS_GAP = 0.03
WAIT_NOTE = 0.6                 # a wait longer than this shows in the status and the log
HOOK_START = 0.2
TARGET_BOX = (120, 70)          # half-size of the area around a target that must still look the same
TARGET_CHANGED = 12             # mean change there that means it's no longer the same target
MAX_EVENTS = 256
OTHER_GAME_POLL = 2.0
# Games that must never share the PC with a hook. Background anti-cheat services alone don't count.
KNOWN_GAMES = {"valorant.exe", "valorant-win64-shipping.exe", "fortniteclient-win64-shipping.exe"}
STEAM_GAMES, THIS_GAME = "\\steamapps\\common\\", "\\steamapps\\common\\bluearchive\\"
# Full-monitor windows that aren't games.
NOT_GAMES = {"bluearchive.exe", "explorer.exe", "shellexperiencehost.exe", "startmenuexperiencehost.exe", "searchhost.exe", "lockapp.exe",
             "textinputhost.exe", "screenclippinghost.exe", "snippingtool.exe", "applicationframehost.exe",
             "chrome.exe", "msedge.exe", "brave.exe", "firefox.exe", "opera.exe", "vivaldi.exe", "vlc.exe", "mpv.exe",
             "mpc-hc64.exe", "potplayermini64.exe", "python.exe", "pythonw.exe", "claude.exe", "code.exe",
             "discord.exe", "obs64.exe"}

VK_F8, VK_DELETE, VK_MASK = 0x77, 0x2E, 0xE8    # VK_MASK is unassigned: it stops a lone Alt opening a menu
CTRL, ALT = {0x11, 0xA2, 0xA3}, {0x12, 0xA4, 0xA5}
BUTTON_KEYS = {0x01, 0x02, 0x04, 0x05, 0x06}
MODIFIER_KEYS = CTRL | ALT | {0x10, 0xA0, 0xA1, 0x5B, 0x5C}
GAME_KEYS = {0x0D, 0x1B, 0x20, 0x46}            # Enter, Esc, Space, F: held as the game takes focus, it could act on them
WAIT_FOR = BUTTON_KEYS | MODIFIER_KEYS | GAME_KEYS
KEY_INJECTED, MOUSE_INJECTED = 0x10 | 0x02, 0x01 | 0x02    # includes "injected from lower integrity"
KEY_EXTENDED = 0x01
WM_KEYDOWN, WM_SYSKEYDOWN, WM_MOUSEMOVE = 0x0100, 0x0104, 0x0200
BUTTON_DOWN = {0x0201: "left", 0x0204: "right", 0x0207: "middle", 0x020B: "x"}
BUTTON_UP = {0x0202: "left", 0x0205: "right", 0x0208: "middle", 0x020C: "x"}
# Everything GetAsyncKeyState can report as physically held, minus VK_PACKET and the reserved code.
HELD_KEYS = tuple(vk for vk in range(1, 255) if vk not in (0x07, 0xE7, VK_MASK))

LAYER = None                    # tests put a fake here, so a test can never install a real hook
_now = time.monotonic
_real = None


class GuardRefused(RuntimeError):
    """The action was not sent, or the user's focus couldn't be given back. The message says which."""


class NotSent(GuardRefused):
    """Nothing was sent. A task that doesn't catch this stops, so it can't carry on as if the input landed."""


class Busy(GuardRefused):
    """Nothing was sent: the user's input got in first. Game starts the action over, with fresh checks."""


class Late(Busy):
    """Nothing was sent, because of timing or the window rather than the user. Game retries this a few times."""




class _Same:
    """Compares equal to any window."""

    def __eq__(self, other):
        return True

    def __ne__(self, other):
        return False

    __hash__ = None


class _BuilderLayer:
    """A builder process can't send input: Game and _send both refuse. This touches nothing on the PC."""
    inert = True

    def held(self):
        return []

    def cursor(self):
        return 0, 0

    def set_cursor(self, x, y):
        return True

    def clamp(self, x, y):
        return x, y

    def foreground(self):
        return _Same()

    def set_foreground(self, hwnd):
        return True

    def same_fullscreen_monitor(self, front, game_hwnd):
        return False

    def other_game(self, game_hwnd=None):
        return None

    def is_game_at(self, x, y, game_hwnd):
        return True

    def send_keys(self, events):
        pass

    def send_button(self, button, up):
        pass

    def start_hooks(self, shield_):
        return "no hook"

    def stop_hooks(self, hooks):
        pass


_BUILDER = _BuilderLayer()


def shield(game, kind, points=()):
    """`guard = shield(game, "click", [(x, y)])`, then `guard.wait_turn()`, then `with guard:` around the action."""
    try:
        layer = _layer()
    except Exception as e:
        raise GuardRefused(f"input refused: the input guard couldn't start ({e})") from e
    return Shield(game, kind, layer, points)


def _layer():
    global _real
    if LAYER is not None:
        return LAYER
    if os.environ.get("BA_ACTOR") == "builder":
        return _BUILDER
    if _real is None:
        _real = Win32Layer()
    return _real


def protecting():
    """False only in a builder process, which can't send input."""
    return not getattr(_layer(), "inert", False)


def other_game(game=None):
    """The name of another game that client input must wait for, or None."""
    try:
        return _layer().other_game(getattr(game, "hwnd", None))
    except Exception:
        return "a program the guard couldn't check"


def give_focus(hwnd):
    """Hand the focus to the user's window. True if it arrived."""
    return bool(_layer().set_foreground(hwnd))


def try_click(game, x, y):
    """For a loop that reads the screen again after every click. False if the click wasn't sent."""
    try:
        game.click(x, y)
    except NotSent as e:
        game.log(f"input guard: {e}; looking at the screen again")
        return False
    return True


def is_fullscreen_game(name, rect, monitor, has_caption, zoomed):
    """A borderless or exclusive window covering a whole monitor, from a program that isn't known not to be a game."""
    if not name or name in NOT_GAMES or has_caption or zoomed:
        return False
    return rect[0] <= monitor[0] and rect[1] <= monitor[1] and rect[2] >= monitor[2] and rect[3] >= monitor[3]


def button_flags(button, up, swapped):
    """SendInput's flags name the physical button, and the hook reports the logical one."""
    if swapped and button in ("left", "right"):
        button = "right" if button == "left" else "left"
    down_flag, up_flag = {"left": (0x02, 0x04), "right": (0x08, 0x10), "middle": (0x20, 0x40),
                          "x1": (0x80, 0x100), "x2": (0x80, 0x100)}[button]
    return (up_flag if up else down_flag), {"x1": 1, "x2": 2}.get(button, 0)


def _button(msg, data):
    name = BUTTON_DOWN.get(msg) or BUTTON_UP.get(msg)
    return f"x{data or 1}" if name == "x" else name


class Shield:
    def __init__(self, game, kind, layer, points=()):
        self.game, self.kind, self.layer, self.points = game, kind, layer, list(points)
        self.inert = getattr(layer, "inert", False)
        self.cap = float("inf") if self.inert else DRAG_CAP if kind == "drag" else CLICK_CAP
        self.deadline = 0.0             # after this the hooks pass everything
        self.aborted = False
        self.cursor = self.front = self.hooks = self._token = None
        self._lock = threading.Lock()
        self._other, self._next_game_check = None, 0.0
        self._pressed = False
        self._dropped = 0
        self._reset()

    def _reset(self):
        self.closing = self.discard = False
        self._queue = []                # keys as (vk, scan, extended, up), buttons as ("mouse", name, up, dx, dy)
        self._down = {}                 # keys pressed during this action
        self._buttons = set()           # buttons pressed during this action
        self._pre = set()               # ordinary keys already held when the hooks went in
        self._owed = set()              # of those, the ones whose release is in the buffer
        self._dx = self._dy = 0
        self._aimed = (0, 0)

    # ---- called from the hook thread: must stay fast, and never raise
    def on_key(self, vk, scan, flags, down):
        """True to swallow this event."""
        if flags & KEY_INJECTED:
            return False
        if vk == VK_F8:
            self.aborted = True
            return False
        with self._lock:
            if self.closing or _now() >= self.deadline:
                return False
            if vk == VK_DELETE and down and self._down.keys() & CTRL and self._down.keys() & ALT:
                self.aborted = self.discard = True      # Ctrl+Alt+Del belongs to Windows
                return False
            ext = bool(flags & KEY_EXTENDED)
            if vk in self._pre:
                if not down:            # the window that saw it pressed should see it released
                    self._pre.discard(vk)
                    self._owed.add(vk)
                    self._queue.append((vk, scan, ext, True))
                return True
            if down:
                if vk not in self._down and len(self._queue) < MAX_EVENTS:   # a held key's repeats add nothing
                    self._down[vk] = (scan, ext)
                    self._queue.append((vk, scan, ext, False))
                return True
            if vk in self._down:
                scan_, ext = self._down.pop(vk)
                self._queue.append((vk, scan_, ext, True))
                return True
            return False                # pressed before the guard: its release must get through

    def on_mouse(self, msg, flags, delta=(0, 0), data=0):
        """True to swallow this event. `delta` is how far a physical move would have moved the cursor."""
        if flags & MOUSE_INJECTED:
            return False
        with self._lock:
            if self.closing or _now() >= self.deadline:
                return False
            if msg in BUTTON_DOWN:
                button = _button(msg, data)
                if len(self._queue) < MAX_EVENTS:
                    self._buttons.add(button)
                    self._queue.append(("mouse", button, False, self._dx, self._dy))
                return True
            if msg in BUTTON_UP:
                button = _button(msg, data)
                if button not in self._buttons:
                    return False        # pressed before the guard: its release must get through
                self._buttons.discard(button)
                self._queue.append(("mouse", button, True, self._dx, self._dy))
                return True
            if msg == WM_MOUSEMOVE:
                self._dx += delta[0]
                self._dy += delta[1]
            return True

    # ---- called from the action
    def check(self, need=0.0):
        """Call before anything is pressed, with how long the press will take. Stops on F8 and Stop, and when
        the press and its release wouldn't finish while the hooks still hold the user's input."""
        if self.aborted:
            raise Stopped()
        self.game.rt.check()
        if _now() + need + RELEASE_WAIT >= self.deadline:
            error = GuardRefused if self._pressed else Late     # after a press, starting over would repeat it
            raise error(f"input refused: the {self.kind} ran past the input guard's time limit")

    def pressed(self):
        self._pressed = True

    def ready(self, view=None):
        """After focus: the game must really be in front, or a press would only activate it, and its
        window must still be where the checks measured it."""
        hwnd = self.game.hwnd
        if self.layer.foreground() != hwnd:
            self.game.focus()
        if self.layer.foreground() != hwnd:
            raise Late("input not sent: the game didn't take the focus, so a press would only activate it")
        if view is not None and not self.inert and tuple(self.game.client_rect()) != tuple(view.client):
            raise Late("input not sent: the game's window moved")

    # ---- waiting for a free moment
    def _busy(self, hwnd):
        """What the client has to wait for right now, or None."""
        now = _now()
        if now >= self._next_game_check:
            self._other = self.layer.other_game(hwnd)
            self._next_game_check = now + OTHER_GAME_POLL
        if self._other:
            return f"waiting for {self._other} to close"
        held = set(self.layer.held()) & WAIT_FOR
        if held & BUTTON_KEYS:
            return "waiting for your mouse button"
        if held & MODIFIER_KEYS:
            return "waiting for your modifier key"
        return "waiting for a held key the game reacts to" if held else None

    def _same_target(self, before):
        import cv2
        import numpy as np
        then, now = np.asarray(before), np.asarray(self.game.grab())
        if then.shape != now.shape:
            return False
        h, w = then.shape[:2]
        for x, y in self.points:
            x1, x2 = max(0, int(x) - TARGET_BOX[0]), min(w, int(x) + TARGET_BOX[0])
            y1, y2 = max(0, int(y) - TARGET_BOX[1]), min(h, int(y) + TARGET_BOX[1])
            if x2 > x1 and y2 > y1 and cv2.absdiff(then[y1:y2, x1:x2], now[y1:y2, x1:x2]).mean() > TARGET_CHANGED:
                return False
        return True

    def wait_turn(self):
        """Wait, however long, for a free moment. Raises NotSent if the target changed meanwhile."""
        hwnd = self.game.hwnd
        why = self._busy(hwnd)
        if not why:
            return True
        before = self.game.grab()
        started, task, shown = _now(), self.game.rt.task, None
        try:
            while why:
                self.game.rt.check()    # Stop and F8 always work here
                if why != shown and _now() - started >= WAIT_NOTE:
                    if shown is None:
                        self.game.log(f"input guard: {why}")
                    shown = why
                    self.game.rt.task = f"{task} ({why})"
                time.sleep(HELD_POLL)
                why = self._busy(hwnd)
        finally:
            if shown:
                self.game.rt.task = task
        if not self._same_target(before):
            raise NotSent(f"input not sent: the {self.kind} target changed while waiting for the user")
        return True

    # ---- taking over
    def _take(self):
        hwnd = self.game.hwnd if self.inert else self.game.hwnd or self.game.find()
        why = self._busy(hwnd)          # Game's checks have just run, so a wait here would make them stale
        if why:
            raise Busy(f"input not sent: {why}")
        self._lock_input()
        self._reset()
        self.deadline = _now() + self.cap
        self.hooks = self.layer.start_hooks(self)
        held = set(self.layer.held())
        if held & WAIT_FOR:
            raise Busy("input not sent: the user's input arrived as the guard started")
        with self._lock:
            self._pre = held
        self.cursor = self.layer.cursor()
        self.front = self.layer.foreground()
        if not self.front:
            raise GuardRefused("input refused: no active window to give the focus back to")
        if self.front != hwnd and self.layer.same_fullscreen_monitor(self.front, hwnd):
            raise GuardRefused("input refused: the active window is on the game's fullscreen monitor, "
                               "and bringing the game forward would make a desktop helper minimize it")
        self.game._local.focus_settle = FOCUS_SETTLE
        self.game._local.focus_nudge = self._nudge

    def _lock_input(self):
        """One automated input at a time, across every process, so two can't save each other's cursor."""
        lock = getattr(self.layer, "lock_input", None)
        if lock is None:
            return
        waited = False
        while self._token is None:
            self.game.rt.check()
            self._token = lock(LOCK_POLL_MS)
            waited = waited or self._token is None
        if waited:
            raise Busy("input not sent: another automated input went first")

    def _step_back(self):
        """Nothing has moved yet: give back anything that arrived and take the hooks out."""
        try:
            if self.hooks is not None:
                self.cursor = self.layer.cursor()
                self._hand_back(True)
        finally:
            self._remove_hooks()
            self.cursor = None

    def __enter__(self):
        try:
            self._take()
        except BaseException as e:
            try:
                self._step_back()
            except Exception:
                pass
            if isinstance(e, (GuardRefused, Stopped)) or not isinstance(e, Exception):
                raise
            raise GuardRefused(f"input refused: the input guard failed ({e})") from e
        return self

    def _nudge(self):
        """The input event Windows wants before a focus change. A real Alt would land in the user's window."""
        self.layer.send_keys([(VK_MASK, 0, False, False), (VK_MASK, 0, False, True)])

    # ---- giving it back
    def _point(self, moved):
        if moved == (0, 0):
            return self.cursor
        return self.layer.clamp(self.cursor[0] + moved[0], self.cursor[1] + moved[1])

    def _put_cursor(self, moved):
        self._aimed = moved
        return self.layer.set_cursor(*self._point(moved))

    def _replay(self, event, deliver, live, hwnd):
        """Send one held event. Releases of anything pressed always go, so nothing can stick."""
        if deliver and self.layer.foreground() == hwnd:
            deliver = False             # a replayed shortcut or click may move the focus, but never to the game
        if event[0] == "mouse":
            _, button, up, dx, dy = event
            if up:
                if live.pop(button, None):
                    self.layer.set_cursor(*self._point((dx, dy)))
                    self._aimed = None
                    self.layer.send_button(button, True)
            elif not deliver or self.layer.is_game_at(*self._point((dx, dy)), hwnd):
                self._dropped += 1      # its release isn't live, so it's dropped too
            else:
                self.layer.set_cursor(*self._point((dx, dy)))
                self._aimed = None
                self.layer.send_button(button, False)
                live[button] = True
            return deliver
        vk, _, _, up = event
        if up:
            if live.pop(vk, None) or vk in self._owed:
                self._owed.discard(vk)
                self.layer.send_keys([event])
        elif deliver:
            self.layer.send_keys([event])
            live[vk] = event
        return deliver

    def _release(self, live):
        """Only when the hooks are gone, so a real release may already have been missed."""
        for key, event in reversed(list(live.items())):
            if isinstance(key, str):
                self.layer.send_button(key, True)
                continue
            if key in ALT:
                self.layer.send_keys([(VK_MASK, 0, False, False), (VK_MASK, 0, False, True)])
            self.layer.send_keys([(event[0], event[1], event[2], True)])

    def _hand_back(self, deliver):
        """Replay the buffer in order, including anything that arrives meanwhile. A key or button still
        physically held stays down, and its real release passes through once this finishes."""
        hwnd = self.game.hwnd
        deliver = deliver and not self.discard
        live, lost_hooks = {}, False
        try:
            while True:
                with self._lock:
                    batch, self._queue = self._queue, []
                    moved = (self._dx, self._dy)
                    if not batch and moved == self._aimed:
                        self.closing = True
                        lost_hooks = _now() >= self.deadline
                        break
                for event in batch:
                    deliver = self._replay(event, deliver, live, hwnd)
                batch.clear()
                with self._lock:
                    moved = (self._dx, self._dy)
                if moved != self._aimed:
                    self._put_cursor(moved)
        except Exception:
            lost_hooks = True
            raise
        finally:
            with self._lock:
                self.closing = True
                self._queue.clear()
                self._down.clear()
                self._buttons.clear()
                self._pre.clear()
                self._owed.clear()
            try:
                if lost_hooks:
                    self._release(live)
            finally:
                live.clear()

    def _remove_hooks(self):
        hooks, self.hooks = self.hooks, None
        try:
            if hooks is not None:
                self.layer.stop_hooks(hooks)
        finally:
            token, self._token = self._token, None
            if token is not None:
                self.layer.unlock_input(token)

    def _return_focus(self):
        """Give the focus back to the user's own window, for as long as that window exists."""
        exists = getattr(self.layer, "window_exists", lambda hwnd: True)
        for attempt in range(FOCUS_TRIES):
            if attempt:
                if not exists(self.front):
                    return False
                time.sleep(FOCUS_GAP)
            if self.layer.set_foreground(self.front):
                return True
        return False

    def _off_the_game(self, hwnd):
        """The user's window can't take the focus (a closed Start menu, say). Their next key must not land in the game."""
        spare = getattr(self.layer, "spare_windows", None)
        if spare is None:
            return False
        for window in spare(hwnd, *self.layer.cursor()):
            if self.layer.set_foreground(window) and self.layer.foreground() == window:
                return True
        return False

    def __exit__(self, etype, exc, tb):
        problems = []
        hwnd = self.game.hwnd
        deliver = False
        try:
            if self._pressed and not self.inert:
                time.sleep(RELEASE_WAIT)
            try:
                with self._lock:
                    moved = (self._dx, self._dy)
                if not self._put_cursor(moved):
                    problems.append("the cursor couldn't be put back")
            except Exception as e:
                problems.append(f"the cursor couldn't be put back ({e})")
            try:
                if self.front == hwnd:
                    deliver = False     # the user was in the game: never replay into it
                elif self.layer.foreground() == self.front or self._return_focus():
                    deliver = self.layer.foreground() == self.front
                if self.front != hwnd and not deliver:
                    if self._off_the_game(hwnd):
                        self.game.log("input guard: your window couldn't take the focus, so it moved off the game. "
                                      "Input made during the click was discarded")
                    else:
                        problems.append("the focus couldn't be given back, so the game still has it. "
                                        "Input made during the click was discarded")
            except Exception as e:
                deliver = False
                problems.append(f"the focus couldn't be given back ({e})")
            try:
                self._hand_back(deliver)
            except Exception as e:
                problems.append(f"held input couldn't be delivered ({e})")
        finally:
            self._remove_hooks()
            self.game._local.__dict__.pop("focus_settle", None)
            self.game._local.__dict__.pop("focus_nudge", None)
        if self._dropped:
            try:
                self.game.log(f"input guard: discarded {self._dropped} click(s) that would have reached the game")
            except Exception:
                pass
            self._dropped = 0
        if problems:
            message = "input guard: " + "; ".join(problems)
            try:
                self.game.log(message)
            except Exception:
                pass
            if etype is None:
                raise GuardRefused(message)
        return False


class _KBD(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _MOUSE(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _Unicode(ctypes.Structure):
    _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", wintypes.LPWSTR)]


class _ProcessImage(ctypes.Structure):
    _fields_ = [("ProcessId", ctypes.c_void_p), ("ImageName", _Unicode)]


class Win32Layer:
    """The real Windows calls. A builder process refuses to install a hook, so a test or probe can't."""
    WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
    QS_ALLINPUT, PM_REMOVE, WM_NULL = 0x04FF, 0x0001, 0x0000
    MARK = 0x42414755               # marks input this guard replays
    SM_SWAPBUTTON = 23
    GA_ROOT, GW_HWNDNEXT, WS_EX_TRANSPARENT = 2, 2, 0x20

    def __init__(self):
        u = self.u = ctypes.WinDLL("user32", use_last_error=True)   # its own copy: argtypes stay private
        k = self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.LRESULT = ctypes.c_ssize_t
        self.HOOKPROC = ctypes.WINFUNCTYPE(self.LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        u.SetWindowsHookExW.argtypes = (ctypes.c_int, self.HOOKPROC, wintypes.HMODULE, wintypes.DWORD)
        u.SetWindowsHookExW.restype = wintypes.HHOOK
        u.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
        u.CallNextHookEx.argtypes = (wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        u.CallNextHookEx.restype = self.LRESULT
        u.GetAsyncKeyState.argtypes = (ctypes.c_int,)
        u.GetAsyncKeyState.restype = ctypes.c_short
        u.PeekMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT)
        u.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        u.MsgWaitForMultipleObjects.argtypes = (wintypes.DWORD, ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, wintypes.DWORD)
        u.MsgWaitForMultipleObjects.restype = wintypes.DWORD
        u.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
        u.IsZoomed.argtypes = (wintypes.HWND,)
        u.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
        u.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
        u.GetAncestor.restype = wintypes.HWND
        u.GetShellWindow.restype = wintypes.HWND
        k.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        k.CreateMutexW.restype = wintypes.HANDLE
        k.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        k.CloseHandle.argtypes = (wintypes.HANDLE,)
        k.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
        k.GetModuleHandleW.restype = wintypes.HMODULE
        k.GetCurrentThreadId.restype = wintypes.DWORD
        self.nt = ctypes.WinDLL("ntdll")
        self.nt.NtQuerySystemInformation.argtypes = (ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                                                     ctypes.POINTER(ctypes.c_ulong))
        self.nt.NtQuerySystemInformation.restype = ctypes.c_long
        self._paths = {}

    def held(self):
        return [vk for vk in HELD_KEYS if self.u.GetAsyncKeyState(vk) & 0x8000]

    def cursor(self):
        p = wintypes.POINT()
        if not self.u.GetCursorPos(ctypes.byref(p)):
            raise OSError("GetCursorPos failed")
        return p.x, p.y

    def set_cursor(self, x, y):
        for _ in range(2):
            self.u.SetCursorPos(int(x), int(y))
            if self.cursor() == (x, y):
                return True
        return False

    def clamp(self, x, y):
        """The nearest point that's on a monitor."""
        import win32api
        left, top, right, bottom = win32api.GetMonitorInfo(win32api.MonitorFromPoint((int(x), int(y)), 2))["Monitor"]
        return max(left, min(right - 1, int(x))), max(top, min(bottom - 1, int(y)))

    def foreground(self):
        import win32gui
        return win32gui.GetForegroundWindow()

    def set_foreground(self, hwnd):
        import win32gui
        if not win32gui.IsWindow(hwnd) or win32gui.IsIconic(hwnd):
            return False
        for attempt in range(3):
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                if attempt == 0:        # Windows wants an input event first. An unassigned key does nothing else.
                    self.send_keys([(VK_MASK, 0, False, False), (VK_MASK, 0, False, True)])
            time.sleep(0.01)
            if win32gui.GetForegroundWindow() == hwnd:
                return True
        return False

    def same_fullscreen_monitor(self, front, game_hwnd):
        """True if `front` is on the monitor the game completely covers."""
        import win32api
        import win32gui
        here, there = win32api.MonitorFromWindow(front, 2), win32api.MonitorFromWindow(game_hwnd, 2)
        if int(here) != int(there):
            return False
        ml, mt, mr, mb = win32api.GetMonitorInfo(there)["Monitor"]
        l, t, r, b = win32gui.GetWindowRect(game_hwnd)
        return l <= ml and t <= mt and r >= mr and b >= mb

    def is_game_at(self, x, y, game_hwnd):
        """True if a click at this point would reach the game. Click-through overlays are looked past,
        and a point that can't be read counts as the game."""
        import win32con
        import win32gui
        try:
            root = self.u.GetAncestor(win32gui.WindowFromPoint((int(x), int(y))), self.GA_ROOT)
            for _ in range(256):
                if not root or root == game_hwnd:
                    return True
                if not win32gui.GetWindowLong(root, win32con.GWL_EXSTYLE) & self.WS_EX_TRANSPARENT:
                    return False
                while True:             # the next real window below it at this point
                    root = win32gui.GetWindow(root, self.GW_HWNDNEXT)
                    if not root:
                        return True
                    left, top, right, bottom = win32gui.GetWindowRect(root)
                    if win32gui.IsWindowVisible(root) and left <= x < right and top <= y < bottom:
                        break
        except Exception:
            return True
        return True

    def window_exists(self, hwnd):
        import win32gui
        return bool(win32gui.IsWindow(hwnd))

    def spare_windows(self, game_hwnd, x, y):
        """Only the desktop. The window under the pointer could be a terminal, a prompt or the client's own
        panel, where the user's next Enter or Space would do something."""
        shell = self.u.GetShellWindow()
        return [shell] if shell and shell != game_hwnd else []

    def lock_input(self, wait_ms):
        """The lock on sending automated input, or None if it's still taken. Windows frees it if a process dies."""
        handle = self.k.CreateMutexW(None, False, "Local\\BAClient.input")
        if not handle:
            raise OSError("the input lock couldn't be created")
        if self.k.WaitForSingleObject(handle, wait_ms) in (0, 0x80):    # free, or its owner died
            return handle
        self.k.CloseHandle(handle)
        return None

    def unlock_input(self, handle):
        self.k.ReleaseMutex(handle)
        self.k.CloseHandle(handle)

    def _image_path(self, pid, name):
        """A process's exe path, lower-case, from a system query. No process is ever opened."""
        key = (pid, name)
        if key not in self._paths:
            if len(self._paths) > 4000:
                self._paths.clear()
            buffer = ctypes.create_unicode_buffer(1024)
            info = _ProcessImage(pid, _Unicode(0, ctypes.sizeof(buffer), ctypes.cast(buffer, wintypes.LPWSTR)))
            ok = self.nt.NtQuerySystemInformation(88, ctypes.byref(info), ctypes.sizeof(info), None) == 0
            self._paths[key] = ctypes.wstring_at(info.ImageName.Buffer, info.ImageName.Length // 2).lower() if ok else ""
        return self._paths[key]

    def _fullscreen_game(self, game_hwnd, procs):
        import win32api
        import win32con
        import win32gui
        import win32process
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd or hwnd == game_hwnd or win32gui.IsIconic(hwnd):
            return None
        pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        if pid == os.getpid():
            return None
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
        monitor = win32api.GetMonitorInfo(win32api.MonitorFromWindow(hwnd, 2))["Monitor"]
        name = procs.get(pid, "")
        full = is_fullscreen_game(name, win32gui.GetWindowRect(hwnd), monitor,
                                  (style & win32con.WS_CAPTION) == win32con.WS_CAPTION, bool(self.u.IsZoomed(hwnd)))
        return f"{name} (fullscreen)" if full else None

    def other_game(self, game_hwnd=None):
        """Another game that's open, by process name, install folder, or a fullscreen window in front."""
        from . import launcher
        procs = launcher.processes()
        for pid, name in procs.items():
            if name in KNOWN_GAMES:
                return name
            path = self._image_path(pid, name)
            if STEAM_GAMES in path and THIS_GAME not in path:
                return name
        return self._fullscreen_game(game_hwnd, procs)

    def send_keys(self, events):
        from .game import INPUT, KEYBDINPUT, KEYEVENTF_KEYUP, _send, _U
        _send(*[INPUT(1, _U(ki=KEYBDINPUT(vk, scan, (KEYEVENTF_KEYUP if up else 0) | (1 if ext else 0), 0, self.MARK)))
                for vk, scan, ext, up in events])

    def send_button(self, button, up):
        from .game import INPUT, INPUT_MOUSE, MOUSEINPUT, _send, _U
        flags, data = button_flags(button, up, bool(self.u.GetSystemMetrics(self.SM_SWAPBUTTON)))
        _send(INPUT(INPUT_MOUSE, _U(mi=MOUSEINPUT(0, 0, data, flags, 0, self.MARK))))

    def start_hooks(self, shield_):
        if os.environ.get("BA_ACTOR") == "builder":
            raise GuardRefused("a builder process never installs a real input hook")
        ready, state = threading.Event(), {"tid": None, "error": None, "quit": False}
        thread = threading.Thread(target=self._pump, args=(shield_, ready, state), daemon=True, name="input-guard")
        thread.start()
        if not ready.wait(HOOK_START):
            state["quit"] = True
            raise GuardRefused("the input guard's hooks didn't start in time")
        if state["error"]:
            raise GuardRefused(f"the input guard's hooks failed ({state['error']})")
        return thread, state

    def stop_hooks(self, hooks):
        thread, state = hooks
        state["quit"] = True
        if state["tid"]:
            self.u.PostThreadMessageW(state["tid"], self.WM_NULL, 0, 0)
        thread.join(0.2)                # if it's slow, it still removes its own hooks at the deadline

    def _pump(self, shield_, ready, state):
        u = self.u
        msg = wintypes.MSG()
        handles = []

        def keyboard(code, wp, lp):
            try:
                if code == 0:
                    k = _KBD.from_address(lp)
                    if shield_.on_key(k.vkCode, k.scanCode, k.flags, wp in (WM_KEYDOWN, WM_SYSKEYDOWN)):
                        return 1
            except Exception:
                pass                    # a broken guard must never eat the user's input
            return u.CallNextHookEx(None, code, wp, lp)

        def mouse(code, wp, lp):
            try:
                if code == 0:
                    m = _MOUSE.from_address(lp)
                    delta = (0, 0)
                    if wp == WM_MOUSEMOVE and not m.flags & MOUSE_INJECTED:
                        here = wintypes.POINT()         # the cursor hasn't moved yet, so this is the real motion
                        if u.GetCursorPos(ctypes.byref(here)):
                            delta = (m.pt.x - here.x, m.pt.y - here.y)
                    if shield_.on_mouse(wp, m.flags, delta, m.mouseData >> 16):
                        return 1
            except Exception:
                pass
            return u.CallNextHookEx(None, code, wp, lp)

        procs = self.HOOKPROC(keyboard), self.HOOKPROC(mouse)   # kept alive for as long as the hooks exist
        try:
            state["tid"] = self.k.GetCurrentThreadId()
            u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)    # creates this thread's message queue
            module = self.k.GetModuleHandleW(None)
            for kind, proc in zip((self.WH_KEYBOARD_LL, self.WH_MOUSE_LL), procs):
                handle = u.SetWindowsHookExW(kind, proc, module, 0)
                if not handle:
                    state["error"] = f"SetWindowsHookEx error {ctypes.get_last_error()}"
                    return
                handles.append(handle)
            ready.set()
            while not state["quit"] and _now() < shield_.deadline:
                wait = max(1, min(15, int((shield_.deadline - _now()) * 1000)))
                u.MsgWaitForMultipleObjects(0, None, False, wait, self.QS_ALLINPUT)
                while u.PeekMessageW(ctypes.byref(msg), None, 0, 0, self.PM_REMOVE):
                    pass
        except Exception as e:
            state["error"] = repr(e)
        finally:
            for handle in handles:
                u.UnhookWindowsHookEx(handle)
            ready.set()
