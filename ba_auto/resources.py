"""Read AP, credits and pyroxene from the top bar.

OCR on these numbers is patchy, so each value is read several ways and needs at least two
reads to agree. A value that can't be read is None, which means unknown and never "safe".
`crops` returns the raw number images: comparing them shows whether a value changed at all,
even when the digits can't be read.
"""
import collections
import re
from pathlib import Path

import cv2
import numpy as np

from . import ocr

GLYPH_DIR = Path(__file__).resolve().parent.parent / "templates" / "digits"
GLYPH_SIZE = (14, 22)
MATCH, MARGIN = 0.82, 0.04
SUB = {"ap": (762, 18, 884, 54), "credits": (1072, 18, 1244, 54), "pyroxene": (1400, 18, 1560, 54)}
LOBBY = {"ap": (806, 36, 908, 74), "credits": (1050, 36, 1195, 74), "pyroxene": (1348, 36, 1480, 74)}
READS = ((1.5, cv2.INTER_CUBIC, 60), (1.5, cv2.INTER_LANCZOS4, 60), (2, cv2.INTER_LANCZOS4, 60),
         (2, cv2.INTER_LANCZOS4, 12), (2.5, cv2.INTER_CUBIC, 60), (3, cv2.INTER_CUBIC, 60),
         (3, cv2.INTER_LANCZOS4, 12), (4, cv2.INTER_CUBIC, 60))


def layout(frame):
    """Which bar is on screen, or None (a popup dims the bar, and battles have none)."""
    top = frame[3:11, 700:1600]
    if top.mean() > 225 and top.std() < 14:
        return SUB
    strip = frame[44:66, 1205:1285]                  # the lobby's pale credits chip
    if strip.mean() > 200 and frame[0:8, 700:1600].mean() < 170:
        return LOBBY
    return None


def crops(frame):
    bar = layout(frame)
    if bar is None:
        return None
    return {k: cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY) for k, (x1, y1, x2, y2) in bar.items()}


def changed(a, b):
    """Whether two number images differ by more than rendering noise."""
    if a is None or b is None or a.shape != b.shape:
        return None
    return int(np.count_nonzero(cv2.absdiff(a, b) > 60)) >= 12


def _variants(gray):
    for fx, interp, pad in READS:
        big = cv2.resize(gray, None, fx=fx, fy=fx, interpolation=interp)
        lo, hi = np.percentile(big, (3, 97))
        img = np.clip((big.astype(float) - lo) * 255 / max(1, hi - lo), 0, 255).astype(np.uint8)
        img = cv2.copyMakeBorder(img, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)
        yield cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


def _parse(name, text):
    text = text.replace(" ", "").replace("O", "0").replace("o", "0")
    if name == "ap":
        m = re.fullmatch(r"(\d{1,4})/(\d{2,4})", text)
        return f"{int(m.group(1))}/{int(m.group(2))}" if m else None
    if re.fullmatch(r"\d{1,3}(,\d{3})*|\d+", text):
        return int(text.replace(",", ""))
    return None


def ocr_value(name, gray):
    """Agreement across several OCR reads. Used to label the digit shapes."""
    votes = collections.Counter()
    for img in _variants(gray):
        value = _parse(name, "".join(w.text for w in ocr.read(img, scale=1)))
        if value is not None:
            votes[value] += 1
    if not votes:
        return None
    (best, n), *rest = votes.most_common(2)
    return best if n >= 2 and (not rest or rest[0][1] < n) else None


# ---- digit shapes: the bar's font is fixed, so matching beats OCR once they're collected
def _spans(cols):
    spans, start = [], None
    for x, c in enumerate(list(cols) + [0]):
        if c and start is None:
            start = x
        elif not c and start is not None:
            spans.append((start, x))
            start = None
    return spans


def segment(gray):
    """Glyph images left to right, without commas. Touching digits are split at their
    thinnest column. Returns [] when the crop has no clear text."""
    lo, hi = np.percentile(gray, (2, 92))
    if hi - lo < 60:
        return []
    ink = gray < lo + (hi - lo) * 0.52
    boxes = []
    for a, b in _spans(ink.sum(axis=0)):
        rows = np.flatnonzero(ink[:, a:b].any(axis=1))
        boxes.append((int(rows[0]), int(rows[-1])))
    boxes = [t for t in boxes if t[1] - t[0] >= 9]
    if not boxes:
        return []
    top, bottom = int(np.median([t[0] for t in boxes])), int(np.median([t[1] for t in boxes]))
    tall = bottom - top + 1
    # Columns are measured above the baseline: a comma hangs below it and touches the digit before.
    cols = ink[top:max(top + 3, bottom - 3)].sum(axis=0)
    spans = [(a, b) for a, b in _spans(cols)
             if ink[top:bottom + 1, a:b].any(axis=1).sum() >= tall * 0.6]
    if not spans:
        return []
    # A "1" is far narrower than other digits, so a run of them mustn't set the width used to split a blob.
    wide = [b - a for a, b in spans if tall * 0.55 <= b - a <= tall * 0.85]
    unit = float(np.median(wide)) if wide else tall * 0.62
    out = []
    for a, b in spans:
        parts = max(1, round((b - a) / unit))
        edges = [a]
        for i in range(1, parts):
            guess = a + (b - a) * i // parts
            window = range(max(a + 1, guess - 3), min(b - 1, guess + 4))
            edges.append(min(window, key=lambda x: cols[x]) if len(window) else guess)
        edges.append(b)
        for x1, x2 in zip(edges, edges[1:]):
            if x2 <= x1:                 # a split can land on the span's edge
                continue
            out.append(cv2.resize(gray[top:bottom + 1, x1:x2], GLYPH_SIZE, interpolation=cv2.INTER_AREA))
    return out


_glyphs = None


def glyphs():
    global _glyphs
    if _glyphs is None:
        _glyphs = {}
        for path in GLYPH_DIR.glob("*.png"):
            img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if img is not None:
                _glyphs.setdefault(path.stem.split("_")[0].replace("slash", "/"), []).append(img)
    return _glyphs


def classify(glyph):
    """The character this shape is, or None unless it's a clear match."""
    best = {}
    for ch, samples in glyphs().items():
        best[ch] = max(float(cv2.matchTemplate(glyph, s, cv2.TM_CCOEFF_NORMED)[0][0]) for s in samples)
    if not best:
        return None
    ranked = sorted(best.items(), key=lambda kv: -kv[1])
    if ranked[0][1] < MATCH or (len(ranked) > 1 and ranked[0][1] - ranked[1][1] < MARGIN):
        return None
    return ranked[0][0]


def read_value(name, gray):
    parts = segment(gray)
    if not parts:
        return None
    chars = [classify(g) for g in parts]
    if None in chars:
        return None
    return _parse(name, "".join(chars))


def read_any(frame):
    """Like read(), but also reads a bar dimmed under a popup. For showing values only: the
    watchdog uses read_value() on crops(), so a dimmed bar can never trip it."""
    values = read(frame)
    if any(v is not None for v in values.values()):
        return values
    for bar in (SUB, LOBBY):
        images = {k: cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY) for k, (x1, y1, x2, y2) in bar.items()}
        dimmed = {k: read_value(k, img) for k, img in images.items()}
        if dimmed["ap"] is not None:     # "a/b" is a strong sign this really is the bar
            return dimmed
    return values


def read(frame):
    """{"ap": "619/206", "credits": int, "pyroxene": int}, None for anything unreadable."""
    images = crops(frame)
    if images is None:
        return {"ap": None, "credits": None, "pyroxene": None}
    return {k: read_value(k, img) for k, img in images.items()}
