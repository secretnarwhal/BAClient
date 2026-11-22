"""The Lesson Atlas: every location's real game map with every student on it, in one zoomable page.

`python -m ba_auto atlas` writes assets/game/atlas_data.js from the last lesson scan and opens
assets/game/atlas.html. It's a local page using art from this PC's install: never upload or publish it.
"""
import json
import os
import re
import shutil
import time
import webbrowser
from pathlib import Path

from . import portraits
from .tasks import lessons

TEMPLATE = Path(__file__).with_name("atlas.html")
ART = portraits.ART
MAPS = ART / "maps" / "maps.json"
PAGE = ART / "atlas.html"
DATA = ART / "atlas_data.js"

# In-game location title -> map name in the game's files.
TITLES = {
    "schale office": "SchaleOffice", "schale residence hall": "SchaleResidence", "gehenna hub": "Gehenna",
    "abydos main building": "Abydos", "millennium study center": "Millennium", "trinity plaza area": "Trinity",
    "red winter federal academy": "Redwinter", "hyakkiyako central area": "Hyakkiyako",
    "du shiratori city": "DU_Shiratori",
}
HIDE = {"DU_Shiratori_Destroyed"}        # the same city in a story state
NAMES = {"SchaleOffice": "Schale Office", "SchaleResidence": "Schale Residence Hall", "Gehenna": "Gehenna Hub",
         "Abydos": "Abydos Main Building", "Millennium": "Millennium Study Center", "Trinity": "Trinity Plaza Area",
         "Redwinter": "Red Winter Federal Academy", "Hyakkiyako": "Hyakkiyako Central Area",
         "DU_Shiratori": "D.U. Shiratori City", "227Onsen": "227 Onsen", "Wildhunt": "Wild Hunt",
         "GameDevRoom": "Game Dev Room", "NewsPaper": "Newspaper"}


def map_key(title, maps):
    """The game-file name for a location title as read on screen."""
    clean = re.sub(r"[^a-z ]", "", (title or "").lower()).strip()
    if clean in TITLES:
        return TITLES[clean]
    squeezed = clean.replace(" ", "")
    for key in maps:
        k = key.lower().replace("_", "")
        if k in squeezed or squeezed in k:
            return key
    return None


def place(zone, students):
    """Balloon positions for a zone: the game's own slots, else a tidy row at its anchor."""
    slots = zone.get("slots") or []
    out = []
    for i, s in enumerate(students):
        if i < len(slots):
            x, y = slots[i]["x"], slots[i]["y"]
        else:
            x, y = zone["x"] + (i - len(slots)) * 190, zone["y"]
        out.append({"x": x, "y": y, "name": s.get("student"), "dev": s.get("dev"),
                    "owned": bool(s.get("owned")), "bond": s.get("bond")})
    return out


def build(maps=None, scan=None):
    maps = json.loads(MAPS.read_text(encoding="utf-8")) if maps is None else maps
    scan = lessons.load() if scan is None else scan
    read = {}
    for loc in scan.get("locations", []):
        key = map_key(loc["location"], maps)
        if key:
            read[key] = loc
    icons, out = {}, []
    for key, m in maps.items():
        if key in HIDE or not m.get("layers"):
            continue
        loc = read.get(key)
        rooms = (loc or {}).get("rooms") or []
        zones = []
        for i, z in enumerate(m["zones"]):
            room = rooms[i] if i < len(rooms) else None
            people = place(z, room["students"]) if room else []
            for p in people:
                entry = portraits.art_entry(p["dev"]) if p["dev"] else {}
                file = entry.get("icon") or entry.get("collection")
                if file:
                    icons[p["dev"]] = file
            zones.append({"id": z["id"], "name": room["room"] if room else z["name"], "x": z["x"], "y": z["y"],
                          "lock": z.get("lock"), "shade": z.get("shade"),
                          "locked": bool(room and room["locked"]), "read": bool(room),
                          "students": people})
        ids = [z["id"] for z in m["zones"] if z.get("id")]
        out.append({"key": key, "order": min(ids) if ids else 9999, "title": loc["location"] if loc else NAMES.get(key, key),
                    "rank": (loc or {}).get("rank"), "scanned": bool(loc), "layers": m["layers"], "zones": zones,
                    "bounds": m.get("bounds") or {}})
    out.sort(key=lambda l: (l["order"], l["title"]))    # the game's own location order
    return {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "scanned_at": scan.get("scanned_at"),
            "complete": bool(scan.get("complete")), "stale": lessons.is_stale(scan) if scan.get("locations") else False,
            "locations": out, "icons": icons}


_cached_maps = (None, None)


def load_maps():
    """maps.json, re-read only when the file changes."""
    global _cached_maps
    stamp = MAPS.stat().st_mtime_ns
    if _cached_maps[0] != stamp:
        _cached_maps = (stamp, json.loads(MAPS.read_text(encoding="utf-8")))
    return _cached_maps[1]


def _shown(maps):
    """Map keys in the game's own location order."""
    keys = [k for k, m in maps.items() if k not in HIDE and m.get("layers")]
    def order(key):
        ids = [z["id"] for z in maps[key]["zones"] if z.get("id")]
        return (min(ids) if ids else 9999, NAMES.get(key, key))
    return sorted(keys, key=order)


def geometry(maps=None):
    """What never changes: each location's map art and zones. The client's page loads this once."""
    maps = load_maps() if maps is None else maps
    return {"locations": [{"key": key, "title": NAMES.get(key, key), "layers": maps[key]["layers"],
                           "bounds": maps[key].get("bounds") or {},
                           "zones": [{"id": z.get("id"), "name": z.get("name"), "x": z["x"], "y": z["y"],
                                      "lock": z.get("lock"), "shade": z.get("shade")} for z in maps[key]["zones"]]}
                          for key in _shown(maps)]}


def live(scan, maps=None, icon=None):
    """The scan placed on the maps. Room ids use the scan's exact names, so a room is never guessed
    from its position. A room with no zone on the map goes in `off_map` and can still be marked."""
    maps = load_maps() if maps is None else maps
    shown, read, off_map = set(_shown(maps)), {}, []

    def rows(loc, zones):
        out, extra = [], []
        for i, room in enumerate(loc.get("rooms") or []):
            zone = zones[i] if i < len(zones) else None
            people = place(zone, room.get("students") or []) if zone else [
                {"x": 0, "y": 0, "name": s.get("student"), "dev": s.get("dev"), "owned": bool(s.get("owned")),
                 "bond": s.get("bond")} for s in room.get("students") or []]
            if icon:
                people = [{**p, "icon": icon(p["dev"])} for p in people]
            row = {"id": f"{loc.get('location')}|{room.get('room')}", "name": room.get("room"), "zone": i if zone else None,
                   "locked": bool(room.get("locked")), "students": people}
            (out if zone else extra).append(row)
        return out, extra

    for loc in scan.get("locations") or []:
        key = map_key(loc.get("location"), maps)
        if key in shown and key not in read:
            read[key] = loc
        else:
            off_map.append({"title": loc.get("location"), "rank": loc.get("rank"), "rooms": rows(loc, [])[1]})
    locations = []
    for key in _shown(maps):
        loc = read.get(key)
        if loc is None:
            locations.append({"key": key, "title": NAMES.get(key, key), "rank": None, "scanned": False, "rooms": []})
            continue
        on_map, extra = rows(loc, maps[key]["zones"])
        locations.append({"key": key, "title": loc.get("location"), "rank": loc.get("rank"), "scanned": True, "rooms": on_map})
        if extra:
            off_map.append({"title": loc.get("location"), "rank": loc.get("rank"), "rooms": extra})
    return {"locations": locations, "off_map": off_map}


def write(data=None):
    data = build() if data is None else data
    tmp = DATA.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text("window.ATLAS = " + json.dumps(data, ensure_ascii=False) + ";\n", encoding="utf-8")
    os.replace(tmp, DATA)
    shutil.copyfile(TEMPLATE, PAGE)
    return data


def open_page():
    write()
    webbrowser.open(PAGE.as_uri())
    return PAGE
