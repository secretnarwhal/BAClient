"""Where the game's picture is inside its window, and conversion to and from reference space.

Tasks, templates and the journal work in the 1920x1080 "reference" layout. The rules below come
from the game's own UI prefabs (NGUI anchors, design size 2960x1440) and its letterbox prefab:

  - The UI fits a 2960x1440 design box: width-limited up to 2.06:1, height-limited beyond that.
    One reference unit is 2960/1920 design units, so the canvas is 1920 wide below 2.06:1.
  - Narrower than 4:3, the game draws a centred 4:3 picture with decorated bars around it.
  - UI groups anchor to the canvas edges. A widget stays with the top, the centre or the bottom
    (and left, centre or right), so at another shape it sits at one of a few known offsets.

`Vision` tries those offsets when matching. `View.compose` also rebuilds a best-effort reference
frame for code that crops fixed regions. All conversion lives in this file.

Any shape other than 16:9 counts as unverified: Game clicks there only at a target matched in
the current frame. A picture that can't be confirmed refuses input altogether.
"""
import threading
import time
from collections import deque

import cv2
import numpy as np

REF_W, REF_H = 1920, 1080
REF_ASPECT = REF_W / REF_H
DESIGN_W, DESIGN_H = 2960, 1440
MIN_ASPECT = 4 / 3               # narrower windows get a 4:3 picture with bars
MAX_ASPECT = DESIGN_W / DESIGN_H # wider than this hasn't been seen, so it needs bars to be trusted
MIN_CANVAS_H = DESIGN_H * REF_W / DESIGN_W
TOLERANCE = 0.02
TOP_BAND, BOTTOM_BAND = 130, 900 # reference rows that travel with the top and bottom edges
LEFT_BAND, RIGHT_BAND = 420, 1500
EDGE_MIN = 7.0                   # how strong a bar's edge must be, in grey levels
TARGET_SECONDS = 8.0
TARGET_TOLERANCE = 16


class ViewFrame(np.ndarray):
    """A reference-space frame that remembers its view and the true-position canvas."""

    def __new__(cls, array, view=None, canvas=None):
        obj = np.asarray(array).view(cls)
        obj.view_info, obj.canvas, obj.hits = view, canvas, {}
        return obj

    def __array_finalize__(self, obj):
        self.view_info = getattr(obj, "view_info", None)
        self.canvas = getattr(obj, "canvas", None)
        self.hits = getattr(obj, "hits", {})


class View:
    def __init__(self, client, rect, confident=True, reason=None):
        self.client = tuple(int(v) for v in client)      # screen x, y, w, h
        self.rect = tuple(int(v) for v in rect)          # picture x, y, w, h inside the client
        self.confident, self.reason = confident, reason
        _, _, vw, vh = self.rect
        aspect = vw / vh if vh > 0 else REF_ASPECT
        self.native = abs(aspect / REF_ASPECT - 1) <= TOLERANCE
        if self.native:
            self.canvas_w, self.canvas_h = float(REF_W), float(REF_H)
        else:
            self.canvas_w = max(float(REF_W), MIN_CANVAS_H * aspect)
            self.canvas_h = self.canvas_w / aspect
        self.scale = vw / self.canvas_w if vw > 0 else 1.0
        self.dx = self.canvas_w - REF_W
        self.dy = self.canvas_h - REF_H  # negative on windows wider than 16:9

    def shifts(self):
        """Where a reference element can be on the canvas: with either edge, or centred."""
        if self.native:
            return [(0, 0)]
        dx, dy = round(self.dx), round(self.dy)
        xs = [0, dx // 2, dx] if dx else [0]
        ys = [0, round(self.dy / 2), dy] if dy else [0]
        return [(x, y) for y in ys for x in xs]

    def default_shift(self, x, y):
        """The usual anchor for a reference point, used only for best-effort frame rebuilding."""
        if self.native:
            return 0, 0
        sy = 0 if y < TOP_BAND else round(self.dy / 2) if y < BOTTOM_BAND else round(self.dy)
        sx = 0 if x < LEFT_BAND else round(self.dx / 2) if x < RIGHT_BAND else round(self.dx)
        return sx, sy

    # ---- conversion ----
    def canvas_to_client(self, cx, cy):
        if self.native:
            return self.rect[0] + cx * self.rect[2] / REF_W, self.rect[1] + cy * self.rect[3] / REF_H
        return self.rect[0] + cx * self.scale, self.rect[1] + cy * self.scale

    def canvas_to_screen(self, cx, cy):
        px, py = self.canvas_to_client(cx, cy)
        return int(self.client[0] + px), int(self.client[1] + py)

    def to_client(self, x, y, shift=None):
        dx, dy = shift if shift is not None else self.default_shift(x, y)
        return self.canvas_to_client(x + dx, y + dy)

    def to_screen(self, x, y, shift=None):
        dx, dy = shift if shift is not None else self.default_shift(x, y)
        return self.canvas_to_screen(x + dx, y + dy)

    def contains_screen(self, sx, sy):
        x, y, w, h = self.rect
        px, py = sx - self.client[0], sy - self.client[1]
        return x <= px < x + w and y <= py < y + h

    # ---- frames ----
    def compose(self, raw):
        """The client capture as a 1920x1080 reference frame."""
        x, y, w, h = self.rect
        picture = raw[y:y + h, x:x + w]
        if self.native:
            if (w, h) == (REF_W, REF_H):
                return ViewFrame(np.ascontiguousarray(picture), self)
            interp = cv2.INTER_AREA if w > REF_W else cv2.INTER_LINEAR
            return ViewFrame(cv2.resize(picture, (REF_W, REF_H), interpolation=interp), self)
        size = (round(self.canvas_w), round(self.canvas_h))
        canvas = cv2.resize(picture, size, interpolation=cv2.INTER_AREA if self.scale > 1 else cv2.INTER_LINEAR)
        out = np.zeros((REF_H, REF_W, 3), np.uint8)
        dx, dy = round(self.dx), round(self.dy)
        rows = ((0, TOP_BAND, 0), (TOP_BAND, BOTTOM_BAND, round(self.dy / 2)), (BOTTOM_BAND, REF_H, dy)) if dy else ((0, REF_H, 0),)
        cols = ((0, LEFT_BAND, 0), (LEFT_BAND, RIGHT_BAND, dx // 2), (RIGHT_BAND, REF_W, dx)) if dx else ((0, REF_W, 0),)
        for r0, r1, ry in rows:
            for c0, c1, cx in cols:
                part = canvas[max(0, r0 + ry):max(0, r1 + ry), max(0, c0 + cx):max(0, c1 + cx)]
                top, left = max(0, -(r0 + ry)), max(0, -(c0 + cx))
                ph, pw = min(part.shape[0], r1 - r0 - top), min(part.shape[1], c1 - c0 - left)
                if ph > 0 and pw > 0:
                    out[r0 + top:r0 + top + ph, c0 + left:c0 + left + pw] = part[:ph, :pw]
        return ViewFrame(out, self, canvas)

    def info(self):
        return {"client": list(self.client), "view": list(self.rect), "scale": round(self.scale, 4),
                "native": self.native, "confident": self.confident}


def crop(frame, box, shift=(0, 0)):
    """Pixels of a reference box (x1, y1, x2, y2) on an element found at anchor offset `shift`.
    Use vision.shift_of(frame, template) for the shift. At 16:9 this is just frame[y1:y2, x1:x2]."""
    x1, y1, x2, y2 = box
    view, canvas = getattr(frame, "view_info", None), getattr(frame, "canvas", None)
    if view is None or view.native or canvas is None:
        return np.asarray(frame[y1:y2, x1:x2])
    dx, dy = shift
    return np.asarray(canvas[max(0, y1 + dy):max(0, y2 + dy), max(0, x1 + dx):max(0, x2 + dx)])


def from_rect(x, y, w, h):
    """A view for a window that hasn't been captured: only 16:9 can be assumed."""
    client = (x, y, w, h)
    if h > 0 and abs((w / h) / REF_ASPECT - 1) <= TOLERANCE:
        return View(client, (0, 0, w, h))
    return View(client, (0, 0, w, h), False, f"the game's picture in the {w}x{h} window hasn't been confirmed")


def _bars(gray, want):
    """The two edges of a centred picture `want` rows long, if they're really in the image."""
    size = gray.shape[0]
    edges = np.abs(np.diff(gray.astype(np.int16), axis=0)).mean(axis=1)
    start = (size - want) / 2
    t0 = max(1, int(start) - 4)
    first = t0 + int(np.argmax(edges[t0:int(start) + 5]))
    b0 = max(first + 1, int(start + want) - 5)
    last = b0 + int(np.argmax(edges[b0:min(size - 1, int(start + want) + 5)]))
    if min(edges[first], edges[last]) < max(EDGE_MIN, float(np.median(edges)) * 2.5):
        return None
    return first + 1, last + 1


def detect(raw, left=0, top=0):
    """The View for one client-area capture."""
    h, w = raw.shape[:2]
    client = (left, top, w, h)
    if w < 320 or h < 180:
        return View(client, (0, 0, w, h), False, f"the game window is too small ({w}x{h})")
    aspect = w / h
    if abs(aspect / REF_ASPECT - 1) <= TOLERANCE:
        return View(client, (0, 0, w, h))
    if aspect < MIN_ASPECT * (1 - TOLERANCE):
        want = w / MIN_ASPECT
        found = _bars(cv2.cvtColor(raw, cv2.COLOR_BGR2GRAY), want)
        if found is None:
            return View(client, (0, round((h - want) / 2), w, round(want)), False,
                        f"can't confirm the game's picture inside the {w}x{h} window")
        return View(client, (0, found[0], w, found[1] - found[0]))
    if aspect > MAX_ASPECT * (1 + TOLERANCE):
        want = h * MAX_ASPECT
        found = _bars(cv2.cvtColor(raw, cv2.COLOR_BGR2GRAY).T, want)
        if found is None:
            return View(client, (0, 0, w, h), False,
                        f"the {w}x{h} window is wider than any layout the game was checked at")
        return View(client, (found[0], 0, found[1] - found[0], h))
    return View(client, (0, 0, w, h))


class Targets:
    """Template matches seen by this thread, so a click can prove what it's aimed at."""

    def __init__(self):
        self._local = threading.local()

    def _items(self):
        if not hasattr(self._local, "items"):
            self._local.items = deque(maxlen=80)
        return self._local.items

    def note(self, vision, name, center, point=None, shift=(0, 0), click_shift=None):
        self._items().append((vision, name, tuple(center), tuple(point or center), tuple(shift),
                              tuple(click_shift if click_shift is not None else shift), time.monotonic()))

    def confirm(self, frame, x, y):
        """(template, click shift) proving reference point (x, y) in this frame, or None."""
        now = time.monotonic()
        for vision, name, center, point, shift, click_shift, at in reversed(self._items()):
            if now - at > TARGET_SECONDS or abs(point[0] - x) > 3 or abs(point[1] - y) > 3:
                continue
            if vision.matches_at(frame, name, center, shift, TARGET_TOLERANCE):
                return name, click_shift
        return None

    def clear(self):
        self._items().clear()


TARGETS = Targets()
