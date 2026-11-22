"""Auto-lessons: spend lesson tickets on the best rooms for the students you care about.

  python -m ba_auto.tasks.lesson_run                           plan only (the default): no game access
  python -m ba_auto.tasks.lesson_run --rehearse                open and check the first room, spend nothing
  python -m ba_auto.tasks.lesson_run --live --max-tickets 1    spend one ticket
  python -m ba_auto.tasks.lesson_run --list-rooms              room ids and scan_ts for `lesson-teach`

`lesson-teach` (`teach`) teaches exactly the rooms the user marked in the Atlas.

Priorities are in config/lessons.json. Rooms, students and who you own come from the last lesson scan
alone: the roster isn't used. A missing, partial or out-of-date lesson scan refuses the run.

A live run checks the room, its students and the ticket count on screen before pressing Start Lesson,
presses it once per lesson and never retries it, and proves the spend by re-reading the count. Any
mismatch or unknown screen stops the run where it is and saves the frame. The result and reward
screens aren't mapped yet: until result_confirm and reward_confirm are cut into templates/lessons/,
a live run stops on the first result screen with its frames in captures/lesson_run/.
"""
import argparse
import collections
import difflib
import json
import os
import re
import sys
import time

import cv2
import numpy as np

from .. import buttons, daily, journal, nav, ocr, portraits, viewport
from ..runtime import Stopped
from ..screens import _dimmed, label
from . import lessons

CONFIG = journal.ROOT / "config" / "lessons.json"
CAPTURES = journal.ROOT / "captures" / "lesson_run"
TEACH_STATUS = journal.ROOT / "data" / "lesson_teach.json"
AT_RISK = journal.ROOT / "data" / "lesson_at_risk.json"     # rooms whose Start was pressed since the last reset

PACK = "lessons"
START_BUTTON = "lessons/start_lesson"
MAP_BUTTON = "lessons/all_locations"
TICKET_LABEL = "lessons/tickets_owned"
RESULT_BUTTONS = ("lessons/result_confirm", "lessons/reward_confirm")    # cut after the first live recon
ALL_TITLE, DIALOG_TITLE = "nav_all_locations", "nav_location_info"

TICKET_BOX = (60, 135, 440, 182)
POPUP_TICKET_X, POPUP_TICKET_DY = (840, 1100), (56, 100)     # All Locations' count, below its title's centre
START_ZONE = (735, 780, 1180, 895)
DIALOG_ROOM_BOX = (585, 245, 1150, 300)  # stops short of a third portrait
DIALOG_SLOT_X = (1377, 1272, 1167)       # right to left: the row is right-aligned
DIALOG_SLOT_DY, DIALOG_SLOT_W, DIALOG_SLOT_H = 66, 72, 66    # top of the row, below the dialog title's centre
REWARD_X, REWARD_STEP, REWARD_Y = 478, 107, (536, 616)
REWARD_SLOTS = 9
REWARD_MIN_PURPLE = 800                  # relationship items are purple hexagons; live: 1707+, other rewards: 0
CARD_CLICK = (251, 95)                   # on the card's name row, clear of the portraits
CARD_POLLS = 12                          # 3 s for a room card to open Location Info
MAP_POLLS = 20
BUBBLE_SCALES = (1.05, 1.15, 1.25, 1.35, 1.45)
BUBBLE_MIN_SCORE = 0.72
BUBBLE_MIN_BRIGHTNESS = 0.75             # a locked room's bubble is dimmed well below this
MAP_UI = ((0, 0, 1920, 70), (60, 115, 440, 195), (1360, 115, 1900, 375), (1595, 940, 1900, 1080),
          (0, 480, 95, 610), (1825, 480, 1920, 610))    # top bar, tickets, area card, All Locations, arrows

MAX_TICKETS = 20
SHOW_WHEN_UNKNOWN = 6
TICKET_READS = 5
DIALOG_READS = 3
FINISH_POLLS = 60                        # 30 s for the result screens to appear or change
DIALOG_STILL = 8                         # 4 s on the same dialog means Start Lesson didn't take
MAX_RESULT_PRESSES = 6
MAX_KEPT_FRAMES = 40
NAME_MATCH = 0.85

CONFIG_KEYS = {"students", "default_weight", "locations", "fill_leftover_tickets"}


class LessonRunError(nav.NavError):
    """The run stopped, or refused to start. The message says why."""


def _key(name):
    return re.sub(r"[^a-z0-9*]", "", str(name or "").lower())


def _same_name(a, b):
    a, b = _key(a), _key(b)
    return bool(a and b) and (a == b or difflib.SequenceMatcher(None, a, b).ratio() >= NAME_MATCH)


# ---------------------------------------------------------------- config
def _number(value, what, low=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < low:
        raise LessonRunError(f"config/lessons.json: {what} must be a number of at least {low}")
    return value


def load_config(path=None):
    path = path or CONFIG
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise LessonRunError(f"no priorities file at {path}") from None
    except ValueError as e:
        raise LessonRunError(f"config/lessons.json isn't valid JSON: {e}") from None
    if not isinstance(raw, dict):
        raise LessonRunError("config/lessons.json must be one JSON object")
    unknown = sorted(k for k in raw if k not in CONFIG_KEYS and not k.startswith("_"))
    if unknown:
        raise LessonRunError(f"config/lessons.json has unknown settings: {', '.join(unknown)}")
    places = raw.get("locations", {})
    if not isinstance(places, dict) or set(places) - {"allow", "block"} or not all(
            isinstance(places.get(k, []), list) for k in ("allow", "block")):
        raise LessonRunError('config/lessons.json: locations must be {"allow": [...], "block": [...]}')
    fill = raw.get("fill_leftover_tickets", True)
    if not isinstance(fill, bool):
        raise LessonRunError("config/lessons.json: fill_leftover_tickets must be true or false")
    cfg = {
        "default_weight": _number(raw.get("default_weight", 1), "default_weight"),
        "allow": [str(x) for x in places.get("allow", [])],
        "block": [str(x) for x in places.get("block", [])],
        "fill": fill,
        "students": {},
    }
    students = raw.get("students", {})
    if not isinstance(students, dict):
        raise LessonRunError("config/lessons.json: students must be an object of name to weight")
    for name, rule in students.items():
        rule = rule if isinstance(rule, dict) else {"weight": rule}
        if set(rule) != {"weight"}:
            raise LessonRunError(f'config/lessons.json: "{name}" must be a weight, like "{name}": 5')
        cfg["students"][_key(name)] = {"name": name, "weight": _number(rule["weight"], f"{name}'s weight")}
    return cfg


# ---------------------------------------------------------------- scoring
def student_value(s, cfg):
    """(value of a lesson with this student, the reason in words)."""
    name = s.get("student") or s.get("dev") or "?"
    if not s.get("owned"):
        return 0.0, f"{name}: not owned, earns no relationship points"
    rule = cfg["students"].get(_key(s.get("student"))) or cfg["students"].get(_key(s.get("dev"))) or {}
    weight = rule.get("weight", cfg["default_weight"])
    source = "priority" if rule else "default"
    if weight == 0:
        return 0.0, f"{name}: weight 0, ignored"
    return float(weight), f"{name}: owned, {source} weight {weight:g}"


def _location_allowed(name, cfg):
    if any(_same_name(name, b) for b in cfg["block"]):
        return False, "blocked in config"
    if cfg["allow"] and not any(_same_name(name, a) for a in cfg["allow"]):
        return False, "not on the allow list"
    return True, ""


def rank_rooms(data, cfg):
    """(usable rooms, best first; [(where, why it was skipped)])."""
    rows, skipped, order = [], [], 0
    for loc in data.get("locations", []):
        ok, why = _location_allowed(loc["location"], cfg)
        names = collections.Counter(r["room"] for r in loc["rooms"])
        for room in loc["rooms"]:
            where = f"{loc['location']} > {room['room']}"
            order += 1
            people = room.get("students", [])
            if not ok:
                skipped.append((where, why))
            elif room.get("locked"):
                skipped.append((where, "locked"))
            elif names[room["room"]] > 1:
                skipped.append((where, "the scan has two rooms with this name, so they can't be told apart"))
            elif not people:
                skipped.append((where, "no students to confirm on screen"))
            elif any(not s.get("dev") for s in people):
                skipped.append((where, "a face couldn't be identified, so it can't be confirmed on screen"))
            else:
                values = [student_value(s, cfg) for s in people]
                rows.append({**_room_pick(loc["location"], room, order),
                             "score": round(sum(v for v, _ in values), 3),
                             "why": [w for _, w in values]})
    rows.sort(key=lambda r: (-r["score"], -r["owned"], r["order"]))
    return rows, skipped


def _room_pick(location, room, order):
    """What the runner needs to find this room and confirm it on screen."""
    people = room["students"]
    return {"location": location, "room": room["room"], "order": order,
            "owned": sum(1 for s in people if s.get("owned")),
            "students": [s.get("student") or s["dev"] for s in people],
            "devs": [s["dev"] for s in people],
            "owned_devs": [s["dev"] for s in people if s.get("owned")]}


def scan_problems(data, now=None):
    """Why this lesson scan can't be trusted today. Empty means it can."""
    now = time.time() if now is None else now
    if not data.get("locations"):
        return ["there is no lesson scan: run `ctl run lesson-scan`"]
    out = []
    if lessons.is_stale(data, now):
        out.append(f"the lesson scan ({data.get('scanned_at')}) is from before the last daily reset, "
                   "when students moved: run `ctl run lesson-scan`")
    if not data.get("complete"):
        out.append("the lesson scan is partial: run a full `ctl run lesson-scan`")
    people = [s for l in data["locations"] for r in l["rooms"] if not r.get("locked") for s in r["students"]]
    if any(not isinstance(s.get("owned"), bool) for s in people):
        out.append("the lesson scan doesn't say who is owned for every student: run `ctl run lesson-scan`")
    elif people and not any(s["owned"] for s in people):
        out.append(f"the lesson scan marks none of its {len(people)} students as owned, so its ownership read "
                   "failed. Not scoring every room as zero")
    return out


def _warnings(data, cfg):
    out = []
    seen = {_key(s.get(k)) for l in data.get("locations", []) for r in l["rooms"] for s in r["students"]
            for k in ("student", "dev")}
    for key, rule in cfg["students"].items():
        if key not in seen:
            out.append(f'"{rule["name"]}" in config/lessons.json isn\'t in any readable room today: '
                       "check the spelling if you expected them")
    places = [l["location"] for l in data.get("locations", [])]
    for name in cfg["allow"] + cfg["block"]:
        if places and not any(_same_name(name, p) for p in places):
            out.append(f'location "{name}" in config/lessons.json isn\'t in the scan')
    coded = sorted({s["dev"] for l in data.get("locations", []) for r in l["rooms"] for s in r["students"]
                    if s.get("owned") and s.get("dev") and re.fullmatch(r"CH\d+", str(s.get("student")))})
    if coded:
        out.append(f"{len(coded)} owned students have no in-game name yet, so a priority by name can't reach them. "
                   f"List them by code to weight them: {', '.join(coded)}")
    return out


def build_plan(data, cfg, tickets=None, max_tickets=None, now=None):
    problems = scan_problems(data, now)
    if problems:
        return {"refused": problems, "picks": [], "queue": [], "skipped": [], "warnings": [],
                "tickets": tickets, "budget": 0}
    rows, skipped = rank_rooms(data, cfg)
    worth = [r for r in rows if r["score"] > 0]
    rest = [r for r in rows if r["score"] <= 0]
    queue = worth + (rest if cfg["fill"] else [])
    if not cfg["fill"]:
        skipped += [(f"{r['location']} > {r['room']}", "nobody worth raising, and fill_leftover_tickets is off")
                    for r in rest]
    budget = SHOW_WHEN_UNKNOWN if tickets is None else tickets
    if max_tickets is not None:
        budget = min(budget, max_tickets)
    return {"refused": [], "queue": queue, "picks": queue[:budget], "skipped": skipped, "tickets": tickets,
            "budget": budget, "warnings": _warnings(data, cfg), "scanned_at": data.get("scanned_at")}


def known_tickets(now=None):
    """Today's ticket count from the last daily reading, or None."""
    row = daily.load().get("lesson") or {}
    return row.get("lesson_tickets") if row.get("at", 0) >= daily.last_reset(now) else None


def load_scan():
    """The saved lesson scan as it is. lessons.load() would also pull bond from the roster."""
    try:
        return json.loads(lessons.DATA.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"scanned_at": None, "ts": 0, "complete": False, "locations": []}


def make_plan(tickets=None, max_tickets=None, now=None, config=None):
    cfg = load_config(config)
    try:
        return build_plan(load_scan(), cfg, tickets, max_tickets, now)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        return {"refused": [f"couldn't read the lesson scan: {e!r}"], "picks": [], "queue": [], "skipped": [],
                "warnings": [], "tickets": tickets, "budget": 0}


def format_plan(plan):
    if plan["refused"]:
        return "\n".join(["Refusing to plan lessons:"] + [f"  - {p}" for p in plan["refused"]])
    count = "unknown, showing up to %d" % plan["budget"] if plan["tickets"] is None else str(plan["tickets"])
    lines = [f"Lesson plan. Scan {plan['scanned_at']}. Tickets: {count}."]
    if not plan["picks"]:
        lines.append("No room to spend a ticket on.")
    for i, r in enumerate(plan["picks"], 1):
        lines.append(f"{i}. {r['location']} > {r['room']}  (score {r['score']:g}): {', '.join(r['students'])}")
        lines += [f"     {w}" for w in r["why"]]
    left = plan["budget"] - len(plan["picks"])
    if plan["tickets"] is not None and left > 0:
        lines.append(f"{left} ticket(s) would stay unspent: not enough usable rooms.")
    if plan["skipped"]:
        lines.append("Skipped:")
        lines += [f"  {where}: {why}" for where, why in plan["skipped"]]
    if plan["warnings"]:
        lines.append("Check:")
        lines += [f"  {w}" for w in plan["warnings"]]
    return "\n".join(lines)


# ---------------------------------------------------------------- reading the screen
def load_pack(vision):
    """Load templates/lessons/ into this Vision and check the ones a run can't work without."""
    vision.load_pack(PACK)
    missing = [n for n in (START_BUTTON, MAP_BUTTON, TICKET_LABEL) if not vision.has(n)]
    if missing:
        raise LessonRunError(f"missing templates: {', '.join(missing)}")


def group_frame(vision, frame, template):
    """A plain reference image with `template`'s UI group at its 16:9 place, or None if it isn't on screen.
    A popup moves as one piece, so its fixed boxes (including lessons.read_rooms) are read from this."""
    if not vision.find(frame, template):
        return None
    view, canvas = getattr(frame, "view_info", None), getattr(frame, "canvas", None)
    if view is None or view.native or canvas is None:
        return frame
    dx, dy = vision.shift_of(frame, template)
    ch, cw = canvas.shape[:2]
    x1, y1, x2, y2 = max(0, -dx), max(0, -dy), min(viewport.REF_W, cw - dx), min(viewport.REF_H, ch - dy)
    out = np.zeros((viewport.REF_H, viewport.REF_W, 3), np.uint8)
    out[y1:y2, x1:x2] = viewport.crop(frame, (x1, y1, x2, y2), (dx, dy))
    return out


def _ticket_value(words):
    m = re.search(r"(\d+)\s*/\s*(\d+)", " ".join(w.text for w in words))
    if not m:
        return None
    have, most = int(m.group(1)), int(m.group(2))
    return have if 0 <= have <= most <= MAX_TICKETS else None


def read_tickets(vision, frame):
    """Tickets Owned on the Lesson screen or the map, or None. A dialog opened from All Locations hides it."""
    if not vision.find(frame, TICKET_LABEL):
        return None
    pixels = viewport.crop(frame, TICKET_BOX, vision.shift_of(frame, TICKET_LABEL))
    return None if pixels.size == 0 else _ticket_value(ocr.read(pixels, scale=2))


def read_popup_tickets(vision, frame):
    """Tickets Owned as shown on an open All Locations popup, or None."""
    title = vision.find(frame, ALL_TITLE)
    flat = group_frame(vision, frame, ALL_TITLE) if title else None
    if flat is None:
        return None
    box = (POPUP_TICKET_X[0], title[1] + POPUP_TICKET_DY[0], POPUP_TICKET_X[1], title[1] + POPUP_TICKET_DY[1])
    return _ticket_value(ocr.read(flat, box, scale=3))


def _on_map(vision, frame):
    return (lessons._is_map(vision)(frame) and bool(vision.find(frame, MAP_BUTTON))
            and not _dimmed(vision, frame, MAP_BUTTON))


def card_point(frame, room):
    """Where to click this room's card on an open All Locations popup, or None."""
    for cy in lessons.CARD_Y:
        for cx in lessons.CARD_X:
            text = " ".join(w.text for w in ocr.read(frame, (cx + 125, cy + 25, cx + 490, cy + 85), scale=2))
            if _same_name(re.sub(r"[^A-Za-z ]", "", text), room):
                return cx + CARD_CLICK[0], cy + CARD_CLICK[1]
    return None


def dialog_top(vision, frame):
    """The y of the portrait row, measured from the dialog's title, or None."""
    title = vision.find(frame, DIALOG_TITLE)
    return None if not title else title[1] + DIALOG_SLOT_DY


def dialog_slots(frame, count, top):
    """[x of each portrait], left to right, or None if the row doesn't hold exactly `count` people."""
    if not 1 <= count <= len(DIALOG_SLOT_X):
        return None
    if count < len(DIALOG_SLOT_X):
        x = DIALOG_SLOT_X[count]
        spare = frame[top:top + DIALOG_SLOT_H, x:x + DIALOG_SLOT_W]
        if float(cv2.cvtColor(spare, cv2.COLOR_BGR2GRAY).std()) >= 14:
            return None
    return list(reversed(DIALOG_SLOT_X[:count]))


def dialog_faces(frame, count, top):
    """Portrait crops from the Location Info dialog, or None if the head count is wrong."""
    slots = dialog_slots(frame, count, top)
    return None if slots is None else [frame[top:top + DIALOG_SLOT_H, x:x + DIALOG_SLOT_W] for x in slots]


def reward_students(frame):
    """How many relationship items the reward row offers: one per owned student in the room."""
    y1, y2 = REWARD_Y
    count = 0
    for i in range(REWARD_SLOTS):
        x = REWARD_X + REWARD_STEP * i
        hsv = cv2.cvtColor(frame[y1:y2, x + 8:x + 96], cv2.COLOR_BGR2HSV)
        if int(np.count_nonzero(cv2.inRange(hsv, (125, 45, 90), (175, 255, 255)))) >= REWARD_MIN_PURPLE:
            count += 1
    return count


def face_among(crop, candidates):
    """Which expected student this face is, or None. Checking a known name needs less margin than naming
    a stranger, which matters in small windows, but the student must still beat everyone in the art pack."""
    bank = portraits.bank()
    patch = portraits._patch(crop)
    small = cv2.resize(patch, (portraits.PATCH[0] // 2, portraits.PATCH[1] // 2), interpolation=cv2.INTER_AREA)
    rough = sorted(((portraits._best(images, small), name) for name, images in portraits._coarse().items()),
                   reverse=True)
    names = {name for _, name in rough[:portraits.SHORTLIST]} | {c for c in candidates if c in bank}
    scores = {name: portraits._best(bank[name], patch) for name in names}
    if not scores:
        return None
    top = max(scores, key=scores.get)
    return top if top in candidates and scores[top] >= portraits.MIN_SCORE else None


def match_faces(crops, devs):
    """Who each portrait is, in crop order, or None unless they are exactly these students."""
    if crops is None or len(crops) != len(devs):
        return None
    left, who = list(devs), []
    for crop in crops:
        hit = face_among(crop, left)
        if hit is None:
            return None
        left.remove(hit)
        who.append(hit)
    return who


def faces_match(crops, devs):
    return match_faces(crops, devs) is not None


def scan_still_matches(expected, now):
    """A first check on All Locations: same head count, and no face that is clearly someone else.
    A face too small to name passes here, because the dialog checks every face before Start Lesson."""
    if len(expected) != len(now):
        return False
    left = collections.Counter(expected)
    for dev in now:
        if dev:
            if left[dev] <= 0:
                return False
            left[dev] -= 1
    return True


def dialog_problem(vision, frame, pick):
    """Why this Location Info dialog isn't the room we meant to open, or None."""
    flat = group_frame(vision, frame, DIALOG_TITLE)
    if flat is None:
        return "the Location Info title isn't on screen"
    text = " ".join(w.text for w in ocr.read(flat, DIALOG_ROOM_BOX, scale=2))
    if not _same_name(re.sub(r"[^A-Za-z ]", "", text), pick["room"]):
        return f"the dialog shows room '{text}', expected '{pick['room']}'"
    view = getattr(frame, "view_info", None)
    # Only over the bare map: elsewhere the area panel has its own anchor, or All Locations half covers it.
    # The clear map's name was already checked before the room was opened.
    if (view is None or view.native) and vision.find(frame, TICKET_LABEL):
        area = lessons.area_name(frame)
        if area and not _same_name(area, pick["location"]):
            return f"the dialog is over location '{area}', expected '{pick['location']}'"
    top = dialog_top(vision, frame)
    slots = dialog_slots(flat, len(pick["devs"]), top)
    who = match_faces(dialog_faces(flat, len(pick["devs"]), top), pick["devs"])
    if who is None:
        return f"the dialog's students aren't {pick['devs']}"
    owned = sorted(dev for dev, x in zip(who, slots) if lessons._owned(flat, x, top))
    if owned != sorted(pick["owned_devs"]):
        return f"the dialog shows {owned} as owned, the scan said {sorted(pick['owned_devs'])}"
    rewards = reward_students(flat)
    if rewards != len(owned):
        return f"the reward row has {rewards} relationship items for {len(owned)} owned students"
    return None


def card_faces(frame, point, count):
    """The portraits on a room's All Locations card, for finding them on the map."""
    cx, cy = point[0] - CARD_CLICK[0], point[1] - CARD_CLICK[1]
    return [np.array(frame[cy + lessons.SLOT_Y:cy + lessons.SLOT_Y + lessons.SLOT_H, cx + dx:cx + dx + lessons.SLOT_W])
            for dx in lessons.SLOT_DX[:count]]


def find_bubble(frame, faces):
    """The centre of a lit map bubble showing one of `faces`, or None. Locked rooms' bubbles are dimmed,
    and points under the map's own buttons and panels are refused."""
    best = None
    image = np.asarray(frame)
    for face in faces:
        inner = face[8:-16, 10:-16]              # no frame or heart: those differ between screens
        for scale in BUBBLE_SCALES:
            tpl = cv2.resize(inner, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)
            res = cv2.matchTemplate(image, tpl, cv2.TM_CCOEFF_NORMED)
            _, score, _, loc = cv2.minMaxLoc(res)
            if score < BUBBLE_MIN_SCORE or (best and score <= best[0]):
                continue
            h, w = tpl.shape[:2]
            x, y = loc[0] + w // 2, loc[1] + h // 2
            if any(a <= x <= c and b <= y <= d for a, b, c, d in MAP_UI):
                continue
            if float(image[loc[1]:loc[1] + h, loc[0]:loc[0] + w].mean()) < float(tpl.mean()) * BUBBLE_MIN_BRIGHTNESS:
                continue
            best = (score, (x, y))
    return best[1] if best else None


# ---------------------------------------------------------------- stopping
def _save_frame(frame, stamp, name):
    folder = CAPTURES / stamp
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.png"
    cv2.imwrite(str(path), np.asarray(frame))
    return path


def _stop(game, stamp, message, frame=None):
    """Stop where we are: no closing, no retry. A person looks at the saved frame."""
    try:
        frame = game.grab() if frame is None else frame
        path = _save_frame(frame, stamp, f"stop_{time.strftime('%H%M%S')}")
        message = f"{message} (frame: {path})"
    except Stopped:
        raise
    except Exception as e:
        message = f"{message} (couldn't save the frame: {e!r})"
    raise LessonRunError(message)


def _tickets(game, vision, stamp, frame=None, reader=None):
    """The ticket count, read the same way twice from separate frames."""
    last = None
    for _ in range(TICKET_READS):
        now = game.grab() if frame is None else frame
        frame = None
        value = (reader or read_tickets)(vision, now)
        if value is not None and value == last:
            return value
        last = value if value is not None else last
        game.sleep(0.3)
    _stop(game, stamp, "couldn't read the ticket count")


# ---------------------------------------------------------------- walking the game
def _enter(game, vision, log):
    nav.go_lobby(game, vision, log)
    lessons._click(game, lessons.LESSON_BUTTON, vision, ("lobby", "lesson"))
    return lessons._wait(game, vision, lambda f: label(vision, f) == "lesson", "the Lesson screen")


def _go_area(game, vision, frame, target, stamp):
    """Press `>` until the map shows `target`. Returns the map frame."""
    seen = set()
    for _ in range(lessons.MAX_AREAS):
        here = lessons.area_name(frame)
        if not here:
            _stop(game, stamp, "couldn't read the location's name", frame)
        if _same_name(here, target):
            return frame
        if here in seen:
            _stop(game, stamp, f"went through every location without finding '{target}'", frame)
        seen.add(here)
        frame = lessons._next_area(game, vision, here)
        if frame is None:
            _stop(game, stamp, f"the location stopped changing before reaching '{target}'")
    _stop(game, stamp, f"didn't reach '{target}'")


def _poll(game, test, polls):
    """A settled frame once `test` passes, or None."""
    for _ in range(polls):
        game.sleep(0.25)
        if test(game.grab()):
            game.sleep(0.5)
            return game.grab()
    return None


def _open_by_bubble(game, vision, pick, faces, is_dialog, stamp, log):
    """The card didn't open the room: close All Locations and click a student on the map instead."""
    frame = game.grab()
    if not lessons._is_all_locations(vision)(frame):
        _stop(game, stamp, "the room card didn't open Location Info, and this isn't All Locations", frame)
    view = getattr(frame, "view_info", None)
    if view is not None and not view.native:
        _stop(game, stamp, "the room card didn't open Location Info, and map bubbles are only clicked at 16:9", frame)
    lessons._click(game, lessons.POPUP_CLOSE, vision, ("All Locations", "close"))
    frame = _poll(game, lambda f: _on_map(vision, f), MAP_POLLS)
    if frame is None:
        _stop(game, stamp, "the map didn't come back after closing All Locations")
    if not _same_name(lessons.area_name(frame), pick["location"]):
        _stop(game, stamp, f"the map isn't '{pick['location']}' after closing All Locations", frame)
    point = find_bubble(frame, faces)
    if point is None:
        _stop(game, stamp, f"no lit student bubble for '{pick['room']}' is visible on the map", frame)
    log(f"lesson run: room card didn't open; clicking the student on the map at {point}")
    game.click(*point)
    dialog = _poll(game, is_dialog, CARD_POLLS)
    if dialog is None:
        _stop(game, stamp, "the student on the map didn't open Location Info")
    return dialog


def _open_room(game, vision, pick, tickets, stamp, log):
    """From a clear map to a confirmed Location Info dialog. Returns its frame."""
    lessons._click(game, lessons.ALL_LOCATIONS, vision, ("lesson_location", "all_locations"))
    popup = lessons._wait(game, vision, lessons._is_all_locations(vision), "All Locations")
    shown = _tickets(game, vision, stamp, popup, lambda v, f: read_popup_tickets(v, f))
    if shown != tickets:
        _stop(game, stamp, f"All Locations shows {shown} tickets, the map showed {tickets}", popup)
    flat = group_frame(vision, popup, ALL_TITLE)
    if flat is None:
        _stop(game, stamp, "All Locations went away before it could be read", popup)
    room = next((r for r in lessons.read_rooms(flat) if _same_name(r["room"], pick["room"])), None)
    if room is None:
        _stop(game, stamp, f"'{pick['room']}' isn't on All Locations", popup)
    if room["locked"]:
        _stop(game, stamp, f"'{pick['room']}' is locked", popup)
    now = [s["dev"] for s in room["students"]]
    if not scan_still_matches(pick["devs"], now):
        _stop(game, stamp, f"students in '{pick['room']}' changed since the scan: {now}, expected {pick['devs']}. "
                           "Run a fresh lesson-scan", popup)
    point = card_point(flat, pick["room"])
    if point is None:
        _stop(game, stamp, f"couldn't find the card for '{pick['room']}'", popup)
    fresh = game.grab()
    if not lessons._is_all_locations(vision)(fresh) or not buttons.tie(vision, fresh, ALL_TITLE, *point):
        _stop(game, stamp, "All Locations changed before the room card could be clicked", fresh)
    faces = card_faces(flat, point, len(pick["devs"]))
    game.click(*point)

    def is_dialog(f):
        top = nav.top_popup(vision, f)
        return bool(top and top[0] == "Location Info")

    dialog = _poll(game, is_dialog, CARD_POLLS)
    if dialog is None:
        dialog = _open_by_bubble(game, vision, pick, faces, is_dialog, stamp, log)
    problem = "the dialog never settled"
    for _ in range(DIALOG_READS):
        problem = dialog_problem(vision, dialog, pick)
        if not problem:
            return dialog
        game.sleep(0.4)
        dialog = game.grab()
    _stop(game, stamp, problem, dialog)


def _press_start(game, vision, stamp, before_click=None):
    """Press Start Lesson, exactly once, from a fresh frame. Callers treat this as having spent a ticket.
    `before_click` runs after every check and right before the click, to record that a ticket is at risk."""
    frame = game.grab()
    top = nav.top_popup(vision, frame)
    if not top or top[0] != "Location Info":
        _stop(game, stamp, "the Location Info dialog went away before Start Lesson", frame)
    point = vision.find(frame, START_BUTTON)
    if not point or _dimmed(vision, frame, START_BUTTON):
        _stop(game, stamp, "Start Lesson isn't available in the dialog", frame)
    x1, y1, x2, y2 = START_ZONE
    if not (x1 <= point[0] <= x2 and y1 <= point[1] <= y2):
        _stop(game, stamp, f"Start Lesson matched at {point}, outside where it belongs", frame)
    if before_click:
        before_click()
    game.click(*point)


def _finish(game, vision, stamp, log):
    """After Start Lesson: walk the known result screens back to the clear map. Returns its frame."""
    kept = presses = still = 0
    last = None
    for _ in range(FINISH_POLLS):
        game.sleep(0.5)
        frame = game.grab()
        if last is None or float(cv2.absdiff(frame, last).mean()) > 3:
            last = frame
            if kept < MAX_KEPT_FRAMES:
                _save_frame(frame, stamp, f"after_start_{kept:02d}")
                kept += 1
        if _on_map(vision, frame):
            game.sleep(0.8)
            again = game.grab()
            if _on_map(vision, again):
                return again
            continue
        top = nav.top_popup(vision, frame)
        if top and top[0] == "Location Info":
            still += 1
            if still >= DIALOG_STILL:
                _stop(game, stamp, "still on Location Info after Start Lesson. Not pressing it again, "
                                   "because the first press may have counted", frame)
            continue
        still = 0
        if top and top[0] == "All Locations":
            lessons._click(game, lessons.POPUP_CLOSE, vision, ("All Locations", "close"))
            game.sleep(0.8)
            continue
        for name in RESULT_BUTTONS:
            point = vision.find(frame, name) if vision.has(name) else None
            if point and not _dimmed(vision, frame, name):
                presses += 1
                if presses > MAX_RESULT_PRESSES:
                    _stop(game, stamp, "the result screens didn't end", frame)
                log(f"lesson run: {name.split('/')[-1]}")
                game.click(*point)
                game.sleep(1.0)
                break
    _stop(game, stamp, f"unknown screen after Start Lesson (on '{label(vision, game.grab())}'). A ticket was "
                       f"probably spent but not proven. Frames are in {CAPTURES / stamp}")


def _at_risk_rows(now=None):
    """{room id: time Start was pressed} since the last daily reset. An unreadable file stops the run."""
    try:
        rows = json.loads(AT_RISK.read_text(encoding="utf-8"))
        reset = daily.last_reset(time.time() if now is None else now)
        return {str(k): float(t) for k, t in rows.items() if float(t) >= reset}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError, AttributeError) as e:
        raise LessonRunError(f"{AT_RISK.name} can't be read, so a room may be taught twice: {e!r}") from e


def _note_at_risk(pick):
    """Recorded before Start is pressed. If this fails, Start isn't pressed."""
    rows = _at_risk_rows()
    rows[room_id(pick["location"], pick["room"])] = time.time()
    AT_RISK.parent.mkdir(parents=True, exist_ok=True)
    tmp = AT_RISK.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, AT_RISK)


def _summary(spent, tickets, note):
    return {"spent": len(spent), "rooms": spent, "tickets_left": tickets, "note": note}


def _open_checked(game, vision, frame, pick, tickets, stamp, log):
    """Go to this room and confirm it. Returns the ticket count, with the room's dialog open."""
    frame = _go_area(game, vision, frame, pick["location"], stamp)
    before = _tickets(game, vision, stamp, frame)
    if before != tickets:
        _stop(game, stamp, f"the ticket count moved from {tickets} to {before} on its own", frame)
    dialog = _open_room(game, vision, pick, before, stamp, log)
    behind = read_tickets(vision, dialog)    # None when All Locations is underneath; it was read just before
    if behind is not None and behind != before:
        _stop(game, stamp, f"the dialog shows {behind} tickets, the map showed {before}", dialog)
    return before


def _teach_open_room(game, vision, pick, before, stamp, log, before_click=None):
    """Press Start once, walk the result screens, and prove one ticket went. Returns (map frame, tickets)."""
    where = f"{pick['location']} > {pick['room']}"
    log(f"lesson run: starting {where} ({before} tickets)")
    _press_start(game, vision, stamp, before_click)
    frame = _finish(game, vision, stamp, log)
    after = _tickets(game, vision, stamp, frame)
    if after != before - 1:
        _stop(game, stamp, f"expected {before - 1} tickets after the lesson in {where}, read {after}", frame)
    log(f"lesson run: {where} done, {before} -> {after} tickets")
    return frame, after


def run(game, vision, live=False, rehearse=False, max_tickets=None, log=None, config=None):
    """Default: plan only, no game access. rehearse: open and check one room. live: spend tickets."""
    log = log or game.log
    if live and rehearse:
        raise LessonRunError("choose --live or --rehearse, not both")
    if max_tickets is not None and (not isinstance(max_tickets, int) or max_tickets < 1):
        raise LessonRunError("max_tickets must be a whole number of at least 1")
    plan = make_plan(tickets=known_tickets(), max_tickets=max_tickets, config=config)
    if plan["refused"]:
        raise LessonRunError("; ".join(plan["refused"]))
    risky = _at_risk_rows()
    if risky:
        for key in ("queue", "picks"):
            plan[key] = [p for p in plan[key] if room_id(p["location"], p["room"]) not in risky]
        log(f"lesson run: leaving out {len(risky)} room(s) whose Start Lesson was already pressed today")
    if not live and not rehearse:
        log(format_plan(plan))
        return {"dry_run": True, "rooms": [f"{r['location']} > {r['room']}" for r in plan["picks"]]}

    load_pack(vision)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    game.rt.task = "Lesson rehearsal" if rehearse else "Lessons"
    spent = []
    try:
        frame = _enter(game, vision, log)
        tickets = _tickets(game, vision, stamp, frame)
        log(f"lesson run: {tickets} tickets")
        if tickets == 0:
            nav.go_lobby(game, vision, log)
            return _summary(spent, 0, "no tickets left today")
        budget = tickets if max_tickets is None else min(tickets, max_tickets)
        targets = plan["queue"][:1 if rehearse else budget]
        if not targets:
            nav.go_lobby(game, vision, log)
            return _summary(spent, tickets, "no usable room")
        lessons._click(game, lessons.FIRST_ROW, vision, ("lesson", "schale_office"))
        frame = lessons._wait(game, vision, lambda f: _on_map(vision, f), "the location map")
        for pick in targets:
            where = f"{pick['location']} > {pick['room']}"
            before = _open_checked(game, vision, frame, pick, tickets, stamp, log)
            if rehearse:
                log(f"lesson run: rehearsal confirmed {where}: {', '.join(pick['students'])}; {before} tickets")
                nav.go_lobby(game, vision, log)
                return _summary(spent, before, f"rehearsal confirmed {where}; nothing spent")
            frame, tickets = _teach_open_room(game, vision, pick, before, stamp, log,
                                              lambda room=pick: _note_at_risk(room))
            spent.append(where)
            game.rt.bump("lessons")
        nav.go_lobby(game, vision, log)
        note = "stopped at --max-tickets" if max_tickets is not None and len(spent) >= max_tickets and tickets else None
        return _summary(spent, tickets, note or ("no tickets left" if tickets == 0 else "no more usable rooms"))
    finally:
        game.rt.task = "idle"


# ---------------------------------------------------------------- teaching the rooms marked in the Atlas
def room_id(location, room):
    return f"{location}|{room}"


def select_rooms(rooms, scan_ts, tickets=None, now=None):
    """The picks for exactly these room ids, in the scan's zone order. Refuses the whole selection
    if anything is wrong with it. Sends no input."""
    if not isinstance(rooms, (list, tuple)) or not rooms or not all(isinstance(r, str) for r in rooms):
        raise LessonRunError("lesson-teach needs a list of marked rooms")
    if isinstance(scan_ts, bool) or not isinstance(scan_ts, (int, float)):
        raise LessonRunError("lesson-teach needs the scan_ts of the scan the rooms were marked from")
    try:
        data = load_scan()
        problems = scan_problems(data, now)
        if problems:
            raise LessonRunError("; ".join(problems))
        if scan_ts != data.get("ts"):
            raise LessonRunError("the lesson scan changed after these rooms were marked: check them and try again")
        table, ambiguous, order = {}, set(), 0
        for loc in data["locations"]:
            for room in loc["rooms"]:
                order += 1
                rid = room_id(loc["location"], room["room"])
                if rid in table:
                    ambiguous.add(rid)
                table[rid] = (loc["location"], room, order)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        raise LessonRunError(f"couldn't read the lesson scan: {e!r}") from None
    out, picks = [], []
    for rid, count in collections.Counter(rooms).items():
        if count > 1:
            out.append(f"{rid} is listed twice")
        if rid not in table:
            out.append(f"{rid} isn't in the current scan")
            continue
        location, room, position = table[rid]
        if rid in ambiguous:
            out.append(f"{rid} appears twice in the scan, so it can't be told apart")
        elif room.get("locked"):
            out.append(f"{rid} is locked")
        elif not room.get("students"):
            out.append(f"{rid} has no students to confirm on screen")
        elif any(not s.get("dev") for s in room["students"]):
            out.append(f"{rid} has a face that couldn't be identified, so it can't be confirmed on screen")
        else:
            picks.append(_room_pick(location, room, position))
    if tickets is not None and len(rooms) > tickets:
        out.append(f"{len(rooms)} rooms are marked, but there are only {tickets} tickets")
    if out:
        raise LessonRunError("; ".join(out))
    return sorted(picks, key=lambda p: p["order"])


class _TeachStatus:
    """data/lesson_teach.json, the page's view of this run. Written atomically."""

    def __init__(self, picks, scan_ts, rehearse, log):
        self.log = log
        self.data = {"run_id": time.strftime("%Y%m%d-%H%M%S"), "state": "running", "scan_ts": scan_ts,
                     "tickets_start": None, "tickets_now": None,
                     "rooms": [{"id": room_id(p["location"], p["room"]), "state": "queued", "note": None} for p in picks]}
        if rehearse:
            self.data["rehearse"] = True
        self.write()

    def write(self):
        TEACH_STATUS.parent.mkdir(parents=True, exist_ok=True)
        tmp = TEACH_STATUS.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, TEACH_STATUS)

    def tickets(self, count):
        if self.data["tickets_start"] is None:
            self.data["tickets_start"] = count
        self.data["tickets_now"] = count
        self.write()

    def room(self, index, state, note=None):
        self.data["rooms"][index].update(state=state, note=note)
        self.write()

    def close(self, state, error=None, current=None, at_risk=False):
        """End the run. Never raises, so it can't hide the reason the run ended."""
        self.data["state"] = state
        if error:
            self.data["error"] = error
        if current is not None:
            entry = self.data["rooms"][current]
            if at_risk:
                entry.update(state="failed", note=f"Start Lesson was pressed, so the ticket may be spent: {error}")
            elif state == "stopped":
                entry.update(state="skipped", note="Stopped before Start Lesson: no ticket spent")
            else:
                entry.update(state="failed", note=f"No ticket spent: {error}")
        why = "Rehearsal checks only the first room" if self.data.get("rehearse") and state == "done" else "Not started"
        for entry in self.data["rooms"]:
            if entry["state"] == "queued":
                entry.update(state="skipped", note=why)
        try:
            self.write()
        except OSError as e:
            self.log(f"lesson teach: couldn't save the final status: {e!r}")


def teach(game, vision, rooms, scan_ts, rehearse=False, log=None):
    """Teach exactly these rooms, one ticket each. rehearse opens and checks the first and spends nothing."""
    log = log or game.log
    picks = select_rooms(rooms, scan_ts, known_tickets())
    load_pack(vision)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    status = _TeachStatus(picks, scan_ts, rehearse, log)
    game.rt.task = "Lesson rehearsal" if rehearse else "Teaching lessons"
    spent, current, at_risk = [], None, False

    def about_to_press():
        nonlocal at_risk
        status.room(current, "teaching")     # recorded first: if this fails, Start is never pressed
        _note_at_risk(picks[current])
        at_risk = True

    try:
        frame = _enter(game, vision, log)
        tickets = _tickets(game, vision, stamp, frame)
        status.tickets(tickets)
        log(f"lesson teach: {len(picks)} rooms, {tickets} tickets")
        if len(picks) > tickets:
            nav.go_lobby(game, vision, log)
            raise LessonRunError(f"{len(picks)} rooms are marked, but there are only {tickets} tickets")
        lessons._click(game, lessons.FIRST_ROW, vision, ("lesson", "schale_office"))
        frame = lessons._wait(game, vision, lambda f: _on_map(vision, f), "the location map")
        for index, pick in enumerate(picks[:1] if rehearse else picks):
            current, at_risk = index, False
            status.room(index, "opening")
            before = _open_checked(game, vision, frame, pick, tickets, stamp, log)
            if rehearse:
                status.room(index, "done", "Rehearsal: room and students confirmed, nothing spent")
                current = None
                log(f"lesson teach: rehearsal confirmed {pick['location']} > {pick['room']}; {before} tickets")
                break
            frame, tickets = _teach_open_room(game, vision, pick, before, stamp, log, about_to_press)
            spent.append(room_id(pick["location"], pick["room"]))
            status.data["tickets_now"] = tickets
            status.room(index, "done")
            current, at_risk = None, False
            game.rt.bump("lessons")
        nav.go_lobby(game, vision, log)
        status.close("done")
        note = "rehearsal: nothing spent" if rehearse else f"taught {len(spent)} of {len(picks)} marked rooms"
        return _summary(spent, tickets, note)
    except Stopped:
        status.close("stopped", "Stopped by the user", current, at_risk)
        raise
    except Exception as e:
        status.close("failed", str(e), current, at_risk)
        raise
    finally:
        game.rt.task = "idle"


def teach_task(game, vision, args):
    """For control.build_task. Checks the selection now, so a bad request never starts a run."""
    rooms, scan_ts, rehearse = args.get("rooms"), args.get("scan_ts"), args.get("rehearse")
    if rehearse is not None and not isinstance(rehearse, bool):
        raise ValueError("rehearse must be true or false")
    try:
        select_rooms(rooms, scan_ts, known_tickets())
    except LessonRunError as e:
        raise ValueError(str(e)) from None
    return lambda: teach(game, vision, list(rooms), scan_ts, rehearse=bool(rehearse))


def list_rooms():
    """The room ids and scan_ts that `ctl run lesson-teach` takes."""
    try:
        data = load_scan()
    except (OSError, ValueError) as e:
        return [f"couldn't read the lesson scan: {e!r}"]
    problems = scan_problems(data)
    if problems:
        return ["Can't teach from this scan:"] + [f"  - {p}" for p in problems]
    lines = [f"scan_ts {data['ts']!r}"]
    for loc in data["locations"]:
        for room in loc["rooms"]:
            people = ", ".join((s.get("student") or "?") + ("" if s.get("owned") else " (not owned)") for s in room["students"])
            state = "locked" if room.get("locked") else people or "nobody read"
            lines.append(f"{room_id(loc['location'], room['room'])}  [{state}]")
    return lines


# ---------------------------------------------------------------- command line
def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m ba_auto.tasks.lesson_run",
                                 description="Spend lesson tickets by student priority. Plans only unless told otherwise.")
    ap.add_argument("--live", action="store_true", help="really spend tickets")
    ap.add_argument("--rehearse", action="store_true", help="open and check the first room, spend nothing")
    ap.add_argument("--max-tickets", type=int, default=None, metavar="N", help="spend at most N tickets")
    ap.add_argument("--list-rooms", action="store_true", help="print the room ids and scan_ts for lesson-teach")
    a = ap.parse_args(argv)
    if a.list_rooms:
        print("\n".join(list_rooms()))
        return 0
    if a.max_tickets is not None and a.max_tickets < 1:
        ap.error("--max-tickets must be at least 1")
    if a.live and a.rehearse:
        ap.error("choose --live or --rehearse, not both")
    try:
        if not a.live and not a.rehearse:
            plan = make_plan(tickets=known_tickets(), max_tickets=a.max_tickets)
            print(format_plan(plan))
            if not plan["refused"]:
                print("\nDry run: nothing was clicked or spent.")
            return 1 if plan["refused"] else 0
        from ..game import Game
        from ..vision import Vision
        game = Game()
        result = run(game, Vision(game.rt), live=a.live, rehearse=a.rehearse, max_tickets=a.max_tickets, log=print)
        print(json.dumps(result, indent=1, ensure_ascii=False))
        return 0
    except (RuntimeError, Stopped) as e:         # NavError, and Game refusing input, are RuntimeErrors
        print(f"STOPPED: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
