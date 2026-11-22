"""The battlefield from one frame: floating HP bars, who they belong to, and how full they are.

Every unit on the field carries a white-outlined bar. Allies fill theirs lime. Enemies carry a round
dark skull badge on the bar's left end, and that badge is what marks a bar as an enemy's: a bar with
neither reads as "other" and is never targeted. Tick marks split a bar into chunks of HP, so
chunks x fill gives a rough size signal for comparing enemies.
"""
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

AREA = (0, 130, 1920, 860)       # below the top bar, above the cost bar
CARDS = (1262, 820, 1745, 1010)
BAR_H = (7, 17)                  # inner height between the outline's top and bottom edges
BAR_W = (70, 190)
BADGE = Path(__file__).resolve().parents[2] / "templates" / "battler" / "enemy_badge.png"
BADGE_MATCH = 0.7
_badge_img = None


@dataclass
class Bar:
    x1: int
    y1: int
    x2: int
    y2: int
    kind: str                    # "ally", "enemy" or "other"
    fill: float                  # 0..1 of the bar's width
    chunks: int                  # tick-mark segments, at least 1
    shield: bool = False         # an ally's pale shield bar sits on top of the lime one

    @property
    def unit(self):
        """Roughly where the unit stands: bars float above its head."""
        return (self.x1 + self.x2) // 2, self.y2 + 70

    @property
    def size(self):
        return self.chunks * self.fill


def _white(bgr):
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    return cv2.inRange(hsv, (0, 0, 205), (180, 70, 255))


def _lines(mask):
    """Horizontal white runs as (y, x1, x2)."""
    long_runs = cv2.morphologyEx(mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (BAR_W[0] - 10, 1)))
    n, _, stats, _ = cv2.connectedComponentsWithStats(long_runs, connectivity=8)
    out = []
    for x, y, w, h, _ in stats[1:n]:
        if h <= 4 and BAR_W[0] - 10 <= w <= BAR_W[1] + 10:
            out.append((int(y + h // 2), int(x), int(x + w)))
    return sorted(out)


def _pairs(lines):
    """Top and bottom edges of the same bar."""
    used, out = set(), []
    for i, (y1, a1, b1) in enumerate(lines):
        if i in used:
            continue
        for j in range(i + 1, len(lines)):
            y2, a2, b2 = lines[j]
            if y2 - y1 > BAR_H[1]:
                break
            if j in used or y2 - y1 < BAR_H[0]:
                continue
            if abs(a1 - a2) <= 8 and abs(b1 - b2) <= 8:
                used.update((i, j))
                out.append((min(a1, a2), y1, max(b1, b2), y2))
                break
    return out


def _chunks(strip):
    """Tick marks are short dark gaps in a bright fill, or bright marks on a dark track."""
    gray = cv2.cvtColor(strip, cv2.COLOR_BGR2GRAY).astype(np.float32)
    row = gray[max(0, gray.shape[0] // 2 - 1):gray.shape[0] // 2 + 2].mean(axis=0)
    if row.size < 12:
        return 1
    local = cv2.blur(row.reshape(1, -1), (9, 1)).ravel()
    dev = np.abs(row - local)
    ticks, last = 0, -6
    for i in np.flatnonzero(dev > 16):
        if 3 <= i < row.size - 3 and i - last >= 6:
            ticks += 1
            last = i
    return min(ticks + 1, 12)


def _badge():
    global _badge_img
    if _badge_img is None:
        _badge_img = cv2.imread(str(BADGE))
    return _badge_img


def _has_skull(frame, x1, y1, y2):
    """The enemy badge (templates/battler/enemy_badge.png) at the bar's left end."""
    tpl = _badge()
    if tpl is None:
        return False
    cy = (y1 + y2) // 2
    crop = frame[max(0, cy - 22):min(frame.shape[0], cy + 22), max(0, x1 - 34):min(frame.shape[1], x1 + 8)]
    if crop.shape[0] < tpl.shape[0] or crop.shape[1] < tpl.shape[1]:
        return False
    return float(cv2.minMaxLoc(cv2.matchTemplate(crop, tpl, cv2.TM_CCOEFF_NORMED))[1]) >= BADGE_MATCH


def _classify(frame, box):
    x1, y1, x2, y2 = box
    inner = frame[y1 + 2:y2 - 1, x1 + 2:x2 - 2]
    if inner.size == 0:
        return None
    hsv = cv2.cvtColor(inner, cv2.COLOR_BGR2HSV)
    lime = (hsv[:, :, 0] >= 25) & (hsv[:, :, 0] <= 50) & (hsv[:, :, 1] > 130) & (hsv[:, :, 2] > 130)
    w = inner.shape[1]
    if lime.mean() > 0.04:
        cols = np.flatnonzero(lime.mean(axis=0) > 0.4)
        return "ally", (float(cols[-1] + 1) / w if cols.size else 0.0), _chunks(inner)
    bright = hsv[:, :, 2] > 125
    cols = np.flatnonzero(bright.mean(axis=0) > 0.5)
    fill = float(cols[-1] + 1) / w if cols.size else 0.0
    return ("enemy" if _has_skull(frame, x1, y1, y2) else "other"), fill, _chunks(inner)


def bars(frame):
    """Every floating HP bar on the field."""
    ax1, ay1, ax2, ay2 = AREA
    view = np.asarray(frame[ay1:ay2, ax1:ax2])
    out = []
    for x1, y1, x2, y2 in _pairs(_lines(_white(view))):
        box = (x1 + ax1, y1 + ay1, x2 + ax1, y2 + ay1)
        if not BAR_W[0] <= box[2] - box[0] <= BAR_W[1]:
            continue
        if box[2] > CARDS[0] and box[0] < CARDS[2] and box[3] > CARDS[1]:
            continue
        got = _classify(np.asarray(frame), box)
        if not got:
            continue
        if got[0] == "other":            # a shield bar: the ally's lime bar is right below it
            below = (box[0], box[3], box[2], box[3] + (box[3] - box[1]) + 2)
            under = _classify(np.asarray(frame), below) if below[3] < frame.shape[0] else None
            if under and under[0] == "ally":
                out.append(Bar(*below, "ally", round(under[1], 3), under[2], True))
                continue
        out.append(Bar(*box, got[0], round(got[1], 3), got[2]))
    return out


def enemies(found):
    return [b for b in found if b.kind == "enemy"]


def allies(found):
    return [b for b in found if b.kind == "ally"]
