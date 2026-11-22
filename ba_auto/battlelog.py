"""Battle recorder: writes one line to data/battles.jsonl for every fight the client sees.

It only watches frames, so it covers HUD runs and hand-played fights alike. Each record holds the
team from the Formation screen, the boss, the outcome, the time and the damage. A field that
wasn't on screen is null rather than a guess.
"""
import json
import re
import time

from . import battlestate as bs
from . import journal, nav, ocr

LOG = journal.ROOT / "data" / "battles.jsonl"
TEAM_BOXES = ((400, 795, 600, 835), (725, 795, 935, 835), (1060, 795, 1270, 835), (1395, 795, 1600, 835),
              (720, 985, 910, 1040), (1245, 985, 1430, 1040))
DEFEAT_TIME = (970, 444, 1150, 486)
DEFEAT_TEXT = (590, 668, 1330, 714)
WIN_TIME = (1715, 44, 1860, 86)
WIN_DAMAGE = (1320, 44, 1480, 86)
FORMATIONS = ("formation", "alliance_formation")


def read_team(frame):
    team = []
    for box in TEAM_BOXES:
        name = " ".join(w.text for w in ocr.read(frame, box, scale=3)).strip()
        if name and "slot" not in name.lower() and "empty" not in name.lower():
            team.append(name)
    return team


def _digits(text):
    d = re.sub(r"[^\d]", "", text or "")
    return int(d) if d else None


def read_result(frame, screen):
    if screen == "defeat":
        text = " ".join(w.text for w in ocr.read(frame, DEFEAT_TEXT, scale=2))
        m = re.search(r"Dealt\s+([\d,. ]+)\s+damage to\s+(.+?)\.?$", text)
        return {"outcome": "defeat", "time": bs.read_timer(frame, DEFEAT_TIME),
                "damage": _digits(m.group(1)) if m else None, "boss": m.group(2).strip() if m else None}
    damage = " ".join(w.text for w in ocr.read(frame, WIN_DAMAGE, scale=2))
    return {"outcome": "won", "time": bs.read_timer(frame, WIN_TIME), "damage": _digits(damage), "boss": None}


class BattleWatcher:
    """Fed frames by the client's monitor thread, about once a second."""

    def __init__(self, rt=None):
        self.rt = rt
        self.team, self.boss = [], None
        self.fighting = False
        self._team_at = self._boss_at = 0.0
        self._last_key = None

    def observe(self, vision, frame, screen):
        now = time.monotonic()
        if screen in FORMATIONS and not nav.top_popup(vision, frame) and now - self._team_at > 4:
            self._team_at = now
            team = read_team(frame)
            if team:
                self.team = team
        elif screen == "in_battle":
            self.fighting = True
            if now - self._boss_at > 8:
                self._boss_at = now
                state = bs.read(vision, frame, text=True)
                if state and state.get("boss"):
                    self.boss = state["boss"]
        elif screen in ("battle_result", "defeat"):
            result = read_result(frame, screen)
            key = (screen, result["time"], result["damage"])
            if key != self._last_key and (self.fighting or result["time"]):
                self._last_key = key
                return self._write(result)
        return None

    def _write(self, result):
        record = {
            "when": time.strftime("%Y-%m-%d %H:%M:%S"),
            "outcome": result["outcome"],
            "boss": result["boss"] or self.boss,
            "time": result["time"],
            "damage": result["damage"],
            "team": list(self.team),
        }
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        journal.record("battle", **record)
        if self.rt is not None:
            self.rt.log(f"battle: {record['outcome']} vs {record['boss']} in {record['time']}, damage {record['damage']}")
        self.fighting, self.boss = False, None
        return record


def load():
    try:
        rows = LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in rows:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def show(boss=None, last=20, as_json=False):
    rows = load()
    if boss:
        rows = [r for r in rows if boss.lower() in str(r.get("boss")).lower()]
    wins = sum(r["outcome"] == "won" for r in rows)
    print(f"{len(rows)} fights, {wins} won")
    rows = rows[-last:]
    if as_json:
        print(json.dumps(rows, indent=1, ensure_ascii=False))
        return 0
    for r in rows:
        dmg = f"{r['damage']:,}" if isinstance(r.get("damage"), int) else "?"
        print(f"{r['when']}  {r['outcome']:7}{str(r.get('boss'))[:20]:21}{str(r.get('time')):11}{dmg:>14}  "
              + ", ".join(r.get("team") or []))
    return 0
