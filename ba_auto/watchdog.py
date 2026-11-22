"""Resource watchdog: tracks AP, credits and pyroxene, and halts everything if pyroxene falls.

While data/ctl/halt.json exists, Game refuses all input from every process and the client
won't start runs. A person has to look and clear it: `python -m ba_auto ctl clear-halt`.
A value that can't be read is unknown, never safe: if the pyroxene number visibly changes
and stays unreadable, that halts too.
"""
import json
import os
import re
import threading
import time

import cv2

from . import journal, resources

HALT = journal.ROOT / "data" / "ctl" / "halt.json"
ACK = journal.ROOT / "data" / "ctl" / "halt_ack.json"
ACK_SECONDS = 120                # after a person clears a halt, the popup may be answered
# Game messages that end the session. Going on would kick whoever else is signed in.
BLOCKERS = ("duplicate login", "returning to title")
# The daily reset (always click through it). Only this exact wording
# counts, so a Duplicate login or any other "returning to title" message stays a hard stop.
DATE_NOTICE = "thedatehaschangedreturningtotitlescreen"
# The client always reconnects. Only this exact wording, and only its Reconnect.
NETWORK_NOTICE = "thenetworkconnectionisunstabletaptheconfirmbuttontoreturntothetitlescreen"
STABLE = 2                       # readings in a row before a change counts (numbers animate)
UNREADABLE = 4                   # steady, changed and unreadable for this many checks halts
UNREAD_NOTICE = 3                # monitor checks of an open Notice with no readable text before it halts
MIN_NOTICE_LETTERS = 4           # fewer than this is OCR noise, not a message
FILE_TRIES = 6                   # Windows refuses to replace or delete a file while another thread reads it
FILE_RETRY = 0.02
UNREAD_REASON = "game says: a Notice is open and its text can't be read"


class CheckFailed(RuntimeError):
    """A safety check couldn't run, so nobody knows what's on screen. Input is refused, without a halt."""


def _retry(action):
    """Run a replace or delete, waiting out a reader that has the file open for a moment."""
    for attempt in range(FILE_TRIES):
        try:
            return action()
        except PermissionError:
            if attempt == FILE_TRIES - 1:
                raise
            time.sleep(FILE_RETRY)


def _write_json(path, record):
    """Write a whole file or nothing. The name is per thread: the worker and the monitor can both halt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    _retry(lambda: os.replace(tmp, path))


def _remove(path):
    def unlink():
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    _retry(unlink)


def _read_record(path, what):
    """A stop record, or None if there's no file. A file that isn't a real record still counts as a stop."""
    unread = {"reason": f"{what} file exists but could not be read", "unreadable": True}
    try:
        record = json.loads(_retry(lambda: path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return unread
    return record if isinstance(record, dict) and record else unread


def _hold_file():
    return HALT.with_name("login_hold.json")


def login_hold():
    """Set by a session-ending message such as Duplicate login. While it's set nothing logs in
    automatically, even after the halt is cleared: someone else may be on the account."""
    return _read_record(_hold_file(), "login hold")


def _write_hold(record):
    _write_json(_hold_file(), record)


def mark_login_hold(**fields):
    hold = login_hold()
    if hold is not None and not hold.get("unreadable"):      # never write a placeholder over the real reason
        _write_hold({**hold, **fields})


def clear_login_hold(by, note=""):
    hold = login_hold()
    if hold is None:
        return None
    _remove(_hold_file())
    journal.record("login_hold_cleared", by=by, note=note, was=hold.get("reason"))
    return hold


def halted():
    """The halt record, or None."""
    return _read_record(HALT, "halt")


def halt(reason, **details):
    """Stop all game input until a person clears it. Keeps the first reason if already halted."""
    if "game says" in str(reason) and login_hold() is None:
        _write_hold({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "reason": reason})
    existing = halted()
    if existing:
        return existing
    record = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "reason": reason, **details}
    _write_json(HALT, record)
    journal.record("halt", **record)
    return record


_vision = None


def _top_notice(frame):
    """The text of a Notice that is the top layer, or None."""
    global _vision
    try:
        from . import nav
        from .screens import _dimmed
        from .vision import Vision
        if _vision is None:
            _vision = Vision()           # no runtime: stays off the overlay
        if not _vision.find(frame, "nav_notice") or _dimmed(_vision, frame, "nav_notice"):
            return None
        return nav._notice_text(frame)
    except Exception as e:
        raise CheckFailed(f"input refused: the popup check couldn't run ({e!r})") from e


def is_date_notice(text):
    """The whole Notice is the daily reset's wording, allowing for OCR's usual letter mix-ups. Any extra word fails."""
    return _fold(text) == _fold(DATE_NOTICE)


def date_notice(frame):
    """True when the daily date-change Notice is on top."""
    return is_date_notice(_top_notice(frame))


def date_confirm(frame):
    """Where that Notice's Confirm button is matched in this frame, or None."""
    try:
        return _vision.find(frame, "date_notice_confirm") if _vision is not None else None
    except Exception:
        return None


def is_network_notice(text):
    """The whole Notice is the network-unstable wording. Any extra word fails."""
    return bool(text) and _fold(text) == _fold(NETWORK_NOTICE)


def network_notice(frame):
    """True when the network-unstable Notice is on top."""
    return is_network_notice(_top_notice(frame))


def network_reconnect(frame):
    """Where that Notice's Reconnect button is matched in this frame, or None."""
    try:
        return _vision.find(frame, "notice_reconnect") if _vision is not None else None
    except Exception:
        return None


def _fold(text):
    """Letters only, with the shapes OCR mixes up made equal, so a misread message still matches."""
    return re.sub(r"[^a-z]", "", (text or "").lower().translate(str.maketrans("10|!i", "lolll"))).replace("rn", "m")


def _no_text(text):
    """A Notice whose reading has no real words in it. None means no Notice is open."""
    return text is not None and len(_fold(text)) < MIN_NOTICE_LETTERS


def _ack_text(reason):
    """What a cleared halt's message said. An unreadable Notice is remembered as no text."""
    return "" if reason == UNREAD_REASON else _fold(str(reason).split("game says:", 1)[-1])


def _acknowledged(text):
    """True for a short time after a person cleared a halt from this same message. A different one still stops."""
    try:
        ack = json.loads(ACK.read_text(encoding="utf-8"))
        if time.time() >= float(ack["until"]):
            return False
        said = ack["text"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return said == _fold(text)           # exact: _fold already absorbs OCR's mix-ups, and a near match could be another message


def _blocker(text):
    """The halt reason for this Notice text, or None."""
    if text is None or is_date_notice(text):
        return None
    folded = _fold(text)
    if not any(_fold(b) in folded for b in BLOCKERS) or _acknowledged(text):
        return None
    return f"game says: {text}"


def blocking_popup(frame):
    """The message if a session-ending Notice is on top, unless a person just cleared it."""
    return _blocker(_top_notice(frame))


def unreadable_notice(frame):
    """True when a Notice is the top layer and none of its text could be read."""
    return _no_text(_top_notice(frame)) and not _acknowledged("")


def on_title(frame):
    """True on the game's title screen, where a click logs in."""
    global _vision
    try:
        from .screens import is_title
        from .vision import Vision
        if _vision is None:
            _vision = Vision()
        return is_title(_vision, frame)
    except Exception as e:
        raise CheckFailed(f"input refused: the title check couldn't run ({e!r})") from e


def clear(by, note=""):
    existing = halted()
    if existing is None:
        return None
    if "game says" in str(existing.get("reason")):     # first, so the monitor can't halt again on the same popup
        _write_json(ACK, {"until": time.time() + ACK_SECONDS, "by": by, "text": _ack_text(existing.get("reason"))})
    _remove(HALT)
    journal.record("halt_cleared", by=by, note=note, was=existing.get("reason"))
    return existing


def compare(before, after):
    """A halt reason if pyroxene fell between two readings, else None."""
    old, new = (before or {}).get("pyroxene"), (after or {}).get("pyroxene")
    if old is not None and new is not None and new < old:
        return f"pyroxene dropped from {old} to {new}"
    return None


class Watchdog:
    """Fed frames by the client's monitor thread, about once a second."""

    def __init__(self, rt=None, frame_saver=None):
        self.rt = rt
        self.values = {"ap": None, "credits": None, "pyroxene": None}
        self._pending = {}
        self._base_crop = None           # the pyroxene number image for the current value
        self._odd = 0
        self._unread_notice = 0

    def rebase(self):
        """After a person clears a halt: accept whatever is on screen as the new baseline."""
        self.values["pyroxene"] = None
        self._pending.pop("pyroxene", None)
        self._base_crop = None
        self._odd = 0
        self._unread_notice = 0

    def _stop_everything(self, reason, frame):
        if halted():                     # already stopped: don't save and log it again every second
            return
        if self.rt is not None:
            self.rt.stop.set()           # first: a halt that can't be saved must still end the run
        record = halt(reason, values={k: v for k, v in self.values.items()},
                      frame=self._save_frame(frame))
        if self.rt is not None:
            self.rt.log(f"WATCHDOG HALT: {record.get('reason')} - all input refused until "
                        "`ctl clear-halt`")

    @staticmethod
    def _save_frame(frame):
        try:
            path = HALT.with_name(f"halt_{time.strftime('%Y%m%d_%H%M%S')}.png")
            cv2.imwrite(str(path), frame)
            return os.path.relpath(path, journal.ROOT)
        except Exception:
            return None

    def observe(self, frame):
        """Check one frame. Returns the halt reason if this frame caused a halt."""
        text = _top_notice(frame)
        blocked = _blocker(text)
        if _no_text(text) and not _acknowledged(""):   # a Notice nobody can read might be the Duplicate login
            self._unread_notice += 1
            if self._unread_notice >= UNREAD_NOTICE:
                blocked = UNREAD_REASON
        else:
            self._unread_notice = 0
        if blocked:
            if not halted():
                self._stop_everything(blocked, frame)
                return blocked
            halt(blocked)                # keeps the first reason, and still records the login hold
            return None
        images = resources.crops(frame)
        if images is None:
            return None                  # no bar on this screen: nothing to learn
        now = {k: resources.read_value(k, img) for k, img in images.items()}
        reason = None
        for key, value in now.items():
            if value is None:
                continue
            count = self._pending[key][1] + 1 if self._pending.get(key, (None,))[0] == value else 1
            self._pending[key] = (value, count)
            if count < STABLE or value == self.values[key]:
                continue
            old = self.values[key]
            self.values[key] = value
            if old is not None:
                journal.record("resources", item=key, before=old, after=value, task=getattr(self.rt, "task", None))
                if self.rt is not None:
                    self.rt.log(f"{key}: {old} -> {value}")
            if key == "pyroxene":
                self._base_crop = images[key]
                self._odd = 0
                reason = compare({"pyroxene": old}, {"pyroxene": value}) or reason
        if now["pyroxene"] is not None and self._base_crop is None and self.values["pyroxene"] == now["pyroxene"]:
            self._base_crop = images["pyroxene"]
        # Cutscenes and white fades can look like the bar's background while showing no counter at all.
        # Only a bar proven by another number in this frame can have a pyroxene value that "changed".
        # A real drop is still caught by compare() as soon as the counter is readable again.
        bar_shown = now["ap"] is not None or now["credits"] is not None
        if now["pyroxene"] is not None or not bar_shown:
            self._odd = 0
        elif self._base_crop is not None:
            moved = resources.changed(self._base_crop, images["pyroxene"])
            self._odd = self._odd + 1 if moved else 0
            if self._odd >= UNREADABLE:
                reason = f"pyroxene changed from {self.values['pyroxene']} and can't be read"
        if reason:
            self._stop_everything(reason, frame)
        return reason
