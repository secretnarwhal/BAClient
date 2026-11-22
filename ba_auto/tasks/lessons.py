"""Read-only lesson scan: every location, room and student, saved to data/lessons.json.

It walks Lesson > Select Location > All Locations for each area. It can click exactly five things:
Lesson on the lobby bar, the first location row, All Locations, that popup's X, and the `>` arrow,
plus nav's Home. It never clicks a room, because a room leads to Start Lesson, which spends a ticket.
Students move at the daily reset, so a scan from before the reset is marked stale.
"""
import difflib
import json
import os
import re
import time

import cv2
import numpy as np

from .. import buttons, daily, journal, nav, ocr, portraits
from ..screens import label

DATA = journal.ROOT / "data" / "lessons.json"
LESSON_BUTTON = (318, 1030)
FIRST_ROW = (1300, 290)
ALL_LOCATIONS = (1746, 992)
POPUP_CLOSE = (1706, 147)
NEXT_AREA = (1860, 545)
ALLOWED = (LESSON_BUTTON, FIRST_ROW, ALL_LOCATIONS, POPUP_CLOSE, NEXT_AREA)
AREA_BOX = (1500, 150, 1885, 202)
RANK_BOX = (1392, 155, 1498, 198)
CARD_X, CARD_Y = (195, 711, 1227), (265, 498, 731)
CARD_W, CARD_H = 503, 215
SLOT_DX, SLOT_Y, SLOT_W, SLOT_H = (39, 147, 255), 125, 76, 72
MAX_AREAS = 30
ROOM_NAMES = ("Theater Room", "Gym", "Library", "Classroom", "Laboratory", "Shooting Range", "Hangar")
AREA_POLLS = 20


class LessonError(nav.NavError):
    """The scan met something it won't click through."""


def _click(game, point, vision=None, proof=None):
    """proof is a (screen, button) catalog entry, or a template for a centre-anchored arrow."""
    if point not in ALLOWED:
        raise LessonError(f"click at {point} is not on the lesson scan's allowlist")
    if vision is not None and proof:
        frame = game.grab()
        if isinstance(proof, tuple):
            buttons.prove(vision, frame, proof[0], proof[1], *point)
        else:
            buttons.tie(vision, frame, proof, *point, anchor="center", reach=1000)
    game.click(*point)


def area_name(frame):
    text = " ".join(w.text for w in ocr.read(frame, AREA_BOX, scale=2)).strip()
    return re.sub(r"\s+", " ", text) or None


def _rank(frame):
    m = re.search(r"(\d+)", " ".join(w.text for w in ocr.read(frame, RANK_BOX, scale=3)))
    return int(m.group(1)) if m else None


def _owned(frame, x, y):
    """Students you own show a pink frame and a heart with their relationship rank."""
    hsv = cv2.cvtColor(frame[y + SLOT_H - 26:y + SLOT_H + 14, x + SLOT_W - 26:x + SLOT_W + 22], cv2.COLOR_BGR2HSV)
    return int(np.count_nonzero(cv2.inRange(hsv, (160, 70, 190), (179, 200, 255)))) > 120


def read_rooms(frame):
    """Every room on an open All Locations popup."""
    rooms = []
    for cy in CARD_Y:
        for cx in CARD_X:
            header = frame[cy + 12:cy + 96, cx + 110:cx + CARD_W - 12]
            name = " ".join(w.text for w in ocr.read(frame, (cx + 125, cy + 25, cx + 490, cy + 85), scale=2)).strip()
            name = re.sub(r"[^A-Za-z ]", "", name).strip()
            locked = float(cv2.cvtColor(header, cv2.COLOR_BGR2GRAY).mean()) < 120
            if not name and not locked:
                continue
            close = difflib.get_close_matches(name, ROOM_NAMES, n=1, cutoff=0.7)   # a lock icon can cover letters
            name = close[0] if close else name
            students = []
            for dx in () if locked else SLOT_DX:     # locked rooms are too dark to read and can't be used
                x, y = cx + dx, cy + SLOT_Y
                crop = frame[y:y + SLOT_H, x:x + SLOT_W]
                if float(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY).std()) < 14:
                    continue             # an empty slot is flat panel colour
                dev, score, _ = portraits.identify(crop)
                owned = _owned(frame, x, y)
                students.append({"student": portraits.display(dev), "dev": dev, "score": score, "owned": owned})
            rooms.append({"room": name or "(locked room)", "locked": locked, "students": students})
    return rooms


def _wait(game, vision, test, what, polls=28):
    for _ in range(polls):
        game.sleep(0.25)
        frame = game.grab()
        if test(frame):
            game.sleep(0.5)
            return game.grab()
    raise LessonError(f"{what} didn't appear (on '{label(vision, game.grab())}')")


def _is_map(vision):
    return lambda f: label(vision, f) == "lesson_location" and not nav.top_popup(vision, f)


def _is_all_locations(vision):
    def test(f):
        popup = nav.top_popup(vision, f)
        return bool(popup and popup[0] == "All Locations")
    return test


def _next_area(game, vision, current):
    """Press `>`. Returns the new map frame, or None if the area doesn't change."""
    for _ in range(2):                  # the first click after a load is sometimes swallowed
        if not _is_map(vision)(game.grab()):
            raise LessonError("left the location map; stopping before the next click")
        _click(game, NEXT_AREA, vision, "title_lesson_location")
        for _ in range(AREA_POLLS):
            game.sleep(0.25)
            frame = game.grab()
            if _is_map(vision)(frame):
                name = area_name(frame)
                if name and name != current:
                    game.sleep(0.8)
                    return game.grab()
    return None


def load():
    """The last scan. Relationship ranks come from the roster: the room hearts are too small to read."""
    try:
        data = json.loads(DATA.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"scanned_at": None, "ts": 0, "complete": False, "locations": []}
    from . import roster
    bonds = {s.get("name"): s.get("bond") for s in roster.load()["students"]}
    for loc in data["locations"]:
        for room in loc["rooms"]:
            for s in room["students"]:
                s["bond"] = bonds.get(s.get("student")) if s.get("owned") else None
    return data


def _has_complete_scan():
    try:
        return json.loads(DATA.read_text(encoding="utf-8")).get("complete") is True
    except (OSError, ValueError, AttributeError):
        return False


def _save(locations, complete):
    """A partial read never replaces a complete scan: it goes beside it, so a failed scan loses nothing."""
    DATA.parent.mkdir(parents=True, exist_ok=True)
    target = DATA if complete or not _has_complete_scan() else DATA.with_name("lessons.partial.json")
    data = {"scanned_at": time.strftime("%Y-%m-%d %H:%M:%S"), "ts": time.time(), "complete": complete,
            "locations": locations}
    tmp = target.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)


def scan(game, vision, limit=None, log=None):
    """Read every lesson location. Returns counts; never starts a lesson."""
    log = log or game.log
    game.rt.task = "Lesson scan"
    nav.go_lobby(game, vision, log)
    for attempt in range(2):            # the first click after the lobby loads is sometimes swallowed
        _click(game, LESSON_BUTTON, vision, ("lobby", "lesson"))
        try:
            _wait(game, vision, lambda f: label(vision, f) == "lesson", "the Lesson screen")
            break
        except LessonError:
            frame = game.grab()
            if attempt or label(vision, frame) != "lobby" or nav.top_popup(vision, frame):
                raise
    _click(game, FIRST_ROW, vision, ("lesson", "schale_office"))
    frame = _wait(game, vision, _is_map(vision), "the location map")
    locations, seen, complete = [], set(), False
    while len(locations) < MAX_AREAS:
        name = area_name(frame)
        if not name:
            raise LessonError("couldn't read the location's name")
        if name in seen:
            complete = True             # `>` wrapped back to the first location
            break
        seen.add(name)
        rank = _rank(frame)
        _click(game, ALL_LOCATIONS, vision, ("lesson_location", "all_locations"))
        popup = _wait(game, vision, _is_all_locations(vision), "All Locations")
        rooms = read_rooms(popup)
        locations.append({"location": name, "rank": rank, "rooms": rooms})
        people = sum(len(r["students"]) for r in rooms)
        game.rt.bump("locations read")
        log(f"lessons: {name}: {len(rooms)} rooms, {people} students")
        _save(locations, False)
        _click(game, POPUP_CLOSE, vision, ("All Locations", "close"))
        frame = _wait(game, vision, _is_map(vision), "the location map after closing")
        if limit and len(locations) >= limit:
            break
        frame = _next_area(game, vision, name)
        if frame is None:
            log("lessons: the area stopped changing; saved as partial")
            break
    _save(locations, complete and not limit)
    try:
        from .. import atlas
        atlas.write()                    # an open Lesson Atlas page picks this up by itself
    except Exception as e:
        log(f"lessons: couldn't update the atlas: {e!r}")
    nav.go_lobby(game, vision, log)
    game.rt.task = "idle"
    return {"locations": len(locations), "students": sum(len(r["students"]) for l in locations for r in l["rooms"]),
            "complete": complete and not limit}


# ---------------------------------------------------------------- reading it back
def is_stale(data, now=None):
    return not data.get("ts") or data["ts"] < daily.last_reset(now)


def best_rooms(data, top=8):
    """Unlocked rooms with the most students you own: the most relationship EXP per ticket."""
    rows = []
    for loc in data["locations"]:
        for room in loc["rooms"]:
            owned = [s for s in room["students"] if s["owned"]]
            if owned and not room["locked"]:
                rows.append({"location": loc["location"], "room": room["room"],
                             "students": [s["student"] or "?" for s in owned]})
    return sorted(rows, key=lambda r: -len(r["students"]))[:top]


def find(data, name):
    needle = name.lower()
    return [(l["location"], r["room"], s) for l in data["locations"] for r in l["rooms"] for s in r["students"]
            if needle in str(s.get("student")).lower()]


def show(student=None, owned_only=False, as_json=False):
    data = load()
    if not data["locations"]:
        print("no lesson scan yet: ctl run lesson-scan")
        return 1
    state = "complete" if data.get("complete") else "PARTIAL"
    stale = "  STALE: from before the last daily reset" if is_stale(data) else ""
    if as_json:
        print(json.dumps({**data, "stale": is_stale(data), "best_rooms": best_rooms(data)}, indent=1, ensure_ascii=False))
        return 0
    print(f"Lessons: {len(data['locations'])} locations, scanned {data['scanned_at']} ({state}){stale}")
    if student:
        hits = find(data, student)
        for location, room, s in hits:
            print(f"  {s['student']}: {location} > {room}" + (f" (bond {s['bond']})" if s.get("bond") else ""))
        if not hits:
            print(f"  {student} isn't in any scanned room today")
        return 0
    for loc in data["locations"]:
        print(f"\n{loc['location']}" + (f" (Rank {loc['rank']})" if loc.get("rank") else ""))
        for room in loc["rooms"]:
            people = [s for s in room["students"] if s["owned"] or not owned_only]
            names = ", ".join((s["student"] or "?") + (f" [{s['bond'] or 'owned'}]" if s["owned"] else "") for s in people)
            print(f"  {room['room'][:18]:19}{'locked  ' if room['locked'] else ''}{names}")
    best = best_rooms(data, 5)
    if best:
        print("\nBest rooms (most students you own):")
        for r in best:
            print(f"  {r['location']} > {r['room']}: {', '.join(r['students'])}")
    return 0
