"""Conquest helper: read the hex map, plan the fewest occupations to reach each enemy, run one step.

The rule: attack an enemy tile whenever one is reachable, and occupy only the tiles
needed to reach enemies. run() is a dry run unless execute=True.

What the saved frames established (templates/conquest/frames, from a Zone 4 session):
- The camera pans when a tile is selected, so no tile position survives a click. The grid is fitted
  to every frame.
- A numbered badge is not a team colour. The Boss itself wears the same teal "1" as the "1" unit,
  and that unit has the red enemy ring. A badge alone never makes a target friendly or hostile.
- The game's own menu decides: a sword on Occupy is an attack, a flag is a plain occupation. An
  attack goes ahead only if the sword is really there.

Window sizes: the board is 3D, so it doesn't follow the UI's anchors. It is read from the converter's
true-position picture, and each tile's point is stored relative to the Conquest title, whose match
proves the click to Game at any window shape.
"""
import json
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from .. import buttons, ocr, viewport
from ..screens import _dimmed
from . import battle

PACK = "conquest"
ROOT = Path(__file__).resolve().parents[2]
PACK_DIR = ROOT / "templates" / PACK
STOPS = ROOT / "captures" / "conquest"
BATTLE_AP = 5                    # every Occupy Base dialog seen so far (576>571, 639>634, 634>629)
TEAM_LEVEL = 72                  # Unit 1, the existing team
MAX_LEVEL_GAP = 12               # Lv.83 was beaten at Lv.72; a wider gap stops for the user
MIN_REC = 10                     # a one-digit recommended level is a misread
BOARD = (370, 130, 1590, 1080)
UI_BOXES = ((0, 0, 480, 415), (1540, 945, 1920, 1080), (780, 60, 1140, 170))
SECTOR_BOX = (80, 352, 470, 396)
AP_BOX = (745, 14, 900, 62)
SCALES = (0.90, 0.95, 1.0, 1.05, 1.10)
RING_MIN = 0.34
STRONG_RING = 0.50
TILE_REACH = 1100                # how far a tile can be from the title it is tied to
NEIGHBOURS = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, -1), (-1, 1))

ALLY, FRONTIER, FAR, BLOCKED = "ally", "frontier", "far", "blocked"
BOSS, LEADER, UNIT = "boss", "leader", "unit"


class ConquestError(RuntimeError):
    """The map couldn't be read well enough to act on."""


@dataclass
class Tile:
    q: int
    r: int
    x: int
    y: int
    state: str = BLOCKED
    unit: str = None
    grade: int = None
    badge: str = None
    red_ring: bool = False
    selected: bool = False

    @property
    def key(self):
        return self.q, self.r

    @property
    def point(self):
        return self.x, self.y

    @property
    def enemy(self):
        if self.unit in (BOSS, LEADER):
            return True
        return self.unit == UNIT and (self.badge == "purple" or self.red_ring)


@dataclass
class ConquestMap:
    tiles: dict
    zone: int = None
    occupied: int = None
    total: int = None
    ap: int = None
    residual: float = 0.0
    problems: list = field(default_factory=list)

    @property
    def enemies(self):
        return [t for t in self.tiles.values() if t.enemy]

    @property
    def allies(self):
        return [t for t in self.tiles.values() if t.state == ALLY]


def ensure_pack(vision):
    if vision.has(f"{PACK}/title"):
        return
    loader = getattr(vision, "load_pack", None)
    if loader is not None:
        loader(PACK)
    else:                                 # until Core's load_pack lands
        for name, meta in json.loads((PACK_DIR / "index.json").read_text(encoding="utf-8")).items():
            img = cv2.imread(str(PACK_DIR / meta["file"]))
            if img is not None:
                vision.images[f"{PACK}/{name}"] = img
                vision.meta[f"{PACK}/{name}"] = meta
    if not vision.has(f"{PACK}/title"):
        raise ConquestError("the conquest template pack didn't load")


def on_map(vision, frame):
    """The map with nothing over it. A popup leaves the map's templates matching but dims them."""
    ensure_pack(vision)
    name = f"{PACK}/manage_ark"
    pos = vision.find(frame, name)
    if not pos or not vision.find(frame, f"{PACK}/title") or vision.find(frame, f"{PACK}/start_battle"):
        return False
    seen = vision.matched_crop(frame, name)
    return seen is not None and seen.size > 0 and float(seen.mean()) >= float(vision.images[name].mean()) * 0.75


# ---- the grid ----

def _glow(frame):
    """Thin bright lines (tile outlines). Local contrast, because the game pulses their brightness."""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (17, 17))
    lines = cv2.morphologyEx(hsv[:, :, 2], cv2.MORPH_TOPHAT, kernel)
    return np.uint8((lines > 30) & (hsv[:, :, 1] < 160)) * 255


def _picture(frame):
    """What the 3D board is really drawn on: the frame at 16:9, the true-position canvas otherwise."""
    view, canvas = getattr(frame, "view_info", None), getattr(frame, "canvas", None)
    return frame if view is None or view.native or canvas is None else canvas


def _regions(frame, image):
    """The board's box on this picture, and every place a UI panel can cover it."""
    h, w = image.shape[:2]
    dx, dy = (w - 1920) // 2, (h - 1080) // 2
    board = (max(0, BOARD[0] + dx), max(0, BOARD[1] + dy), min(w, BOARD[2] + dx), h)
    shifts = frame.view_info.shifts() if image is not frame else [(0, 0)]
    ui = [(x1 + sx, y1 + sy, x2 + sx, y2 + sy) for x1, y1, x2, y2 in UI_BOXES for sx, sy in shifts]
    return board, ui


def _masked_glow(image, board, ui):
    glow = _glow(image)
    x1, y1, x2, y2 = board
    keep = np.zeros(glow.shape, np.uint8)
    keep[y1:y2, x1:x2] = 255
    for bx1, by1, bx2, by2 in ui:
        keep[max(0, by1):max(0, by2), max(0, bx1):max(0, bx2)] = 0
    return cv2.bitwise_and(glow, keep)


def _ring_kernel(scale):
    """An open tile's inner outline: a pointy-top hexagon, squashed by the camera's tilt."""
    rx, ry = 53 * scale, 51 * scale
    w, h = int(rx * 2) + 15, int(ry * 2) + 15
    cx, cy = w / 2, h / 2
    hexagon = np.array([(cx, cy - ry), (cx + rx, cy - ry / 2), (cx + rx, cy + ry / 2),
                        (cx, cy + ry), (cx - rx, cy + ry / 2), (cx - rx, cy - ry / 2)], np.int32)
    kernel = np.zeros((h, w), np.uint8)
    cv2.polylines(kernel, [hexagon], True, 255, 6, cv2.LINE_AA)
    return kernel


def ring_scores(image, board, ui):
    """How much each pixel looks like the centre of an open-frontier ring (full picture size)."""
    glow = cv2.dilate(_masked_glow(image, board, ui), np.ones((3, 3), np.uint8))
    out = np.full(glow.shape, -1.0, np.float32)
    for s in SCALES:
        tpl = _ring_kernel(s)
        h, w = tpl.shape
        res = cv2.matchTemplate(glow, tpl, cv2.TM_CCOEFF_NORMED)
        res = np.nan_to_num(res, nan=-1.0, posinf=-1.0, neginf=-1.0)
        layer = np.full(glow.shape, -1.0, np.float32)
        layer[h // 2:h // 2 + res.shape[0], w // 2:w // 2 + res.shape[1]] = res
        out = np.maximum(out, layer)
    return out


def _peaks(scores, floor=RING_MIN, radius=52, limit=60):
    work = scores.copy()
    found = []
    for _ in range(limit):
        _, value, _, (x, y) = cv2.minMaxLoc(work)
        if value < floor:
            break
        found.append((x, y))
        cv2.circle(work, (x, y), radius, -1.0, -1)
    return found


def _plane(keys):
    keys = np.asarray(keys, np.float64)
    return np.column_stack((keys[:, 0] + keys[:, 1] / 2, keys[:, 1] * 0.8660254))


def _project(H, keys):
    pts = _plane(keys).reshape(-1, 1, 2)
    return cv2.perspectiveTransform(pts, H).reshape(-1, 2)


def fit_grid(peaks):
    """A homography from hex coordinates to the screen, from the open-frontier ring centres."""
    if len(peaks) < 6:
        raise ConquestError(f"only {len(peaks)} open tiles visible, too few to fit the grid")
    pts = np.array(peaks, np.float64)
    dx = pts[:, 0][:, None] - pts[:, 0][None, :]
    dy = pts[:, 1][:, None] - pts[:, 1][None, :]
    across = np.abs(dx[(np.abs(dy) < 16) & (np.abs(dx) > 105) & (np.abs(dx) < 185)])
    if not across.size:
        raise ConquestError("couldn't measure the tile spacing")
    a = float(np.median(across))
    down = dy[(dy > a * 0.5) & (dy < a * 0.95) & (np.abs(dx) > a * 0.3) & (np.abs(dx) < a * 0.7)]
    if not down.size:
        raise ConquestError("couldn't measure the row spacing")
    b = float(np.median(down))
    origin = pts[np.argmin(np.linalg.norm(pts - pts.mean(axis=0), axis=1))]
    inv = np.linalg.inv(np.array([[a, a / 2], [0, b]]))
    keys = np.rint((inv @ (pts - origin).T).T).astype(int)
    H = None
    for _ in range(4):
        H, inliers = cv2.findHomography(_plane(keys), pts, cv2.RANSAC, 10.0)
        if H is None:
            raise ConquestError("the grid fit failed")
        back = cv2.perspectiveTransform(pts.reshape(-1, 1, 2), np.linalg.inv(H)).reshape(-1, 2)
        r = np.rint(back[:, 1] / 0.8660254)
        q = np.rint(back[:, 0] - r / 2)
        new = np.column_stack((q, r)).astype(int)
        if (new == keys).all():
            break
        keys = new
    error = np.linalg.norm(_project(H, keys) - pts, axis=1)
    good = error < 12
    if good.sum() < 6:
        raise ConquestError("the open tiles don't sit on one hex grid")
    return H, float(np.median(error[good]))


# ---- what is on each tile ----

def _area(image, cx, cy, outer, inner=0.0):
    """The pixels of `image` in the ring between two radii around (cx, cy)."""
    r = int(outer) + 1
    h, w = image.shape[:2]
    x0, y0, x1, y1 = max(0, int(cx) - r), max(0, int(cy) - r), min(w, int(cx) + r + 1), min(h, int(cy) + r + 1)
    if x0 >= x1 or y0 >= y1:
        return image[0:0, 0:0]
    yy, xx = np.ogrid[y0 - cy:y1 - cy, x0 - cx:x1 - cx]
    dist = xx * xx + yy * yy
    return image[y0:y1, x0:x1][(dist <= outer * outer) & (dist >= inner * inner)]


def _share(mask, cx, cy, outer, inner=0.0):
    values = _area(mask, cx, cy, outer, inner)
    return np.count_nonzero(values) / values.size if values.size else 0.0


def _on_board(vision, frame, image, board, name, limit=1):
    """Centres of a marker in picture coordinates. Markers ride the 3D board, not a UI anchor."""
    tpl = vision._template(name, getattr(frame, "view_info", None))
    th, tw = tpl.shape[:2]
    x1, y1, x2, y2 = board
    res = cv2.matchTemplate(np.asarray(image[y1:y2, x1:x2]), tpl, cv2.TM_CCOEFF_NORMED)
    floor = vision.meta[name].get("threshold", 0.85)
    out = []
    for _ in range(limit):
        _, score, _, loc = cv2.minMaxLoc(res)
        if score < floor:
            break
        out.append((x1 + loc[0] + tw // 2, y1 + loc[1] + th // 2))
        res[max(0, loc[1] - th):loc[1] + th, max(0, loc[0] - tw):loc[0] + tw] = -1
    return out


def _badges(vision, frame, image, board):
    """Numbered shield badges: [(x, y, colour, w, h, digit)]. Colour is reported, never trusted alone.
    Known shapes come from templates; a digit we have no template for still shows up by colour."""
    out = []
    for name, colour, digit in ((f"{PACK}/badge_3", "purple", 3), (f"{PACK}/badge_1", "teal", 1)):
        out += [(x, y, colour, 37, 37, digit) for x, y in _on_board(vision, frame, image, board, name, 12)]
    hsv = cv2.cvtColor(np.asarray(image), cv2.COLOR_BGR2HSV)
    for colour, lo, hi in (("purple", (125, 70, 50), (165, 255, 190)), ("teal", (88, 130, 90), (104, 255, 205))):
        mask = cv2.inRange(hsv, lo, hi)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        n, _, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
        for i in range(1, n):
            x, y, w, h, area = stats[i]
            if not (20 <= w <= 54 and 26 <= h <= 64 and area >= 240 and area / (w * h) >= 0.42):
                continue
            box = hsv[y:y + h, x:x + w]
            ink = np.count_nonzero((box[:, :, 1] < 80) & (box[:, :, 2] > 185)) / (w * h)
            cx, cy = int(x + w / 2), int(y + h / 2)
            if 0.04 <= ink <= 0.45 and not any(abs(cx - b[0]) < 24 and abs(cy - b[1]) < 24 for b in out):
                out.append((cx, cy, colour, int(w), int(h), None))
    return out


def _grade(frame, badge):
    """The digit on a badge, or None. Only used for ordering and for the printed plan."""
    x, y, _, w, h, digit = badge
    if digit is not None:
        return digit
    box = (max(0, x - w), max(0, y - h), x + w, y + h)
    text = "".join(t.text for t in ocr.read(frame, box, scale=4))
    m = re.search(r"\d", text)
    return int(m.group()) if m else None


def _classify(image, H, scores, board, ui):
    glow = _glow(image)
    hsv = cv2.cvtColor(np.asarray(image), cv2.COLOR_BGR2HSV)
    cyan = cv2.inRange(hsv, (84, 120, 165), (104, 255, 255))
    red = cv2.bitwise_or(cv2.inRange(hsv, (0, 105, 130), (8, 255, 255)),
                         cv2.inRange(hsv, (163, 105, 130), (180, 255, 255)))
    yellow = cv2.inRange(hsv, (20, 110, 170), (36, 255, 255))
    blue = cv2.inRange(hsv, (103, 60, 135), (124, 160, 235))     # a lit floor, at any glow phase
    x1, y1, x2, y2 = board
    keys = [(q, r) for q in range(-14, 15) for r in range(-10, 11)]
    pts = _project(H, keys)
    tiles = {}
    for (q, r), (x, y) in zip(keys, pts):
        if not (x1 <= x < x2 and y1 <= y < y2):
            continue
        if any(bx1 <= x < bx2 and by1 <= y < by2 for bx1, by1, bx2, by2 in ui):
            continue
        side = float(np.linalg.norm(_project(H, [(q + 1, r)])[0] - (x, y)))
        t = Tile(q, r, int(round(x)), int(round(y)))
        cx, cy = t.x, t.y
        win = scores[max(0, cy - 9):cy + 10, max(0, cx - 9):cx + 10]
        ring = float(win.max()) if win.size else -1.0
        t.red_ring = _share(red, cx, cy, side * 0.33, side * 0.17) > 0.08
        t.selected = _share(yellow, cx, cy, side * 0.46, side * 0.28) > 0.10
        px1, py1 = max(0, int(cx - side * .3)), max(0, int(cy - side * .45))
        has_prop = np.count_nonzero(cyan[py1:int(cy + side * .18) + 1, px1:int(cx + side * .3) + 1]) > side * side * 0.035
        centre = _area(hsv[:, :, 2], cx, cy, side * 0.2)
        floor = float(np.median(centre)) if centre.size else 0.0
        t._blue = _share(blue, cx, cy, side * 0.40)
        edge = False
        for dq, dr in NEIGHBOURS:
            mx, my = (_project(H, [(q + dq, r + dr)])[0] + (x, y)) / 2
            if _share(glow, mx, my, max(2.0, side * 0.05)) > 0.45:
                edge = True
                break
        t._ring, t._prop, t._floor, t._edge = ring, has_prop, floor, edge
        tiles[t.key] = t
    return tiles


def _lit(t):
    """A playable floor. Unlit tiles around the board and the pink walls both fail this."""
    return t._blue >= 0.50


def _neighbours(tiles, t):
    return [tiles[(t.q + dq, t.r + dr)] for dq, dr in NEIGHBOURS if (t.q + dq, t.r + dr) in tiles]


def _keep_main_territory(tiles):
    """Ally land is one connected piece, so a stray glow elsewhere can't count as ours."""
    seen, best = set(), []
    for t in tiles.values():
        if t.state != ALLY or t.key in seen:
            continue
        part, queue = [], deque([t])
        seen.add(t.key)
        while queue:
            c = queue.popleft()
            part.append(c)
            for n in _neighbours(tiles, c):
                if n.state == ALLY and n.key not in seen:
                    seen.add(n.key)
                    queue.append(n)
        if len(part) > len(best):
            best = part
    keep = {t.key for t in best}
    for t in tiles.values():
        if t.state == ALLY and t.key not in keep:
            t.state = BLOCKED


def _assign(tiles):
    for t in tiles.values():
        if t.enemy:
            t.state = FAR                 # settled below, from what it touches
        elif t._prop or t._floor >= 235:  # a base, or the bright start tile
            t.state = ALLY
        elif t._ring >= STRONG_RING or (t._ring >= RING_MIN and _lit(t)) or t.selected:
            t.state = FRONTIER
        elif not _lit(t):
            t.state = BLOCKED
        else:
            t.state = ALLY if t._edge else FAR
    _keep_main_territory(tiles)
    queue = deque(t for t in tiles.values() if t.state == ALLY)
    while queue:                          # an inner ally tile has no outline at all
        t = queue.popleft()
        for n in _neighbours(tiles, t):
            if n.state == FAR and not n.enemy:
                n.state = ALLY
                queue.append(n)
    for t in tiles.values():
        touches_ally = any(n.state == ALLY for n in _neighbours(tiles, t))
        if t.enemy and touches_ally:
            t.state = FRONTIER
        elif t.state == BLOCKED and touches_ally and t._ring >= RING_MIN:
            t.state = FRONTIER            # its ring is there, but a glow pulse washed out the floor


def _attach_units(vision, frame, image, board, tiles):
    def nearest(x, y, limit=70):
        best = min(tiles.values(), key=lambda t: (t.x - x) ** 2 + (t.y - y) ** 2, default=None)
        if best and (best.x - x) ** 2 + (best.y - y) ** 2 <= limit ** 2:
            return best
        return None

    labels = []
    for name, kind, dx, dy in ((f"{PACK}/boss", BOSS, -40, 28), (f"{PACK}/leader", LEADER, -15, -36)):
        for pos in _on_board(vision, frame, image, board, name):
            t = nearest(pos[0] + dx, pos[1] + dy, 80)
            if t:
                t.unit = kind
                labels.append((pos, t))
    for badge in _badges(vision, frame, image, board):
        x, y, colour = badge[:3]
        owner = next((t for pos, t in labels if pos[0] - 150 < x < pos[0] and abs(y - pos[1]) < 24), None)
        if owner is not None:
            owner.badge = colour
            owner.grade = _grade(image, badge)
            continue
        t = nearest(x, y - 30, 48)
        if t is not None and t.unit is None:
            t.unit, t.badge = UNIT, colour
            t.grade = _grade(image, badge)


def read_status(vision, frame):
    """(zone, occupied, total, ap) from the map's panels. Anything unread is None."""
    vision.find(frame, f"{PACK}/title")
    shift = vision.shift_of(frame, f"{PACK}/title")     # these panels stay with the title
    crop = viewport.crop(frame, SECTOR_BOX, shift)
    gray = cv2.normalize(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), None, 0, 255, cv2.NORM_MINMAX)
    gray = cv2.resize(gray, None, fx=4, fy=4, interpolation=cv2.INTER_LANCZOS4)
    gray = cv2.copyMakeBorder(gray, 30, 30, 30, 30, cv2.BORDER_CONSTANT, value=255)
    zone = occupied = total = None
    for img in (cv2.resize(crop, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC),
                cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)):       # each reads frames the other misses
        text = " ".join(w.text for w in ocr.read(img, scale=1))
        m = re.search(r"Zone\s*(\d+)\s*Conquest\s*Sector\s*(\d+)\s*/\s*(\d{2})\b", text, re.I)
        if m and int(m.group(2)) <= int(m.group(3)):
            zone, occupied, total = int(m.group(1)), int(m.group(2)), int(m.group(3))
            break
    ap_text = "".join(w.text for w in ocr.read(viewport.crop(frame, AP_BOX, shift), scale=2))
    a = re.match(r"(\d{1,4})\s*[/1]\s*\d{3}$", ap_text)
    return zone, occupied, total, int(a.group(1)) if a else None


def read_map(vision, frame):
    """The whole board from one frame. Raises ConquestError if it isn't readable."""
    if not on_map(vision, frame):
        raise ConquestError("not on the Conquest map")
    image = _picture(frame)
    board, ui = _regions(frame, image)
    scores = ring_scores(image, board, ui)
    H, residual = fit_grid(_peaks(scores))
    tiles = _classify(image, H, scores, board, ui)
    _attach_units(vision, frame, image, board, tiles)
    _assign(tiles)
    sx, sy = vision.shift_of(frame, f"{PACK}/title")
    for tile in tiles.values():           # reference points that the title's match can prove
        tile.x -= sx
        tile.y -= sy
    zone, occupied, total, ap = read_status(vision, frame)
    return ConquestMap(tiles, zone, occupied, total, ap, residual)


def check_map(m):
    """Why this reading can't be acted on, or []. The game's own counter is the cross-check."""
    problems = []
    if m.occupied is None or m.total is None:
        return ["couldn't read the Zone Status sector count (is the panel collapsed?)"]
    if len(m.allies) != m.occupied:
        problems.append(f"read {len(m.allies)} ally tiles but the game says {m.occupied}")
    playable = sum(1 for t in m.tiles.values() if t.state != BLOCKED)
    if playable != m.total:
        problems.append(f"read {playable} playable tiles but the game says {m.total}")
    return problems


# ---- the plan ----

@dataclass
class Step:
    kind: str                  # "attack" or "occupy"
    key: tuple
    point: tuple
    label: str


@dataclass
class Plan:
    steps: list
    unreachable: list

    @property
    def battles(self):
        return sum(1 for s in self.steps if s.kind == "attack")

    @property
    def occupies(self):
        return sum(1 for s in self.steps if s.kind == "occupy")


def _label(t):
    if t.unit == BOSS:
        return "Boss"
    if t.unit == LEADER:
        return "Leader"
    if t.unit == UNIT:
        return f"enemy {t.grade if t.grade is not None else '?'}"
    return "empty tile"


def _rank(t):
    """Numbered units first, then the Leader, and the Boss last."""
    return {UNIT: 0, LEADER: 1, BOSS: 2}.get(t.unit, 3), t.r, t.q


def make_plan(m):
    """Attack whatever is reachable. Otherwise take the shortest empty path to the next enemy."""
    tiles = m.tiles
    ours = {t.key for t in tiles.values() if t.state == ALLY}
    left = {t.key for t in tiles.values() if t.enemy}
    steps = []

    def touching(key, group):
        return [n.key for n in _neighbours(tiles, tiles[key]) if n.key in group]

    while left:
        ready = [k for k in left if touching(k, ours)]
        if ready:
            k = min(ready, key=lambda k: _rank(tiles[k]))
            steps.append(Step("attack", k, tiles[k].point, _label(tiles[k])))
            ours.add(k)
            left.remove(k)
            continue
        came = {k: None for k in ours}
        depth = {k: 0 for k in ours}
        queue = deque(sorted(ours))
        ends, limit = [], None
        while queue:
            k = queue.popleft()
            if limit is not None and depth[k] >= limit:
                continue
            for n in _neighbours(tiles, tiles[k]):
                if n.key in came or n.key in left or n.state == BLOCKED:
                    continue
                came[n.key] = k
                depth[n.key] = depth[k] + 1
                if touching(n.key, left):
                    ends.append(n.key)
                    limit = depth[n.key]
                else:
                    queue.append(n.key)
        if not ends:
            break
        end = min(ends, key=lambda k: (-len(touching(k, left)),
                                       min(_rank(tiles[e]) for e in touching(k, left)), k))
        path = []
        while end is not None and end not in ours:
            path.append(end)
            end = came[end]
        for k in reversed(path):
            steps.append(Step("occupy", k, tiles[k].point, "empty tile"))
            ours.add(k)
    return Plan(steps, [tiles[k] for k in sorted(left)])


def describe(m, plan):
    """The dry-run report."""
    lines = [f"Zone {m.zone}: {m.occupied}/{m.total} sectors, AP {m.ap if m.ap is not None else '?'}"]
    lines.append("Enemies: " + (", ".join(f"{_label(t)} at {t.point}" for t in sorted(m.enemies, key=_rank)) or "none"))
    for i, s in enumerate(plan.steps, 1):
        verb = "Attack" if s.kind == "attack" else "Occupy"
        lines.append(f"  {i}. {verb} {s.label} at {s.point}")
    cost = f"AP: {plan.battles} battles x {BATTLE_AP} = {plan.battles * BATTLE_AP}"
    if plan.occupies:
        cost += f", plus {plan.occupies} empty-tile occupations whose AP cost hasn't been observed"
    lines.append(cost)
    if plan.unreachable:
        lines.append("Unreachable: " + ", ".join(_label(t) for t in plan.unreachable))
    return "\n".join(lines)


# ---- the game's dialogs ----

def parse_ap(text):
    """(before, after) from a cost bubble. OCR reads its arrow as '+', '-5' or nothing at all:
    '576 —..> 571', '639+634', '634-5 629', '6394634'. The caller checks 'before' against the map."""
    nums = re.findall(r"\d+", text)
    if len(nums) == 1 and len(nums[0]) >= 6:
        s = nums[0]
        half = len(s) // 2
        nums = [s[:half], s[-half:]]
    if len(nums) < 2:
        return None
    before, after = int(nums[0]), int(nums[-1])
    return (before, after) if 0 < before - after <= 60 else None


def read_dialog(vision, frame):
    """The Occupy Base dialog: {name, rec, ap_before, ap_after, cost}, or None if it isn't up."""
    button = vision.find(frame, f"{PACK}/start_battle")
    if not button or not vision.find(frame, f"{PACK}/occupy_base"):
        return None
    if _dimmed(vision, frame, f"{PACK}/start_battle"):     # another dialog is on top, and its Confirm sits near this button
        return None
    image = _picture(frame)                 # OCR positions below are on this picture
    shift = vision.shift_of(frame, f"{PACK}/start_battle")
    sx, sy = button[0] + shift[0], button[1] + shift[1]
    h, w = image.shape[:2]
    words = ocr.read(image, (max(0, sx - 500), max(0, sy - 700), min(w, sx + 515), min(h, sy + 100)), scale=2)
    rec = None
    for word in words:
        m = re.search(r"Lv\.?\s*(\d{1,3})", word.text, re.I)
        if m and word.x > sx + 230:
            rec = int(m.group(1))
    ap = parse_ap(ocr.text_in(words, sx + 30, sy - 130, sx + 270, sy - 45))
    zone = next((word for word in words if word.text.lower() == "zone"), None)
    name = ocr.text_in(words, zone.x - 4, zone.y - 10, sx + 340, zone.y + zone.h + 10) if zone else ""
    return {"name": name, "rec": rec, "ap_before": ap[0] if ap else None, "ap_after": ap[1] if ap else None,
            "cost": ap[0] - ap[1] if ap else None, "button": button}


def dialog_problem(d, team_level, max_gap, ap_left, map_ap=None):
    """Why a battle shouldn't start, or None."""
    if d["rec"] is None or d["rec"] < MIN_REC:              # a lost digit reads Lv.83 as Lv.8
        return "couldn't read the recommended level"
    if d["cost"] is None:
        return "couldn't read the AP cost"
    if d["cost"] != BATTLE_AP:
        return f"the dialog's cost reads {d['cost']} AP, not {BATTLE_AP}, so it may be misread"
    if d["ap_before"] < BATTLE_AP or (map_ap is not None and map_ap < BATTLE_AP):   # the game would offer a refill
        return f"there isn't enough AP for a battle (dialog {d['ap_before']}, map {map_ap})"
    if map_ap is not None and abs(d["ap_before"] - map_ap) > 2:     # AP can tick up while we wait
        return f"the dialog says AP {d['ap_before']} but the map said {map_ap}, so the cost may be misread"
    if d["cost"] > ap_left:
        return f"it costs {d['cost']} AP and this run's limit has {ap_left} left"
    if d["rec"] - team_level > max_gap:
        return f"recommended Lv.{d['rec']} is {d['rec'] - team_level} above the team's Lv.{team_level}"
    return None


# ---- one step at a time ----

class Halt(Exception):
    """Stop the run and tell the user why. status is the run's result."""

    def __init__(self, status, reason):
        super().__init__(reason)
        self.status, self.reason = status, reason


def _save(frame, tag):
    STOPS.mkdir(parents=True, exist_ok=True)
    path = STOPS / f"{tag}_{int(time.time())}.png"
    cv2.imwrite(str(path), frame)
    return path


def _stop(frame, status, reason):
    raise Halt(status, f"{reason} (frame: {_save(frame, status)})")


def read_settled(game, vision, tries=7):
    """A map reading that agrees with the game's own counters. The glow pulses and the camera
    pans, so a bad reading gets a few more frames before it counts as a real problem."""
    why, frame = "not on the Conquest map", None
    for i in range(tries):
        if i:
            game.sleep(0.7)
        frame = game.grab()
        if not on_map(vision, frame):
            why = "not on the Conquest map"
            continue
        if vision.find(frame, f"{PACK}/sword") or vision.find(frame, f"{PACK}/flag"):
            why = "a tile menu is open"
            continue
        try:
            m = read_map(vision, frame)
        except ConquestError as e:
            why = str(e)
            continue
        problems = check_map(m)
        if not problems:
            return m
        why = "; ".join(problems)
    _stop(frame, "unreadable", f"can't read the map: {why}")


def _wait_menu(game, vision, seconds=5):
    """The Occupy button once the camera has stopped: ('sword'|'flag', position), or None."""
    last = None
    end = time.monotonic() + seconds
    frame = game.grab()
    while True:
        found = None
        for kind in ("sword", "flag"):
            pos = vision.find(frame, f"{PACK}/{kind}")
            if pos:
                found = (kind, pos)
                break
        if found and last and found[0] == last[0] and abs(found[1][0] - last[1][0]) <= 3 \
                and abs(found[1][1] - last[1][1]) <= 3:
            return found, frame
        last = found
        if time.monotonic() >= end:
            return None, frame
        game.sleep(0.25)
        frame = game.grab()


def _wait_dialog(game, vision, seconds=5):
    end = time.monotonic() + seconds
    while True:
        frame = game.grab()
        d = read_dialog(vision, frame)
        if d:
            return d, frame
        if time.monotonic() >= end:
            return None, frame
        game.sleep(0.3)


def _click_tile(game, vision, step):
    """A tile has no template of its own, so the map's title, matched just now, proves the click."""
    frame = game.grab()
    if not on_map(vision, frame):
        _stop(frame, "lost", f"left the Conquest map before clicking {step.label}")
    if not buttons.tie(vision, frame, f"{PACK}/title", *step.point, reach=TILE_REACH):
        _stop(frame, "no_proof", f"couldn't tie {step.label} at {step.point} to the map's title")
    game.click(*step.point)


def _select(game, vision, step, log):
    """Click the tile and read what the game offers. The sword is what makes it an attack."""
    _click_tile(game, vision, step)
    menu, frame = _wait_menu(game, vision)
    if menu is None:
        _stop(frame, "no_menu", f"{step.label} at {step.point} offered no Occupy button")
    kind, pos = menu
    if step.kind == "attack" and kind != "sword":
        _stop(frame, "not_enemy", f"{step.label} at {step.point} shows a flag, not a sword: "
                                  "the game doesn't treat it as an enemy")
    log(f"conquest: {step.label} at {step.point} offers {kind}")
    return kind, pos, frame


def _back_on_map(game, vision, log, seconds=45):
    """After the reward: skip any story and wait for the map. Nothing else gets clicked."""
    from ..screens import detect
    from . import story
    end = time.monotonic() + seconds
    frame = game.grab()
    while time.monotonic() < end:
        if on_map(vision, frame):
            return
        if detect(vision, frame) == "story":
            log("conquest: skipping a story scene")
            story.skip(game, vision, log)
        else:
            game.sleep(0.8)
        frame = game.grab()
    _stop(frame, "lost", "didn't get back to the Conquest map")


def _fight(game, vision, step, before, opts, log):
    d, frame = _wait_dialog(game, vision)
    if d is None:
        _stop(frame, "no_dialog", f"no Occupy Base dialog for {step.label}")
    log(f"conquest: {d['name']}: Rec. Lv.{d['rec']}, AP {d['ap_before']} > {d['ap_after']}")
    problem = dialog_problem(d, opts["team_level"], opts["max_gap"], opts["ap_left"], before.ap)
    if problem:
        _stop(frame, "needs_user", f"stopped before starting {d['name'] or step.label}: {problem}")
    game.click(*d["button"])
    game.sleep(1.5)
    frame = game.grab()
    if vision.find(frame, f"{PACK}/start_battle"):
        _stop(frame, "stuck", "Start Battle didn't leave the dialog")
    result = battle.run(game, vision, log=log)
    if result != "won":
        _stop(game.grab(), "defeat" if result == "defeat" else f"battle_{result}",
              f"the battle for {step.label} ended: {result}")
    opts["ap_left"] -= d["cost"]
    _back_on_map(game, vision, log)
    after = read_settled(game, vision)
    if after.occupied != before.occupied + 1:
        _stop(game.grab(), "no_progress", f"sectors went {before.occupied} > {after.occupied} after a win")
    return after


def _occupy_empty(game, vision, step, before, pos, opts, log):
    """Pressing the flag. Its result was never seen live, so anything unexpected stops the run.
    Its AP cost is unknown too, so it's budgeted like a battle."""
    if opts["ap_left"] < BATTLE_AP:
        _stop(game.grab(), "needs_user", f"the AP budget has {opts['ap_left']} left: not occupying {step.point}")
    game.click(*pos)
    end = time.monotonic() + 6
    while time.monotonic() < end:
        game.sleep(0.5)
        frame = game.grab()
        if vision.find(frame, f"{PACK}/start_battle"):
            return _fight(game, vision, step, before, opts, log)
        if on_map(vision, frame) and not vision.find(frame, f"{PACK}/flag"):
            _, occupied, _, _ = read_status(vision, frame)
            if occupied == before.occupied + 1:
                opts["ap_left"] -= BATTLE_AP
                return read_settled(game, vision)
    _stop(game.grab(), "no_progress", f"the flag on {step.point} didn't occupy it")


def recon(game, vision, log=None):
    """Live check that spends nothing: open the next target's menu and dialog, save them, close."""
    log = log or game.log
    ensure_pack(vision)
    try:
        m = read_settled(game, vision)
        plan = make_plan(m)
        log(describe(m, plan))
        if not plan.steps:
            return {"status": "clear", "note": "no enemies on this map"}
        step = plan.steps[0]
        out = {"status": "recon", "target": step.label, "point": step.point}
        kind, pos, frame = _select(game, vision, step, log)
    except Halt as h:
        return {"status": h.status, "reason": h.reason}
    out["menu"] = kind
    out["menu_frame"] = str(_save(frame, "recon_menu"))
    if kind != "sword":
        out["note"] = "a flag: pressing it may occupy the tile, so recon stops here"
        return out
    game.click(*pos)
    d, frame = _wait_dialog(game, vision)
    out["dialog_frame"] = str(_save(frame, "recon_dialog"))
    if d is None:
        out["status"] = "no_dialog"
        return out
    out["dialog"] = {k: d[k] for k in ("name", "rec", "ap_before", "ap_after", "cost")}
    close = vision.find(frame, f"{PACK}/close_x")
    if close:
        game.click(*close)
        game.sleep(1)
    else:
        out["note"] = "couldn't find the dialog's X: it's still open, nothing was started"
    return out


def run(game, vision, execute=False, steps=1, team_level=TEAM_LEVEL, max_gap=MAX_LEVEL_GAP, max_ap=None,
        allow_empty=False, log=None):
    """Dry run by default. With execute=True, do up to `steps` plan steps, re-reading after each."""
    log = log or game.log
    ensure_pack(vision)
    done = []
    try:
        m = read_settled(game, vision)
        plan = make_plan(m)
        log(describe(m, plan))
        if not execute:
            return {"status": "dry_run", "zone": m.zone, "sectors": f"{m.occupied}/{m.total}", "ap": m.ap,
                    "battles": plan.battles, "ap_cost": plan.battles * BATTLE_AP,
                    "empty_occupations": plan.occupies, "steps": [(s.kind, s.label, s.point) for s in plan.steps],
                    "unreachable": [_label(t) for t in plan.unreachable]}
        limit = max_ap if max_ap is not None else BATTLE_AP * max(1, steps)
        opts = {"team_level": team_level, "max_gap": max_gap, "ap_left": limit}
        for _ in range(steps):
            plan = make_plan(m)
            if not plan.steps:
                return {"status": "blocked" if plan.unreachable else "clear", "done": done}
            step = plan.steps[0]
            if step.kind == "occupy" and not allow_empty:
                return {"status": "needs_user", "done": done,
                        "reason": f"the next step occupies the empty tile at {step.point}; its flow and AP "
                                  "cost haven't been seen live, so it needs --allow-empty"}
            kind, pos, _ = _select(game, vision, step, log)
            if kind == "sword":
                game.click(*pos)
                m = _fight(game, vision, step, m, opts, log)
            else:
                m = _occupy_empty(game, vision, step, m, pos, opts, log)
            done.append((step.kind, step.label))
            log(f"conquest: done with {step.label}, now {m.occupied}/{m.total}")
        return {"status": "ok", "done": done, "sectors": f"{m.occupied}/{m.total}"}
    except Halt as h:
        log(f"conquest: {h.reason}")
        return {"status": h.status, "reason": h.reason, "done": done}


def main():
    import argparse
    ap = argparse.ArgumentParser(prog="python -m ba_auto.tasks.conquest",
                                 description="Plan Conquest moves. A dry run unless --execute or --recon.")
    ap.add_argument("--frame", help="read a saved client-area capture instead of the game (no input possible)")
    ap.add_argument("--recon", action="store_true", help="open the next target's menu and dialog, then close (no AP)")
    ap.add_argument("--execute", action="store_true", help="really do the steps (spends AP)")
    ap.add_argument("--steps", type=int, default=1)
    ap.add_argument("--team-level", type=int, default=TEAM_LEVEL)
    ap.add_argument("--max-gap", type=int, default=MAX_LEVEL_GAP)
    ap.add_argument("--max-ap", type=int)
    ap.add_argument("--allow-empty", action="store_true", help="also occupy empty tiles on the way to an enemy")
    a = ap.parse_args()
    from ..vision import Vision
    vision = Vision()
    ensure_pack(vision)
    if a.frame:
        raw = cv2.imread(a.frame)
        if raw is None:
            raise SystemExit(f"can't read {a.frame}")
        m = read_map(vision, viewport.detect(raw, 0, 0).compose(raw))
        print(describe(m, make_plan(m)))
        print("Checks:", check_map(m) or "agree with the game's sector counter")
        return
    from ..game import Game
    game = Game()
    if a.recon:
        result = recon(game, vision, log=print)
    else:
        result = run(game, vision, execute=a.execute, steps=a.steps, team_level=a.team_level, max_gap=a.max_gap,
                     max_ap=a.max_ap, allow_empty=a.allow_empty, log=print)
    print(json.dumps(result, indent=1, default=str))


if __name__ == "__main__":
    main()
