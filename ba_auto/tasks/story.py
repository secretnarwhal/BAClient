"""Story skipping.

skip(): from any story scene: Menu -> >> (skip) -> Summary "Skip this story?" ->
Confirm -> reward "click to continue". Returns once the story is gone.

episodes(): on a chapter's Episode List, repeatedly enter the topmost episode
tagged "New" and skip it, until no "New" tags remain.
"""
import time

from .. import buttons, inputguard

MENU_FALLBACK = (1805, 63)
SKIP_FALLBACK = (1812, 183)
CONFIRM_FALLBACK = (1150, 790)
# Main Story's Episode List is staggered: each row's Enter sits at a fixed offset from its New tag.
MAIN_ENTER = ((1757, 381), (1665, 540), (1575, 698), (1483, 857))
MAIN_TAG_TO_ENTER = (695, 68)


def _wait_for(game, vision, names, timeout=8.0, poll=0.4):
    """Wait until any template in `names` is visible; returns (name, pos, frame)."""
    end = time.time() + timeout
    while time.time() < end:
        f = game.grab()
        for n in names:
            p = vision.find(f, n)
            if p:
                return n, p, f
        game.sleep(poll)
    return None, None, None


def skip(game, vision, log=None, timeout=60):
    log = log or game.log
    end = time.time() + timeout
    while time.time() < end:
        name, pos, frame = _wait_for(game, vision,
                                     ["skip_question", "story_skip", "story_menu",
                                      "reward_acquired"], timeout=6)
        if name == "skip_question":
            if inputguard.try_click(game, *(vision.find(frame, "skip_confirm") or CONFIRM_FALLBACK)):
                log("story: confirmed skip")
                game.sleep(2.5)
        elif name == "story_skip":
            if inputguard.try_click(game, *pos):
                game.sleep(1.0)
        elif name == "story_menu":
            if inputguard.try_click(game, *pos):
                game.sleep(0.8)
        elif name == "reward_acquired":
            if buttons.tie(vision, frame, "click_continue", 960, 945):
                if not inputguard.try_click(game, 960, 945):
                    continue
                log("story: reward claimed")
                game.sleep(3)
                return True
            # battle-style reward screen with buttons is handled by the battle task
            return True
        else:
            # nothing story-related visible: either finished or a transition
            return True
    log("story: timeout")
    return False


def _enter_point(vision, frame, layout):
    """Where the topmost New episode's Enter is, or None if there is no New tag."""
    if layout == "main_episode_list_title":
        tag = vision.find(frame, "main_episode_new")
        if not tag:
            return None
        guess = (tag[0] + MAIN_TAG_TO_ENTER[0], tag[1] + MAIN_TAG_TO_ENTER[1])
        best = min(MAIN_ENTER, key=lambda p: abs(p[0] - guess[0]) + abs(p[1] - guess[1]))
        if abs(best[0] - guess[0]) + abs(best[1] - guess[1]) > 40:
            raise RuntimeError(f"episodes: New tag at {tag} doesn't line up with a known row")
        return best
    tag = vision.find(frame, "episode_new")
    # the Enter button sits on the same row, right side, ~65px below the tag
    return (1672, tag[1] + 66) if tag else None


def episodes(game, vision, log=None, limit=50, play=None, retries=0, before_retry=None):
    """`play(game, vision, log)` replaces the plain skip, for a clearer that must check what opened.
    `retries` presses a row's Enter again if Episode Info didn't open, after `before_retry(frame)` checks the screen."""
    log = log or game.log
    done = 0
    while done < limit:
        name, _, frame = _wait_for(game, vision, ["episode_list_title", "main_episode_list_title"], timeout=8)
        if not name:
            if done == 0:                # "done" with nothing read as success before
                raise RuntimeError("episodes: not on an Episode List screen")
            log("episodes: left the Episode List; stopping")
            break
        enter = _enter_point(vision, frame, name)
        if not enter:
            log(f"episodes: no New episodes left ({done} skipped)")
            break
        tag = "main_episode_new" if name == "main_episode_list_title" else "episode_new"
        if not buttons.tie(vision, frame, tag, *enter):    # the row's Enter moves with its New tag
            raise RuntimeError(f"episodes: can't tie the Enter at {enter} to its New tag in this frame")
        pos = None
        for attempt in range(retries + 1):
            if attempt:                  # the last press did nothing: look again before repeating it
                frame = game.grab()
                pos = vision.find(frame, "enter_episode")
                if pos:
                    break
                if before_retry:
                    frame = before_retry(frame)
                if not buttons.tie(vision, frame, tag, *enter):
                    break
                log("episodes: Episode Info didn't open, pressing Enter again")
            inputguard.try_click(game, *enter)
            n, pos, _ = _wait_for(game, vision, ["enter_episode"], timeout=6)
            if n:
                break
        if not pos:
            log("episodes: Episode Info dialog did not open")
            break
        for _ in range(3):
            if inputguard.try_click(game, *pos):
                break
            _, pos, _ = _wait_for(game, vision, ["enter_episode"], timeout=6)
            if not pos:
                break
        else:
            pos = None
        if not pos:
            log("episodes: Enter Episode wasn't sent")
            break
        game.sleep(5)  # loading into the story
        (play or skip)(game, vision, log)
        done += 1
        game.rt.bump("episodes skipped")
        game.sleep(2)
    return done


