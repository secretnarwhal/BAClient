"""Which student is on each EX card. Cards rotate, so this runs on every frame.

Cards are the game's `battle` portrait art, shown in colour when affordable and grey with a diagonal
wipe while cost charges. Matching uses edges of a patch inside the art, so colour and that wipe
barely matter. Only the team's own students are candidates, which keeps it fast and hard to confuse.
A card that doesn't clearly match one candidate reads as None.
"""
import json

import cv2
import numpy as np

from .. import battlestate as bs
from .. import portraits

PATCH = (-40, -34, 46, 38)       # around the card centre: inside the art, clear of badge and number
CARD_CY = 930
SCALES = (0.56, 0.59, 0.62, 0.65, 0.68, 0.71)
MIN_SCORE, MIN_GAP = 0.42, 0.06


def _edges(gray):
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    dx = cv2.Sobel(blur, cv2.CV_32F, 1, 0, ksize=3)
    dy = cv2.Sobel(blur, cv2.CV_32F, 0, 1, ksize=3)
    return cv2.magnitude(dx, dy)


def card_patch(frame, slot, raised=False):
    """Edge image of the middle of a card, or None if it falls outside the frame."""
    hud_frame = getattr(bs, "hud_frame", None)   # Core's bottom-anchored HUD view for non-16:9 windows
    frame = hud_frame(frame) if hud_frame else frame
    cx = bs.SLOTS[slot] + (bs.RAISED_DX if raised else 0)
    cy = CARD_CY + (bs.RAISED_DY if raised else 0)
    x1, y1, x2, y2 = cx + PATCH[0], cy + PATCH[1], cx + PATCH[2], cy + PATCH[3]
    if x1 < 0 or y1 < 0 or x2 > frame.shape[1] or y2 > frame.shape[0]:
        return None
    return _edges(cv2.cvtColor(np.asarray(frame[y1:y2, x1:x2]), cv2.COLOR_BGR2GRAY))


def is_empty(frame, slot):
    """An EMPTY slot has almost no detail compared with a portrait."""
    patch = card_patch(frame, slot)
    return patch is None or float(patch.mean()) < 18


class CardBank:
    """Edge images of the candidates' card art at each trial scale."""

    def __init__(self, devs):
        self.refs = {}
        for dev in devs:
            entry = portraits.art_entry(dev)
            if "battle" not in entry:
                continue
            art = cv2.imread(str(portraits.ART / entry["battle"]), cv2.IMREAD_GRAYSCALE)
            if art is None:
                continue
            self.refs[dev] = [_edges(cv2.resize(art, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)) for s in SCALES]

    def score(self, patch):
        """{dev: best match score} for one card patch."""
        out = {}
        for dev, images in self.refs.items():
            best = -1.0
            for img in images:
                if img.shape[0] >= patch.shape[0] and img.shape[1] >= patch.shape[1]:
                    best = max(best, float(cv2.minMaxLoc(cv2.matchTemplate(img, patch, cv2.TM_CCOEFF_NORMED))[1]))
            out[dev] = best
        return out

    def identify(self, frame, slot, raised=False):
        """(dev or None, score, runner-up score)."""
        patch = card_patch(frame, slot, raised)
        if patch is None or not self.refs:
            return None, 0.0, 0.0
        ranked = sorted(self.score(patch).items(), key=lambda kv: -kv[1])
        top, score = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else -1.0
        if score < MIN_SCORE or score - second < MIN_GAP:
            return None, round(score, 3), round(second, 3)
        return top, round(score, 3), round(second, 3)

    def read(self, frame, selected=None):
        """{slot: dev or None} for the three cards."""
        return {slot: self.identify(frame, slot, slot == selected)[0] for slot in bs.SLOTS}


def all_devs():
    try:
        students = json.loads((portraits.ART / "manifest.json").read_text(encoding="utf-8"))["students"]
    except (OSError, ValueError):
        return []
    return [name for name, entry in students.items() if "battle" in entry]
