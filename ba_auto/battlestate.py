"""Read the battle HUD from a frame: cost, EX cards, targeting, AUTO, timer and boss.

Everything hangs off the pause button template, so nothing here reports a live battle during a
loading screen or a cutscene. Numbers are matched against digit shapes in templates/battle_digits.
A card counts as ready only when its badge reads, its badge is coloured, and the cost covers it.
"""
from pathlib import Path

import cv2
import numpy as np

from . import ocr

DIGITS = Path(__file__).resolve().parent.parent / "templates" / "battle_digits"
GLYPH = (16, 24)
MATCH, MARGIN = 0.72, 0.03

PAUSE, CANCEL, AUTO_LOCK = "bs_pause", "bs_cancel", "bs_auto_lock"
COST_BOX = (1176, 998, 1228, 1044)
SLOTS = {1: 1345, 2: 1497, 3: 1649}              # card centres on the x axis, clicked at y=930
CARD_Y = 930
BADGE_X = {1: 1309, 2: 1461, 3: 1613}
BADGE_Y, RAISED_DY, RAISED_DX = 875, -37, 6      # a selected card lifts up and shifts right
AUTO_BOX = (1768, 992, 1885, 1040)
AUTO_POINT = (1815, 1015)
TIMER_BOX = (1650, 34, 1800, 76)
BOSS_BOX = (1150, 24, 1370, 58)

_shapes = None


def shapes():
    global _shapes
    if _shapes is None:
        _shapes = {}
        for path in DIGITS.glob("*.png"):
            img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if img is not None:
                _shapes.setdefault(path.stem.split("_")[0], []).append(img)
    return _shapes


def glyphs(mask):
    """Normalised images of the tall blobs in a binary mask, left to right."""
    n, labels, st, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    boxes = [(int(s[0]), int(s[1]), int(s[2]), int(s[3]), i + 1) for i, s in enumerate(st[1:n]) if s[3] >= 12 and s[2] >= 3]
    if not boxes:
        return []
    tall = max(b[3] for b in boxes)
    out = []
    for x, y, w, h, i in sorted(b for b in boxes if b[3] >= tall * 0.65):
        part = np.uint8(labels[y:y + h, x:x + w] == i) * 255
        out.append(cv2.resize(part, GLYPH, interpolation=cv2.INTER_AREA))
    return out


def _classify(glyph):
    scores = {d: max(float(cv2.matchTemplate(glyph, s, cv2.TM_CCOEFF_NORMED)[0][0]) for s in ss)
              for d, ss in shapes().items()}
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    if ranked[0][1] < MATCH or (len(ranked) > 1 and ranked[0][1] - ranked[1][1] < MARGIN):
        return None
    return ranked[0][0]


def _number(parts):
    chars = [_classify(g) for g in parts]
    if not chars or None in chars:
        return None
    value = int("".join(chars))
    return value if 0 <= value <= 10 else None


def cost_mask(crop):
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    return cv2.inRange(hsv, (0, 0, 185), (180, 95, 255))


def badge_mask(crop):
    """The digit inside a card's round badge, ready (white on navy) or not (grey on dark)."""
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    disc = np.zeros((h, w), np.uint8)
    cv2.circle(disc, (w // 2, h // 2), 16, 255, -1)
    values = gray[disc > 0]
    level, _ = cv2.threshold(values.reshape(-1, 1), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return cv2.bitwise_and(np.uint8(gray > level) * 255, disc)


def _badge(frame, slot, raised):
    x = BADGE_X[slot] + (RAISED_DX if raised else 0)
    y = BADGE_Y + (RAISED_DY if raised else 0)
    crop = frame[y - 22:y + 22, x - 22:x + 22]
    number = _number(glyphs(badge_mask(crop)))
    if number is None:
        return None, False
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    h, w = crop.shape[:2]
    ring = np.zeros((h, w), np.uint8)
    cv2.circle(ring, (w // 2, h // 2), 18, 255, -1)
    dark = (hsv[:, :, 2] < 130) & (ring > 0)     # the badge's fill, around the digit
    if dark.sum() < 60:
        return number, False
    return number, float(np.median(hsv[:, :, 1][dark])) > 110


def _auto(vision, frame):
    if vision.find(frame, AUTO_LOCK):
        return "locked"
    x1, y1, x2, y2 = AUTO_BOX
    hsv = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2HSV)
    yellow = cv2.inRange(hsv, (18, 120, 150), (36, 255, 255))
    return "on" if np.count_nonzero(yellow) > yellow.size * 0.3 else "off"


def in_battle(vision, frame):
    return bool(vision.find(frame, PAUSE))


def read(vision, frame, text=False):
    """The HUD's state, or None when no battle HUD is up. text=True also OCRs timer and boss."""
    if not in_battle(vision, frame):
        return None
    x1, y1, x2, y2 = COST_BOX
    cost = _number(glyphs(cost_mask(frame[y1:y2, x1:x2])))
    target = vision.find(frame, CANCEL)
    selected = None
    if target:
        selected = min(SLOTS, key=lambda s: abs(SLOTS[s] - target[0]))
    cards = {}
    for slot in SLOTS:
        number, coloured = _badge(frame, slot, slot == selected)
        cards[slot] = {
            "cost": number,
            "ready": bool(number is not None and coloured and cost is not None and number <= cost
                          and selected is None),
            "selected": slot == selected,
        }
    state = {"cost": cost, "cards": cards, "targeting": selected is not None, "selected": selected,
             "auto": _auto(vision, frame)}
    if text:
        state["timer"] = read_timer(frame)
        state["boss"] = " ".join(w.text for w in ocr.read(frame, BOSS_BOX, scale=2)) or None
    return state


def read_timer(frame, box=TIMER_BOX):
    """mm:ss.mmm from the game's italic timer, or None."""
    import re
    x1, y1, x2, y2 = box
    gray = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
    big = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    h, w = big.shape
    upright = cv2.warpAffine(big, np.float32([[1, .2, 0], [0, 1, 0]]), (w + int(h * .2), h),
                             borderMode=cv2.BORDER_REPLICATE)
    _, ink = cv2.threshold(upright, 175, 255, cv2.THRESH_BINARY)
    img = cv2.copyMakeBorder(255 - ink, 24, 24, 24, 24, cv2.BORDER_CONSTANT, value=255)
    text = "".join(t.text for t in ocr.read(cv2.cvtColor(img, cv2.COLOR_GRAY2BGR), scale=1))
    m = re.search(r"(\d{2})[:.]+(\d{2})\.(\d{3})", text)
    return f"{m.group(1)}:{m.group(2)}.{m.group(3)}" if m else None
