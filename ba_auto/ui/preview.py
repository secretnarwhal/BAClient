"""Preview the panel in a local browser without opening a client or touching the game.

  python -m ba_auto.ui.preview real            your real data, read-only, commands off
  python -m ba_auto.ui.preview sample          invented data, under a clear SAMPLE banner
  python -m ba_auto.ui.preview overlay OUT.png the overlay card on saved game frames
  python -m ba_auto.ui.preview shots [DIR]     a PNG of every screen, rendered on a hidden desktop
  python -m ba_auto.ui.preview atlas           the Lessons page with sample data and a stand-in runner

Sample data exists only here. The real client (`ui.app`) can't load it. The PNGs contain game art,
so they stay on this PC like assets/game/.
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import art
from .server import PanelServer
from .state import CtlClient, Store

SCREENS = ("home", "students", "team", "run", "journal", "battles", "lessons")


def _launcher(stage, **extra):
    return {"stage": stage, "reason": None, "needs_user": False, "game_running": stage not in ("not_running", "launching"),
            "auto_login": True, "idle_seconds": None, "idle_needed": None, **extra}


# Every state the launcher card can show, for checking its layout.
LAUNCH_STATES = {
    "not_running": (_launcher("not_running"), None),
    "launching": (_launcher("launching"), None),
    "loading": (_launcher("loading"), None),
    "title": (_launcher("title"), None),
    "title_auto_off": (_launcher("title", auto_login=False), None),
    "waiting_idle": (_launcher("waiting_idle", idle_seconds=12, idle_needed=30), None),
    "logging_in": (_launcher("logging_in"), None),
    "lobby": (_launcher("lobby"), None),
    "stopped": (_launcher("stopped", game_running=False, reason="The title screen didn't appear within 3 minutes."), None),
    "waiting_front": (_launcher("waiting_idle", idle_seconds=5, idle_needed=5), None),
    "duplicate": (_launcher("title"), {"reason": "game says: Duplicate login detected", "time": "2000-01-01 14:03:00"},
                  {"reason": "game says: Duplicate login detected"}),
    "duplicate_hold": (_launcher("stopped", needs_user=True, reason="Duplicate login: auto login is off until the user decides."),
                       None, {"reason": "game says: Duplicate login detected"}),
    "session_hold": (_launcher("stopped", needs_user=True, reason="Duplicate login: auto login is off until the user decides."),
                     None, {"reason": "game says: Returning to title"}),
}


class SampleClient:
    def __init__(self, team, launch=None):
        self._team = team
        self._launch = launch
        self.demo_run = None

    def status(self):
        if self._launch:
            launcher, halt = self._launch[:2]
            screen = {"lobby": "lobby", "title": "title", "not_running": "game not found"}.get(launcher["stage"], "unknown")
            return {"ok": True, "busy": bool(self.demo_run) or launcher["stage"] in ("launching", "loading", "waiting_idle", "logging_in"),
                    "run": self.demo_run, "last": None, "screen": screen, "halt": halt, "launcher": launcher,
                    "resources": {"ap": "142/206", "credits": 12480300, "pyroxene": 1260}, "log": []}
        run = {"id": "sample", "task": "ministories", "label": "Clear all mini stories", "actor": "user",
               "started": "14:02:11", "seconds": 42.0, "outcome": None, "stats": {"chapters": 3, "episodes": 8}}
        return {"ok": True, "busy": True, "run": run, "last": None, "screen": "ministory_list", "halt": None,
                "resources": {"ap": "142/206", "credits": 12480300, "pyroxene": 1260},
                "launcher": _launcher("running"),
                "log": ["14:02:11  start: Clear all mini stories (user)"]}

    def team(self):
        return self._team

    def overlay(self):
        return True


class SampleStore(Store):
    """Invented values for checking layouts. Never writes to data/."""
    sample = True

    def __init__(self, launch=None):
        rng = random.Random(17)
        people = [s for s in art.catalog() if not s["name"].startswith("CH")][:36]
        self._students = [{
            "name": p["name"], "level": rng.randint(35, 80), "stars": rng.randint(1, 5), "bond": rng.randint(1, 20),
            "class": rng.choice(("Striker", "Striker", "Special")), "role": rng.choice(("Tank", "Dealer", "Healer", "Support")),
            "position": rng.choice(("Front", "Middle", "Back")), "attack": rng.choice(("Explosive", "Piercing", "Mystic", "Sonic")),
            "armor": rng.choice(("Light", "Heavy", "Special", "Elastic")), "weapon_type": rng.choice(("SG", "SMG", "AR", "HG", "SR")),
            "stats": {"hp": rng.randint(9000, 40000), "atk": rng.randint(900, 4000), "def": rng.randint(50, 900),
                      "healing": rng.randint(1000, 8000)},
            "skills": {"ex": rng.randint(1, 5), "basic": rng.randint(1, 10)}, "unread": [],
        } for p in people]
        self._team = [s["name"] for s in self._students[3:9]]
        self._featured = "follow"
        names = [s["name"] for s in self._students]
        self._battles = [{"when": f"2026-11-{20 - i // 3:02d} {13 - i % 6:02d}:{(i * 7) % 60:02d}:00",
                          "outcome": "won" if i % 4 else "defeat", "boss": ("Hieronymus", "Binah", "Kaiten FX", None)[i % 4],
                          "time": f"0{i % 3 + 1}:{(i * 11) % 60:02d}.{(i * 37) % 1000:03d}",
                          "damage": 900000 + i * 73310 if i % 5 else None,
                          "team": rng.sample(names, 6)} for i in range(14)][::-1]
        rooms = ("Theater Room", "Gym", "Library", "Classroom", "Laboratory")
        self._lessons = {"scanned_at": "2000-01-01 13:40:02", "complete": True, "ts": time.time(), "locations": [
            {"location": loc, "rank": rng.randint(3, 9), "rooms": [
                {"room": room, "locked": rng.random() < .15, "students": [
                    {"student": n, "dev": art.resolve(n), "owned": rng.random() < .7, "bond": rng.randint(1, 20)}
                    for n in rng.sample(names, rng.randint(0, 3))]} for room in rooms]}
            for loc in ("Schale Office", "Gehenna Hub", "Trinity Plaza Area", "Millennium Study Center")]}
        self._hold = launch[2] if launch and len(launch) > 2 else None
        self._teach = None
        super().__init__(SampleClient(self._team, launch))

    def raw_roster(self):
        return {"scanned_at": "2000-01-01 13:31:44", "complete": True, "students": self._students}

    def raw_battles(self):
        return self._battles

    def raw_lessons(self):
        return self._lessons

    def raw_daily(self):
        now = time.time()
        return {"mail": {"at": now, "mail": 7}, "cafe": {"at": now, "cafe_percent": 62.5},
                "tasks": {"at": now, "daily_done": 5, "daily_goal": 8},
                "lesson": {"at": now, "lesson_tickets": 3, "lesson_max": 6}}

    def login_hold(self):
        return self._hold

    def raw_teach(self):
        return self._teach

    def prefs(self):
        return {"featured": self._featured}

    def set_featured(self, value):
        if value != "follow" and not art.known(value):
            raise ValueError("unknown student")
        self._featured = value

    def journal(self, day=None):
        kinds = [("play", "run start: Clear all mini stories", "run"), ("pointer", "click (1170, 430)", "click"),
                 ("pointer", "click (1868, 538)", "click"), ("check", "run done: Scan missions", "run"),
                 ("swords", "battle won", "battle"), ("alert", "error in Daily scan", "error")]
        rows = [{"time": f"14:{59 - i:02d}:{(i * 13) % 60:02d}", "actor": "user", "kind": k, "icon": ic, "text": text,
                 "path": "campaign -> story_hub" if k == "click" else "", "frame": None}
                for i, (ic, text, k) in enumerate(kinds * 5)]
        return {"day": "2000-01-01", "days": ["2000-01-01"], "rows": rows}


class FullSampleStore(SampleStore):
    """Every student in the art pack with invented values, for measuring the Students page at full size.
    `count` repeats them to stress the grid well past the real roster."""

    def __init__(self, count=None, partial=False, evil=False):
        super().__init__(LAUNCH_STATES["lobby"])
        rng = random.Random(29)
        self.count = count
        self._stress = None
        people = art.catalog()
        if partial:
            people = people[::3]
        self._students = [{
            "name": p["name"], "level": rng.randint(1, 90), "stars": rng.randint(1, 5), "bond": rng.randint(1, 30),
            "class": rng.choice(("Striker", "Striker", "Special")), "role": rng.choice(("Tank", "Dealer", "Healer", "Support", "T.S.")),
            "position": rng.choice(("Front", "Middle", "Back")), "attack": rng.choice(("Explosive", "Piercing", "Mystic", "Sonic")),
            "armor": rng.choice(("Light", "Heavy", "Special", "Elastic")), "weapon_type": rng.choice(("SG", "SMG", "AR", "HG", "SR", "GL")),
            "terrain": {"urban": rng.choice(("green", "yellow", "red")), "outdoor": rng.choice(("green", "yellow", "red")),
                        "indoor": rng.choice(("green", "yellow", None))},
            "stats": {"hp": rng.randint(9000, 60000), "atk": rng.randint(900, 6000), "def": rng.randint(50, 2000),
                      "healing": rng.choice((rng.randint(1000, 8000), None))},
            "skills": {"ex": rng.randint(1, 5), "basic": rng.randint(1, 10), "enhanced": rng.randint(1, 10), "sub": rng.choice((None, 4, 7))},
            "gear": [rng.choice((None, 3, 6, 8)) for _ in range(3)], "weapon_locked": rng.random() < .4,
            "scanned_at": "2000-01-01 13:31:44", "unread": [],
        } for p in people]
        if evil:                        # a hostile name must only ever appear as text
            self._students[0] = {**self._students[0], "name": '<img src=x onerror="window.pwned=1">', "level": 90}
        self._files.clear()

    def raw_roster(self):
        return {"scanned_at": "2000-01-01 13:31:44", "complete": not self.count and len(self._students) >= len(art.catalog()),
                "students": self._students}

    def students(self, rev=None):
        data = super().students()
        if not self.count:
            return {"same": True, "rev": data["rev"]} if rev == data["rev"] else data
        if self._stress is None:
            rows = [{**s, "id": f"{s['id']}#{n}", "name": f"{s['name']} {n}" if n else s["name"]}
                    for n in range(self.count // len(data["students"]) + 1) for s in data["students"]][:self.count]
            self._stress = {**data, "students": rows, "counts": {**data["counts"], "all": len(rows), "yes": len(rows), "read": len(rows)}}
        return {"same": True, "rev": data["rev"]} if rev == data["rev"] else self._stress


class DemoTeach:
    """Stands in for the lesson runner in the `atlas` preview. Only changes in-memory status."""

    def __init__(self, store, step=1.1):
        self.store, self.step = store, step

    def teach(self, rooms, scan_ts, rehearse=False, confirmed=False):
        if not rehearse and confirmed is not True:
            return {"ok": False, "confirm": True, "error": "Confirm before spending tickets"}
        return self.store.desk.start(self, rooms, scan_ts, rehearse=rehearse)

    def handle(self, req):
        args = req["args"]
        rooms = args["rooms"][:1] if args["rehearse"] else args["rooms"]
        status = {"run_id": "preview", "state": "running", "scan_ts": args["scan_ts"], "tickets_start": 3, "tickets_now": 3,
                  "rooms": [{"id": r, "state": "queued", "note": None} for r in rooms]}
        if args["rehearse"]:
            status["rehearse"] = True
        self.store._teach = status
        self.store.client.demo_run = {"id": "preview", "task": "lesson-teach", "label": "Teach lessons", "actor": "user",
                                      "started": time.strftime("%H:%M:%S"), "stats": {}}
        threading.Thread(target=self._walk, args=(status, bool(args["rehearse"])), daemon=True).start()
        return {"ok": True, "id": "preview", "task": "lesson-teach"}

    def _walk(self, status, rehearse):
        for room in status["rooms"]:
            for state in ("opening", "teaching", "done"):
                room["state"] = state
                time.sleep(self.step)
            if not rehearse:
                status["tickets_now"] -= 1
        status["state"] = "done"
        self.store.client.demo_run = None

    def _off(self, *a, **k):
        return {"ok": False, "error": "This is a preview: commands are off"}

    run = stop = open = overlay = auto_login = quit = _off


def atlas_store(marks=(), teach_state=None):
    """A sample store sitting idle in the lobby, for the Lessons page."""
    store = SampleStore(LAUNCH_STATES["lobby"])
    if marks:
        store.desk.book.write({"rooms": list(marks), "spent": [], "spent_ts": None})
    if teach_state:
        ts = store._lessons["ts"]
        store._teach = {"run_id": "sample", "state": teach_state[0], "scan_ts": ts, "tickets_start": 3, "tickets_now": teach_state[1],
                        "rooms": [{"id": i, "state": st, "note": note} for i, st, note in teach_state[2]]}
        if teach_state[0] == "failed":
            store._teach["error"] = "The dialog's students didn't match the scan"
    return store


def sample_rooms(store, count=3):
    """Room ids with students, one per location first, for previews."""
    out = []
    for loc in store._lessons["locations"]:
        for room in loc["rooms"]:
            if room["students"] and not room["locked"]:
                out.append(f"{loc['location']}|{room['room']}")
                break
    return out[:count]


def render_atlas_states(out):
    """The Lessons page: empty, marked, and mid-run."""
    from .. import control
    from .app import CHROME
    exe = next((p for p in CHROME if p.exists()), None)
    if exe is None:
        raise SystemExit("Chrome not found")
    added = "lesson-teach" not in control.TASKS
    if added:
        control.TASKS["lesson-teach"] = ("Teach lessons", "preview")
    base = SampleStore(LAUNCH_STATES["lobby"])
    picks = sample_rooms(base)
    run = ("running", 2, [(picks[0], "done", None), (picks[1], "teaching", None), (picks[2], "queued", None)])
    failed = ("failed", 2, [(picks[0], "done", None), (picks[1], "failed", "No ticket spent: the dialog's students didn't match the scan"), (picks[2], "skipped", "Not started")])
    stores = {"lessons_none_marked": atlas_store(), "lessons_marked": atlas_store(picks),
              "lessons_teaching": atlas_store(teach_state=run), "lessons_failed": atlas_store(teach_state=failed)}
    stores["lessons_teaching"].client.demo_run = {"id": "s", "task": "lesson-teach", "label": "Teach lessons", "actor": "user",
                                                  "started": "14:02:11", "stats": {}}
    made = []
    try:
        with tempfile.TemporaryDirectory(prefix="ba_ui_atlas_") as tmp:
            for name, store in stores.items():
                panel = PanelServer(store).start()
                try:
                    path = Path(out) / f"{name}.png"
                    _shot(exe, Path(tmp) / "profile", f"{panel.url}&to=lessons", path, budget=12000)
                    made.append(path)
                    print(path, flush=True)
                finally:
                    panel.close()
    finally:
        if added:
            control.TASKS.pop("lesson-teach", None)
    return made


def overlay_sheet(out):
    """The real card renderer at its real slots on saved frames."""
    import cv2
    import numpy as np
    from PIL import Image

    from .. import journal, viewport
    from .overlay import card_info, render_card
    from .slots import card_rect
    shots = (("lobby", "lobby.png", "running", "Clear all mini stories"), ("mission", "campaign__mission.png", "idle", "BAClient idle"),
             ("cafe", "cafe.png", "halted", "Halted: pyroxene dropped"), ("tasks", "tasks.png", "running", "Daily scan"))
    team = [art.person(n) for n in ("Koharu", "Eimi", "Chise")]
    tiles = []
    for screen, file, state, title in shots:
        frame = cv2.imread(str(journal.ROOT / "ui-map" / file))
        base = Image.fromarray(frame[:, :, ::-1]).convert("RGBA")
        x, y, w, h = card_rect(screen, viewport.from_rect(0, 0, 1920, 1080))
        base.alpha_composite(render_card(card_info((state, title), screen, team), w, h), (x, y))
        tiles.append(np.asarray(base.convert("RGB").resize((960, 540), Image.LANCZOS)))
    sheet = np.vstack((np.hstack(tiles[:2]), np.hstack(tiles[2:])))
    Image.fromarray(sheet).save(out)
    return out


class _StartupInfo(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR), ("lpTitle", wt.LPWSTR),
                ("dwX", wt.DWORD), ("dwY", wt.DWORD), ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
                ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD), ("dwFillAttribute", wt.DWORD),
                ("dwFlags", wt.DWORD), ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", wt.HANDLE), ("hStdOutput", wt.HANDLE),
                ("hStdError", wt.HANDLE)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE), ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]


def _run_on_hidden_desktop(args, timeout=45):
    """Run a process on a private desktop, so no window can reach a window manager or take focus."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.CreateDesktopW.restype = wt.HANDLE
    user32.CreateDesktopW.argtypes = (wt.LPCWSTR, wt.LPCWSTR, ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.c_void_p)
    user32.CloseDesktop.argtypes = (wt.HANDLE,)
    kernel32.CreateProcessW.argtypes = (wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wt.BOOL, wt.DWORD,
                                        ctypes.c_void_p, wt.LPCWSTR, ctypes.POINTER(_StartupInfo),
                                        ctypes.POINTER(_ProcessInfo))
    kernel32.WaitForSingleObject.argtypes = (wt.HANDLE, wt.DWORD)
    kernel32.TerminateProcess.argtypes = (wt.HANDLE, wt.UINT)
    kernel32.CloseHandle.argtypes = (wt.HANDLE,)
    name = f"BAClientShots{os.getpid()}"
    desk = user32.CreateDesktopW(name, None, None, 0, 0x10000000, None)
    if not desk:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        si = _StartupInfo(cb=ctypes.sizeof(_StartupInfo), lpDesktop=name)
        pi = _ProcessInfo()
        line = ctypes.create_unicode_buffer(subprocess.list2cmdline(args))
        if not kernel32.CreateProcessW(None, line, None, None, False, 0x08000000, None, None,
                                       ctypes.byref(si), ctypes.byref(pi)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if kernel32.WaitForSingleObject(pi.hProcess, int(timeout * 1000)) != 0:
                kernel32.TerminateProcess(pi.hProcess, 1)
                raise TimeoutError("the renderer didn't finish")
        finally:
            kernel32.CloseHandle(pi.hProcess)
            kernel32.CloseHandle(pi.hThread)
    finally:
        user32.CloseDesktop(desk)


def _shot(exe, profile, url, path, budget=6000):
    _run_on_hidden_desktop([str(exe), "--headless=new", "--disable-gpu", "--hide-scrollbars",
                            "--no-first-run", "--no-default-browser-check", "--disable-extensions",
                            f"--user-data-dir={profile}", "--force-device-scale-factor=1",
                            "--window-size=1500,940", f"--virtual-time-budget={budget}",
                            f"--screenshot={path}", url])


def render_launcher_sheet(out):
    """The launcher card in every state, cropped from real renders of the dashboard."""
    from PIL import Image, ImageDraw

    from .app import CHROME
    exe = next((p for p in CHROME if p.exists()), None)
    if exe is None:
        raise SystemExit("Chrome not found")
    crops = []
    from .. import control
    added = "launch" not in control.TASKS       # this process only, so the enabled button can be seen
    if added:
        control.TASKS["launch"] = ("Launch Blue Archive", "preview")
    with tempfile.TemporaryDirectory(prefix="ba_ui_launch_") as tmp:
        for name, launch in LAUNCH_STATES.items():
            panel = PanelServer(SampleStore(launch)).start()
            try:
                path = Path(tmp) / f"{name}.png"
                _shot(exe, Path(tmp) / "profile", f"{panel.url}&to=home", path)
            finally:
                panel.close()
            if name in ("not_running", "duplicate", "duplicate_hold", "session_hold"):
                Image.open(path).save(Path(out).with_name(f"launcher_{name}.png"))
            top = 196 if name == "duplicate" else 188 if name.endswith("_hold") else 124
            crop = Image.open(path).convert("RGB").crop((805, top, 1488, top + 150))
            ImageDraw.Draw(crop).text((6, 2), name, fill=(200, 0, 120))
            crops.append(crop)
    if added:
        control.TASKS.pop("launch", None)
    w, h = crops[0].size
    sheet = Image.new("RGB", (w * 2 + 10, (h + 8) * ((len(crops) + 1) // 2)), (232, 243, 252))
    for i, crop in enumerate(crops):
        sheet.paste(crop, ((w + 10) * (i % 2), (h + 8) * (i // 2)))
    sheet.save(out)
    return out


def render_shots(out):
    """A PNG of every screen, with sample data and with the real data on this PC."""
    from .app import CHROME
    exe = next((p for p in CHROME if p.exists()), None)
    if exe is None:
        raise SystemExit("Chrome not found")
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    made = []
    with tempfile.TemporaryDirectory(prefix="ba_ui_shots_") as profile:
        for label, store, routes in (("sample", SampleStore(), SCREENS), ("real", Store(CtlClient()), SCREENS)):
            panel = PanelServer(store).start()
            try:
                for route in routes:
                    path = out / f"{label}_{route}.png"
                    _shot(exe, profile, f"{panel.url}&to={route}", path)
                    made.append(path)
                    print(path, flush=True)
            finally:
                panel.close()
    made.append(Path(overlay_sheet(out / "overlay.png")))
    made.append(Path(render_launcher_sheet(out / "launcher_states.png")))
    made += render_atlas_states(out)
    return made


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ba_auto.ui.preview")
    ap.add_argument("mode", choices=("real", "sample", "overlay", "shots", "atlas", "atlas-shots"))
    ap.add_argument("out", nargs="?")
    a = ap.parse_args(argv)
    if a.mode == "overlay":
        print(overlay_sheet(a.out or "overlay_preview.png"))
        return 0
    if a.mode == "shots":
        render_shots(a.out or Path(__file__).with_name("previews"))
        return 0
    if a.mode == "atlas-shots":
        render_atlas_states(a.out or Path(__file__).with_name("previews"))
        return 0
    if a.mode == "atlas":                       # sample data with a stand-in runner, so the whole flow can be clicked
        from .. import control
        control.TASKS.setdefault("lesson-teach", ("Teach lessons", "preview"))
        store = atlas_store()
        panel = PanelServer(store, DemoTeach(store)).start()
    else:
        store = SampleStore() if a.mode == "sample" else Store(CtlClient())
        panel = PanelServer(store).start()
    print(panel.url, flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        panel.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
