"""Smart battle: play a fight's EX skills with the battler policy instead of the game's AUTO.

    python -m ba_auto.tasks.smartbattle                  # plan only: prints readiness, no input
    python -m ba_auto.tasks.smartbattle --recon          # read each card's skill text, then cancel
    python -m ba_auto.tasks.smartbattle --live           # control the battle on screen

`fight()` is the interface a raid runner would call. It handles one battle already on screen. It never
starts a battle, so it can't spend a ticket. Any doubt hands the fight back to AUTO rather than leaving
the team idle. The player's AUTO setting is put back while the battle HUD is still up. The game keeps
AUTO between battles, so if the fight ends first, the setting is saved and the next smart fight
restores it. F8 stops everything at once and sends no further clicks, not even the restore.
"""
import argparse
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from .. import battlestate as bs
from .. import ocr
from .. import portraits
from ..battler import cards, perception as eyes, policy
from ..battler.catalog import Catalog
from ..battler.perception import Perception
from ..runtime import Stopped
from .battle import FIELD

ROOT = Path(__file__).resolve().parents[2]
RECON = ROOT / "captures" / "battler" / "recon"
PENDING = ROOT / "captures" / "battler" / "pending_auto.json"
BANNER_BOX = (170, 44, 1096, 136)  # the skill text shown while a card is selected
MAX_FAILS = 2                      # failed casts in a row before handing back to AUTO
MAX_AUTO_RETRIES = 3               # presses to get AUTO back on after handing the fight back
AIM_STUCK = 4.0                    # seconds a card may stay selected before the fight goes back to AUTO
HUD_LOST = 25.0                    # seconds with no HUD and no result screen before giving up


@dataclass
class Result:
    outcome: str = "running"       # ended, fallback_ended, lost_hud, timeout, stopped, refused, no_battle
    battle: str = None             # won or defeat, from the result screen
    casts: list = field(default_factory=list)
    fallback: str = None
    auto_before: str = None
    auto_after: str = None
    latency_ms: dict = field(default_factory=dict)
    seconds: float = 0.0
    summary: dict = field(default_factory=dict)

    def public(self):
        return {"outcome": self.outcome, "battle": self.battle, "casts": len(self.casts), "fallback": self.fallback,
                "auto_before": self.auto_before, "auto_after": self.auto_after, "latency_ms": self.latency_ms,
                "seconds": round(self.seconds, 1), "summary": self.summary, "cast_log": self.casts}


class Timing:
    def __init__(self):
        self.samples = {}

    def add(self, name, seconds):
        self.samples.setdefault(name, []).append(seconds * 1000)

    def summary(self):
        out = {}
        for name, values in self.samples.items():
            ordered = sorted(values)
            out[name] = {"n": len(ordered), "median": round(ordered[len(ordered) // 2]), "max": round(ordered[-1])}
        return out


def _press(game, x, y):
    """A click on the battle HUD or field. Outside 16:9 this needs Core's checked battle click."""
    battle_click = getattr(game, "click_battle", None)
    if battle_click is not None:
        return battle_click(x, y)
    return game.click(x, y)


def click_problem(game, frame):
    """Why this window can't take battle clicks, or None. Checked before AUTO is touched."""
    view = getattr(frame, "view_info", None)
    if view is None or view.native or hasattr(game, "click_battle"):
        return None
    return (f"the game's picture is {view.rect[2]}x{view.rect[3]}, not 16:9, and fixed battle positions are "
            "refused there until Core adds Game.click_battle")


def _wait(game, vision, test, seconds, poll=0.04):
    end = time.monotonic() + seconds
    while True:
        frame = game.grab()
        state = bs.read(vision, frame)
        if state is not None and test(state):
            return state, frame
        if time.monotonic() >= end:
            return None, frame
        game.sleep(poll)


def set_auto(game, vision, want, log, tries=3):
    """Turn AUTO on or off, confirming from the screen. True when it's in the wanted state."""
    for _ in range(tries):
        state = bs.read(vision, game.grab())
        if state is None:
            return False
        if state["auto"] == "locked":
            return False
        if state["auto"] == want:
            return True
        if state["targeting"]:
            _press(game, bs.SLOTS[state["selected"]], bs.CARD_Y)  # a second press cancels the selection
            game.sleep(0.2)
            continue
        _press(game, *bs.AUTO_POINT)
        done, _ = _wait(game, vision, lambda s: s["auto"] == want, 1.2)
        if done:
            return True
    log(f"smartbattle: couldn't turn AUTO {want}")
    return False


def in_field(point):
    return FIELD[0] <= point[0] <= FIELD[2] and FIELD[1] <= point[1] <= FIELD[3]


def cast(game, vision, perception, decision, timing, log):
    """Select the card, click the target, confirm it fired. Returns (ok, why)."""
    slot, point = decision.slot, tuple(int(v) for v in decision.point)
    if not in_field(point):
        return False, f"target {point} is outside the battlefield"
    frame = game.grab()
    state = bs.read(vision, frame)
    if state is None or state["targeting"] or not state["cards"][slot]["ready"]:
        return False, "the card is no longer ready"
    dev = perception.bank.identify(frame, slot)[0]
    if perception.card_map.get(dev) != decision.student:
        return False, f"card {slot} changed before the click"
    started = time.perf_counter()
    _press(game, bs.SLOTS[slot], bs.CARD_Y)
    timing.add("card_click", time.perf_counter() - started)
    aimed = time.perf_counter()
    state, _ = _wait(game, vision, lambda s: s["selected"] == slot, 1.0)
    timing.add("select_seen", time.perf_counter() - aimed)
    if state is None:
        late = bs.read(vision, game.grab())
        if late is not None and late["selected"] == slot:     # it selected after the wait: back out, or it stays aiming
            _press(game, bs.SLOTS[slot], bs.CARD_Y)
            game.sleep(0.15)
        return False, "the card didn't select"
    started = time.perf_counter()
    _press(game, *point)
    timing.add("target_click", time.perf_counter() - started)
    fired = time.perf_counter()
    went_off = _fired(game, vision, 1.4)
    timing.add("fire_seen", time.perf_counter() - fired)
    if went_off:
        return True, None
    state = bs.read(vision, game.grab())
    if state is not None and state["selected"] == slot:   # still aiming: back out, on the HUD only
        _press(game, bs.SLOTS[slot], bs.CARD_Y)
        game.sleep(0.15)
    return False, "the target wasn't accepted"


def _fired(game, vision, seconds, poll=0.04):
    """Targeting closed, or the HUD went away because the skill's scene played or the fight ended."""
    end = time.monotonic() + seconds
    while True:
        state = bs.read(vision, game.grab())
        if state is None or not state["targeting"]:
            return True
        if time.monotonic() >= end:
            return False
        game.sleep(poll)


def fight(game, vision, team=None, timeout=420, log=None, catalog=None, frames=None):
    """Control the battle on screen until it ends. Returns a Result."""
    log = log or game.log
    catalog = catalog or Catalog()
    result, timing = Result(), Timing()
    started = time.monotonic()
    opening = game.grab()
    first = bs.read(vision, opening)
    if first is None:
        result.outcome = "no_battle"
        return result
    problem = click_problem(game, opening)
    if problem:
        log(f"smartbattle: not taking control: {problem}")
        result.outcome = "refused"
        result.fallback = problem
        result.auto_before = result.auto_after = first["auto"]
        return result
    if first["auto"] == "locked":
        log("smartbattle: AUTO is locked in this fight, so there's no safe fallback; not taking control")
        result.auto_before = "locked"
        result.outcome = "auto_locked"
        return result
    result.auto_before = _pending() or first["auto"]   # an earlier fight may still owe the player's setting

    perception = Perception(vision, catalog, team)
    skills = catalog.skills_for(team)
    memory, fails, handed_back = None, 0, False
    auto_retries = 0
    aiming_since = None
    hud_at = time.monotonic()
    try:
        if not set_auto(game, vision, "off", log):
            result.fallback = "couldn't turn AUTO off"
            handed_back = True
        while time.monotonic() - started < timeout:
            frame = game.grab()
            now = time.monotonic() - started
            if handed_back:
                state = bs.read(vision, frame)
                if state is not None:
                    hud_at = time.monotonic()
                    if state["auto"] == "off" and auto_retries < MAX_AUTO_RETRIES:   # an earlier toggle may have landed late
                        auto_retries += 1
                        set_auto(game, vision, "on", log, tries=1)
                else:
                    result.battle = eyes.battle_end(vision, frame)
                    if result.battle or time.monotonic() - hud_at >= HUD_LOST:
                        result.outcome = "fallback_ended" if result.battle else "lost_hud"
                        break
                game.sleep(0.5)
                continue
            tick = time.perf_counter()
            reading = perception.read(frame, now)
            timing.add("perceive", time.perf_counter() - tick)
            if reading is None:                  # a skill's scene or wave change hides the HUD for a while
                result.battle = eyes.battle_end(vision, frame)
                if result.battle:
                    result.outcome = "ended"
                    break
                if time.monotonic() - hud_at >= HUD_LOST:
                    result.outcome = "lost_hud"
                    log(f"smartbattle: no battle HUD or result screen for {HUD_LOST:.0f}s; stopping. Check AUTO on screen")
                    break
                game.sleep(0.1)
                continue
            hud_at = time.monotonic()
            if reading.obs.targeting:            # the policy only waits while aiming, so a stuck card would idle the team
                aiming_since = aiming_since or time.monotonic()
                if time.monotonic() - aiming_since >= AIM_STUCK:
                    handed_back = _hand_back(game, vision, result, "a card stayed selected", log)
                    continue
            else:
                aiming_since = None
            for student in perception.card_map.values():
                skills.setdefault(student, catalog.skill(student))
            decision, memory = policy.decide(reading.obs, skills, memory, catalog.policy)
            if decision.kind == "fallback":
                handed_back = _hand_back(game, vision, result, decision.reason, log)
                continue
            if decision.kind == "cast":
                ok, why = cast(game, vision, perception, decision, timing, log)
                entry = {"t": round(now, 2), "student": decision.student, "slot": decision.slot,
                         "point": list(decision.point), "reason": decision.reason, "ok": ok, "why": why,
                         "cost": reading.obs.cost, "enemies": len(reading.obs.enemies), "wave": reading.obs.wave}
                result.casts.append(entry)
                log(f"smartbattle: {'cast' if ok else 'FAILED'} {decision.student}: {decision.reason}"
                    + (f" ({why})" if why else ""))
                fails = 0 if ok else fails + 1
                if not ok:
                    memory.last_cast = None
                if fails >= MAX_FAILS:
                    handed_back = _hand_back(game, vision, result, f"{fails} casts failed in a row: {why}", log)
                continue
            game.sleep(0.08)
        else:
            result.outcome = "timeout"
    except Stopped:
        result.outcome = "stopped"
        raise
    except RuntimeError as err:                  # Game refused an input: never retry around its guards
        result.outcome = "refused"
        result.fallback = str(err)
        log(f"smartbattle: input refused, stopping: {err}")
    finally:
        perception.close()
        result.summary = perception.summary()
        result.seconds = time.monotonic() - started
        result.latency_ms = timing.summary()
        result.auto_after = _restore(game, vision, result.auto_before, log)
    return result


def _hand_back(game, vision, result, why, log):
    result.fallback = why
    log(f"smartbattle: handing the fight back to AUTO: {why}")
    if not set_auto(game, vision, "on", log):
        log("smartbattle: AUTO could not be turned back on; the team may be idle")
    return True


def _pending():
    try:
        return json.loads(PENDING.read_text(encoding="utf-8")).get("auto")
    except (OSError, ValueError):
        return None


def _remember(auto):
    """The game keeps AUTO between battles, so a setting we couldn't put back is owed to the next fight."""
    if auto in ("on", "off"):
        PENDING.parent.mkdir(parents=True, exist_ok=True)
        PENDING.write_text(json.dumps({"auto": auto}), encoding="utf-8")


def _settled():
    try:
        PENDING.unlink()
    except OSError:
        pass


def _restore(game, vision, before, log):
    """Put AUTO back how the player had it. F8 means no more clicks, so then it's only noted."""
    if before not in ("on", "off"):
        return None
    if game.rt.stop.is_set():
        _remember(before)
        log(f"smartbattle: stopped, so no more clicks. AUTO was {before} before this fight; check it on screen")
        return None
    try:
        state = bs.read(vision, game.grab())
        if state is None:                       # the battle is over and its AUTO button is gone
            _remember(before)
            log(f"smartbattle: the fight ended before AUTO could be put back to {before}; "
                "the next smart fight restores it")
            return None
        if state["auto"] != before:
            set_auto(game, vision, before, log)
            state = bs.read(vision, game.grab())
        if state and state["auto"] == before:
            _settled()
        else:
            _remember(before)
        return state["auto"] if state else None
    except Exception as err:
        _remember(before)
        log(f"smartbattle: couldn't restore AUTO: {err}")
        return None


def recon(game, vision, log=None, catalog=None, timeout=150):
    """Read each team member's skill text from the card's targeting banner, then cancel.

    Needs a low-stakes battle on screen. Only three cards show at once, so AUTO plays between reads
    to rotate the hand. A selection is always cancelled, so recon itself spends no cost.
    """
    log = log or game.log
    catalog = catalog or Catalog()
    opening = game.grab()
    state = bs.read(vision, opening)
    if state is None:
        log("recon: no battle on screen")
        return []
    before = state["auto"]
    if before == "locked":
        log("recon: AUTO is locked here; use another battle")
        return []
    problem = click_problem(game, opening)
    if problem:
        log(f"recon: not starting: {problem}")
        return []
    perception = Perception(vision, catalog, background=False)
    out_dir = RECON / time.strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    out, seen, full = [], set(), None
    hud_at = time.monotonic()
    goal = len(catalog.team) or 6
    deadline = time.monotonic() + timeout

    def who(frame, slot, ready):
        nonlocal full
        dev = perception.bank.identify(frame, slot)[0]
        if dev:
            return perception.card_map.get(dev)
        if not ready:                            # grey cards are only trusted against the team
            return None
        full = full or cards.CardBank(cards.all_devs())
        dev = full.identify(frame, slot)[0]
        return portraits.display(dev) if dev else None

    try:
        while len(seen) < goal and time.monotonic() < deadline:
            frame = game.grab()
            state = bs.read(vision, frame)
            if state is None:
                if eyes.battle_end(vision, frame) or time.monotonic() - hud_at >= HUD_LOST:
                    log("recon: the battle ended")
                    break
                game.sleep(0.25)
                continue
            hud_at = time.monotonic()
            if state["targeting"]:
                game.sleep(0.1)
                continue
            hand = {s: who(frame, s, c["ready"]) for s, c in state["cards"].items() if c["cost"] is not None}
            fresh = [s for s, name in hand.items() if name and name not in seen]
            if not fresh:
                if state["auto"] != "on" and not set_auto(game, vision, "on", log):
                    break
                game.sleep(0.4)                  # AUTO plays until an unread card comes up
                continue
            if state["auto"] == "on":
                if not set_auto(game, vision, "off", log):
                    break
                continue
            ready = [s for s in fresh if state["cards"][s]["ready"]]
            if not ready:
                game.sleep(0.3)
                continue
            slot = ready[0]
            name, cost = hand[slot], state["cards"][slot]["cost"]
            seen.add(name)
            _press(game, bs.SLOTS[slot], bs.CARD_Y)
            aimed, shot = _wait(game, vision, lambda s: s["selected"] == slot, 1.0)
            if aimed is None:
                log(f"recon: {name} didn't enter targeting; it may have cast at once")
                cv2.imwrite(str(out_dir / f"slot{slot}_no_targeting.png"), np.asarray(shot))
                catalog.observe(name, cost=cost)
                out.append({"slot": slot, "student": name, "cost": cost, "text": None, "targeting": False})
                continue
            game.sleep(0.25)                     # let the banner and the range overlay finish drawing
            shot = game.grab()
            text = " ".join(w.text for w in ocr.read(shot, BANNER_BOX, scale=2)).strip() or None
            cv2.imwrite(str(out_dir / f"{name.replace(' ', '_')}.png"), np.asarray(shot))
            _press(game, bs.SLOTS[slot], bs.CARD_Y)   # a second press cancels: nothing is spent
            _wait(game, vision, lambda s: not s["targeting"], 1.0)
            skill = catalog.observe(name, text=text, cost=cost)
            log(f"recon: {name} cost {cost}: {skill.target} from: {text}")
            out.append({"slot": slot, "student": name, "cost": cost, "text": text, "target": skill.target})
    except RuntimeError as err:
        log(f"recon: input refused, stopping: {err}")
    finally:
        perception.close()
        _restore(game, vision, before, log)
    (out_dir / "recon.json").write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    log(f"recon: read {len(seen)} of {goal} students; frames and results in {out_dir}")
    return out


def readiness(team=None, catalog=None):
    """What the battler knows about this team. Sends no input."""
    catalog = catalog or Catalog()
    rows = catalog.coverage(team)
    return {"team": rows, "unknown": [r["student"] for r in rows if r["target"] == "unknown"],
            "no_card_art": [r["student"] for r in rows if not r["dev"]]}


def run(game, vision, team=None, timeout=420):
    """The entry point for `ctl run smart-battle`."""
    return fight(game, vision, team, timeout).public()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="control the battle on screen")
    ap.add_argument("--recon", action="store_true", help="read each card's skill text and cancel")
    ap.add_argument("--team", help="comma-separated names, default: the catalog's team")
    ap.add_argument("--timeout", type=int, default=420)
    a = ap.parse_args(argv)
    team = [s.strip() for s in a.team.split(",")] if a.team else None
    if not (a.live or a.recon):
        print(json.dumps(readiness(team), indent=1, ensure_ascii=False))
        return 0
    from ..game import Game
    from ..vision import Vision
    game, vision = Game(), Vision()
    if a.recon:
        print(json.dumps(recon(game, vision, log=print), indent=1, ensure_ascii=False))
        return 0
    print(json.dumps(fight(game, vision, team, a.timeout, log=print).public(), indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
