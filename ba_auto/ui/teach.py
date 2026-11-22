"""Marking lesson rooms on the Atlas and starting the lesson runner from a real selection.

Marking never spends anything. Only `Desk.start` can start `lesson-teach`, and only with exactly the
rooms that are marked, checked against the current scan and ticket count. The runner repeats its
own checks before any input.
"""
import hashlib
import json
import os
import threading
import time

from .. import atlas, control, daily, journal
from ..tasks import lessons
from . import art

MARKS = journal.ROOT / "data" / "ui" / "lesson_marks.json"
STATUS = journal.ROOT / "data" / "lesson_teach.json"
TASK = "lesson-teach"
SPENT_STATES = ("opening", "teaching", "done", "failed")    # a ticket may be gone: never offer these again
RUN_STATES = ("running", "done", "stopped", "failed")
ROOM_STATES = ("queued", "opening", "teaching", "done", "failed", "skipped")


def room_id(location, room):
    return f"{location}|{room}"


def index(scan):
    """({room id: (location, room)}, ids that appear more than once)."""
    table, twice = {}, set()
    for loc in scan.get("locations") or []:
        for room in loc.get("rooms") or []:
            rid = room_id(loc.get("location"), room.get("room"))
            if rid in table:
                twice.add(rid)
            table[rid] = (loc, room)
    return table, twice


def read_status(path=STATUS):
    """The runner's status file, or None. A damaged file is reported instead of being ignored."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"state": "unreadable", "rooms": []}
    return data if isinstance(data, dict) else {"state": "unreadable", "rooms": []}


def spent_rooms(status, scan_ts):
    """Rooms the runner may already have spent a ticket on, for this scan."""
    if not status or status.get("rehearse") or status.get("scan_ts") != scan_ts:
        return set()
    return {r.get("id") for r in status.get("rooms") or [] if r.get("state") in SPENT_STATES}


def why_not(rid, table, twice, spent=()):
    """Why this room can't be marked, or None."""
    if rid not in table:
        return "That room isn't in the current scan."
    if rid in twice:
        return "The scan lists this room name twice, so it can't be told apart."
    room = table[rid][1]
    if room.get("locked"):
        return "This room is locked."
    if not room.get("students"):
        return "Nobody was read in this room."
    if any(not s.get("dev") for s in room["students"]):
        return "A face here couldn't be identified, so the room can't be confirmed. Scan again with the game at 16:9."
    if rid in spent:
        return "This room was already run from this scan, so its ticket may be spent. Scan again to mark it."
    return None


def problems(scan, rooms, tickets, spent=(), now=None):
    """Everything that blocks teaching exactly these rooms. Empty means it may start."""
    out = []
    if not scan.get("locations"):
        return ["Scan the lessons first."]
    if lessons.is_stale(scan, now):
        out.append("The scan is from before today's reset. Scan again.")
    if not scan.get("complete"):
        out.append("The scan isn't complete. Finish a full scan first.")
    if not rooms:
        out.append("Mark at least one room.")
    table, twice = index(scan)
    if len(set(rooms)) != len(rooms):
        out.append("A room is listed twice.")
    for rid in dict.fromkeys(rooms):
        why = why_not(rid, table, twice, spent)
        if why:
            out.append(f"{rid.replace('|', ' > ')}: {why}")
    if tickets is None:
        out.append("Today's ticket count hasn't been read.")
    elif len(rooms) > tickets:
        out.append(f"{len(rooms)} rooms marked, but only {tickets} tickets.")
    return out


class MarkBook:
    """Saved marks. With no path they live in memory, for previews."""

    def __init__(self, path=MARKS):
        self.path = path
        self._memory = {}

    def read(self, now=None):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path else dict(self._memory)
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict) or data.get("saved", 0) < daily.last_reset(now):
            return {"rooms": [], "spent": [], "spent_ts": None}   # students move at the daily reset
        rooms = [r for r in data.get("rooms") or [] if isinstance(r, str)]
        spent = [r for r in data.get("spent") or [] if isinstance(r, str)]
        return {"rooms": list(dict.fromkeys(rooms)), "spent": spent, "spent_ts": data.get("spent_ts")}

    def write(self, data, now=None):
        record = {**data, "saved": time.time() if now is None else now}
        if self.path is None:
            self._memory = record
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)


class Desk:
    """The Lessons page's data and actions, on top of a Store."""

    def __init__(self, store, book=None):
        self.store = store
        self.book = book or MarkBook()
        self._lock = threading.RLock()
        self._seen = (None, set())      # rooms seen spent for this scan, in case the status file is replaced

    # ---- reading ----
    def _spent(self, book, status, scan_ts):
        earlier = set(book["spent"]) if book.get("spent_ts") == scan_ts else set()
        seen = self._seen[1] if self._seen[0] == scan_ts else set()
        seen = seen | spent_rooms(status, scan_ts)
        self._seen = (scan_ts, seen)
        return earlier | seen

    def _tickets(self, status, scan_ts):
        """(owned, max). A run's own count is newer than the last passive reading of the screen."""
        owned, most = self.store.lesson_tickets()
        now = (status or {}).get("tickets_now")
        if (owned is not None and isinstance(now, int) and not isinstance(now, bool)
                and not (status or {}).get("rehearse") and status.get("scan_ts") == scan_ts):
            owned = min(owned, now)
        return owned, most

    def _gate(self, client):
        """Why no run can start right now, or None."""
        if TASK not in control.TASKS:
            return "The lesson runner isn't installed yet."
        if not client.get("ok"):
            return "The client isn't running."
        if client.get("halt"):
            return "Game input is halted."
        if client.get("busy"):
            run = client.get("run") or {}
            return f"Waiting for {run.get('label') or 'the current run'} to finish."
        return None

    def view(self, rev=None):
        with self._lock:
            scan = self.store.scan_data()
            status = self.store.raw_teach()
            client = self.store.client.status()
            tickets, ticket_max = self._tickets(status, scan.get("ts") or 0)
            book = self.book.read()
        ts = scan.get("ts") or 0
        table, twice = index(scan)
        spent = self._spent(book, status, ts)
        marks = [r for r in book["rooms"] if r in table and not why_not(r, table, twice, spent)]
        same_scan = bool(status) and status.get("scan_ts") == ts
        states = {r.get("id"): r for r in (status or {}).get("rooms") or []} if same_scan else {}
        run = client.get("run") or {}
        active = run.get("task") == TASK
        scanning = run.get("task") == "lesson-scan"
        rehearsing = bool(status and status.get("rehearse"))

        def decorate(room):
            rid = room["id"]
            entry = states.get(rid) or {}
            why = why_not(rid, table, twice, spent)
            shown_state = None if (rehearsing and not active) or rid in marks else entry.get("state")
            return {**room, "marked": rid in marks, "markable": why is None and not active, "why": why,
                    "state": shown_state if shown_state in ROOM_STATES else None, "note": entry.get("note")}

        live = atlas.live(scan, icon=_icon)
        locations = [{**loc, "rooms": [decorate(r) for r in loc["rooms"]]} for loc in live["locations"]]
        off_map = [{**loc, "rooms": [decorate(r) for r in loc["rooms"]]} for loc in live["off_map"]]

        gate = self._gate(client)
        blockers = ([gate] if gate else []) + (problems(scan, marks, tickets, spent) if marks else [])
        running_status = (status or {}).get("state") == "running"
        out = {
            "has_scan": bool(scan.get("locations")), "scanned_at": scan.get("scanned_at"), "ts": ts,
            "complete": bool(scan.get("complete")), "stale": bool(scan.get("locations")) and lessons.is_stale(scan),
            "read": len(scan.get("locations") or []), "scanning": scanning,
            "tickets": {"owned": tickets, "max": ticket_max}, "marks": marks,
            "can_teach": bool(marks) and not blockers, "blockers": blockers, "gate": gate,
            "runner": TASK in control.TASKS, "connected": bool(client.get("ok")),
            "teach": status if same_scan or active else None, "teach_active": active,
            "teach_interrupted": bool(running_status and client.get("ok") and not active),
            "locations": locations, "off_map": off_map,
        }
        out["rev"] = hashlib.sha1(json.dumps(out, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
        return {"same": True, "rev": rev} if rev and rev == out["rev"] else out

    # ---- changing marks ----
    def mark(self, rid, on):
        with self._lock:
            client = self.store.client.status()
            if (client.get("run") or {}).get("task") == TASK:
                return {"ok": False, "error": "Lessons are being taught. Marks are locked until it ends."}
            scan = self.store.scan_data()
            book = self.book.read()
            rooms = list(book["rooms"])
            if on and rid not in rooms:
                if scan.get("locations") and lessons.is_stale(scan):
                    return {"ok": False, "error": "The scan is from before today's reset. Scan again."}
                table, twice = index(scan)
                status = self.store.raw_teach()
                spent = self._spent(book, status, scan.get("ts") or 0)
                why = why_not(rid, table, twice, spent)
                if why:
                    return {"ok": False, "error": why}
                live = [r for r in rooms if r in table and not why_not(r, table, twice, spent)]
                tickets = self._tickets(status, scan.get("ts") or 0)[0]
                if tickets is not None and len(live) + 1 > tickets:
                    return {"ok": False, "error": f"You only have {tickets} tickets."}
                rooms = live + [rid]
            elif not on:
                rooms = [r for r in rooms if r != rid]
            self.book.write({**book, "rooms": rooms})
        return {"ok": True, "lessons": self.view()}

    def clear(self):
        with self._lock:
            client = self.store.client.status()
            if (client.get("run") or {}).get("task") == TASK:
                return {"ok": False, "error": "Lessons are being taught. Marks are locked until it ends."}
            self.book.write({**self.book.read(), "rooms": []})
        return {"ok": True, "lessons": self.view()}

    def remark_skipped(self):
        """Put a run's skipped rooms back on the marks, up to the ticket count. Starts nothing."""
        with self._lock:
            if (self.store.client.status().get("run") or {}).get("task") == TASK:
                return {"ok": False, "error": "Lessons are being taught. Marks are locked until it ends."}
            scan, status, book = self.store.scan_data(), self.store.raw_teach(), self.book.read()
            ts = scan.get("ts") or 0
            if not status or status.get("rehearse") or status.get("scan_ts") != ts:
                return {"ok": False, "error": "There are no skipped rooms to mark."}
            table, twice = index(scan)
            spent = self._spent(book, status, ts)
            tickets = self._tickets(status, ts)[0]
            rooms = [r for r in book["rooms"] if r in table and not why_not(r, table, twice, spent)]
            for entry in status.get("rooms") or []:
                rid = entry.get("id")
                if (entry.get("state") == "skipped" and rid not in rooms and not why_not(rid, table, twice, spent)
                        and (tickets is None or len(rooms) < tickets)):
                    rooms.append(rid)
            self.book.write({**book, "rooms": rooms})
        return {"ok": True, "lessons": self.view()}

    # ---- teaching ----
    def start(self, ctl, rooms, scan_ts, rehearse=False):
        """Start the runner with exactly the marked rooms. Refuses anything else before any input."""
        with self._lock:
            scan = self.store.scan_data()
            status = self.store.raw_teach()
            book = self.book.read()
            ts = scan.get("ts") or 0
            if scan_ts != ts:
                return {"ok": False, "error": "The scan changed. Check your marks and try again."}
            if sorted(rooms) != sorted(book["rooms"]) or len(set(rooms)) != len(rooms):
                return {"ok": False, "error": "The rooms sent don't match your marks."}
            gate = self._gate(self.store.client.status())
            if gate:
                return {"ok": False, "error": gate}
            spent = self._spent(book, status, ts)
            blocked = problems(scan, rooms, self._tickets(status, ts)[0], spent)
            if blocked:
                return {"ok": False, "error": blocked[0], "blockers": blocked}
            table, _ = index(scan)
            order = [r for r in table if r in set(rooms)]     # the game's own location order
            reply = ctl.handle({"op": "run", "task": TASK, "actor": "user",
                                "args": {"rooms": order, "scan_ts": ts, "rehearse": bool(rehearse)}})
            if reply.get("ok"):                               # the run replaces the status file, so keep what's spent
                self.book.write({"rooms": book["rooms"] if rehearse else [], "spent": sorted(spent), "spent_ts": ts})
            return reply


def _icon(dev):
    urls = art.urls(dev) if dev else {}
    return urls.get("icon") or urls.get("small") or urls.get("portrait")
