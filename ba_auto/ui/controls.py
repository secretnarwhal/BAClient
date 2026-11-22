"""Client controls behind the panel: halt review, read-only guard status, restart and inspection.

Clearing a halt goes through the same `Controller.handle` op as `ctl clear-halt`, and the login hold
through the same `watchdog.clear_login_hold`. Each clear names the exact record the person reviewed,
so a newer halt can't be cleared by a page that was showing an older one.
"""
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

from .. import control, journal

HOLD_PHRASE = "NOBODY ELSE"
NOTE_MIN, NOTE_MAX = 3, 200
INSPECT_VIEWS = ("look", "detect", "who", "boss")
INSPECT_TIMEOUT = 25
_FRAME = re.compile(r"halt_\d{8}_\d{6}\.png")
_BLOCK = re.compile(r"input (?:postponed|refused):[^'\"\n]*")


def record_key(record):
    """Identifies one exact halt or hold record."""
    if record is None:
        return None
    text = json.dumps(record, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def clean_note(note):
    """The person's reason, for the journal. Raises ValueError if there isn't a real one."""
    if not isinstance(note, str):
        raise ValueError("Write a short note saying what you checked")
    text = " ".join("".join(c if c.isprintable() else " " for c in note).split())
    if not NOTE_MIN <= len(text) <= NOTE_MAX:
        raise ValueError(f"Write a note of {NOTE_MIN}-{NOTE_MAX} characters saying what you checked")
    return text


def frame_file(record):
    """The saved halt frame for this record, only if it's a real halt frame in data/ctl."""
    name = Path(str((record or {}).get("frame") or "").replace("\\", "/")).name
    if not _FRAME.fullmatch(name):
        return None
    path = (control.STATE / name)
    return path if path.is_file() else None


def halt_view(status, hold):
    """What the review sheet shows, from the client's own status and the watchdog's hold record."""
    halt = status.get("halt") or None
    now = status.get("resources") or {}
    out = {"now": {k: now.get(k) for k in ("ap", "credits", "pyroxene")}, "halt": None, "hold": None}
    if halt:
        reason = str(halt.get("reason") or "")
        out["halt"] = {"key": record_key(halt), "reason": reason, "time": halt.get("time"), "values": halt.get("values") or {},
                       "has_frame": frame_file(halt) is not None, "game_says": "game says" in reason,
                       "duplicate": "duplicate login" in reason.lower()}
    if hold:
        reason = str(hold.get("reason") or "")
        out["hold"] = {"key": record_key(hold), "reason": reason, "time": hold.get("time"), "title_seen": bool(hold.get("title_seen")),
                       "duplicate": "duplicate login" in reason.lower(), "phrase": HOLD_PHRASE}
    return out


# ---------------------------------------------------------------- input guard
def guard_state(status=None):
    """Read-only. The guard runs for every input, and the panel has no way to change that."""
    blocked = _last_block(status or {})
    try:
        from .. import inputguard
    except Exception as e:
        return {"available": False, "active": None, "problem": f"the input guard couldn't load: {e}", "last_block": blocked}
    check = getattr(inputguard, "enabled", None)
    if check is None:                   # no off switch exists
        return {"available": True, "active": True, "problem": None, "last_block": blocked}
    try:
        active = bool(check())
    except Exception as e:              # the guard itself refuses input while it can't tell
        return {"available": True, "active": None, "problem": str(e), "last_block": blocked}
    return {"available": True, "active": active, "problem": None if active else "The guard reports that it isn't active",
            "last_block": blocked}


def _last_block(status):
    """The newest reason the guard postponed or refused an input, from the client's own log and last run."""
    last = status.get("last") or {}
    for line in [str(last.get("error") or ""), *reversed([str(x) for x in status.get("log") or []])]:
        match = _BLOCK.search(line)
        if match:
            return match.group(0).strip()
    return None


# ---------------------------------------------------------------- restart
def spawn_restart(pid):
    """A small detached helper that reopens the client after this process has exited."""
    exe = Path(sys.executable).with_name("pythonw.exe")
    if not exe.exists():
        exe = Path(sys.executable)
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    for extra in (subprocess.CREATE_BREAKAWAY_FROM_JOB, 0):       # not every job allows breakaway
        try:
            return subprocess.Popen([str(exe), "-m", "ba_auto.ui.restart", str(int(pid))], cwd=str(journal.ROOT), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags | extra)
        except OSError:
            if not extra:
                raise


# ---------------------------------------------------------------- read-only views
def _plain(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


class Inspector:
    """look, detect, who and the boss reader, one at a time on one thread, and never during a run.
    It has its own Vision, so its template matches never appear on the game's overlay."""

    def __init__(self, game, busy, advise=None, vision=None):
        self.game, self.busy, self.advise = game, busy, advise
        self.vision = vision
        self._gate = threading.Lock()
        self._pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="ui-inspect")   # capture handles are per thread

    def run(self, view):
        if view not in INSPECT_VIEWS:
            return {"ok": False, "error": "unknown view"}
        if self.busy():
            return {"ok": False, "error": "A run is active. Inspect waits, so it can't slow capture or matching"}
        if not self._gate.acquire(blocking=False):
            return {"ok": False, "error": "Still reading the last request"}
        future = self._pool.submit(self._read, view)
        future.add_done_callback(lambda _: self._gate.release())
        try:
            return {"ok": True, "view": view, "result": _plain(future.result(timeout=INSPECT_TIMEOUT))}
        except concurrent.futures.TimeoutError:
            return {"ok": False, "error": "The game took too long to read"}
        except Exception as e:
            return {"ok": False, "error": f"Couldn't read the game: {e}"}

    def _read(self, view):
        if self.vision is None:
            from ..vision import Vision
            self.vision = Vision()      # no runtime, so nothing is drawn on the overlay
        frame = self.game.grab()
        return getattr(self, f"_{view}")(frame)

    def _look(self, frame):
        from .. import catalog, look
        out = look.describe(self.vision, frame, save=False, window=self.game.window_problem())
        out["buttons"] = [{"name": b["name"], "class": b["class"]} for b in out.get("buttons") or [] if b["class"] != catalog.NEVER]
        return out

    def _detect(self, frame):
        from ..screens import detect, scores
        top = sorted(scores(self.vision, frame).items(), key=lambda kv: -kv[1])[:10]
        return {"screen": detect(self.vision, frame), "scores": top}

    def _who(self, frame):
        from .. import nav, portraits
        from ..screens import label
        screen, popup = label(self.vision, frame), nav.top_popup(self.vision, frame)
        layout = "rooms" if popup and popup[0] == "All Locations" else screen if screen in portraits.LAYOUTS else None
        if layout is None:
            return {"error": "Open the Students list, All Locations or a battle result first", "screen": screen}
        return {"layout": layout, "students": portraits.who(frame, layout)}

    def _boss(self, frame):
        from .. import advisor
        enemy = advisor.read_enemy(frame)
        advice = self.advise(enemy.get("armor"), enemy.get("attack")) if self.advise and (enemy.get("armor") or enemy.get("attack")) else None
        return {"enemy": enemy, "advice": advice}
