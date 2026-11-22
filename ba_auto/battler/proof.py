"""Proof: the smart battler against the game's AUTO on one repeatable low-stakes stage.

    python -m ba_auto.battler.proof --stage N1-1 --mode auto    # watch a fight the player runs on AUTO
    python -m ba_auto.battler.proof --stage N1-1 --mode smart   # control a fight already on screen
    python -m ba_auto.battler.proof --stage N1-1                # compare what's been recorded

It never starts a battle, so it can't spend AP or a ticket by itself. Each run adds a line to
captures/battler/proof.jsonl. A verdict needs at least MIN_RUNS of each mode on the same stage.
"""
import argparse
import json
import statistics
import time
from pathlib import Path

from .. import battlestate as bs
from .perception import battle_end

LOG = Path(__file__).resolve().parents[2] / "captures" / "battler" / "proof.jsonl"
MIN_RUNS = 3


def load(path=None):
    try:
        lines = Path(path or LOG).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def record(row, path=None):
    path = Path(path or LOG)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _median(values):
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 1) if values else None


def summarize(rows, stage):
    """{mode: figures} for one stage. Runs with no read outcome count as runs but not wins."""
    out = {}
    for mode in ("auto", "smart"):
        runs = [r for r in rows if r.get("stage") == stage and r.get("mode") == mode]
        wins = [r for r in runs if r.get("outcome") == "won"]
        out[mode] = {"runs": len(runs), "wins": len(wins),
                     "clear_seconds": _median([r.get("clear_seconds") for r in wins]),
                     "survivors": _median([r.get("survivors") for r in wins]),
                     "handed_back": sum(bool(r.get("fallback")) for r in runs)}
    return out


def verdict(summary):
    auto, smart = summary["auto"], summary["smart"]
    if auto["runs"] < MIN_RUNS or smart["runs"] < MIN_RUNS:
        return f"not enough runs yet: AUTO {auto['runs']}, smart {smart['runs']}, need {MIN_RUNS} of each"
    if smart["handed_back"]:
        return f"not a fair result: smart handed back to AUTO in {smart['handed_back']} of {smart['runs']} runs"
    a_rate, s_rate = auto["wins"] / auto["runs"], smart["wins"] / smart["runs"]
    if s_rate != a_rate:
        return f"smart {'wins more' if s_rate > a_rate else 'wins less'}: {s_rate:.0%} against AUTO's {a_rate:.0%}"
    if auto["clear_seconds"] is None or smart["clear_seconds"] is None:
        return "same win rate, but clear times weren't readable"
    delta = auto["clear_seconds"] - smart["clear_seconds"]
    if abs(delta) < 1.0:
        return f"no clear difference in time (AUTO {auto['clear_seconds']}s, smart {smart['clear_seconds']}s)"
    return f"smart is {abs(delta):.1f}s {'faster' if delta > 0 else 'slower'} than AUTO (median clear time)"


def wait_battle(game, vision, seconds=60):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if bs.read(vision, game.grab()) is not None:
            return True
        game.sleep(0.5)
    return False


def wait_auto(game, vision, seconds=8):
    """AUTO is on. `ctl run battle` may switch it on a moment after the HUD appears."""
    end = time.monotonic() + seconds
    while True:
        state = bs.read(vision, game.grab())
        if state is not None and state["auto"] == "on":
            return True
        if time.monotonic() >= end:
            return False
        game.sleep(0.25)


def read_outcome(game, vision, seconds=25):
    """'won' or 'defeat' from the result screen, or None if it wasn't seen."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ended = battle_end(vision, game.grab())
        if ended:
            return ended
        game.sleep(0.4)
    return None


def run(game, vision, stage, mode, team=None, log=None):
    """Measure one fight. Starting it is the player's job, and that's what spends AP."""
    from . import shadow
    from ..tasks import smartbattle
    log = log or game.log
    if not wait_battle(game, vision):
        log("proof: no battle came up")
        return None
    if mode == "auto":
        if not wait_auto(game, vision):
            log("proof: AUTO must be on for an AUTO baseline; nothing recorded")
            return None
        got = shadow.run(game, vision, 420, team, log=log)
        summary, fallback, casts, seen = got["summary"], None, None, got.get("outcome")
    else:
        got = smartbattle.fight(game, vision, team, log=log)
        if got.outcome in ("no_battle", "refused", "auto_locked"):
            log(f"proof: smart run didn't take control ({got.outcome}); nothing recorded")
            return None
        summary, fallback, casts, seen = got.summary, got.fallback, len(got.casts), got.battle
    row = {"when": time.strftime("%Y-%m-%d %H:%M:%S"), "stage": stage, "mode": mode,
           "outcome": seen or read_outcome(game, vision), "fallback": fallback, "casts": casts, **summary}
    record(row)
    log(f"proof: {mode} on {stage}: {row['outcome']}, {row.get('clear_seconds')}s, {row.get('survivors')} up")
    return row


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", required=True)
    ap.add_argument("--mode", choices=("auto", "smart"))
    ap.add_argument("--team")
    a = ap.parse_args(argv)
    if a.mode:
        from ..game import Game
        from ..vision import Vision
        team = [s.strip() for s in a.team.split(",")] if a.team else None
        print(json.dumps(run(Game(), Vision(), a.stage, a.mode, team, print), indent=1, ensure_ascii=False))
    summary = summarize(load(), a.stage)
    print(json.dumps(summary, indent=1))
    print(verdict(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
