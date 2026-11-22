"""One frame in, one policy observation out. Reads only; nothing here can send input.

Text (timer, boss HP) goes through Windows OCR, which is slow, so it runs on a worker thread and the
last value is used meanwhile. A card outside the expected team is looked up against the whole roster
on the same worker, and its skill stays unknown until the catalog has seen it.
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .. import battlestate as bs
from .. import ocr, portraits
from ..screens import detect
from . import cards, field
from .policy import Ally, Card, Enemy, Obs, WaveTracker

PACK = Path(__file__).resolve().parents[2] / "templates" / "battler"
BOSS_HP_BOX = (640, 62, 1365, 92)
END_SCREENS = {"battle_result": "won", "reward": "won", "defeat": "defeat", "battle_tip": "defeat"}
START_GRACE = 5.0                # seconds at the start when a boss may not be placed yet


def battle_end(vision, frame):
    """'won' or 'defeat' when a result screen is up. A HUD that's merely hidden (a skill's scene, a
    wave transition, a popup over the result) returns None, so it never counts as the fight ending."""
    return END_SCREENS.get(detect(vision, frame))


def parse_clock(text):
    """Seconds from mm:ss.mmm, or None."""
    m = re.fullmatch(r"(\d{2}):(\d{2})\.(\d{3})", text or "")
    return int(m.group(1)) * 60 + int(m.group(2)) + int(m.group(3)) / 1000 if m else None


def parse_boss_hp(text):
    """(hp, max hp) from the boss bar's '1,263,102 / 1,999,998', or None."""
    m = re.search(r"([\d][\d,.\s]*)/\s*([\d][\d,.\s]*)", text or "")
    if not m:
        return None
    hp, top = (re.sub(r"\D", "", g) for g in m.groups())
    if not hp or not top or int(top) <= 0 or int(hp) > int(top):
        return None
    return int(hp), int(top)


def has_boss(frame):
    tpl = _pack_image("boss_tag")
    if tpl is None:
        return False
    meta = _pack_meta().get("boss_tag") or {}
    x1, y1, x2, y2 = meta.get("region", (500, 100, 650, 150))
    roi = np.asarray(frame[y1:y2, x1:x2])
    if roi.shape[0] < tpl.shape[0] or roi.shape[1] < tpl.shape[1]:
        return False
    return float(cv2.minMaxLoc(cv2.matchTemplate(roi, tpl, cv2.TM_CCOEFF_NORMED))[1]) >= meta.get("threshold", 0.72)


_images, _meta = {}, None


def _pack_image(name):
    if name not in _images:
        _images[name] = cv2.imread(str(PACK / f"{name}.png"))
    return _images[name]


def _pack_meta():
    global _meta
    if _meta is None:
        try:
            _meta = json.loads((PACK / "index.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _meta = {}
    return _meta


@dataclass
class Reading:
    obs: Obs
    state: dict
    bars: list
    boss: dict
    why_unsure: str = None


class Perception:
    def __init__(self, vision, catalog, team=None, ocr_every=2.0, background=True):
        self.vision, self.catalog = vision, catalog
        self.card_map = catalog.card_map(team)
        self.bank = cards.CardBank(self.card_map)
        self.waves = WaveTracker()
        self.ocr_every = ocr_every
        self.boss_points = catalog.config.get("boss_points") or {}
        self.started = None
        self.timer, self.timer_at = None, None
        self.boss = {"name": None, "hp": None, "max": None}
        self._text_at = None
        self._pool = ThreadPoolExecutor(1, thread_name_prefix="battler") if background else None
        self._job = None
        self._full = None
        self._lookup_at = -999.0
        self.extra = {}                  # students found outside the expected team
        self.first_timer, self.last_timer = None, None
        self.recent_allies, self.peak_allies = [], 0

    def close(self):
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=True)

    # ---- slow reads
    def _read_text(self, frame):
        timer = parse_clock(bs.read_timer(frame))
        name = " ".join(w.text for w in ocr.read(frame, bs.BOSS_BOX, scale=2)).strip() or None
        hp = parse_boss_hp(" ".join(w.text for w in ocr.read(frame, BOSS_HP_BOX, scale=2)))
        return timer, name, hp

    def _collect(self, t):
        if self._job is None or not self._job[0].done():
            return
        future, at = self._job
        self._job = None
        try:
            result = future.result()
        except Exception:
            return
        if result[0] == "card":
            _, dev = result
            if dev and dev not in self.card_map:
                name = portraits.display(dev)
                self.card_map[dev] = name
                self.extra[dev] = name
                self.bank.refs[dev] = self._full.refs[dev]
            return
        timer, name, hp = result[1:]
        if timer is not None:
            self.timer, self.timer_at = timer, at
            self.first_timer = timer if self.first_timer is None else self.first_timer
            self.last_timer = timer
        if name:
            self.boss["name"] = name
        if hp:
            self.boss["hp"], self.boss["max"] = hp

    def _lookup(self, patch):
        if self._full is None:
            self._full = cards.CardBank(cards.all_devs())
        ranked = sorted(self._full.score(patch).items(), key=lambda kv: -kv[1])
        if len(ranked) > 1 and ranked[0][1] >= cards.MIN_SCORE + 0.1 and ranked[0][1] - ranked[1][1] >= cards.MIN_GAP * 2:
            return "card", ranked[0][0]
        return "card", None

    def _schedule(self, frame, t, unread):
        if self._job is not None or self._pool is None:
            return
        if self._text_at is None or t - self._text_at >= self.ocr_every:
            self._text_at = t
            copy = np.array(frame)
            self._job = (self._pool.submit(lambda: ("text", *self._read_text(copy))), t)
        elif unread is not None and t - self._lookup_at >= 1.5:
            self._lookup_at = t
            patch = cards.card_patch(frame, unread)
            if patch is not None:
                self._job = (self._pool.submit(self._lookup, patch), t)

    # ---- one frame
    def read(self, frame, t):
        """A Reading, or None when no battle HUD is on screen."""
        state = bs.read(self.vision, frame)
        if state is None:
            return None
        self.started = self.started if self.started is not None else t
        self._collect(t)

        hand, unread = [], None
        for slot, info in state["cards"].items():
            student = None
            if info["cost"] is not None:
                dev = self.bank.identify(frame, slot, slot == state["selected"])[0]
                student = self.card_map.get(dev) if dev else None
                if student is None and unread is None:
                    unread = slot
            hand.append(Card(slot, student, info["cost"], info["ready"]))
        self._schedule(frame, t, unread)

        found = field.bars(frame)
        foes = [Enemy(b.unit[0], b.unit[1], round(b.size, 2)) for b in field.enemies(found)]
        friends = tuple(Ally(b.unit[0], b.unit[1], b.fill, b.shield) for b in field.allies(found))

        self.recent_allies = (self.recent_allies + [len(friends)])[-8:]
        self.peak_allies = max(self.peak_allies, len(friends))

        why = None
        boss_on = has_boss(frame)
        if boss_on:
            point = self._boss_point()
            if point:
                top = max((e.size for e in foes), default=1.0)
                foes = [Enemy(point[0], point[1], max(top, 1.0) * 2, True)] + foes
            elif foes:
                big = max(foes, key=lambda e: e.size)
                foes = [Enemy(e.x, e.y, e.size, e is big) for e in foes]
            elif t - self.started > START_GRACE:
                why = "a boss is up but its position isn't known (add it to boss_points in config/battler.json)"

        wave, age, _ = self.waves.update(t, len(foes))
        timer = self.timer - (t - self.timer_at) if self.timer is not None else None
        if state["cost"] is None and why is None:
            why = "cost unreadable"
        boss = dict(self.boss, present=boss_on)
        if boss["hp"] and boss["max"]:
            boss["frac"] = round(boss["hp"] / boss["max"], 4)
        obs = Obs(t=t, cost=state["cost"], cards=tuple(hand), enemies=tuple(foes), allies=friends, wave=wave,
                  wave_age=round(age, 2), timer=timer, sure=why is None, targeting=state["targeting"])
        return Reading(obs, state, found, boss, why)

    def summary(self):
        """Figures for comparing fights. Survivors are bars seen near the end, so a rough count."""
        clear = None
        if self.first_timer is not None and self.last_timer is not None:
            clear = round(self.first_timer - self.last_timer, 1)
        return {"timer_start": self.first_timer, "timer_end": self.last_timer, "clear_seconds": clear,
                "survivors": max(self.recent_allies, default=None), "team_seen": self.peak_allies,
                "waves": self.waves.wave, "boss": self.boss.get("name"), "boss_hp": self.boss.get("hp")}

    def _boss_point(self):
        name = (self.boss.get("name") or "").lower()
        for key, point in self.boss_points.items():
            if key.lower() in name and name:
                return tuple(point)
        return None
