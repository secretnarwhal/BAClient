"""Battles: auto-proceed through one fight, and use EX skills on checked battle state.

run(): Formation -> Mobilize -> (AUTO) -> Battle Complete -> reward. It stops on a defeat, when
AUTO is locked, or on timeout, and only clicks a button whose template is on screen. Unknown
screens are saved to captures/ so new templates can be cut from them.

use_ex(): waits for a card to be really ready, clicks it, confirms targeting came up, clicks the
target and confirms the skill went off. It never clicks while the battle HUD isn't showing.
"""
import re
import time
from pathlib import Path

import cv2
import numpy as np

from .. import battlestate as bs
from .. import buttons, nav, ocr, viewport
from ..screens import _dimmed, detect

CAPTURES = Path(__file__).resolve().parents[2] / "captures"
FIELD = (40, 130, 1880, 840)     # where an EX target can be: clear of the top bar and the cards
UNIT_ROW = (150, 790, 1640, 850)  # Mobilize Unit: each Striker's "Lv.NN" and name
MAX_UNIT_PRESSES = 2
OCCUPY_POLLS = 6                  # seconds the Occupy Base dialog may stay up before handing back


def _click_button(game, vision, frame, name):
    pos = vision.find(frame, name)
    if pos:
        game.click(*pos)
    return pos


def unit_has_striker(frame):
    """Whether the selected unit on Mobilize Unit has a student in it. Unreadable counts as no."""
    view = getattr(frame, "view_info", None)
    shift = view.default_shift(960, 820) if view is not None else (0, 0)
    pixels = viewport.crop(frame, UNIT_ROW, shift)
    if pixels.size == 0:
        return False
    for scale in (2, 3):
        text = " ".join(w.text for w in ocr.read(np.asarray(pixels), scale=scale))
        if re.search(r"L[vu][.,]?\s*\d", text):
            return True
    return False


def _wait_leave(game, vision, screen, seconds):
    """Wait for the screen to change, so one button is never pressed twice."""
    for _ in range(int(seconds / 0.5)):
        game.sleep(0.5)
        if detect(vision, game.grab()) != screen:
            return True
    return False


def run(game, vision, timeout=300, log=None):
    log = log or game.log
    start = time.time()
    last, unknown_since, saved_unknown = None, None, False
    fought = entered = False             # entered: the fight was really on screen
    presses = {}                         # Mobilize presses per screen. Most fights cost AP
    auto_tries = occupy = 0
    while time.time() - start < timeout:
        frame = game.grab()
        screen = detect(vision, frame)
        t = int(time.time() - start)
        if screen != last:
            log(f"[{t:3d}s] {screen}")
            last = screen
        if screen == "unknown":
            unknown_since = unknown_since or time.time()
            if not saved_unknown and time.time() - unknown_since > 20:
                CAPTURES.mkdir(exist_ok=True)
                p = CAPTURES / f"unknown_{int(time.time())}.png"
                cv2.imwrite(str(p), frame)
                log(f"        saved unrecognised screen to {p}")
                saved_unknown = True
            game.sleep(1)
            continue
        unknown_since, saved_unknown = None, False

        if screen == "occupy_dialog":
            # Never pressed here. Start Battle spends AP, and only conquest.run checks the level and cost first.
            occupy += 1
            if occupy >= OCCUPY_POLLS:
                log("battle: the Occupy Base dialog is open; `conquest-step` presses Start Battle after its checks")
                return "occupy_dialog"
            game.sleep(1)
        elif screen in ("formation", "alliance_formation"):
            popup = nav.top_popup(vision, frame)
            if popup:
                if popup[0] != "Relationship Rank Buff":
                    log(f"battle: {popup[0]} is open over the formation; stopping")
                    return "blocked"
                game.click(*popup[1])
                game.sleep(1.2)
                continue
            title = "formation_title" if screen == "formation" else "alliance_title"
            if _dimmed(vision, frame, title):    # a dialog nobody recognises: its button isn't ours to press through
                log("battle: an unrecognised dialog is open over the formation; stopping")
                return "blocked"
            if entered or presses.get(screen, 0) >= MAX_UNIT_PRESSES:
                log("battle: back on the formation, or Mobilize didn't start the fight; stopping")
                return "blocked"
            if _click_button(game, vision, frame, "mobilize"):
                presses[screen] = presses.get(screen, 0) + 1
                fought = True
                _wait_leave(game, vision, screen, 8)
            else:
                game.sleep(1)
        elif screen == "mobilize_unit":
            if nav.top_popup(vision, frame) or _dimmed(vision, frame, "mobilize_unit_title"):
                log("battle: a popup is open over Mobilize Unit; stopping")
                return "blocked"
            if not unit_has_striker(frame):      # choosing the team is the player's job, not this task's
                log("battle: this unit on Mobilize Unit is empty; stopping so a team can be chosen")
                return "needs_team"
            if entered or presses.get(screen, 0) >= MAX_UNIT_PRESSES:
                log("battle: Mobilize didn't start the fight; stopping")
                return "blocked"
            if _click_button(game, vision, frame, "mobilize"):
                presses[screen] = presses.get(screen, 0) + 1
                fought = True
                _wait_leave(game, vision, screen, 8)
            else:
                game.sleep(1)
        elif screen == "in_battle":
            fought = entered = True
            state = bs.read(vision, frame)
            if state and state["auto"] == "locked":
                log("battle: AUTO is locked in this fight, so it needs manual skills (ctl run ex)")
                return "auto_locked"
            if state and state["auto"] == "off" and not state["targeting"] and auto_tries < 3:
                auto_tries += 1
                if buttons.tie(vision, frame, "bs_auto_off", *bs.AUTO_POINT):
                    game.click(*bs.AUTO_POINT)
                    log("battle: turned AUTO on")
                game.sleep(1.5)
            else:
                game.sleep(1)
        elif screen == "battle_result":
            if _click_button(game, vision, frame, "result_confirm"):
                _wait_leave(game, vision, screen, 6)
            else:
                game.sleep(1)
        elif screen == "story":
            from . import story
            story.skip(game, vision, log)
        elif screen == "reward":
            if buttons.tie(vision, frame, "click_continue", 960, 945):
                game.click(960, 945)
            elif not _click_button(game, vision, frame, "reward_confirm"):
                game.sleep(1)
                continue
            game.sleep(4)
            log(f"[{int(time.time() - start):3d}s] reward claimed - done")
            return "won"
        elif screen in ("defeat", "battle_tip"):
            log("defeat - stopping so you can decide what to do")
            return "defeat"
        elif screen == "conquest_map" and fought:
            log("back on the map - done")
            return "won"
        else:
            game.sleep(1)
    log("timeout")
    return "timeout"


def _wait_state(game, vision, test, seconds, poll=0.08):
    """Poll the HUD until test(state) holds. Returns the state, or None on timeout."""
    end = time.monotonic() + seconds
    while True:
        state = bs.read(vision, game.grab())
        if state is not None and test(state):
            return state
        if time.monotonic() >= end:
            return None
        game.sleep(poll)


def use_ex(game, vision, slot, target=None, times=1, timeout=60, log=None):
    """Use the EX card in `slot` (1-3) `times` times. Returns how many went off."""
    log = log or game.log
    if slot not in bs.SLOTS:
        raise ValueError("slot must be 1, 2 or 3")
    if target is not None:
        x, y = target
        if not (FIELD[0] <= x <= FIELD[2] and FIELD[1] <= y <= FIELD[3]):
            raise ValueError(f"target {target} is outside the battlefield {FIELD}")
    card = (bs.SLOTS[slot], bs.CARD_Y)
    used = 0
    for _ in range(times):
        ready = _wait_state(game, vision, lambda s: s["cards"][slot]["ready"] and not s["targeting"], timeout)
        if ready is None:
            log(f"ex: card {slot} wasn't ready within {timeout}s ({used} used)")
            break
        spent = ready["cards"][slot]["cost"]
        game.click(*card)
        aiming = _wait_state(game, vision, lambda s: s["selected"] == slot, 1.2)
        if aiming is None:
            log(f"ex: card {slot} didn't select; trying once more")
            again = bs.read(vision, game.grab())
            if not (again and again["cards"][slot]["ready"] and not again["targeting"]):
                log("ex: the card is no longer ready; stopping")
                break
            game.click(*card)
            aiming = _wait_state(game, vision, lambda s: s["selected"] == slot, 1.2)
            if aiming is None:
                log("ex: the card won't select; stopping")
                break
        if target is None:
            game.click(*card)            # a second press on the card cancels targeting
            raise ValueError(f"card {slot} needs a target: pass --at X,Y")
        game.click(*target)
        fired = _wait_state(game, vision, lambda s: not s["targeting"], 1.5)
        if fired is None:
            log("ex: target wasn't accepted; targeting is still open, stopping")
            break
        used += 1
        game.rt.bump("EX used")
        log(f"ex: card {slot} (cost {spent}) fired, {used}/{times}")
    return used
