"""Read-only roster scan: walk every student's detail page into data/roster.json.

The task can click exactly four things: Students on the lobby bar, the first student card,
the detail page's `>` arrow, and (through nav) Home or a known popup's X/Cancel. Every click
is checked against the zones of the spending controls on the detail page, and the page must
show the Basic Info tab before `>` is pressed. Anything unexpected stops the scan.
"""
import collections
import difflib
import json
import os
import re
import time
from pathlib import Path

import cv2
import numpy as np

from .. import buttons, nav, ocr, portraits, viewport
from ..screens import _dimmed, label

ROOT = Path(__file__).resolve().parent.parent.parent
DATA = ROOT / "data"
ROSTER = DATA / "roster.json"
UNREAD = DATA / "roster_unread"
TIERS = ROOT / "templates" / "tiers"

LIST_TITLE, ALL_TAB, BASIC_TAB, WEAPON_LOCK = ("roster_list_title", "roster_all_tab",
                                               "roster_basic_tab", "roster_weapon_lock")
STUDENTS_BUTTON = (490, 1030)
FIRST_CARD = (210, 450)
NEXT_ARROW = (1873, 578)
ALLOWED = (STUDENTS_BUTTON, FIRST_CARD, NEXT_ARROW)

# Controls on the detail page that spend or change something, as (x1, y1, x2, y2). Each zone covers
# the control at native 16:9 and in the older stretched captures, so it errs on the large side.
DANGER = {
    "Level Up tab":     (1272, 168, 1542, 258),
    "Unlock Mystic":    (1544, 168, 1824, 258),
    "Details":          (1614, 276, 1800, 328),
    "Talent Unlock":    (1660, 330, 1798, 445),
    "Skill Level Up":   (1656, 476, 1800, 638),
    "Skill icons":      (1014, 474, 1650, 640),
    "Weapon Growth":    (1656, 672, 1800, 804),
    "Weapon":           (1144, 668, 1660, 812),
    "Gear slots":       (1016, 830, 1586, 968),
    "Gear Level Up":    (1584, 830, 1800, 966),
    "Mindscape Bloom":  (862, 240, 980, 366),
}
MARGIN = 40
MAX_STUDENTS = 400
TURN_POLLS = 14                  # about 3.5 s for the next student's page to appear
UNREAD_LIMIT = 40                # pages kept for fields; a page with no name is always kept

ATTACKS = ("Explosive", "Piercing", "Mystic", "Sonic")
ARMORS = ("Light", "Heavy", "Special", "Elastic", "Composite")
ROLES = ("Tank", "Dealer", "Healer", "Support", "T.S.")
POSITIONS = ("Front", "Middle", "Back")
CLASSES = ("Striker", "Special")
WEAPONS = ("SG", "SMG", "HG", "GL", "SR", "AR", "MG", "RL", "MT", "RG", "FT")

# All measured on real pages at native 1920x1080 (data/roster_unread/). The ui-map captures are a
# 1920x1053 window stretched to 1080, so positions taken from them are up to 12 px off.
BOX = {
    "bond": (62, 841, 108, 884), "name": (110, 834, 436, 890), "class": (508, 847, 624, 885),
    "level": (56, 893, 124, 925), "role": (130, 946, 242, 986), "attack": (298, 943, 430, 987),
    "armor": (288, 997, 420, 1041), "position": (56, 997, 236, 1041), "weapon": (1040, 744, 1120, 786),
    "hp": (1125, 345, 1320, 383), "atk": (1395, 345, 1640, 383),
    "def": (1080, 399, 1320, 436), "healing": (1436, 399, 1640, 436),
}
SKILL_BOX = {"ex": (1040, 602, 1160, 636), "basic": (1200, 602, 1320, 636),
             "enhanced": (1360, 602, 1480, 636), "sub": (1516, 602, 1640, 636)}
TERRAIN_AT = {"urban": (453, 1017), "outdoor": (514, 1017), "indoor": (575, 1017)}
WEAPON_HIGH = (1040, 722, 1120, 760)  # some pages draw the type higher, with no shield icon above it
BOND_DIGITS = (68, 846, 102, 880)
FACE_BOX = (330, 230, 660, 560)       # where the student's sprite shows their face, for portraits.identify
# (sharpen, scale, shear, threshold), best first. Measured on the real pages, native and in the tile.
EXP_BOX = (200, 893, 620, 925)        # "22/16685" on the level bar
# The EXP each level needs, read off real pages. It's large plain text, so it settles the small italic level.
EXP_TO_LEVEL = {10: 1, 1685: 39, 1735: 40, 2585: 45, 3985: 50, 4365: 51, 5795: 54, 6385: 55, 7040: 56, 7765: 57,
                9445: 59, 12615: 62, 15220: 64, 16685: 65, 18265: 66, 19965: 67, 29610: 72, 31565: 73}
LEVEL_READS = ((1, 5, .3, 175), (0, 5, .3, 160), (1, 6, .3, 130), (1, 4, 0, 190), (0, 5, 0, 160), (1, 5, .22, 160))
GEAR_SLOTS = (1018, 1158, 1298)
GEAR_LABEL_Y = (912, 960)
TIER_MATCH, TIER_MARGIN = 0.93, 0.03  # real labels score 0.95+; other tiers reach 0.89
LABEL_INK = 120                       # blue pixels that mean a tier label is drawn in the slot
LOCKED_BELOW = 130                    # a locked slot's tile is dark; an open one is pale
# Letters this OCR really confuses. A name is corrected only across one of these, because many real
# students differ from each other by a letter or two (Misaki and Kisaki, Mari and Himari).
CONFUSED = {frozenset(p) for p in ("vy", "uv", "il", "ao", "ce", "hn")}
STARS_BOX = (330, 851, 510, 887)
NAME_BAR = (112, 838, 430, 888)
LEFT_CARD = (30, 780, 650, 1060)      # follows the window's bottom edge
RIGHT_PANEL = (985, 165, 1830, 990)   # one group with the Basic Info tab
# Parts of each group that look the same for every student, cut at native 16:9.
GROUP_ANCHORS = ((LEFT_CARD, "anchor_left.png", (438, 944, 606, 986)),
                 (RIGHT_PANEL, "anchor_right.png", (1040, 188, 1232, 236)))


class RosterError(nav.NavError):
    """The scan met something it won't click through."""


def check_point(point):
    """Refuse any click that isn't allowed or lands near a spending control."""
    x, y = point
    if point not in ALLOWED:
        raise RosterError(f"click at {point} is not on the roster scan's allowlist")
    for name, (x1, y1, x2, y2) in DANGER.items():
        if x1 - MARGIN <= x <= x2 + MARGIN and y1 - MARGIN <= y <= y2 + MARGIN:
            raise RosterError(f"click at {point} is too close to {name}")


def _click(game, point, vision=None, proof=None, anchor=None):
    """proof is a (screen, button) catalog entry or a template that's matched on this screen."""
    check_point(point)
    if vision is not None and proof:
        frame = game.grab()
        if isinstance(proof, tuple):
            proven = buttons.prove(vision, frame, proof[0], proof[1], *point)
        else:
            proven = buttons.tie(vision, frame, proof, *point, anchor=anchor)
        if not proven:
            raise RosterError(f"can't confirm the click at {point} on this frame; stopping")
    game.click(*point)


# ---------------------------------------------------------------- reading a page
def _ocr(frame, box, scales=(3, 2)):
    for scale in scales:
        text = " ".join(w.text for w in ocr.read(frame, box, scale=scale)).strip()
        if text:
            yield text


def _flat(frame, vision):
    """The page with each UI group at its 16:9 place. At 16:9 that's the frame itself."""
    view, canvas = getattr(frame, "view_info", None), getattr(frame, "canvas", None)
    if view is None or view.native or canvas is None:
        return frame
    out = np.array(frame)
    for box, file, mark in GROUP_ANCHORS:
        shift = _group_shift(view, canvas, file, mark)
        if shift is None:                # not this page: leave it, and the reads come back unread
            continue
        x1, y1, x2, y2 = box
        part = viewport.crop(frame, box, shift)
        out[y1:y1 + part.shape[0], x1:x1 + part.shape[1]] = part[:y2 - y1, :x2 - x1]
    return out


def _group_shift(view, canvas, file, mark):
    """Which anchor this UI group is at, from a part of it that's on every student's page."""
    template = cv2.imread(str(TIERS / file))
    if template is None:
        return None
    x1, y1, x2, y2 = mark
    best, score = None, 0.0
    for dx, dy in view.shifts():
        roi = canvas[max(0, y1 + dy - 5):y2 + dy + 5, max(0, x1 + dx - 5):x2 + dx + 5]
        if roi.shape[0] < template.shape[0] or roi.shape[1] < template.shape[1]:
            continue
        s = float(cv2.minMaxLoc(cv2.matchTemplate(roi, template, cv2.TM_CCOEFF_NORMED))[1])
        if s > score:
            best, score = (dx, dy), s
    return best if score >= 0.8 else None


def _alt_reads(frame, box):
    """More OCR passes for a box the plain read missed: sharpened, enlarged and thresholded."""
    x1, y1, x2, y2 = box
    gray = cv2.cvtColor(np.asarray(frame)[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
    for source in (gray, _sharp(gray)):
        for scale in (2, 3, 4):
            big = cv2.resize(source, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            _, ink = cv2.threshold(big, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            if ink.mean() < 127:         # always dark text on a light page
                ink = 255 - ink
            for img in (big, ink):
                pad = cv2.copyMakeBorder(img, 24, 24, 24, 24, cv2.BORDER_REPLICATE)
                text = " ".join(w.text for w in ocr.read(cv2.cvtColor(pad, cv2.COLOR_GRAY2BGR), scale=1)).strip()
                if text:
                    yield text


def _number(frame, box):
    for text in _ocr(frame, box):
        digits = re.sub(r"[^\d]", "", text.replace("O", "0").replace("o", "0"))
        if digits:
            return int(digits)
    seen = []
    for text in _alt_reads(frame, box):  # a second-chance number has to be read the same way twice
        digits = re.sub(r"[^\d]", "", text.replace("O", "0").replace("o", "0"))
        if digits:
            if digits in seen:
                return int(digits)
            seen.append(digits)
    return None


def _choice(frame, box, options):
    """Closest known word. Text that resembles none is kept, marked, rather than guessed."""
    raw = None
    for text in _ocr(frame, box):
        raw = raw or text
        found = difflib.get_close_matches(text.title(), options, n=1, cutoff=0.6)
        if found:
            return found[0]
    for text in _alt_reads(frame, box):
        raw = raw or text
        found = difflib.get_close_matches(text.title(), options, n=1, cutoff=0.7)
        if found:
            return found[0]
    return f"?{raw}" if raw else None


def _skill(frame, box):
    for text in _ocr(frame, box, scales=(2, 3, 4)):
        text = text.replace(" ", "")
        m = re.search(r"(MAX|\d+)$", text, re.I)
        if m:
            return "MAX" if m.group(1).upper() == "MAX" else int(m.group(1))
        if re.search(r"[.,:][lI|!]$", text):     # OCR's usual reading of "Lv.1"
            return 1
    seen = []
    for text in _alt_reads(frame, box):
        m = re.search(r"[.,:]\s*(MAX|\d+|[lI|!])$", text.replace(" ", ""), re.I)
        if m:
            value = "MAX" if m.group(1).upper() == "MAX" else 1 if m.group(1) in "lI|!" else int(m.group(1))
            if value in seen:
                return value
            seen.append(value)
    return None


def _digits(gray, scale):
    big = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    img = cv2.copyMakeBorder(big, 12, 12, 12, 12, cv2.BORDER_CONSTANT, value=255)
    text = " ".join(w.text for w in ocr.read(cv2.cvtColor(img, cv2.COLOR_GRAY2BGR), scale=1))
    return re.sub(r"\D", "", text.replace("O", "0").replace("o", "0"))


def _lone_digit(heart):
    """A single digit in the heart, by shape, because OCR can't read a lone 1 or 7. None if unsure."""
    global _bond_glyphs
    if _bond_glyphs is None:
        _bond_glyphs = {int(path.stem[-1]): cv2.imread(str(path), cv2.IMREAD_GRAYSCALE) for path in TIERS.glob("bond_[0-9].png")}
    ink = (heart < 115).astype(np.uint8)
    ink[:3] = ink[-3:] = 0
    ink[:, :3] = ink[:, -3:] = 0
    n, _, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    tall = [s for s in stats[1:n] if s[3] >= 14]
    if len(tall) != 1:
        return None
    x, y, w, h = (int(v) for v in tall[0][:4])
    if w <= 9:                           # the only digit that is one narrow stroke
        return 1
    glyph = cv2.resize(heart[y:y + h, x:x + w], (16, 24), interpolation=cv2.INTER_AREA)
    scores = sorted(((float(cv2.matchTemplate(glyph, t, cv2.TM_CCOEFF_NORMED)[0][0]), d) for d, t in _bond_glyphs.items()),
                    reverse=True)
    if len(scores) >= 2 and scores[0][0] >= 0.9 and scores[0][0] - scores[1][0] >= 0.1:
        return scores[0][1]
    return None


def _bond(frame):
    """The number in the heart. OCR won't read a lone digit, so a failed read is retried with a
    real "10" from the same heart placed in front of it, and that prefix is removed."""
    x1, y1, x2, y2 = BOND_DIGITS
    heart = cv2.cvtColor(np.asarray(frame)[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
    for scale in (2, 3):
        digits = _digits(heart, scale)
        if digits and 1 <= int(digits) <= 100:
            return int(digits)
    lone = _lone_digit(heart)
    if lone is not None:
        return lone
    prefix = cv2.imread(str(TIERS / "bond_prefix.png"), cv2.IMREAD_GRAYSCALE)
    if prefix is None or prefix.shape != heart.shape:
        return None
    joined = np.hstack([prefix, heart])
    for scale in (2, 3, 2.5):
        digits = _digits(joined, scale)
        if digits.startswith("10") and len(digits) in (3, 4) and 1 <= int(digits[2:]) <= 100:
            return int(digits[2:])
    seen = []
    for text in _alt_reads(frame, BOND_DIGITS):
        digits = re.sub(r"\D", "", text.replace("O", "0").replace("o", "0"))
        if digits and 1 <= int(digits) <= 100:
            if digits in seen:
                return int(digits)
            seen.append(digits)
    return None


def _sharp(gray):
    """Undo the slight blur of a window that isn't exactly 1920 wide."""
    return cv2.addWeighted(gray, 1.8, cv2.GaussianBlur(gray, (0, 0), 1.2), -0.8, 0)


def _weapon(frame):
    for box in (BOX["weapon"], WEAPON_HIGH):
        for text in _ocr(frame, box, scales=(3, 2, 4)):
            token = re.sub(r"[^A-Z]", "", text.upper())
            if token in WEAPONS:
                return token
    for box in (BOX["weapon"], WEAPON_HIGH):
        for text in _alt_reads(frame, box):
            token = re.sub(r"[^A-Z]", "", text.upper())
            if token in WEAPONS:
                return token
    area = np.asarray(frame)[715:795, 1030:1130]
    for path in TIERS.glob("weapon_*.png"):      # labels OCR keeps misreading, by shape
        label = cv2.imread(str(path))
        if label is not None and float(cv2.minMaxLoc(cv2.matchTemplate(area, label, cv2.TM_CCOEFF_NORMED))[1]) >= 0.9:
            return path.stem.split("_")[1]
    for x1, y1, x2, y2 in (BOX["weapon"], WEAPON_HIGH):
        gray = _sharp(cv2.cvtColor(np.asarray(frame)[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY))
        for scale in (2, 3, 4):
            big = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            img = cv2.copyMakeBorder(big, 24, 24, 24, 24, cv2.BORDER_REPLICATE)
            text = "".join(w.text for w in ocr.read(cv2.cvtColor(img, cv2.COLOR_GRAY2BGR), scale=1))
            token = re.sub(r"[^A-Z]", "", text.upper())
            if token in WEAPONS:
                return token
    return None


def _read_name(frame, box):
    """The name on the card. Short names defeat a single OCR pass, so it's read several ways."""
    x1, y1, x2, y2 = box
    reads = [_clean_name(t) for t in _ocr(frame, box, scales=(3, 2, 4))]
    if not any(reads) or not any(r in known_names() or _base(r) in known_names() for r in reads):
        gray = cv2.cvtColor(np.asarray(frame)[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
        for source in (gray, _sharp(gray)):
            for scale, threshold in ((3, 170), (4, 150), (2, 170), (4, 190)):
                big = cv2.resize(source, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
                _, ink = cv2.threshold(big, threshold, 255, cv2.THRESH_BINARY)
                img = cv2.copyMakeBorder(255 - ink, 36, 36, 36, 36, cv2.BORDER_CONSTANT, value=255)
                text = _clean_name(" ".join(w.text for w in ocr.read(cv2.cvtColor(img, cv2.COLOR_GRAY2BGR), scale=1)))
                if text:
                    reads.append(text)
    reads = [r for r in reads if len(_key(r)) >= 2]
    if not reads:
        return ""
    for r in reads:
        if r in known_names() or _base(r) in known_names():
            return r
    return collections.Counter(reads).most_common(1)[0][0]


def _clean_name(text):
    """OCR sometimes loses a capital: every word of a student's name starts with one."""
    text = re.sub(r"[^A-Za-z0-9 ()'.\-*]", "", text).strip(" .-")
    return re.sub(r"(^|[ (])([a-z])", lambda m: m.group(1) + m.group(2).upper(), text)


def _exp_need(frame):
    """The EXP this level needs, from the bar's "have/need" text, or None."""
    for text in _ocr(frame, EXP_BOX, scales=(2, 3)):
        m = re.search(r"(\d+)/(\d{2,6})$", text.replace(" ", ""))
        if m:
            return int(m.group(2))
    return None


def _level_text(frame):
    """(value, agreed): the italic "Lv.NN", and whether two different reads gave the same number."""
    x1, y1, x2, y2 = BOX["level"]
    gray = cv2.cvtColor(np.asarray(frame)[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
    sharp = _sharp(gray)
    seen = []
    for sharpen, scale, shear, threshold in LEVEL_READS:
        big = cv2.resize(sharp if sharpen else gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        h, w = big.shape
        upright = cv2.warpAffine(big, np.float32([[1, shear, 0], [0, 1, 0]]), (w + int(h * shear), h),
                                 borderMode=cv2.BORDER_REPLICATE)
        _, ink = cv2.threshold(upright, threshold, 255, cv2.THRESH_BINARY)
        img = cv2.copyMakeBorder(255 - ink, 30, 30, 30, 30, cv2.BORDER_CONSTANT, value=255)
        text = "".join(w.text for w in ocr.read(cv2.cvtColor(img, cv2.COLOR_GRAY2BGR), scale=1))
        m = re.search(r"[.,](\d{1,3})$", text.replace(" ", ""))
        if m and 1 <= int(m.group(1)) <= 120:
            value = int(m.group(1))
            if value in seen:
                return value, True
            seen.append(value)
    return (seen[0], False) if len(seen) == 1 else (None, False)


def _level(frame):
    """The level, cross-checked: a plausible wrong number is worse than an unread one."""
    need = _exp_need(frame)
    if need in EXP_TO_LEVEL:
        return EXP_TO_LEVEL[need]
    value, agreed = _level_text(frame)
    if value is None:
        return None
    if need is None:                     # nothing to check it against: "Lv.6" may be a cut-off "Lv.65"
        return value if agreed and value >= 10 else None
    below = max((lv for n, lv in EXP_TO_LEVEL.items() if n < need), default=0)
    above = min((lv for n, lv in EXP_TO_LEVEL.items() if n > need), default=121)
    return value if below < value < above else None


def _stars(frame):
    """(count, left edge). Stars touch each other, so count by width (22 px each)."""
    x0, y0, x1, y1 = STARS_BOX
    hsv = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (18, 120, 180), (35, 255, 255))
    n, _, st, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    blobs = [s for s in st[1:n] if s[4] > 150]
    if not blobs:
        return None, 436
    left = min(int(s[0]) for s in blobs)
    right = max(int(s[0] + s[2]) for s in blobs)
    return max(1, min(5, round((right - left) / 22))), x0 + left


def _hue_name(hue):
    for limit, name in ((10, "red"), (20, "orange"), (35, "yellow"), (85, "green"),
                        (130, "blue"), (160, "purple"), (170, "pink")):
        if hue < limit:
            return name
    return "red"


def _terrain(frame):
    """Each terrain face's colour. What each colour is worth has not been confirmed yet."""
    out = {}
    for name, (x, y) in TERRAIN_AT.items():
        hsv = cv2.cvtColor(frame[y - 14:y + 14, x - 14:x + 14], cv2.COLOR_BGR2HSV).reshape(-1, 3)
        vivid = hsv[(hsv[:, 1] > 110) & (hsv[:, 2] > 120)]
        out[name] = _hue_name(float(np.median(vivid[:, 0]))) if len(vivid) > 40 else None
    return out


_tier_glyphs = None
_bond_glyphs = None
_known = None


def _gear_tier(frame, x):
    global _tier_glyphs
    if _tier_glyphs is None:
        _tier_glyphs = {}
        for path in TIERS.glob("T*.png"):
            img = cv2.imread(str(path))
            if img is not None:
                _tier_glyphs[int(path.stem[1:])] = img
    slot = frame[GEAR_LABEL_Y[0]:GEAR_LABEL_Y[1], x:x + 56]
    scores = sorted(((float(cv2.minMaxLoc(cv2.matchTemplate(slot, glyph, cv2.TM_CCOEFF_NORMED))[1]), tier)
                     for tier, glyph in _tier_glyphs.items()), reverse=True)
    if not scores or scores[0][0] < TIER_MATCH or (len(scores) > 1 and scores[0][0] - scores[1][0] < TIER_MARGIN):
        return None                  # a tier with no template yet stays unread instead of a near miss
    return scores[0][1]


def _identify(frame):
    """(art pack id, score) from the student's sprite, or (None, score) when no match is clear."""
    x1, y1, x2, y2 = FACE_BOX
    try:
        dev, score, _ = portraits.identify(np.asarray(frame)[y1:y2, x1:x2])
    except Exception:
        return None, None
    return dev, score


def _gear_slot(frame, x):
    """(state, tier): 'tier' with its number, 'empty', 'locked', or 'unread' for a label with no template."""
    tier = _gear_tier(frame, x)
    if tier is not None:
        return "tier", tier
    slot = np.asarray(frame)[GEAR_LABEL_Y[0]:GEAR_LABEL_Y[1], x:x + 56]
    blue = int(np.count_nonzero(cv2.inRange(cv2.cvtColor(slot, cv2.COLOR_BGR2HSV), (95, 110, 120), (112, 255, 255))))
    if blue >= LABEL_INK:
        return "unread", None
    tile = cv2.cvtColor(np.asarray(frame)[846:858, x + 34:x + 112], cv2.COLOR_BGR2GRAY)
    return ("locked" if float(np.median(tile)) < LOCKED_BELOW else "empty"), None


def _weapon_locked(vision, frame):
    score = vision.match(frame, WEAPON_LOCK)[0]
    if score >= 0.8:
        return True
    if score <= 0.5:
        return False
    # In between, the panel itself says: a locked weapon's panel is dark, an unlocked one is pale.
    panel = cv2.cvtColor(np.asarray(frame)[690:720, 1190:1600], cv2.COLOR_BGR2GRAY)
    level = float(np.median(panel))
    return True if level < 110 else (False if level > 170 else None)


def parse(frame, vision):
    """Everything readable from a Basic Info page."""
    source = frame
    frame = _flat(frame, vision)
    stars, stars_left = _stars(frame)
    x1, y1, x2, y2 = BOX["name"]
    read = _read_name(frame, (x1, y1, min(x2, stars_left - 4), y2))
    name, status = check_name(read)
    dev, face = _identify(frame)
    if not name and dev and not portraits.display(dev).startswith("CH"):
        name, status = portraits.display(dev), "art"     # OCR had nothing usable, but the face is certain
    slots = [_gear_slot(frame, x) for x in GEAR_SLOTS]
    entry = {
        "name": name,
        "dev": dev,
        "dev_score": face,
        "name_read": read or None,
        "name_status": status,
        "level": _level(frame),
        "stars": stars,
        "bond": _bond(frame),
        "class": _choice(frame, BOX["class"], CLASSES),
        "role": _choice(frame, BOX["role"], ROLES),
        "position": _choice(frame, BOX["position"], POSITIONS),
        "attack": _choice(frame, BOX["attack"], ATTACKS),
        "armor": _choice(frame, BOX["armor"], ARMORS),
        "weapon_type": _weapon(frame),
        "terrain": _terrain(frame),
        "stats": {k: _number(frame, BOX[k]) for k in ("hp", "atk", "def", "healing")},
        "skills": {k: _skill(frame, b) for k, b in SKILL_BOX.items()},
        "gear": [tier for _, tier in slots],
        "gear_state": [state for state, _ in slots],
        "weapon_locked": _weapon_locked(vision, source),
        "scanned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    entry["unread"] = _unread_fields(entry)
    return entry


def known_names():
    """Student names the client can vouch for: the art pack's, and those learned next to their faces."""
    global _known
    if _known is None:
        try:
            devs = json.loads((portraits.ART / "manifest.json").read_text(encoding="utf-8"))["students"]
        except (OSError, ValueError):
            devs = {}
        _known = {portraits.display(d) for d in devs if not d.startswith("CH")}
        _known |= {d.partition("_")[0] for d in devs if not d.startswith("CH")}
    return _known | {n for n in portraits.names().values() if n}


def _key(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _base(name):
    return (name or "").partition(" (")[0]


def _one_confusion(a, b):
    """True if the two differ in exactly one letter, and it's a pair OCR really mixes up."""
    if len(a) != len(b):
        return False
    pairs = [frozenset((x, y)) for x, y in zip(a, b) if x != y]
    return len(pairs) == 1 and pairs[0] in CONFUSED


def check_name(read, taken=()):
    """(name or None, status). 'known', 'corrected' to the one close known name, 'new' for a clean
    read that resembles nobody, or 'unverified' when it's close to several and can't be settled."""
    if not read:
        return None, "unread"
    known = known_names()
    if read in known or _base(read) in known:
        return read, "known"
    base = _base(read)
    used = {_key(t) for t in taken}
    variant = read[len(base):]
    close = [n for n in {_base(k) for k in known} if _one_confusion(_key(base), _key(n))]
    free = [n for n in close if _key(n + variant) not in used]
    if len(free) == 1:
        return free[0] + variant, "corrected"
    if close:                            # several candidates, or the only one is already someone else's page
        return None, "unverified"
    return read, "new"


def resolve_names(entries):
    """After a scan: settle unclear names using who else was read, and never let two pages share one."""
    taken = {e["name"] for e in entries if e.get("name") and e.get("name_status") in ("known", "new")}
    for e in entries:
        if e.get("name_status") in ("unverified", "corrected"):
            name, status = check_name(e.get("name_read"), taken)
            e["name"], e["name_status"] = name, status
            if name:
                taken.add(name)
    seen = set()
    for e in entries:
        name = e.get("name")
        if name and name in seen:
            e["name"], e["name_status"] = None, "duplicate"
        elif name:
            seen.add(name)
        if not e.get("name") and "unread" in e and "name" not in e["unread"]:
            e["unread"] = ["name"] + e["unread"]
        elif e.get("name") and "name" in e.get("unread", []):
            e["unread"] = [u for u in e["unread"] if u != "name"]
    return entries


def _unread_fields(e):
    missing = [k for k in ("name", "level", "stars", "bond", "class", "role", "position",
                           "attack", "armor", "weapon_type", "weapon_locked") if e[k] is None]
    missing += [k for k in ("class", "role", "position", "attack", "armor")
                if isinstance(e[k], str) and e[k].startswith("?")]
    missing += [f"terrain.{k}" for k, v in e["terrain"].items() if v is None]
    missing += [f"stats.{k}" for k, v in e["stats"].items() if v is None]
    missing += [f"skills.{k}" for k, v in e["skills"].items() if v is None]
    states = e.get("gear_state") or ["unread" if v is None else "tier" for v in e["gear"]]
    missing += [f"gear.{i + 1}" for i, v in enumerate(states) if v == "unread"]
    return missing


# ---------------------------------------------------------------- walking the pages
def _name_crop(frame):
    x1, y1, x2, y2 = NAME_BAR
    return cv2.cvtColor(np.asarray(frame)[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)


def _same(a, b):
    """Same name bar. Counts changed pixels: one different letter (Mika, Miku) barely moves
    an average, and calling two students the same would end the scan early."""
    return int(np.count_nonzero(cv2.absdiff(a, b) > 50)) < 25


def _panel(frame):
    return frame[260:990, 1000:1820]


def _settled(game):
    """A frame that has stopped changing on the info panel."""
    frame = game.grab()
    for _ in range(6):
        game.sleep(0.25)
        later = game.grab()
        if float(cv2.absdiff(_panel(frame), _panel(later)).mean()) < 1.5:
            return later
        frame = later
    return frame


def _wait_for(game, vision, template, what, polls=20):
    for _ in range(polls):
        frame = game.grab()
        if vision.find(frame, template):
            return frame
        game.sleep(0.25)
    raise RosterError(f"{what} did not appear (on '{label(vision, game.grab())}')")


def _next(game, vision, current):
    """Press `>`. Returns the new page, or None when the page no longer changes."""
    for _ in range(2):                  # the first click after a load is sometimes swallowed
        frame = game.grab()
        if not vision.find(frame, BASIC_TAB) or _dimmed(vision, frame, BASIC_TAB):
            raise RosterError("left the Basic Info page, or a popup is over it; stopping before the next click")
        _click(game, NEXT_ARROW, vision, BASIC_TAB, anchor="center")
        for _ in range(TURN_POLLS):
            game.sleep(0.25)
            frame = game.grab()
            if vision.find(frame, BASIC_TAB) and not _same(_name_crop(frame), current):
                return _settled(game)
    return None


def load():
    if ROSTER.exists():
        return json.loads(ROSTER.read_text(encoding="utf-8"))
    return {"scanned_at": None, "complete": False, "students": []}


def _save(entries, complete):
    """Every page read is kept. Unnamed pages keep their scan position, so a short test scan can't erase
    a longer one and no student disappears because OCR missed a name."""
    DATA.mkdir(parents=True, exist_ok=True)
    unnamed = [e for e in entries if not e.get("name")]
    if complete:
        merged = list(entries)
    else:
        previous = load()["students"]
        fresh = {_key(e["name"]): e for e in entries if e.get("name")}    # "chise" and "Chise" are one student
        here = {e.get("index") for e in unnamed}
        merged, kept = [], set()
        for s in previous:
            if not s.get("name"):
                if s.get("index") not in here:
                    merged.append(s)
            elif _key(s["name"]) not in kept:
                kept.add(_key(s["name"]))
                merged.append(fresh.pop(_key(s["name"]), s))
        merged += list(fresh.values()) + unnamed
    data = {"scanned_at": time.strftime("%Y-%m-%d %H:%M:%S"), "complete": bool(complete and not unnamed),
            "count": len(merged), "pages_read": len(entries), "unnamed": len(unnamed),
            "with_gaps": sum(1 for e in entries if e.get("unread")), "students": merged}
    tmp = ROSTER.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, ROSTER)


def _keep_unread(frame, entry, kept):
    """Save the page when a field couldn't be read. The scan position keeps every file distinct."""
    if not entry["unread"] or (kept >= UNREAD_LIMIT and entry.get("name")):
        return kept
    UNREAD.mkdir(parents=True, exist_ok=True)
    tag = re.sub(r"\W+", "_", entry.get("name") or entry.get("name_read") or "unnamed")
    path = UNREAD / f"{entry.get('index', kept):03d}_{tag}.png"
    cv2.imwrite(str(path), np.asarray(frame))
    entry["page"] = os.path.relpath(path, ROOT)
    return kept + 1


def scan(game, vision, limit=None, log=None):
    """Read students from the lobby onward. Returns the count; never spends anything."""
    log = log or game.log
    game.rt.task = "Roster scan"
    nav.go_lobby(game, vision, log)
    _click(game, STUDENTS_BUTTON, vision, ("lobby", "students"))
    game.sleep(2.5)
    frame = _wait_for(game, vision, LIST_TITLE, "the Students list")
    if not vision.find(frame, ALL_TAB):
        raise RosterError("the Students list isn't on the All tab; select All, then run again")
    try:
        from .. import portraits
        portraits.learn_list(frame)      # pairs faces with the names printed under them
    except Exception as e:
        log(f"roster: couldn't learn portrait names: {e!r}")
    _click(game, FIRST_CARD, vision, LIST_TITLE)
    game.sleep(2.0)
    _wait_for(game, vision, BASIC_TAB, "a student's Basic Info page")
    frame = _settled(game)

    entries, seen, kept, complete = [], [], 0, False
    while True:
        crop = _name_crop(frame)
        if any(_same(crop, s) for s in seen):
            complete = True             # the arrow wrapped back to a student already read
            break
        seen.append(crop)
        entry = parse(frame, vision)
        entry["index"] = len(entries) + 1
        entries.append(entry)
        kept = _keep_unread(frame, entry, kept)
        game.rt.bump("students read")
        log(f"roster: {entry['name']} Lv.{entry['level']}"
            + (f" (unread: {', '.join(entry['unread'])})" if entry["unread"] else ""))
        if len(entries) % 10 == 0:
            resolve_names(entries)
            _save(entries, False)
        if limit and len(entries) >= limit:
            break
        if len(entries) >= MAX_STUDENTS:
            log(f"roster: stopped at the {MAX_STUDENTS} student safety limit")
            break
        frame = _next(game, vision, crop)
        if frame is None:               # could be the last student, or a stalled game
            log("roster: the page stopped changing; saved as partial, so nothing older is dropped")
            break
    resolve_names(entries)
    _save(entries, complete and not limit)
    unread = sum(1 for e in entries if e["unread"])
    unnamed = sum(1 for e in entries if not e.get("name"))
    log(f"roster: {len(entries)} students read and saved, {unread} with unread fields, {unnamed} without a name"
        + (f" (pages in {UNREAD.name}/)" if unread else ""))
    nav.go_lobby(game, vision, log)
    game.rt.task = "idle"
    return {"students": len(entries), "with_unread_fields": unread, "unnamed": unnamed,
            "complete": bool(complete and not limit and not unnamed)}


# ---------------------------------------------------------------- querying
def query(students, name=None, armor=None, attack=None, role=None, klass=None, position=None,
          terrain=None, min_level=None):
    """Filter roster entries. Text filters match any part of the value, ignoring case."""
    def has(value, wanted):
        return wanted is None or (value is not None and wanted.lower() in str(value).lower())

    out = []
    for s in students:
        if not (has(s.get("name"), name) and has(s.get("armor"), armor) and has(s.get("attack"), attack)
                and has(s.get("role"), role) and has(s.get("class"), klass) and has(s.get("position"), position)):
            continue
        if min_level is not None and (s.get("level") or 0) < min_level:
            continue
        if terrain:
            where, _, colour = terrain.partition("=")
            if not has((s.get("terrain") or {}).get(where.lower()), colour or None):
                continue
        out.append(s)
    return out


def _gear_text(tier, state):
    return f"T{tier}" if tier else {"empty": "-", "locked": "x"}.get(state, "?")


def _sort_value(s, key):
    if key in ("hp", "atk", "def", "healing"):
        return (s.get("stats") or {}).get(key) or 0
    value = s.get(key)
    return value if isinstance(value, (int, float)) else 0


def show(args):
    data = load()
    if not data["students"]:
        print("no roster yet: ctl run roster-scan")
        return 1
    rows = query(data["students"], args.name, args.armor, args.attack, args.role, args.klass,
                 args.position, args.terrain, args.min_level)
    if args.sort == "name":
        rows.sort(key=lambda s: s.get("name") or "")
    else:
        rows.sort(key=lambda s: (-_sort_value(s, args.sort), s.get("name") or ""))
    state = "complete" if data.get("complete") else "PARTIAL"
    print(f"{len(rows)} of {data.get('count', len(data['students']))} students  "
          f"(scan {data.get('scanned_at')}, {state})")
    if args.json:
        print(json.dumps(rows[:args.last], indent=1, ensure_ascii=False))
        return 0
    print(f"{'name':26}{'Lv':>3} {'*':>2} {'bond':>4}  {'class':8}{'role':9}{'pos':7}{'attack':10}"
          f"{'armor':8}{'ATK':>6}{'HP':>7}  skills        gear    terrain U/O/I")
    for s in rows[:args.last]:
        sk = s.get("skills") or {}
        tr = s.get("terrain") or {}
        print(f"{str(s.get('name'))[:25]:26}{str(s.get('level') or '?'):>3} {str(s.get('stars') or '?'):>2} "
              f"{str(s.get('bond') or '?'):>4}  {str(s.get('class') or '?'):8}{str(s.get('role') or '?'):9}"
              f"{str(s.get('position') or '?'):7}{str(s.get('attack') or '?'):10}{str(s.get('armor') or '?'):8}"
              f"{str((s.get('stats') or {}).get('atk') or '?'):>6}{str((s.get('stats') or {}).get('hp') or '?'):>7}  "
              f"{'/'.join(str(sk.get(k) or '?') for k in ('ex', 'basic', 'enhanced', 'sub')):13} "
              f"{'/'.join(_gear_text(g, st) for g, st in zip(s.get('gear') or [], s.get('gear_state') or [None] * 9)):8}"
              f"{'/'.join(str(tr.get(k) or '?') for k in ('urban', 'outdoor', 'indoor'))}")
    if len(rows) > args.last:
        print(f"... {len(rows) - args.last} more (--last N)")
    return 0
