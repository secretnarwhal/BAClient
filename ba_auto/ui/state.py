"""Read-only view of the client's real data for the panel.

Nothing here touches the game, and nothing is invented: a value that wasn't read is None and the
panel shows it as unknown. Files are re-read only when they change, so polling stays cheap and
never competes with capture or matching.
"""
import json
import os
import threading
import time

from .. import battlelog, control, daily, journal, watchdog
from ..tasks import lessons, roster
from . import art, commands, controls, students as student_list, teach

PREFS = journal.ROOT / "data" / "ui" / "prefs.json"
JOURNAL_ROWS = 300
JOURNAL_KEEP = 4000

# Tasks the panel may start, with how it presents them. A task missing here stays ctl-only, so a
# new task (or one needing arguments, like `sweep` and `ex`) never becomes a one-click button by
# accident. `spends` makes the panel ask first and the server refuse an unconfirmed request.
PANEL_TASKS = {
    "lobby": {"group": "Navigate", "icon": "home"},
    "skip": {"group": "Story", "icon": "skip"},
    "episodes": {"group": "Story", "icon": "book"},
    "ministories": {"group": "Story", "icon": "sparkle"},
    "battle": {"group": "Battle", "icon": "swords", "spends": "Starting a fight can use AP."},
    # The panel always starts tasks with no options, which for these four means a dry run. Their
    # real modes (--go, --live, conquest-step) and MomoTalk's replies can't be undone or spend
    # something, so they stay in `ctl` until each has passed its live test.
    "lesson-run": {"group": "Plans", "icon": "book", "label": "Plan lessons",
                   "desc": "List the rooms it would pick. Doesn't touch the game or spend tickets."},
    "claims": {"group": "Plans", "icon": "check", "label": "Preview claims",
               "desc": "Open mail and tasks and list what could be claimed. Claims nothing."},
    "cafe": {"group": "Plans", "icon": "sparkle", "label": "Preview cafe",
             "desc": "Open the cafe and check earnings, headpats and invites. Claims and pats nothing."},
    "conquest": {"group": "Plans", "icon": "map", "label": "Plan Conquest",
                 "desc": "Read the map on screen and show the plan and AP cost. No clicks."},
    "daily-scan": {"group": "Read only", "icon": "list"},
    "roster-scan": {"group": "Read only", "icon": "students"},
    "lesson-scan": {"group": "Read only", "icon": "pin"},
    "sweep-scan": {"group": "Read only", "icon": "radar"},
    "launch": {"group": "Launcher", "icon": "power", "dashboard": True},    # has its own card on Home
}

LAUNCH_STAGES = ("not_running", "launching", "loading", "title", "waiting_idle", "logging_in", "lobby", "running",
                 "stopped")


def _flag(value):
    return value if isinstance(value, bool) else None


def _whole(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _stage_from_screen(screen):
    """What the screen name alone proves, for a client that doesn't report a launcher yet."""
    if screen in (None, "", "-"):
        return None, None
    if screen == "game not found":
        return "not_running", False
    base = str(screen).split("+")[0]
    return (base if base in ("title", "lobby") else "running"), True


def launcher_state(status, hold=None):
    """The launcher card's data. Only the client's own report can enable Launch or the toggle.
    `hold` is the watchdog's login hold, which names what the game really said."""
    connected = bool(status.get("ok"))
    halt = status.get("halt") or None
    reason = str((halt or {}).get("reason") or "")
    raw = status.get("launcher")
    reported = isinstance(raw, dict) and raw.get("stage") in LAUNCH_STAGES
    if reported:
        out = {"stage": raw["stage"], "reason": raw.get("reason"), "needs_user": raw.get("needs_user") is True,
               "game_running": _flag(raw.get("game_running")), "auto_login": _flag(raw.get("auto_login")),
               "idle_seconds": _whole(raw.get("idle_seconds")), "idle_needed": _whole(raw.get("idle_needed"))}
    else:
        stage, running = _stage_from_screen(status.get("screen")) if connected else (None, None)
        out = {"stage": stage, "reason": None, "needs_user": False, "game_running": running, "auto_login": None,
               "idle_seconds": None, "idle_needed": None}
    hold_reason = str((hold or {}).get("reason") or "")
    reported_hold = out["needs_user"] and "duplicate login" in str(out["reason"] or "").lower()
    in_halt = "duplicate login" in reason.lower()
    out["login_hold"] = bool(hold) or (reported_hold and hold is None)
    out["duplicate"] = in_halt or "duplicate login" in (hold_reason if hold else str(out["reason"] or "") if reported_hold else "").lower()
    if in_halt:
        out.update(stage="stopped", reason=reason, needs_user=True)
    elif hold and out["stage"] == "stopped":
        out.update(reason=hold_reason or out["reason"], needs_user=True)
    out["reported"] = reported
    out["available"] = "launch" in control.TASKS
    out["can_launch"] = bool(out["available"] and reported and connected and not status.get("busy") and not halt
                             and out["game_running"] is False and not out["needs_user"] and not out["login_hold"]
                             and out["stage"] in ("not_running", "stopped"))
    return out


def _stamp(*paths):
    out = []
    for p in paths:
        try:
            s = os.stat(p)
            out.append((s.st_mtime_ns, s.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


class Store:
    """Real data from data/ and logs/. `client` supplies live status (see LiveClient, CtlClient)."""
    sample = False

    def __init__(self, client):
        self.client = client
        self._lock = threading.Lock()
        self._files = {}
        self._journal = {}
        self.desk = teach.Desk(self, teach.MarkBook(None) if self.sample else None)

    def _cached(self, name, paths, build):
        key = _stamp(*paths)
        with self._lock:
            hit = self._files.get(name)
            if hit and hit[0] == key:
                return hit[1]
        value = build()
        with self._lock:
            self._files[name] = (key, value)
        return value

    # ---- preferences (only ever the featured student) ----
    def prefs(self):
        try:
            data = json.loads(PREFS.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def set_featured(self, value):
        if value != "follow" and not art.known(value):
            raise ValueError("unknown student")
        PREFS.parent.mkdir(parents=True, exist_ok=True)
        tmp = PREFS.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({**self.prefs(), "featured": value}), encoding="utf-8")
        os.replace(tmp, PREFS)

    # ---- files ----
    def raw_roster(self):
        return roster.load()

    def raw_battles(self):
        return battlelog.load()

    def raw_lessons(self):
        return lessons.load()

    def raw_daily(self):
        return daily.load()

    def roster(self):
        def build():
            try:
                data = self.raw_roster()
            except (OSError, ValueError):
                return {"scanned_at": None, "complete": False, "students": [], "error": "roster.json couldn't be read"}
            students = [{**s, **{k: v for k, v in art.person(s.get("name")).items() if k != "name"}}
                        for s in data.get("students", [])]
            return {"scanned_at": data.get("scanned_at"), "complete": bool(data.get("complete")), "students": students}
        return self._cached("roster", (roster.ROSTER, art.portraits.NAMES), build)

    def students(self, rev=None):
        """The Students page's list. A caller that already has this revision gets a tiny reply."""
        paths = (roster.ROSTER, lessons.DATA, art.portraits.NAMES, art.ART / "manifest.json")
        data = self._cached("students", paths, lambda: student_list.build(self.roster(), self.scan_data()))
        return {"same": True, "rev": data["rev"]} if rev == data["rev"] else data

    # ---- the Run page's controls ----
    def commands(self):
        return commands.describe()

    def halt_view(self):
        return controls.halt_view(self.client.status(), self.login_hold())

    def halt_frame(self):
        return controls.frame_file(self.client.status().get("halt"))

    def guard(self):
        return controls.guard_state(self.client.status())

    def client_choice(self):
        from .. import client as client_choice
        return client_choice.choice()

    def raw_sweep(self):
        from ..tasks import sweeper
        return sweeper.load()

    def sweep(self):
        """The last mission scan, for the sweep picker. Nothing here is estimated."""
        from ..tasks import sweeper
        try:
            data = self.raw_sweep() or {}
        except (OSError, ValueError):
            return {"rows": [], "ap": None, "error": "the mission scan couldn't be read"}
        rows = []
        for r in data.get("rows") or []:
            try:
                cost = sweeper.cost(r["key"])
            except (KeyError, ValueError, TypeError):
                cost = None
            hard = r.get("kind") == "Hard"
            rows.append({"key": r.get("key"), "kind": r.get("kind"), "status": r.get("status"), "remaining": r.get("remaining"),
                         "cost": cost, "max": (r.get("remaining") or 0) if hard else control.MAX_SWEEPS})
        return {"rows": rows, "ap": data.get("ap"), "scanned_at": data.get("scanned_at")}

    def battles(self):
        def build():
            rows = self.raw_battles()
            won = sum(r.get("outcome") == "won" for r in rows)
            shown = [{**r, "team": [art.person(n) for n in r.get("team") or []]} for r in rows[-200:]]
            return {"total": len(rows), "won": won, "rows": shown[::-1]}
        return self._cached("battles", (battlelog.LOG, art.portraits.NAMES), build)

    def scan_data(self):
        """The last lesson scan, re-read only when it changes."""
        def build():
            try:
                return self.raw_lessons()
            except (OSError, ValueError, KeyError):
                return {"scanned_at": None, "ts": 0, "complete": False, "locations": []}
        return self._cached("scan", (lessons.DATA, roster.ROSTER), build)

    def raw_teach(self):
        return teach.read_status()

    def lesson_tickets(self):
        """(tickets owned, maximum) from today's reading, or (None, None)."""
        row = self.raw_daily().get("lesson") or {}
        if row.get("at", 0) < daily.last_reset():
            return None, None
        return row.get("lesson_tickets"), row.get("lesson_max")

    def lessons(self, rev=None):
        return self.desk.view(rev)

    def advise(self, armor=None, attack=None):
        """The team advisor's suggestion. It recommends only and never changes a team."""
        from .. import advisor
        students = self.roster()["students"]
        if not students:
            return {"team": [], "notes": [], "empty": True}
        result = advisor.advise(students, armor, attack)
        result["team"] = [{**r, "art": art.person(r["name"])["art"]} for r in result["team"]]
        return {"team": result["team"], "notes": result["notes"], "enemy": result["enemy"]}

    def todo(self, ap=None):
        return daily.plan(data=self.raw_daily(), ap=ap)

    def tasks(self):
        rows, ctl_only = [], []
        for name, (label, desc) in control.TASKS.items():
            meta = PANEL_TASKS.get(name)
            if name == teach.TASK or (meta and meta.get("dashboard")):
                continue                # these have their own controls
            if meta:
                rows.append({"name": name, "label": label, "desc": desc, **meta})  # meta can rename a dry run
            else:
                ctl_only.append({"name": name, "label": label, "desc": desc})
        return {"tasks": rows, "ctl_only": ctl_only}

    def days(self):
        return journal.days()

    def journal(self, day=None):
        day = day or next(iter(journal.days()), None)
        if day is None:
            return {"day": None, "days": [], "rows": []}
        with self._lock:
            offset, events = self._journal.get(day, (0, []))
            fresh, offset = journal.read_new(day, offset)
            events = (events + fresh)[-JOURNAL_KEEP:]
            self._journal[day] = (offset, events)
        rows = journal.timeline(events)[-JOURNAL_ROWS:]
        return {"day": day, "days": journal.days()[:14], "rows": [_journal_row(day, r) for r in reversed(rows)]}

    # ---- team and featured student ----
    def team(self, live_names=None):
        names = [n for n in (live_names or []) if n]
        source, when = "formation" if names else None, None
        if not names:
            for r in reversed(self.raw_battles()):
                if r.get("team"):
                    names, source, when = list(r["team"]), "battle", r.get("when")
                    break
        return {"source": source, "when": when, "members": [art.person(n) for n in names]}

    def featured(self, team):
        choice = self.prefs().get("featured") or "follow"
        if choice != "follow" and art.known(choice):
            return {"mode": "chosen", **art.person(dev=choice)}
        leader = next((m for m in team["members"] if m["art"].get("portrait")), None)
        if leader:
            return {"mode": "leader", **leader}
        return {"mode": "follow", "name": None, "dev": None, "art": {}}

    def login_hold(self):
        try:
            return watchdog.login_hold()
        except (OSError, ValueError):
            return {"reason": "the login hold file couldn't be read"}

    # ---- the once-a-second payload ----
    def state(self):
        status = self.client.status()
        res = status.get("resources") or {}
        team = self.team(self.client.team())
        return {
            "api": 2,                   # this server has the command, halt and client controls
            "sample": self.sample,
            "connected": bool(status.get("ok")),
            "busy": bool(status.get("busy")),
            "run": status.get("run"),
            "last": status.get("last"),
            "screen": status.get("screen"),
            "halt": status.get("halt"),
            "log": status.get("log") or [],
            "resources": {k: res.get(k) for k in ("ap", "credits", "pyroxene")},
            "todo": self.todo(res.get("ap")),
            "team": team,
            "featured": self.featured(team),
            "overlay": self.client.overlay(),
            "launcher": launcher_state(status, self.login_hold()),
            "now": time.strftime("%H:%M:%S"),
        }


def _journal_row(day, r):
    kind = r.get("kind")
    icon = {"click": "pointer", "drag": "drag", "key": "key", "run": "play", "error": "alert",
            "battle": "swords"}.get(kind, "dot")
    if kind == "run" and r.get("phase") == "end":
        icon = {"done": "check", "stopped": "stop", "error": "alert"}.get(r.get("outcome"), "play")
    frame = r.get("frame")
    return {"time": r.get("time"), "actor": r.get("actor"), "kind": kind, "icon": icon, "task": r.get("task"),
            "text": journal.describe(r), "path": journal.path_text(r),
            "outcome": r.get("outcome"), "frame": f"/frame/{day}/{frame}" if frame else None}


class LiveClient:
    """Status straight from the HUD this panel runs in."""

    def __init__(self, hud):
        self.hud = hud

    def status(self):
        status = self.hud.ctl.handle({"op": "status"})
        for key in ("run", "last"):     # only runs the panel itself started as a dry run are renamed
            run = status.get(key)
            if run and run.get("id") in self.hud.panel_labels:
                status[key] = {**run, "label": self.hud.panel_labels[run["id"]]}
        return status

    def team(self):
        return list(self.hud.battles.team)

    def overlay(self):
        return self.hud.overlay_on


class CtlClient:
    """Status from the running client over `ctl`. Read-only, used by the preview."""

    def status(self):
        try:
            return control.call("status", timeout=0.5)
        except Exception:
            return {"ok": False}

    def team(self):
        return []

    def overlay(self):
        return None
