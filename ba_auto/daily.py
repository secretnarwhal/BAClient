"""Daily planner: what's left today, read from the game's own screens.

The HUD updates data/daily.json whenever play passes one of these screens. `ctl run daily-scan`
visits them all, using free navigation only. `todo` turns the readings into a next-action list.
A reading from before the last daily reset counts as unknown. The reset is taken as 19:00 UTC,
inferred from event times shown in game.
"""
import calendar
import json
import os
import re
import threading
import time

from . import buttons, catalog, journal, nav, ocr, resources
from .screens import label

DATA = journal.ROOT / "data" / "daily.json"
_save_lock = threading.Lock()
RESET_HOUR_UTC = 19
PASSIVE_EVERY = 30               # seconds between passive reads of the same screen

_TICKETS = r"(\d+)\s*/\s*(\d+)"
READS = {
    "tasks": ((1150, 966, 1282, 1002), _TICKETS, ("daily_done", "daily_goal")),
    "mail": ((1560, 90, 1900, 170), r"(\d+)\s*/\s*200", ("mail",)),
    "lesson": ((60, 135, 440, 182), _TICKETS, ("lesson_tickets", "lesson_max")),
    "cafe": ((1650, 985, 1860, 1030), r"([\d.]+)\s*%", ("cafe_percent",)),
    "bounty": ((60, 135, 440, 182), _TICKETS, ("bounty_tickets", "bounty_max")),
    "scrimmage": ((60, 135, 440, 182), _TICKETS, ("scrimmage_tickets", "scrimmage_max")),
    "tactical_challenge": ((40, 700, 640, 960), _TICKETS, ("tactical_tickets", "tactical_max")),
}
# (screen, buttons to press from the lobby, in order)
ROUTE = (("tasks", ("tasks", "daily_tab")), ("mail", ("mail",)), ("lesson", ("lesson",)), ("cafe", ("cafe",)),
         ("bounty", ("campaign", "bounty")), ("scrimmage", ("campaign", "scrimmage")),
         ("tactical_challenge", ("campaign", "tactical_challenge")))


def last_reset(now=None):
    now = time.time() if now is None else now
    t = time.gmtime(now)
    today = calendar.timegm((t.tm_year, t.tm_mon, t.tm_mday, RESET_HOUR_UTC, 0, 0))
    return today if now >= today else today - 86400


def load():
    try:
        return json.loads(DATA.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(data):
    DATA.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, DATA)


def read_screen(frame, screen):
    """The numbers on this screen, or None if they can't be read."""
    box, pattern, names = READS[screen]
    text = " ".join(w.text for w in ocr.read(frame, box, scale=2))
    m = re.search(pattern, text)
    if not m:
        return None
    return {n: float(v) if "." in v else int(v) for n, v in zip(names, m.groups())}


def record(screen, values, now=None):
    with _save_lock:                     # the monitor thread and a daily-scan run both record
        data = load()
        data[screen] = {"at": time.time() if now is None else now, **values}
        _save(data)


class DailyWatcher:
    """Fed frames by the client's monitor thread."""

    def __init__(self):
        self._at = {}

    def observe(self, frame, screen):
        now = time.monotonic()
        if screen not in READS or now - self._at.get(screen, -PASSIVE_EVERY) < PASSIVE_EVERY:
            return None
        self._at[screen] = now
        values = read_screen(frame, screen)
        if values:
            record(screen, values)
        return values


def _press(game, vision, screen, name):
    x, y, kind = catalog.SCREENS[screen][name]
    if kind != catalog.NAV:
        raise nav.NavError(f"daily-scan only uses free navigation, not {name}")
    if not buttons.prove(vision, game.grab(), screen, name, x, y):
        raise nav.NavError(f"can't confirm {name} at this window shape")
    game.click(x, y)


def _arrive(game, vision, want, polls=32):
    for _ in range(polls):
        game.sleep(0.25)
        frame = game.grab()
        if label(vision, frame) == want and not nav.top_popup(vision, frame):
            game.sleep(0.6)
            return game.grab()
    return None


def scan(game, vision, log=None):
    """Visit each daily screen with free navigation and read it. Claims and spends nothing."""
    log = log or game.log
    game.rt.task = "Daily scan"
    found = {}
    for screen, path in ROUTE:
        nav.go_lobby(game, vision, log)
        here, frame = "lobby", None
        for button in path:
            _press(game, vision, here, button)
            target = "tasks" if button == "daily_tab" else button
            frame = _arrive(game, vision, target)
            if frame is None:
                game.sleep(1.0)
                if label(vision, game.grab()) == here:     # the first click is sometimes swallowed
                    _press(game, vision, here, button)
                    frame = _arrive(game, vision, target)
            if frame is None:
                raise nav.NavError(f"daily-scan: {target} didn't open")
            here = target
        values = read_screen(frame, screen)
        if values:
            record(screen, values)
            found[screen] = values
            log(f"daily: {screen} {values}")
        else:
            log(f"daily: couldn't read {screen}")
    nav.go_lobby(game, vision, log)
    game.rt.task = "idle"
    return {"read": len(found), "of": len(ROUTE)}


def plan(data=None, ap=None, now=None):
    """Next actions, most useful first, plus what is unknown."""
    data = load() if data is None else data
    reset = last_reset(now)
    fresh = {k: v for k, v in data.items() if isinstance(v, dict) and v.get("at", 0) >= reset}
    # Mail and the cafe build up over time, so an old reading is still a hint to go and look.
    todo, unknown = [], [k for k in READS if k not in fresh]

    def add(text, how):
        todo.append({"do": text, "how": how})

    if fresh.get("mail", {}).get("mail"):
        add(f"Claim mail ({fresh['mail']['mail']} waiting)", "press mail, then press claim_all")
    if fresh.get("cafe", {}).get("cafe_percent", 0) > 0:
        add(f"Claim cafe earnings ({fresh['cafe']['cafe_percent']}%)", "press cafe, press earnings, press claim, press close")
    t = fresh.get("tasks")
    if t and t["daily_done"] < t["daily_goal"]:
        add(f"Daily tasks {t['daily_done']}/{t['daily_goal']}: clear what's left, then claim",
            "press tasks, press daily_tab. Skip \"Recharge AP\": it spends pyroxene")
    elif t:
        add("Daily task goal reached: claim anything left", "press tasks, press claim_all")
    if fresh.get("lesson", {}).get("lesson_tickets"):
        add(f"Use {fresh['lesson']['lesson_tickets']} lesson tickets", "press lesson; All Locations shows who's in each room (who)")
    for key, title in (("bounty", "Bounty"), ("scrimmage", "Scrimmage"), ("tactical", "Tactical Challenge")):
        row = fresh.get("tactical_challenge" if key == "tactical" else key, {})
        if row.get(f"{key}_tickets"):
            add(f"{title}: {row[f'{key}_tickets']} tickets left", f"open {title} from Campaign")
    if ap:
        m = re.fullmatch(r"(\d+)/(\d+)", str(ap))
        if m and int(m.group(1)) >= int(m.group(2)):
            add(f"AP is at or over cap ({ap}): spend it so regen isn't wasted", "sweep with ctl run sweep")
    return {"todo": todo, "unknown": unknown, "ap": ap, "reset_utc_hour": RESET_HOUR_UTC}


def show(frame=None, as_json=False):
    ap = resources.read_any(frame)["ap"] if frame is not None else None
    result = plan(ap=ap)
    if as_json:
        print(json.dumps(result, indent=1))
        return 0
    if not result["todo"] and result["unknown"]:
        print("Nothing read since the last reset.")
    for i, row in enumerate(result["todo"], 1):
        print(f"{i}. {row['do']}\n     {row['how']}")
    if result["unknown"]:
        print("Not read since the last reset:", ", ".join(result["unknown"]), "(ctl run daily-scan)")
    return 0
