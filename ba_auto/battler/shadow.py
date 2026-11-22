"""Shadow mode: watch a battle under AUTO and log what the smart battler would do. Sends no input.

    python -m ba_auto.battler.shadow --seconds 180          # watch the live game
    python -m ba_auto.battler.shadow --replay captures\\rec  # run over saved frames

Writes captures/battler/shadow/<time>/decisions.jsonl plus an annotated frame for each new decision.
"""
import argparse
import json
import time
from pathlib import Path

import cv2
import numpy as np

from .. import battlestate as bs
from . import policy
from .catalog import Catalog
from . import field, perception as eyes_mod
from .perception import Perception

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "captures" / "battler" / "shadow"
MAX_FRAMES = 240
RAW_EVERY, RAW_MAX = 2.5, 48     # clean frames kept for cutting enemy-bar templates
WAVE_SIZE = 3
HUD_LOST = 25.0
COLORS = {"ally": (80, 255, 80), "enemy": (60, 60, 255), "other": (150, 150, 150)}


class ReadOnly:
    """The only part of Game that shadow mode can reach, so it can't send input by mistake."""

    def __init__(self, game):
        self.grab, self.sleep, self.log = game.grab, game.sleep, game.log


def annotate(frame, reading, decision):
    img = frame.copy()
    for b in reading.bars:
        cv2.rectangle(img, (b.x1, b.y1), (b.x2, b.y2), COLORS[b.kind], 2)
        if b.kind != "other":
            cv2.putText(img, f"{b.fill:.0%}", (b.x2 + 4, b.y2), 0, .5, COLORS[b.kind], 1, cv2.LINE_AA)
    for e in reading.obs.enemies:
        cv2.drawMarker(img, (e.x, e.y), (0, 0, 255), cv2.MARKER_TILTED_CROSS, 22 if e.boss else 14, 2)
    for c in reading.obs.cards:
        x = bs.SLOTS[c.slot] - 70
        label = f"{c.student or '?'} {c.cost if c.cost is not None else '-'}{'*' if c.ready else ''}"
        cv2.putText(img, label[:22], (x, 852), 0, .45, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(img, label[:22], (x, 852), 0, .45, (255, 255, 255), 1, cv2.LINE_AA)
    if decision.point:
        cv2.circle(img, tuple(int(v) for v in decision.point), 26, (0, 255, 255), 3)
    obs = reading.obs
    lines = [f"{decision.kind.upper()} {decision.student or ''}: {decision.reason}"[:120],
             f"cost {obs.cost}  wave {obs.wave} ({obs.wave_age:.0f}s)  enemies {len(obs.enemies)}  allies {len(obs.allies)}"
             f"  timer {obs.timer if obs.timer is None else round(obs.timer)}  boss {reading.boss.get('frac')}"]
    for i, text in enumerate(lines):
        y = 170 + i * 26
        cv2.putText(img, text, (20, y), 0, .62, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(img, text, (20, y), 0, .62, (0, 255, 255), 1, cv2.LINE_AA)
    return img


class Recorder:
    def __init__(self, out=None):
        self.dir = Path(out) if out else OUT / time.strftime("%Y%m%d_%H%M%S")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file = open(self.dir / "decisions.jsonl", "a", encoding="utf-8")
        self.saved, self.last_key = 0, None
        self.counts = {"cast": 0, "wait": 0, "fallback": 0}
        self.raw_saved, self.raw_at = 0, None
        self.peak_enemies, self.peak_unclassified, self.wave_frames = 0, 0, 0

    def _keep_raw(self, frame, t):
        if self.raw_saved >= RAW_MAX or (self.raw_at is not None and t - self.raw_at < RAW_EVERY):
            return
        (self.dir / "raw").mkdir(exist_ok=True)
        self.raw_saved += 1
        self.raw_at = t
        cv2.imwrite(str(self.dir / "raw" / f"{self.raw_saved:03d}_{t:06.1f}s.png"), np.asarray(frame))

    def wave_report(self):
        """Whether this fight showed a real group of enemies, which is what AoE holding needs."""
        if self.wave_frames:
            verdict = "wave seen"
        elif self.peak_unclassified >= WAVE_SIZE:
            verdict = "maybe a wave: bars seen but not recognised as enemies, so the badge template needs checking"
        else:
            verdict = "no wave in this fight: try the next one"
        return {"verdict": verdict, "peak_enemies": self.peak_enemies, "peak_unclassified_bars": self.peak_unclassified,
                f"frames_with_{WAVE_SIZE}_plus_enemies": self.wave_frames, "raw_frames": self.raw_saved}

    def add(self, frame, reading, decision, t):
        self.counts[decision.kind] += 1
        foes = len(field.enemies(reading.bars))
        self.peak_enemies = max(self.peak_enemies, foes)
        self.peak_unclassified = max(self.peak_unclassified, sum(b.kind == "other" for b in reading.bars))
        self.wave_frames += foes >= WAVE_SIZE
        self._keep_raw(frame, t)
        key = (decision.kind, decision.slot, decision.student, decision.reason.split(":")[0])
        fresh = key != self.last_key
        self.last_key = key
        if not fresh:
            return
        obs = reading.obs
        name = None
        if self.saved < MAX_FRAMES:
            self.saved += 1
            name = f"{self.saved:04d}_{decision.kind}.jpg"
            cv2.imwrite(str(self.dir / name), annotate(frame, reading, decision), [cv2.IMWRITE_JPEG_QUALITY, 80])
        row = {"t": round(t, 2), "decision": decision.kind, "student": decision.student, "slot": decision.slot,
               "target": decision.target, "point": decision.point, "reason": decision.reason,
               "released_by": decision.released_by, "held": decision.held, "cost": obs.cost, "wave": obs.wave,
               "enemies": len(obs.enemies), "allies": len(obs.allies), "timer": obs.timer, "boss": reading.boss,
               "cards": [[c.slot, c.student, c.cost, c.ready] for c in obs.cards], "unsure": reading.why_unsure,
               "frame": name}
        self.file.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        self.file.flush()

    def close(self):
        self.file.close()


def _step(perception, frame, t, memory, skills, cfg):
    reading = perception.read(frame, t)
    if reading is None:
        return None, memory, None
    # Merge in anyone the perception found outside the expected team, as unknown skills.
    for student in perception.card_map.values():
        skills.setdefault(student, perception.catalog.skill(student))
    decision, memory = policy.decide(reading.obs, skills, memory, cfg)
    if decision.kind == "cast":
        memory.last_cast = None          # nothing was actually cast, so don't throttle the next look
    return reading, memory, decision


def run(game, vision, seconds=180, team=None, every=0.25, log=None, out=None):
    """Watch the live game. Stops after `seconds`, or once a seen battle has ended."""
    eyes = ReadOnly(game)
    log = log or eyes.log
    view = getattr(eyes.grab(), "view_info", None)
    if view is not None and not view.native and not hasattr(bs, "hud_frame"):
        reason = (f"the game's picture is {view.rect[2]}x{view.rect[3]}, where card badges misread until Core adds "
                  "battlestate.hud_frame")
        log(f"shadow: not started: {reason}")
        return {"blocked": reason, "battle_seen": False}
    catalog = Catalog()
    perception = Perception(vision, catalog, team)
    skills = catalog.skills_for(team)
    recorder = Recorder(out)
    memory, seen, hud_at, outcome = None, False, None, None
    log(f"shadow: watching for up to {seconds}s, no input will be sent; output in {recorder.dir}")
    start = time.monotonic()
    try:
        while time.monotonic() - start < seconds:
            frame = eyes.grab()
            t = time.monotonic() - start
            reading, memory, decision = _step(perception, frame, t, memory, skills, catalog.policy)
            if reading is None:
                if seen:
                    outcome = eyes_mod.battle_end(vision, frame)
                    if outcome or t - hud_at >= HUD_LOST:
                        log(f"shadow: the battle ended ({outcome or 'no result screen seen'})")
                        break
            else:
                seen, hud_at = True, t
                recorder.add(frame, reading, decision, t)
            eyes.sleep(every)
    finally:
        perception.close()
        recorder.close()
    wave = recorder.wave_report()
    log(f"shadow: {recorder.counts}, saved {recorder.saved} frames to {recorder.dir}; {wave['verdict']}")
    return {"dir": str(recorder.dir), "counts": recorder.counts, "battle_seen": seen, "outcome": outcome, "wave": wave,
            "extra": perception.extra, "summary": perception.summary()}


def replay(paths, vision, team=None, out=None, dt=1.0, catalog=None):
    """Run the whole pipeline over saved frames, with `dt` seconds between them."""
    catalog = catalog or Catalog()
    perception = Perception(vision, catalog, team, background=False)
    skills = catalog.skills_for(team)
    recorder = Recorder(out)
    memory, t, used = None, 0.0, 0
    for path in paths:
        frame = cv2.imread(str(path))
        if frame is None or frame.shape[:2] != (1080, 1920):
            continue
        reading, memory, decision = _step(perception, frame, t, memory, skills, catalog.policy)
        if reading is not None:
            recorder.add(frame, reading, decision, t)
            used += 1
        t += dt
    perception.close()
    recorder.close()
    return {"dir": str(recorder.dir), "counts": recorder.counts, "frames": used}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=int, default=180)
    ap.add_argument("--replay", help="a folder of saved frames instead of the live game")
    ap.add_argument("--team", help="comma-separated names, default: the catalog's team")
    a = ap.parse_args(argv)
    team = [s.strip() for s in a.team.split(",")] if a.team else None
    from ..vision import Vision
    vision = Vision()
    if a.replay:
        result = replay(sorted(Path(a.replay).glob("*.png")), vision, team)
    else:
        from ..game import Game
        result = run(Game(), vision, a.seconds, team, log=print)
    print(json.dumps(result, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
