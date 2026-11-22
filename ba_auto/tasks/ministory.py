"""Clear every Mini Story or Group Story chapter tagged "New".

Works from the lobby, Campaign, Story hub or the chapter list. For each
chapter with a New tag it opens the chapter, skips every New episode (see
story.episodes), goes back, and moves on, paging right until the last page.

The game reopens the list on whichever page it was last showing, which is not
always the first one, so a page is always reached by counting from page 1.

Group Story is strict because no one has opened one of its chapters yet. It
stops, saves the frame and presses nothing more on a battle, a popup, or any
screen that isn't on its route or a plain story skip or reward.
"""
import time
from dataclasses import dataclass

import cv2
import numpy as np

from .. import buttons, inputguard, journal, reconnect
from ..nav import top_popup
from ..screens import detect, label
from . import story

HOME = (1855, 37)
BACK = (85, 55)
CAMPAIGN_STORY_CARD = (1740, 300)
CARD_OFFSET = (400, 100)          # from a chapter's New tag to the middle of its card
CARDS_AREA = (110, 200, 1830, 960)
MAX_PAGES = 30
MAX_CHAPTERS = 200
PLAY_POLLS = 90                   # half-second looks for a story to open and finish
ROUTE_RETRIES = 4                 # a frame mid-transition can look unknown for a moment
OPEN_RETRIES = 2                  # extra presses of a row's Enter when Episode Info doesn't open
SETTLE_LOOKS = 8                  # looks for a reopened list to finish sliding to its last page
INFO_X = (1372, 241)              # Episode Info's X, to check the match before pressing it
INFO_CLOSE_TRIES = 3
FADE_LOOKS = 3                    # one-second looks before a dim screen counts as a popup
STOPS = journal.ROOT / "captures" / "group_story_stops"
# Group Story never presses anything on these.
BATTLE = {"formation", "alliance_formation", "mobilize_unit", "battle_tip", "battle_result", "defeat",
          "occupy_dialog", "conquest_map", "in_battle"}


@dataclass(frozen=True)
class Kind:
    """What differs between the story lists this clearer walks."""
    title: str                    # the list's title template
    hub_card: str                 # what to click on the Story hub
    task: str
    word: str                     # for the log
    name: str
    episode_lists: tuple = ("episode_list_title",)
    strict: bool = False          # stop on anything that isn't a plain story skip or reward


MINI = Kind("ministory_title", "story_hub_mini", "Clear all mini stories", "mini stories", "Mini Story")
GROUP = Kind("title_group_story", "story_hub_group", "Clear all group stories", "group stories", "Group Story",
             ("episode_list_title", "main_episode_list_title"), strict=True)


class StoryStop(RuntimeError):
    """A strict clearer met something it won't touch. The message says what, and where the frame is."""


def _refuse(game, log, kind, why, frame=None):
    """Save what is on screen, say why, and end the run without pressing anything else."""
    frame = game.grab() if frame is None else frame
    path = STOPS / f"{time.strftime('%Y%m%d_%H%M%S')}.png"
    try:
        STOPS.mkdir(parents=True, exist_ok=True)
        saved = cv2.imwrite(str(path), np.asarray(frame))
    except (OSError, cv2.error):
        saved = False
    message = f"{kind.word}: stopped on {why}. Frame: {path if saved else 'could not be saved'}"
    log(message)
    raise StoryStop(message)


def _hazard(vision, frame):
    """Why a strict clearer must not touch this frame, or None."""
    here = detect(vision, frame)
    if here in BATTLE:
        return f"a battle screen ({here})"
    popup = top_popup(vision, frame)
    if popup:
        return f"a popup ({popup[0]})"
    named = label(vision, frame)
    return f"an unfamiliar popup over {named[:-6]}" if named.endswith("+popup") else None


def _guard(game, vision, log, kind, frame):
    if reconnect.answer(game, frame, log):
        frame = game.grab()
    return _hazard_check(game, vision, log, kind, frame)


def _hazard_check(game, vision, log, kind, frame):
    """The frame to act on, or a stop. A fade between screens dims them like a popup, so a dim
    screen only counts once it has stayed dim. Battles and known popups stop at once."""
    why = _hazard(vision, frame)
    for _ in range(FADE_LOOKS):
        if not why or not why.startswith("an unfamiliar popup"):
            break
        game.sleep(1.0)
        frame = game.grab()
        why = _hazard(vision, frame)
    if why:
        _refuse(game, log, kind, why, frame)
    return frame


def _on_episodes(vision, frame, kind):
    return any(vision.find(frame, n) for n in kind.episode_lists)


def _play_plain(game, vision, log, kind):
    """After Enter Episode: skip the story and take its reward, ending on the Episode List.
    A battle, a popup, or anything that never becomes a story stops the run."""
    seen = False
    for _ in range(PLAY_POLLS):
        frame = _guard(game, vision, log, kind, game.grab())
        if vision.find(frame, "reward_acquired"):
            if not buttons.tie(vision, frame, "click_continue", 960, 945):
                _refuse(game, log, kind, "a reward screen without its plain continue", frame)
            if inputguard.try_click(game, 960, 945):
                seen = True
                game.sleep(3)
        elif any(vision.find(frame, n) for n in ("skip_question", "story_skip", "story_menu")):
            story.skip(game, vision, log)
            seen = True
        elif seen and _on_episodes(vision, frame, kind):
            return
        else:
            game.sleep(0.5)
    _refuse(game, log, kind, "an episode that never became a plain story")


def _cards(frame):
    x1, y1, x2, y2 = CARDS_AREA
    return frame[y1:y2, x1:x2]


def _turn(game, vision, arrow_name):
    """Click a page arrow and wait for the cards to change. False if they never do."""
    frame = game.grab()
    arrow = vision.find(frame, arrow_name)
    if not arrow:
        return False
    before = _cards(frame)
    for _ in range(3):                # screens can swallow the first click after loading
        inputguard.try_click(game, *arrow)
        for _ in range(8):
            game.sleep(0.5)
            if cv2.absdiff(before, _cards(game.grab())).mean() > 4:
                game.sleep(1.0)       # let the page animation finish
                return True
    return False


def _settled(game, frame):
    """The game reopens a list on its last page and slides there, so the first look can be the wrong page."""
    before = _cards(frame)
    for _ in range(SETTLE_LOOKS):
        game.sleep(0.4)
        now = _cards(game.grab())
        if cv2.absdiff(before, now).mean() <= 1:
            return
        before = now


def goto_page(game, vision, log, page, kind=MINI):
    """Put the list on `page` (0 is the first page). True on success."""
    for _ in range(MAX_PAGES):
        if not vision.find(game.grab(), "prev_page"):
            break
        if not _turn(game, vision, "prev_page"):
            log(f"{kind.word}: could not turn back a page")
            return False
    else:
        log(f"{kind.word}: could not reach the first page")
        return False
    for i in range(page):
        if not _turn(game, vision, "next_page"):
            log(f"{kind.word}: could not reach page {i + 2}")
            return False
    return True


def _press(game, vision, frame, log, scope, name, point, kind=MINI):
    """Click a known button only if it's proven on this frame. False stops the walk."""
    if not buttons.prove(vision, frame, scope, name, *point):
        log(f"{kind.word}: can't confirm {scope}/{name} on this screen; stopping")
        return False
    inputguard.try_click(game, *point)  # the caller's loop reads the screen again either way
    return True


def _close_episode_info(game, vision, log, kind, frame):
    """A leftover Episode Info: press its X. Its Enter is never pressed here."""
    x = vision.find(frame, "episode_info_close")
    if not x or abs(x[0] - INFO_X[0]) > 30 or abs(x[1] - INFO_X[1]) > 30:
        _refuse(game, log, kind, "an Episode Info dialog whose X can't be confirmed", frame)
    inputguard.try_click(game, *x)


def goto_list(game, vision, log, page=0, kind=MINI):
    """Navigate to this kind's chapter list, on the given page. True on success.
    A strict kind only follows the known route: it never skips a story it didn't open or goes Home."""
    misses = closes = 0
    for _ in range(12):
        f = game.grab()
        if kind.strict:
            f = _guard(game, vision, log, kind, f)
            if vision.find(f, "episode_info_title"):
                closes += 1
                if closes > INFO_CLOSE_TRIES:
                    _refuse(game, log, kind, "an Episode Info dialog that won't close", f)
                log(f"{kind.word}: closing a leftover Episode Info")
                _close_episode_info(game, vision, log, kind, f)
                game.sleep(1.5)
                continue
        if vision.find(f, kind.title) and not (kind.strict and _on_episodes(vision, f, kind)):
            if kind.strict:
                _settled(game, f)
            return goto_page(game, vision, log, page, kind)
        if _on_episodes(vision, f, kind):
            if not _press(game, vision, f, log, "top", "back", BACK, kind):
                return False
        elif vision.find(f, "story_hub_mini"):
            p = vision.find(f, kind.hub_card)
            if not p:
                log(f"{kind.word}: can't see the {kind.name} card on the Story hub; stopping")
                return False
            inputguard.try_click(game, *p)
        elif vision.find(f, "campaign_title"):
            if not _press(game, vision, f, log, "campaign", "story", CAMPAIGN_STORY_CARD, kind):
                return False
        elif (p := vision.find(f, "lobby_campaign")):
            inputguard.try_click(game, *p)
        elif kind.strict:
            misses += 1
            if misses > ROUTE_RETRIES:
                _refuse(game, log, kind, f"a screen off the route to {kind.name} ({label(vision, f)})", f)
            game.sleep(1.5)
            continue
        elif vision.find(f, "story_menu") or vision.find(f, "skip_question"):
            story.skip(game, vision, log)
        elif vision.find(f, "reward_acquired"):
            if not buttons.tie(vision, f, "click_continue", 960, 945):
                log(f"{kind.word}: can't confirm the reward's continue; stopping")
                return False
            inputguard.try_click(game, 960, 945)
        elif not _press(game, vision, f, log, "top", "home", HOME, kind):    # unknown screen: go home
            return False
        game.sleep(3)
    log(f"{kind.word}: could not reach the {kind.name} list")
    return False


def _open_chapter(game, vision, tag, kind=MINI, log=None):
    """Click a chapter card until the list goes away. False if it never opens."""
    x, y = tag[0] + CARD_OFFSET[0], tag[1] + CARD_OFFSET[1]
    for _ in range(3):                # screens can swallow the first click after loading
        if not buttons.tie(vision, game.grab(), "chapter_new", x, y, center=tag):
            return False
        inputguard.try_click(game, x, y)
        game.sleep(2.5)
        f = game.grab()
        if kind.strict:
            f = _guard(game, vision, log or game.log, kind, f)
            if _on_episodes(vision, f, kind):
                return True
        if not vision.find(f, kind.title):
            return True
    return False


def _skip_chapter(game, vision, log, kind):
    if not kind.strict:
        return story.episodes(game, vision, log)
    if not story._wait_for(game, vision, list(kind.episode_lists), timeout=8)[0]:
        _refuse(game, log, kind, "a chapter that didn't open an Episode List")
    _guard(game, vision, log, kind, game.grab())
    return story.episodes(game, vision, log, play=lambda g, v, l: _play_plain(g, v, l, kind), retries=OPEN_RETRIES,
                          before_retry=lambda f: _before_retry(game, vision, log, kind, f))


def _before_retry(game, vision, log, kind, frame):
    """Enter is only pressed again on the plain list: never behind a dialog that has no Enter Episode."""
    frame = _guard(game, vision, log, kind, frame)
    if vision.find(frame, "episode_info_title"):
        _refuse(game, log, kind, "an Episode Info dialog with no plain Enter Episode", frame)
    return frame


def _still_new(game, vision, kind):
    """True if the open Episode List still has a New episode."""
    name, _, frame = story._wait_for(game, vision, list(kind.episode_lists), timeout=4)
    return bool(name) and story._enter_point(vision, frame, name) is not None


def _walk(game, vision, log, kind, limit):
    """(chapters, episodes, outcome, chapters left unfinished)"""
    game.rt.task = kind.task
    page, chapters, episodes, unfinished = 0, 0, 0, 0
    outcome = "finished"
    dead = set()                    # (page, tag position) that yielded nothing - don't retry
    if not goto_list(game, vision, log, kind=kind):
        if kind.strict:
            _refuse(game, log, kind, f"a screen it couldn't reach the {kind.name} list from")
        return 0, 0, "not reached", 0
    while True:
        f = game.grab()
        tags = [t for t in vision.find_all(f, "chapter_new")
                if (page, t[0] // 50, t[1] // 50) not in dead]
        if tags:
            tag = tags[0]
            opened = _open_chapter(game, vision, tag, kind, log)
            n = _skip_chapter(game, vision, log, kind) if opened else 0
            if kind.strict and (not opened or _still_new(game, vision, kind)):
                dead.add((page, tag[0] // 50, tag[1] // 50))
                unfinished += 1
                episodes += n
                log(f"{kind.word}: chapter at page {page + 1} is unfinished after {n} episodes; leaving it New")
            elif n == 0:
                dead.add((page, tag[0] // 50, tag[1] // 50))
                log(f"{kind.word}: chapter at page {page + 1} had nothing to skip, skipping it")
            else:
                chapters += 1
                episodes += n
                game.rt.bump("chapters cleared")
                log(f"{kind.word}: chapter done ({n} episodes) - {chapters} chapters so far")
            if not goto_list(game, vision, log, page, kind):
                outcome = "stopped early"
                break
            if limit is not None and chapters + unfinished >= limit:
                outcome = "reached the limit"
                break
            continue
        if not vision.find(f, "next_page"):
            break
        if not _turn(game, vision, "next_page"):
            log(f"{kind.word}: could not reach page {page + 2}")
            outcome = "stopped early"
            break
        page += 1
        log(f"{kind.word}: page {page + 1}")
    log(f"{kind.word}: {outcome} - {chapters} chapters, {episodes} episodes"
        + (f", {unfinished} unfinished" if unfinished else ""))
    if kind.strict and outcome == "stopped early":
        _refuse(game, log, kind, f"a list it couldn't keep walking, after {chapters} chapters")
    game.rt.task = "idle"
    return chapters, episodes, outcome, unfinished


def _clear(game, vision, log, kind, limit=None):
    log = log or game.log
    try:
        return _walk(game, vision, log, kind, limit)
    except StoryStop:
        game.rt.task = "idle"
        raise


def clear_all(game, vision, log=None):
    return _clear(game, vision, log, MINI)[1]


def clear_group(game, vision, log=None, limit=None):
    """Clear Group Story chapters tagged New; `limit` is how many chapters. Raises StoryStop on
    anything that isn't a plain story skip or reward."""
    if limit is not None and not 1 <= limit <= MAX_CHAPTERS:
        raise ValueError(f"limit must be 1-{MAX_CHAPTERS}")
    chapters, episodes, outcome, unfinished = _clear(game, vision, log, GROUP, limit)
    result = {"chapters": chapters, "episodes": episodes, "outcome": outcome}
    if unfinished:
        result["unfinished"] = unfinished
    return result
