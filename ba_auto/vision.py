"""Template matching against reference crops in templates/.

templates/templates.json maps a name to:
  file       png crop (1920x1080 reference space)
  region     [x1, y1, x2, y2] area to search (crop box padded)
  threshold  minimum normalized match score (TM_CCOEFF_NORMED)

At window shapes other than 16:9, frames carry a true-position canvas (see viewport.py). Matching
then searches each place the element's anchor can put it, with the template softened to the
capture's resolution. Results stay in reference coordinates; the anchor offset is remembered so a
click lands on what was actually matched.
"""
import json
import os
import re
from pathlib import Path

import cv2

from .viewport import TARGETS

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT / "templates"
INDEX = TEMPLATE_DIR / "templates.json"
AUTO_PACKS = ("conquest", "title")   # packs that shared screen detection depends on


class Vision:
    def __init__(self, rt=None):
        self.rt = rt
        self._soft = {}
        self._packs = {}                 # pack name -> index file's modified time
        self.reload()

    def _record(self, name, center, score, shift=(0, 0)):
        if self.rt is not None and center:
            import time
            h, w = self.images[name].shape[:2]
            box = (center[0] - w // 2, center[1] - h // 2, center[0] + w // 2, center[1] + h // 2)
            self.rt.matches.append((name, box, score, time.time(), shift))

    def reload(self):
        self.meta = json.loads(INDEX.read_text()) if INDEX.exists() else {}
        self.images = {}
        self._soft = {}
        for name, m in self.meta.items():
            img = cv2.imread(str(TEMPLATE_DIR / m["file"]))
            if img is not None:
                self.images[name] = img
        packs = set(self._packs) | set(AUTO_PACKS)
        self._packs = {}
        for pack in sorted(packs):
            self.load_pack(pack)

    def load_pack(self, pack):
        """Load templates/<pack>/index.json as "pack/name" entries. Returns how many are loaded.
        Safe to call repeatedly: it only re-reads a pack whose index changed. A pack that doesn't
        exist yet loads nothing, so `has` stays False for its names."""
        if not re.fullmatch(r"[a-z0-9_]+", pack):
            raise ValueError(f"bad template pack name: {pack!r}")
        index = TEMPLATE_DIR / pack / "index.json"
        prefix = pack + "/"
        try:
            stamp = index.stat().st_mtime_ns
        except OSError:
            return 0
        if self._packs.get(pack) == stamp:
            return sum(n.startswith(prefix) for n in self.images)
        entries = json.loads(index.read_text(encoding="utf-8"))
        images = {}
        for name, m in entries.items():
            img = cv2.imread(str(TEMPLATE_DIR / pack / m["file"]))
            if img is not None:
                images[prefix + name] = img
        for name in [n for n in self.meta if n.startswith(prefix) and n not in images]:
            self.meta.pop(name, None)
            self.images.pop(name, None)
        self.meta.update({prefix + n: m for n, m in entries.items() if prefix + n in images})
        self.images.update(images)
        for key in [k for k in self._soft if k[0].startswith(prefix)]:
            del self._soft[key]
        self._packs[pack] = stamp
        return len(images)

    def has(self, name):
        return name in self.images

    def _template(self, name, view):
        """The template as it looks after the capture's down- and up-scaling."""
        tpl = self.images[name]
        if view is None or view.scale >= 0.92:
            return tpl
        key = (name, round(view.scale, 3))
        if key not in self._soft:
            h, w = tpl.shape[:2]
            small = cv2.resize(tpl, (max(1, round(w * view.scale)), max(1, round(h * view.scale))),
                               interpolation=cv2.INTER_AREA)
            self._soft[key] = cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)
        return self._soft[key]

    def _search(self, frame, name):
        """(score, reference centre, anchor shift) of the best match inside `name`'s region."""
        x1, y1, x2, y2 = self.meta[name]["region"]
        view = getattr(frame, "view_info", None)
        if view is None or view.native or getattr(frame, "canvas", None) is None:
            tpl = self._template(name, view)
            roi = frame[y1:y2, x1:x2]
            if roi.shape[0] < tpl.shape[0] or roi.shape[1] < tpl.shape[1]:
                return 0.0, None, (0, 0)
            res = cv2.matchTemplate(roi, tpl, cv2.TM_CCOEFF_NORMED)
            _, score, _, loc = cv2.minMaxLoc(res)
            h, w = tpl.shape[:2]
            return float(score), (x1 + loc[0] + w // 2, y1 + loc[1] + h // 2), (0, 0)
        tpl = self._template(name, view)
        h, w = tpl.shape[:2]
        ch, cw = frame.canvas.shape[:2]
        best = (0.0, None, (0, 0))
        seen = []
        for dx, dy in view.shifts():
            xa, ya, xb, yb = max(0, x1 + dx), max(0, y1 + dy), min(cw, x2 + dx), min(ch, y2 + dy)
            if yb - ya < h or xb - xa < w:
                continue
            res = cv2.matchTemplate(frame.canvas[ya:yb, xa:xb], tpl, cv2.TM_CCOEFF_NORMED)
            _, score, _, loc = cv2.minMaxLoc(res)
            found = (float(score), (xa + loc[0] + w // 2 - dx, ya + loc[1] + h // 2 - dy), (dx, dy))
            seen.append(found)
            if score > best[0]:
                best = found
        if best[1] is None:
            return best
        # A tall or wide region can hold the same pixels under several anchors, all scoring the same.
        # The true reading is the one whose reference position belongs to that anchor.
        spot = (best[1][0] + best[2][0], best[1][1] + best[2][1])
        for found in seen:
            if (found[0] >= best[0] - 0.002 and (found[1][0] + found[2][0], found[1][1] + found[2][1]) == spot
                    and view.default_shift(*found[1]) == found[2]):
                return found
        return best

    def match(self, frame, name):
        """(score, (cx, cy)) for the best match of `name` inside its region."""
        score, center, _ = self._search(frame, name)
        return score, center

    def find(self, frame, name):
        """Center of `name` if it is on screen, else None."""
        if name not in self.images:
            return None
        score, c, shift = self._search(frame, name)
        if score >= self.meta[name].get("threshold", 0.85):
            self._record(name, c, score, shift)
            TARGETS.note(self, name, c, shift=shift)
            hits = getattr(frame, "hits", None)
            if hits is not None:
                hits[name] = shift
            return c
        return None

    def shift_of(self, frame, name):
        """The anchor offset `name` was last found at in this frame."""
        return getattr(frame, "hits", {}).get(name, (0, 0))

    def matches_at(self, frame, name, center, shift=(0, 0), pad=16):
        """Whether template `name` is on screen within `pad` pixels of `center`."""
        if name not in self.images:
            return False
        view = getattr(frame, "view_info", None)
        canvas = getattr(frame, "canvas", None)
        native = view is None or view.native or canvas is None
        image = frame if native else canvas
        tpl = self._template(name, view)
        h, w = tpl.shape[:2]
        cx, cy = center[0] + (0 if native else shift[0]), center[1] + (0 if native else shift[1])
        x1, y1 = max(0, cx - w // 2 - pad), max(0, cy - h // 2 - pad)
        x2, y2 = min(image.shape[1], cx + w // 2 + pad + 1), min(image.shape[0], cy + h // 2 + pad + 1)
        roi = image[y1:y2, x1:x2]
        if roi.shape[0] < h or roi.shape[1] < w:
            return False
        score = cv2.minMaxLoc(cv2.matchTemplate(roi, tpl, cv2.TM_CCOEFF_NORMED))[1]
        return score >= self.meta[name].get("threshold", 0.85)

    def matched_crop(self, frame, name):
        """The image under the best match of `name`, wherever its anchor put it."""
        _, center, shift = self._search(frame, name)
        if center is None:
            return None
        h, w = self.images[name].shape[:2]
        view = getattr(frame, "view_info", None)
        canvas = getattr(frame, "canvas", None)
        image = frame if view is None or view.native or canvas is None else canvas
        x, y = center[0] + shift[0] - w // 2, center[1] + shift[1] - h // 2
        return image[max(0, y):y + h, max(0, x):x + w]

    def find_all(self, frame, name, limit=12):
        """Centers of every match of `name` in its region, top-to-bottom, left-to-right."""
        if name not in self.images:
            return []
        view = getattr(frame, "view_info", None)
        canvas = getattr(frame, "canvas", None)
        x1, y1, x2, y2 = self.meta[name]["region"]
        shift = (0, 0)
        image, tpl = frame, self._template(name, view)
        if view is not None and not view.native and canvas is not None:
            score, _, shift = self._search(frame, name)
            if score < self.meta[name].get("threshold", 0.85):
                return []
            image, tpl = canvas, self._template(name, view)
            x1, y1, x2, y2 = (max(0, x1 + shift[0]), max(0, y1 + shift[1]),
                              min(canvas.shape[1], x2 + shift[0]), min(canvas.shape[0], y2 + shift[1]))
        h, w = tpl.shape[:2]
        res = cv2.matchTemplate(image[y1:y2, x1:x2], tpl, cv2.TM_CCOEFF_NORMED)
        thr = self.meta[name].get("threshold", 0.85)
        out = []
        for _ in range(limit):
            _, score, _, loc = cv2.minMaxLoc(res)
            if score < thr:
                break
            c = (x1 + loc[0] + w // 2 - shift[0], y1 + loc[1] + h // 2 - shift[1])
            out.append(c)
            self._record(name, c, score, shift)
            TARGETS.note(self, name, c, shift=shift)
            # suppress this peak so the next-best distinct match comes out
            res[max(0, loc[1] - h):loc[1] + h, max(0, loc[0] - w):loc[0] + w] = -1
        return sorted(out, key=lambda p: (p[1] // 40, p[0]))

    def save(self, frame, name, box, pad=60, threshold=0.85):
        x1, y1, x2, y2 = box
        TEMPLATE_DIR.mkdir(exist_ok=True)
        cv2.imwrite(str(TEMPLATE_DIR / f"{name}.png"), frame[y1:y2, x1:x2])
        h, w = frame.shape[:2]
        self.meta[name] = {
            "file": f"{name}.png",
            "region": [max(0, x1 - pad), max(0, y1 - pad), min(w, x2 + pad), min(h, y2 + pad)],
            "threshold": threshold,
        }
        shared = {n: m for n, m in self.meta.items() if "/" not in n}   # packs keep their own index
        tmp = INDEX.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(shared, indent=2))
        os.replace(tmp, INDEX)
        self.reload()
