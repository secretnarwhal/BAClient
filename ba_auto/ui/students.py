"""The Students page's list: everyone in the art pack, merged with what the game has really shown.

Nothing is filled in from outside the game. A value that wasn't read stays out of the row, so the
page shows it as unread. Ownership has three answers, because a partial scan can't say someone is
missing: "yes", "no" or "unknown", each with where the answer came from. Art that only has a code
like CH0345 can't be matched to a roster name, so a finished scan says nothing about it.
"""
import hashlib
import json
import re

from . import art, thumbs

FIELDS = ("level", "stars", "bond", "class", "role", "position", "attack", "armor", "weapon_type", "weapon_locked")
NESTED = ("terrain", "stats", "skills")
CODE = re.compile(r"CH\d+")


def _read(value):
    """None for a value the scanner couldn't read, including its '?guess' strings."""
    return None if isinstance(value, str) and value.startswith("?") else value


def _blanks(row):
    count = sum(_read(row.get(k)) is None for k in FIELDS)
    count += sum(_read(v) is None for k in NESTED for v in (row.get(k) or {}).values())
    return count + sum(v is None for v in row.get("gear") or [])


def _still_unread(key, row):
    head, _, tail = key.partition(".")
    if head == "gear" and tail.isdigit():
        gear = row.get("gear") or []
        index = int(tail) - 1
        return index >= len(gear) or gear[index] is None
    if head in NESTED and tail:
        return _read((row.get(head) or {}).get(tail)) is None
    return _read(row.get(key)) is None


def _combine(rows):
    """One student scanned more than once. The most complete reading wins, and blanks fill from the others."""
    ordered = sorted(rows, key=lambda r: (_blanks(r), _invert(r.get("scanned_at"))))
    merged = dict(ordered[0])
    for key in FIELDS:
        merged[key] = next((v for v in (_read(r.get(key)) for r in ordered) if v is not None), None)
    for key in NESTED:
        names = [n for r in ordered for n in (r.get(key) or {})]
        merged[key] = {n: next((v for v in (_read((r.get(key) or {}).get(n)) for r in ordered) if v is not None), None)
                       for n in dict.fromkeys(names)}
    size = max((len(r.get("gear") or []) for r in ordered), default=0)
    merged["gear"] = [next((g[i] for g in (r.get("gear") or [] for r in ordered) if i < len(g) and g[i] is not None), None)
                      for i in range(size)]
    merged["unread"] = [k for k in dict.fromkeys(k for r in rows for k in r.get("unread") or []) if _still_unread(k, merged)]
    return merged


def _invert(stamp):
    """Sorts newer timestamps first."""
    return "".join(chr(0x10FFFF - ord(c)) for c in str(stamp or ""))


def _owned_in_lessons(scan):
    seen = {}
    for location in (scan or {}).get("locations") or []:
        for room in location.get("rooms") or []:
            for student in room.get("students") or []:
                dev, owned = student.get("dev"), student.get("owned")
                if dev and isinstance(owned, bool):
                    seen.setdefault(dev, set()).add(owned)
    return seen


def _ownership(in_roster, flags, complete, coded=False, unlinked_rows=False):
    """(owned, source, note). `coded` is art with no name yet. `unlinked_rows` means some roster
    names have no art, so a coded portrait seen as owned is probably one of them, not an extra student."""
    if in_roster:
        return "yes", "roster", None
    if flags == {True}:
        if unlinked_rows and coded:
            return "unknown", "lessons", "Yours in Lessons, name not linked yet"
        if unlinked_rows and complete:  # a finished scan missed this name, so one of its rows is probably a misread of it
            return "unknown", "lessons", "Yours in Lessons, not matched to the roster"
        return "yes", "lessons", None
    if flags == {False}:
        return "no", "lessons", None
    if flags:
        return "unknown", None, "Lesson screens disagreed"
    if coded:
        return "unknown", None, "Name not linked yet"
    return ("no", "roster", None) if complete else ("unknown", None, None)


def _art(dev):
    urls = art.urls(dev) if dev else {}
    if dev and urls.get("portrait"):
        urls["thumb"] = thumbs.url(dev, thumbs.SIZES[1])
        urls["thumb_small"] = thumbs.url(dev, thumbs.SIZES[0])
    return {k: v for k, v in urls.items() if k in ("thumb", "thumb_small", "portrait", "full", "icon", "small") and v}


def _display_name(rows, dev):
    """A scanned name, unless the scanner's reading lost its capitals."""
    for row in rows:
        name = row.get("name")
        if name and not name.islower():
            return name
    return art.display(dev) if dev else next((r.get("name") for r in rows if r.get("name")), None)


def build(roster, scan, catalog=None):
    """{rev, counts, students}. `roster` is Store.roster() and `scan` is the last lesson scan."""
    catalog = art.catalog() if catalog is None else catalog
    complete = bool(roster.get("complete"))
    flags = _owned_in_lessons(scan)
    grouped, order = {}, []
    for row in roster.get("students") or []:
        dev = row.get("dev")
        name = row.get("name")
        key = dev or f"name:{re.sub(r'[^a-z0-9]', '', str(name or '').lower())}"
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(row)

    students, seen = [], set()
    for key in order:
        rows = grouped[key]
        row = _combine(rows)
        dev = row.get("dev")
        seen.add(dev)
        owned, source, note = _ownership(True, flags.get(dev), complete)
        entry = {"id": key, "name": _display_name(rows, dev) or "Unknown", "dev": dev, "owned": owned, "owned_from": source,
                 "owned_note": note, "read": True, "scanned_at": row.get("scanned_at"), "unread": row.get("unread") or [], "art": _art(dev)}
        entry.update({k: row[k] for k in FIELDS if row.get(k) is not None})
        for k in NESTED:
            if row.get(k):
                entry[k] = row[k]
        if row.get("gear"):
            entry["gear"] = row["gear"]
        students.append(entry)
    unlinked_rows = any(not s["dev"] for s in students)
    for person in catalog:
        dev = person["dev"]
        if dev in seen:
            continue
        seen.add(dev)
        coded = bool(CODE.fullmatch(person["name"]))
        owned, source, note = _ownership(False, flags.get(dev), complete, coded, unlinked_rows)
        students.append({"id": dev, "name": person["name"], "dev": dev, "owned": owned, "owned_from": source,
                         "owned_note": note, "read": False, "art": _art(dev)})

    counts = {"all": len(students), "yes": 0, "no": 0, "unknown": 0, "read": 0,
              "no_art": sum(s["read"] and not s["art"] for s in students)}
    for s in students:
        counts[s["owned"]] += 1
        counts["read"] += s["read"]
    body = {"scanned_at": roster.get("scanned_at"), "complete": complete, "error": roster.get("error"),
            "counts": counts, "students": students}
    text = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return {"rev": hashlib.sha1(text).hexdigest()[:16], **body}
