"""Start Blue Archive through Steam, and get from the title screen to the lobby.

Launch: never a second copy, never closes the game, at most one launch per request and a cooldown
between requests. It only asks Steam (steam://rungameid) and never touches Steam's own dialogs.

Log in: presses CLICK TO START and nothing else
on the title, then uses nav's safe popup handling. It stops, without clicking, on a duplicate login,
an update, download or maintenance message, a login or account screen, or anything unrecognised.
Presses are limited and back off, also across runs.

The cursor is the user's. The input guard hands the cursor, focus and typing straight back after a press,
so a press only waits while the user is actually in the game, or while another game is open.
"""
import contextlib
import ctypes
import json
import os
import re
import subprocess
import threading
import time
from ctypes import wintypes

import cv2
import numpy as np
import win32gui
import win32process

from . import buttons, catalog, inputguard, journal, nav, watchdog
from .runtime import Stopped
from .screens import _dimmed, is_title, label

APP_ID = "3557620"
STEAM_URL = f"steam://rungameid/{APP_ID}"
GAME_EXE = "bluearchive.exe"
WINDOW_TITLE = "Blue Archive"
STEAM_PROCESSES = {"steam.exe", "steamwebhelper.exe"}
STEAM_USUAL_WINDOWS = {"", "steam", "friends list", "special offers"}

WINDOW_TIMEOUT = 180.0           # Steam asked -> the game's window exists
TITLE_TIMEOUT = 300.0            # window -> title screen
STEAM_GRACE = 25.0               # a new Steam window this old, with no game yet, is a prompt
RELAUNCH_COOLDOWN = 300.0        # no launch loop, even across requests
IDLE_NEEDED = 5                  # seconds without keyboard or mouse before a press
IDLE_WAIT = 180.0                # how long a started run waits for that
AFTER_PRESS = 90.0               # press -> lobby
TITLE_RETRY = 15.0               # still on the title this long after a press: it didn't take
STUCK_SECONDS = 30.0             # an unrecognised screen that stops changing
BACKOFF = (20.0, 60.0)           # before the second and third press
MAX_PRESSES = 3                  # per request
RECENT_WINDOW = 1800.0           # across requests: at most RECENT_LIMIT presses in this long
RECENT_LIMIT = 5
STOP_SHOWN = 300.0               # how long the HUD keeps showing why it stopped
MAX_DATE_NOTICES = 2             # per request
DATE_NOTICE_WAIT = 60.0          # Confirm -> title
FOCUS_WATCH = 20.0               # after the window appears, how long a focus grab by the game is undone
FOCUS_RETURNS = 2                # at most this many times, so a user who clicks into the game keeps it
LOBBY_CHECKS = 5                 # readings of a clear lobby that prove a person logged back in
# Only tasks that are safe to start over. Anything that can spend or claim is reported instead.
RESTARTABLE = {"lobby", "daily-scan", "roster-scan", "lesson-scan", "sweep-scan", "ministories"}
DANGER = re.compile(r"\b(updates?|download\w*|maintenance|password|credentials?|sign[ -]?in|log[ -]?in|accounts?|"
                    r"terms|agree|verify|verification|purchase)\b", re.I)


class LauncherStopped(RuntimeError):
    """The launcher stopped on purpose. The message says why; nothing more was clicked."""


def _now():
    return time.monotonic()


def _wall():
    return time.time()


# ------------------------------------------------------------------ settings and press history
_file_lock = threading.RLock()   # each change re-reads and rewrites launcher.json, so one can't undo another


def _file():
    from . import control
    return control.STATE / "launcher.json"


def _load():
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data):
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def auto_login():
    """On unless someone turned it off."""
    return _load().get("auto_login", True) is not False


def set_auto_login(on, by="user"):
    with _file_lock:
        data = _load()
        data["auto_login"] = bool(on)
        _save(data)
    journal.record("launcher", setting="auto_login", value=bool(on), by=by)
    return bool(on)


# ------------------------------------------------------------------ what's running (read-only)
class _Entry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


_kernel = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
_kernel.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_Entry))
_kernel.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_Entry))
_kernel.CloseHandle.argtypes = (wintypes.HANDLE,)


def processes():
    """{pid: lower-case exe name}, from a system snapshot. No process is opened."""
    snap = _kernel.CreateToolhelp32Snapshot(0x2, 0)
    if snap in (None, wintypes.HANDLE(-1).value):
        return {}
    out = {}
    try:
        entry = _Entry()
        entry.dwSize = ctypes.sizeof(_Entry)
        ok = _kernel.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out[entry.th32ProcessID] = entry.szExeFile.lower()
            ok = _kernel.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _kernel.CloseHandle(snap)
    return out


def window_open():
    hwnd = win32gui.FindWindow(None, WINDOW_TITLE)
    return bool(hwnd and win32gui.IsWindowVisible(hwnd))


def game_running():
    return window_open() or GAME_EXE in processes().values()


def steam_running():
    return "steam.exe" in processes().values()


def steam_windows():
    """Titles of Steam's visible windows, other than its usual ones. Only read, never touched."""
    steam = {pid for pid, name in processes().items() if name in STEAM_PROCESSES}
    titles = set()

    def visit(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd).strip()
            if title.lower() not in STEAM_USUAL_WINDOWS and win32process.GetWindowThreadProcessId(hwnd)[1] in steam:
                titles.add(title)
    win32gui.EnumWindows(visit, None)
    return titles


class _LastInput(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def idle_seconds():
    """Seconds since the last keyboard or mouse input on this PC."""
    info = _LastInput(ctypes.sizeof(_LastInput), 0)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0


def foreground_is_game(game):
    try:
        return win32gui.GetForegroundWindow() == game.find()
    except Exception:
        return False


class _FocusKeeper:
    """A game that has just opened grabs the focus. If the user was elsewhere, give it back."""

    def __init__(self):
        self.left, self.until = FOCUS_RETURNS, None
        try:
            self.front = win32gui.GetForegroundWindow() if inputguard.protecting() else 0
        except Exception:
            self.front = 0

    def check(self, game, log):
        if not self.front or not self.left:
            return
        self.until = self.until or _now() + FOCUS_WATCH
        if _now() >= self.until:
            self.left = 0
            return
        try:
            hwnd = game.find()
            if hwnd == self.front or win32gui.GetForegroundWindow() != hwnd:
                return
            self.left -= 1
            given = inputguard.give_focus(self.front)
            log("launcher: the game took the focus, gave it back" if given else
                "launcher: the game took the focus and it couldn't be given back")
        except Exception as e:
            log(f"launcher: couldn't check the focus ({e})")


def _open_url():
    """Ask Steam to start the game. The one line in the client that can launch it."""
    if os.environ.get("BA_ACTOR") == "builder":
        raise LauncherStopped("the builder session can't launch the game")
    subprocess.Popen(["explorer.exe", STEAM_URL], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


# ------------------------------------------------------------------ what the HUD shows
_lock = threading.Lock()
_state = {"stage": None, "reason": None, "needs_user": False, "idle": None, "at": 0.0, "active": 0}


def _report(stage, log=None, reason=None, needs_user=False, idle=None):
    with _lock:
        changed = stage != _state["stage"] or reason != _state["reason"]
        _state.update(stage=stage, reason=reason, needs_user=needs_user, idle=idle, at=_now())
    if changed:
        journal.record("launcher", stage=stage, reason=reason, needs_user=needs_user)
        if log:
            log(f"launcher: {stage.replace('_', ' ')}" + (f": {reason}" if reason else ""))


def _stop(log, reason, needs_user=False):
    _report("stopped", log, reason, needs_user)
    return LauncherStopped(reason)


@contextlib.contextmanager
def _working(log):
    with _lock:
        _state["active"] += 1
    try:
        yield
    except Stopped:
        _report("stopped", log, "stopped by the user")
        raise
    finally:
        with _lock:
            _state["active"] -= 1


def status(screen=None):
    """The launcher's state for `ctl status` and the HUD. Never raises."""
    try:
        running = game_running()
        hold = watchdog.login_hold()
        with _lock:
            s = dict(_state)
        stage, reason, needs = s["stage"], s["reason"], s["needs_user"]
        idle = s["idle"]
        if not s["active"]:
            age = _now() - s["at"]
            if hold:
                stage, needs = "stopped", True
                reason = (f"{hold.get('reason')}: auto login is off until the user decides. After that: "
                          "python -m ba_auto ctl clear-login-hold --note WHY")
            elif not ((stage == "stopped" and age < STOP_SHOWN) or (stage == "waiting_idle" and age < 3)):
                base = str(screen or "").split("+")[0]
                stage = ("not_running" if not running else "loading" if not window_open() else
                         base if base in ("title", "lobby") else "running")
                reason, needs, idle = None, False, None
        stage = stage or ("running" if running else "not_running")
        waiting = stage == "waiting_idle"
        return {"stage": stage, "reason": reason, "needs_user": bool(needs), "game_running": running,
                "auto_login": auto_login(), "idle_seconds": int(idle or 0) if waiting else None,
                "idle_needed": IDLE_NEEDED if waiting else None}
    except Exception as e:
        return {"stage": "stopped", "reason": f"launcher status failed: {e!r}", "needs_user": False,
                "game_running": None, "auto_login": None, "idle_seconds": None, "idle_needed": None}


# ------------------------------------------------------------------ logging in
def _title_clear(vision, frame):
    """The title with nothing open over it."""
    return is_title(vision, frame) and not nav.top_popup(vision, frame) and not _dimmed(vision, frame, "title/menu")


def _center_text(frame):
    return nav._notice_text(frame)


def _check_blockers(frame, log):
    halt = watchdog.halted()
    if halt:
        raise _stop(log, f"input is halted: {halt.get('reason')}", True)
    hold = watchdog.login_hold()
    if hold:
        raise _stop(log, f"{hold.get('reason')}: on hold for the user, not logging in", True)
    blocked = watchdog.blocking_popup(frame)
    if blocked:
        watchdog.halt(blocked, input="login")
        raise _stop(log, blocked, True)


def _grab(game, log):
    try:
        return game.grab()
    except Stopped:
        raise
    except Exception as e:
        raise _stop(log, f"can't read the game: {e}", True) from e


def _press_wait(game):
    """Why Click to Start must wait right now, or None."""
    other = inputguard.other_game(game)
    if other:
        return f"waiting for {other} to close"
    if foreground_is_game(game) and idle_seconds() < IDLE_NEEDED:
        return "waiting while the user is in the game"
    return None


def _wait_idle(game, log, wait):
    end = _now() + wait
    while True:
        why = _press_wait(game)
        if why is None:
            return
        _report("waiting_idle", log, idle=min(idle_seconds(), IDLE_NEEDED))
        if _now() >= end:
            raise _stop(log, f"ready to log in: {why}")
        game.sleep(1.0)


def _note_press(log):
    now = _wall()
    with _file_lock:
        data = _load()
        recent = [t for t in data.get("presses", []) if isinstance(t, (int, float)) and now - t < RECENT_WINDOW]
        if len(recent) >= RECENT_LIMIT:
            raise _stop(log, f"Click to Start was pressed {len(recent)} times in the last "
                             f"{int(RECENT_WINDOW / 60)} minutes: not pressing it again", True)
        data["presses"] = recent + [now]
        _save(data)


def _press(game, vision, frame, log):
    x, y, _ = catalog.SCREENS["title"]["click_to_start"]
    if not _title_clear(vision, frame):
        raise _stop(log, "something is open over the title screen: not pressing Click to Start", True)
    if not buttons.prove(vision, frame, "title", "click_to_start", x, y):
        raise _stop(log, "can't confirm Click to Start at this window shape", True)
    _note_press(log)
    _report("logging_in", log)
    game.allow_title_once = True
    try:
        game.click(x, y)
    finally:
        game.allow_title_once = False


def _dismiss_date_notice(game, vision, frame, log):
    """Confirm the daily date-change Notice, then wait for the title."""
    pos = vision.find(frame, "date_notice_confirm")
    if not pos:
        raise _stop(log, "can't confirm the date-change notice's button", True)
    log("launcher: the date changed, confirming the notice")
    game.click(*pos)
    end = _now() + DATE_NOTICE_WAIT
    while _now() < end:
        game.sleep(1.0)
        frame = _grab(game, log)
        _check_blockers(frame, log)
        if not watchdog.date_notice(frame) and label(vision, frame) in ("title", "lobby"):
            return
    raise _stop(log, "the title didn't appear after the date-change notice", True)


def _after_press(game, vision, log):
    """'lobby', or 'title' if the press didn't take. Anything else stops."""
    start = _now()
    previous, still_since = None, _now()
    while _now() - start < AFTER_PRESS:
        game.sleep(1.0)
        frame = _grab(game, log)
        _check_blockers(frame, log)
        if watchdog.date_notice(frame):
            _dismiss_date_notice(game, vision, frame, log)
            return "title"
        here = label(vision, frame)
        if here == "lobby":
            return "lobby"
        if here == "title":
            if _now() - start >= TITLE_RETRY:
                return "title"
            continue
        if here != "unknown" or nav.top_popup(vision, frame):
            try:
                nav.go_lobby(game, vision, log)
            except nav.NavError as e:
                raise _stop(log, f"stopped after login: {e}", True) from e
            return "lobby"
        text = _center_text(frame)
        hit = DANGER.search(text or "")
        if hit:
            raise _stop(log, f"the game is showing '{hit.group(0)}', so a person should look: {text[:160]}", True)
        small = cv2.cvtColor(cv2.resize(np.asarray(frame), (160, 90), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
        if previous is None or float(cv2.absdiff(previous, small).mean()) > 1.0:
            still_since = _now()
        previous = small
        if _now() - still_since >= STUCK_SECONDS:
            raise _stop(log, "stopped on a screen the client doesn't recognise", True)
    raise _stop(log, f"didn't reach the lobby within {int(AFTER_PRESS)} s of pressing Click to Start", True)


def log_in(game, vision, log=None, idle_wait=IDLE_WAIT):
    """From the title to the lobby. Raises LauncherStopped when it stops for any reason."""
    log = log or game.log
    if journal.read_only():
        raise LauncherStopped("the builder session can't log in")
    presses = notices = 0
    with _working(log):
        while True:
            frame = _grab(game, log)
            _check_blockers(frame, log)
            if watchdog.date_notice(frame):
                if notices >= MAX_DATE_NOTICES:
                    raise _stop(log, "the date-change notice keeps coming back", True)
                notices += 1
                _wait_idle(game, log, idle_wait)
                frame = _grab(game, log)
                _check_blockers(frame, log)
                if watchdog.date_notice(frame):
                    _dismiss_date_notice(game, vision, frame, log)
                continue
            here = label(vision, frame)
            if here == "lobby":
                _report("lobby", log)
                return {"login": "lobby", "presses": presses}
            if here != "title":
                raise _stop(log, f"not on the title screen (on {here})")
            if presses >= MAX_PRESSES:
                raise _stop(log, f"still on the title after {presses} presses", True)
            if presses:
                game.sleep(BACKOFF[min(presses - 1, len(BACKOFF) - 1)])
            _report("title", log)
            _wait_idle(game, log, idle_wait)
            frame = _grab(game, log)          # the screen may have changed during the wait
            _check_blockers(frame, log)
            if label(vision, frame) != "title":
                continue
            _press(game, vision, frame, log)
            presses += 1
            if _after_press(game, vision, log) == "lobby":
                _report("lobby", log)
                return {"login": "lobby", "presses": presses}


# ------------------------------------------------------------------ launching
def _wait_window(game, log, baseline):
    start = _now()
    seen = {}
    while _now() - start < WINDOW_TIMEOUT:
        if window_open():
            return
        if GAME_EXE not in processes().values():
            now = _now()
            current = steam_windows() - baseline
            seen = {t: seen.get(t, now) for t in current}
            for title, since in seen.items():
                if now - since >= STEAM_GRACE:
                    raise _stop(log, f"Steam is showing '{title}': please deal with it, then launch again", True)
        game.sleep(1.0)
    raise _stop(log, f"the game's window didn't appear within {int(WINDOW_TIMEOUT)} s. Check Steam", True)


def _wait_title(game, vision, log, keeper=None):
    start = _now()
    while _now() - start < TITLE_TIMEOUT:
        if not game_running():
            raise _stop(log, "the game closed while loading. Not launching it again", True)
        if keeper:
            keeper.check(game, log)
        try:
            frame = game.grab()
        except Stopped:
            raise
        except Exception:
            frame = None
        view = getattr(frame, "view_info", None)
        if frame is not None and view is not None and view.confident:
            _check_blockers(frame, log)
            here = label(vision, frame)
            if here in ("title", "lobby"):
                return here
            if here != "unknown":
                return "running"
            text = _center_text(frame)
            hit = DANGER.search(text or "")
            if hit:
                raise _stop(log, f"the game is showing '{hit.group(0)}', so a person should look: {text[:160]}", True)
        game.sleep(2.0)
    raise _stop(log, f"the title screen didn't appear within {int(TITLE_TIMEOUT)} s", True)


def launch(game, vision, log=None, auto=None):
    """Make sure the game is up, then log in if Auto login is on. Never starts a second copy."""
    log = log or game.log
    if journal.read_only():
        raise LauncherStopped("the builder session can't launch the game")
    auto = auto_login() if auto is None else auto
    launched = False
    with _working(log):
        if game_running():
            log("launcher: Blue Archive is already running")
            if not window_open():
                _report("loading", log)
                _wait_window(game, log, steam_windows())
                reached = _wait_title(game, vision, log)
            else:
                here = label(vision, _grab(game, log))
                reached = here if here in ("title", "lobby") else "running"
        else:
            if not steam_running():
                raise _stop(log, "Steam isn't running. Open it, then launch again")
            data = _load()
            ago = _wall() - float(data.get("last_launch") or 0)
            if ago < RELAUNCH_COOLDOWN:
                raise _stop(log, f"the game was launched {int(ago)} s ago. Not launching it again yet")
            other = inputguard.other_game(game)
            if other:
                raise _stop(log, f"{other} is open: not launching Blue Archive over it")
            baseline = steam_windows()
            with _file_lock:
                data = _load()
                data["last_launch"] = _wall()
                _save(data)
            keeper = _FocusKeeper()
            _report("launching", log)
            _open_url()
            launched = True
            _wait_window(game, log, baseline)
            _report("loading", log)
            reached = _wait_title(game, vision, log, keeper)
        if reached == "title" and auto:
            return {"launched": launched, **log_in(game, vision, log)}
        _report(reached, log)
        return {"launched": launched, "stage": reached}


# ------------------------------------------------------------------ the client's title watcher
class TitleWatcher:
    """Fed the screen name about once a second. Logs in only when the user isn't using the PC."""

    def __init__(self, ctl, game, vision, log):
        self.ctl, self.game, self.vision, self.log = ctl, game, vision, log
        self.latched = False             # tried on this visit to the title: wait until it's left
        self.lobby_seen = 0

    def _watch_hold(self, hold, screen):
        if screen == "title" and not hold.get("title_seen"):
            watchdog.mark_login_hold(title_seen=True)
        if screen == "lobby" and hold.get("title_seen"):
            self.lobby_seen += 1
            if self.lobby_seen >= LOBBY_CHECKS:      # someone logged back in by hand
                watchdog.clear_login_hold("client", "a person logged back in")
                self.lobby_seen = 0
        else:
            self.lobby_seen = 0

    def observe(self, screen, frame=None):
        hold = watchdog.login_hold()
        if hold:
            self._watch_hold(hold, screen)
            return
        self.lobby_seen = 0
        if screen != "title" and not (frame is not None and watchdog.date_notice(frame)):
            self.latched = False
            return
        if self.latched or not auto_login() or self.ctl.busy or watchdog.halted():
            return
        if _press_wait(self.game):
            _report("waiting_idle", self.log, idle=min(idle_seconds(), IDLE_NEEDED))
            return
        run = self.ctl.start("Auto login", lambda: log_in(self.game, self.vision, self.log, idle_wait=10.0),
                             actor="client", name="login")
        self.latched = run is not None
