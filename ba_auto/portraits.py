"""Identify students on screen by matching their faces against the local art pack.

assets/game/ is an optional local art pack and stays on this PC. Matching works on any crop of a
student card. A result is returned only when the best match clearly beats the runner-up, so an
unknown face reads as None rather than as the nearest guess.
"""
import json
import os
import re
from pathlib import Path

import cv2
import numpy as np

ART = Path(__file__).resolve().parent.parent / "assets" / "game"
CANDIDATE_KINDS = ("collection", "icon", "icon_small", "battle")
WIDTHS = (96, 112, 128, 146, 168)    # candidate sizes tried against a 96 px wide crop
PATCH = (56, 56)
MIN_SCORE, MIN_GAP = 0.62, 0.05

# On-screen card positions as (x1, y1, x2, y2), by the screen that shows them.
LAYOUTS = {
    "students": [(x, y, x + 262, y + 248) for y in (307, 721) for x in (80, 375, 669, 963, 1257, 1552)],
    "battle_result": [(82, 918, 206, 1012), (232, 918, 356, 1012), (382, 918, 506, 1012), (534, 918, 658, 1012),
                      (712, 918, 836, 1012), (862, 918, 986, 1012)],
}
# All Locations: room name -> portrait boxes, inset to stay inside the slanted frames.
ROOMS = {
    "Theater Room": [(234, 390, 310, 462), (342, 390, 418, 462)],
    "Gym": [(750, 390, 826, 462), (858, 390, 934, 462), (966, 390, 1042, 462)],
    "Library": [(1266, 390, 1342, 462)],
    "Classroom": [(234, 623, 310, 695), (342, 623, 418, 695), (450, 623, 526, 695)],
    "Laboratory": [(750, 623, 826, 695)],
}

NAMES = ART.parent.parent / "data" / "portrait_names.json"
SHORTLIST = 14
_bank = None
_small = None
_manifest = None


def _clean(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def bank():
    """{dev name: [images at each trial width]}, built once from the art pack."""
    global _bank
    if _bank is None:
        _bank = {}
        try:
            students = json.loads((ART / "manifest.json").read_text(encoding="utf-8"))["students"]
        except (OSError, ValueError):
            students = {}
        for name, entry in students.items():
            if not re.match(r"^(CH\d+|[A-Z][a-z]+(_[A-Za-z0-9]+)*)$", name) or "guard" in name.lower():
                continue
            images = []
            for kind in CANDIDATE_KINDS:
                if kind not in entry:
                    continue
                img = cv2.imread(str(ART / entry[kind]))
                if img is None:
                    continue
                for w in WIDTHS:
                    images.append(cv2.resize(img, (w, max(PATCH[1] + 2, round(img.shape[0] * w / img.shape[1]))),
                                             interpolation=cv2.INTER_AREA))
            if images:
                _bank[name] = images
    return _bank


def _patch(crop):
    """The face area of a card crop at the size candidates are searched with."""
    h = max(PATCH[1] + 4, round(crop.shape[0] * 96 / crop.shape[1]))
    small = cv2.resize(crop, (96, h), interpolation=cv2.INTER_AREA)
    x = (96 - PATCH[0]) // 2
    y = max(0, min(h - PATCH[1], round(h * 0.18)))
    return small[y:y + PATCH[1], x:x + PATCH[0]]


def _best(images, patch):
    best = -1.0
    for img in images:
        if img.shape[0] >= patch.shape[0] and img.shape[1] >= patch.shape[1]:
            best = max(best, float(cv2.minMaxLoc(cv2.matchTemplate(img, patch, cv2.TM_CCOEFF_NORMED))[1]))
    return best


def _coarse():
    """Half-size copies, for a fast first pass over every student."""
    global _small
    if _small is None:
        _small = {name: [cv2.resize(i, (i.shape[1] // 2, i.shape[0] // 2), interpolation=cv2.INTER_AREA)
                         for i in images] for name, images in bank().items()}
    return _small


def identify(crop):
    """(dev name or None, score, runner-up score)."""
    patch = _patch(crop)
    small = cv2.resize(patch, (PATCH[0] // 2, PATCH[1] // 2), interpolation=cv2.INTER_AREA)
    rough = sorted(((_best(images, small), name) for name, images in _coarse().items()), reverse=True)
    scores = {name: _best(bank()[name], patch) for _, name in rough[:SHORTLIST]}
    if not scores:
        return None, 0.0, 0.0
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top, score = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    if score < MIN_SCORE or score - second < MIN_GAP:
        return None, round(score, 3), round(second, 3)
    return top, round(score, 3), round(second, 3)


def who(frame, layout):
    """Students on a known layout, in screen order."""
    if layout == "rooms":
        return {room: [_entry(frame, b) for b in boxes] for room, boxes in ROOMS.items()}
    return [_entry(frame, b) for b in LAYOUTS[layout]]


def art_entry(dev):
    """The art pack's files for a student, or {}."""
    global _manifest
    if _manifest is None:
        try:
            _manifest = json.loads((ART / "manifest.json").read_text(encoding="utf-8"))["students"]
        except (OSError, ValueError):
            _manifest = {}
    return _manifest.get(dev) or {}


def _entry(frame, box):
    x1, y1, x2, y2 = box
    dev, score, _ = identify(frame[y1:y2, x1:x2])
    return {"student": display(dev), "dev": dev, "score": score}


def names():
    """Learned {dev name: name shown in game}."""
    try:
        return json.loads(NAMES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def display(dev):
    """The in-game name if it has been seen, else a readable form of the dev name."""
    if dev is None:
        return None
    known = names().get(dev)
    if known:
        return known
    if dev.startswith("CH"):
        return dev
    base, _, variant = dev.partition("_")
    return f"{base} ({variant})" if variant else base


def learn(pairs):
    """Remember {dev name: in-game name} seen together on one screen."""
    current = names()
    fresh = {d: n for d, n in pairs if d and n and current.get(d) != n}
    if fresh:
        NAMES.parent.mkdir(parents=True, exist_ok=True)
        current.update(fresh)
        tmp = NAMES.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(current, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(tmp, NAMES)
    return len(fresh)


def learn_list(frame):
    """Pair the Students list's cards with the names printed under them."""
    from . import ocr
    pairs = []
    for x1, y1, x2, y2 in LAYOUTS["students"]:
        label = " ".join(w.text for w in ocr.read(frame, (x1 - 22, y2 + 72, x2 + 22, y2 + 116), scale=2)).strip()
        if label:
            pairs.append((identify(frame[y1:y2, x1:x2])[0], label))
    return learn(pairs)
