"""Event Recap story completer: clear unfinished event stories, then return to the lobby.

Lobby > Event Recap > Event Story tab, then for each card with unfinished story: open it, skip its
New episodes with story.skip (Menu > >> > Confirm), take the reward screens those stories show, and go
back through Home. The game's own markers are the progress record, so a new run resumes by itself.

Nobody has captured the Event Story grid or the inside of an event yet, so `run` refuses to start until
`missing()` is empty. Those values come from the recon below, never from a guess:
  GRID, CARD_OFFSET, CARD_BOX, SCROLL_AT, SCROLL_BY, the es_tab_story_on, es_card_dot and es_card_check
  templates, and the two findings DOT_MEANS_UNFINISHED and CARD_OPENS_EPISODES.

Safety:
  - Clicks are the Event Recap button, the Event Story tab, a card, a New row's Enter, Enter Episode, the
    story skip buttons, a story reward's continue, and nav's Home. Nothing else can be reached, so the
    event shop, Claim buttons, Mobilize and Start are never pressed.
  - Every click is tied to a template matched in a fresh frame.
  - It stops, with the frame in the journal, on anything else: a card that opens another screen, an
    episode that isn't a story (a battle), a row that stays New, or a grid that doesn't respond.

Read-only recon, run through ctl so F8 stops it. It never enters a story and never claims:
  ctl run event-stories-recon --stage grid               open the tab and save it
  ctl run event-stories-recon --stage scroll:-300 --at X,Y   one drag, before and after (16:9 only)
  ctl run event-stories-recon --stage card --at X,Y      open one card, save what appears, stay there
"""
import time
from collections import namedtuple

import cv2
import numpy as np

from .. import buttons, journal, nav, viewport
from ..screens import _dimmed, label
from . import story

PACK = "eventstory"
RECAP_BUTTON = "btn__lobby__event_recap"
RECAP_TITLE = "title_event_recap"
TAB_STORY_OFF = "eventstory/es_tab_story_off"
TAB_PROGRESS_ON = "eventstory/es_tab_progress_on"
TAB_STORY_ON = "eventstory/es_tab_story_on"      # from recon
CARD_DOT = "eventstory/es_card_dot"              # from recon
CARD_CHECK = "eventstory/es_card_check"          # from recon
EPISODE_LIST = "episode_list_title"
EPISODE_NEW = "episode_new"
EPISODE_INFO = "episode_info_title"
ENTER_EPISODE = "enter_episode"
STORY_SCREENS = ("story_menu", "story_skip", "skip_question", "reward_acquired")

# Set these from the recon frames.
GRID = None                     # (x1, y1, x2, y2): the visible card area
CARD_OFFSET = None              # (dx, dy): from a card's orange dot to a safe point on the card
CARD_BOX = None                 # (dx1, dy1, dx2, dy2): the card's extent, relative to its dot
SCROLL_AT = None                # (x, y): where a scroll drag starts
SCROLL_BY = None                # how far one drag moves, in reference pixels
DOT_MEANS_UNFINISHED = None     # True once recon shows the orange dot marks unfinished story
CARD_OPENS_EPISODES = None      # True once recon shows a card opens a normal Episode List

PANEL = (330, 130, 1900, 1060)  # recon clicks and drags stay inside this
ROW_ZONE = (1560, 200, 1790, 1010)
TIE_REACH = 1100
POLL = 0.6
CLICK_POLLS = 8
LOAD_POLLS = 14
STORY_POLLS = 30
SETTLE_POLLS = 8
MAX_EVENTS = 100
MAX_EPISODES = 60               # in one event
MAX_PASSES = 300
MAX_SCROLLS = 40
MAX_VISIBLE = 24
MOVED = 1.5                     # mean grey change on the quarter-size grid that counts as movement
SAME_CARD = 0.97                # correlation at which two card prints are one card
PRINT_SIZE = (64, 40)
SAME_ROW = 2.0                  # mean grey difference under which an episode row hasn't changed
CAPTURES = journal.ROOT / "captures" / "eventstory"

Card = namedtuple("Card", "dot point checked print")
_story_open = False             # this task entered a story that hasn't finished; lets a new run resume it


class EventStoryError(nav.NavError):
    """The task met something it won't click through. `.report` holds what it finished."""


def missing(vision):
    """What recon still has to provide before `run` may click anything."""
    gaps = [n for n in (TAB_STORY_ON, CARD_DOT, CARD_CHECK) if not vision.has(n)]
    gaps += [k for k in ("GRID", "CARD_OFFSET", "CARD_BOX", "SCROLL_AT", "SCROLL_BY") if globals()[k] is None]
    return gaps + [k for k in ("DOT_MEANS_UNFINISHED", "CARD_OPENS_EPISODES") if globals()[k] is not True]


def _fail(game, frame, why):
    journal.record("log", frame=frame, msg=f"event stories stopped: {why}", task=game.rt.task)
    raise EventStoryError(f"event stories: {why} (frame in the journal)")


def _inside(point, box):
    return box[0] <= point[0] < box[2] and box[1] <= point[1] < box[3]


def _press(game, vision, proof, point, done, what, zone=None, center=None, polls=CLICK_POLLS):
    """Click `point`, tied to `proof` in a fresh frame, until `done(frame)`. One retry for a swallowed
    click, and only if the target is still there."""
    if zone and not _inside(point, zone):
        raise EventStoryError(f"event stories: {what} at {point} is outside its zone")
    frame = None
    for _ in range(2):
        frame = game.grab()
        if not buttons.tie(vision, frame, proof, *point, reach=TIE_REACH, center=center):
            _fail(game, frame, f"{what}: its target is no longer on screen")
        game.click(*point)
        for _ in range(polls):
            game.sleep(POLL)
            frame = game.grab()
            if done(frame):
                return frame
    _fail(game, frame, f"{what}: nothing changed after 2 clicks")


def _wait(game, condition, polls):
    """The first frame meeting `condition`, or None."""
    for _ in range(polls):
        frame = game.grab()
        if condition(frame):
            return frame
        game.sleep(POLL)
    return None


# ---------------------------------------------------------------- reaching the grid
def _on_recap(vision, frame):
    return bool(vision.find(frame, RECAP_TITLE)) and not _dimmed(vision, frame, RECAP_TITLE)


def on_story_tab(vision, frame):
    """Event Recap is on top with its Event Story tab selected."""
    if not _on_recap(vision, frame):
        return False
    if vision.has(TAB_STORY_ON):
        return bool(vision.find(frame, TAB_STORY_ON))
    return not vision.find(frame, TAB_STORY_OFF) and not vision.find(frame, TAB_PROGRESS_ON)


def _open_recap(game, vision, log):
    frame = game.grab()
    if _on_recap(vision, frame):
        return frame
    nav.go_lobby(game, vision, log)
    frame = game.grab()
    button = vision.find(frame, RECAP_BUTTON)
    if not button:
        _fail(game, frame, "can't see the Event Recap button on the lobby")
    return _press(game, vision, RECAP_BUTTON, button, lambda f: _on_recap(vision, f), "opening Event Recap",
                  polls=LOAD_POLLS)


def _open_story_tab(game, vision, frame):
    if on_story_tab(vision, frame):
        return frame
    tab = vision.find(frame, TAB_STORY_OFF)
    if not tab:
        _fail(game, frame, "can't find the Event Story tab")
    return _press(game, vision, TAB_STORY_OFF, tab, lambda f: on_story_tab(vision, f), "opening the Event Story tab")


def _open_grid(game, vision, log):
    return _open_story_tab(game, vision, _open_recap(game, vision, log))


# ---------------------------------------------------------------- reading and walking the grid
def _in_card(point, dot):
    dx, dy = point[0] - dot[0], point[1] - dot[1]
    return CARD_BOX[0] <= dx <= CARD_BOX[2] and CARD_BOX[1] <= dy <= CARD_BOX[3]


def _print(vision, frame, dot):
    """A small picture of the card, the same wherever the list has scrolled it."""
    x, y = dot
    box = (max(0, x + CARD_BOX[0]), max(0, y + CARD_BOX[1]), x + CARD_BOX[2], y + CARD_BOX[3])
    vision.find(frame, CARD_DOT)
    crop = viewport.crop(frame, box, vision.shift_of(frame, CARD_DOT))
    if crop.size == 0:
        return np.zeros(PRINT_SIZE[::-1], np.float32)
    grey = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    return np.float32(cv2.resize(grey, PRINT_SIZE, interpolation=cv2.INTER_AREA))


def read_cards(vision, frame):
    """Cards with an orange dot whose click point is fully on screen, top-left first."""
    dots = vision.find_all(frame, CARD_DOT, limit=MAX_VISIBLE)
    if not dots:
        return []
    checks = vision.find_all(frame, CARD_CHECK, limit=MAX_VISIBLE)
    cards = []
    for dot in dots:
        point = (dot[0] + CARD_OFFSET[0], dot[1] + CARD_OFFSET[1])
        if _inside(point, GRID):
            cards.append(Card(dot, point, any(_in_card(c, dot) for c in checks), _print(vision, frame, dot)))
    return cards


def _same(a, b):
    """Two prints show one card. Correlation, because cards share a frame and differ in their art:
    a wrong yes would silently skip an event."""
    if a.std() < 1 or b.std() < 1:      # a blank crop has no pattern to correlate
        return False
    return float(cv2.matchTemplate(a, b, cv2.TM_CCOEFF_NORMED)[0, 0]) >= SAME_CARD


def _grid_view(vision, frame):
    vision.find(frame, RECAP_TITLE)
    crop = viewport.crop(frame, GRID, vision.shift_of(frame, RECAP_TITLE))
    grey = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    return cv2.resize(grey, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA).astype(np.int16)


def _scroll(game, vision, down):
    """One drag. True if the grid moved, False at its end, None if this window shape can't drag."""
    frame = game.grab()
    view = getattr(frame, "view_info", None)
    if view is not None and not view.native:
        return None
    before = _grid_view(vision, frame)
    x, y = SCROLL_AT
    game.drag(x, y, x, y + (-SCROLL_BY if down else SCROLL_BY))
    last = None
    for _ in range(SETTLE_POLLS):
        game.sleep(POLL)
        frame = game.grab()
        if not on_story_tab(vision, frame):
            _fail(game, frame, "the grid went away during a scroll")
        now = _grid_view(vision, frame)
        if last is not None and float(np.abs(last - now).mean()) <= MOVED:
            return float(np.abs(before - now).mean()) > MOVED
        last = now
    _fail(game, frame, "the grid never settled after a scroll")


def _find_card(game, vision, dead, report):
    """The next card to open, from the top of the list, or None."""
    for _ in range(MAX_SCROLLS):
        moved = _scroll(game, vision, down=False)
        if moved is None:
            break
        if not moved:
            break
    else:
        _fail(game, game.grab(), "never reached the top of the list")
    for _ in range(MAX_SCROLLS):
        for card in read_cards(vision, game.grab()):
            if not card.checked and not any(_same(card.print, d) for d in dead):
                return card
        moved = _scroll(game, vision, down=True)
        if moved is None:
            report["cant_scroll"] = True
            return None
        if not moved:
            return None
    _fail(game, game.grab(), "never reached the end of the list")


# ---------------------------------------------------------------- one event
class _RewardWatch:
    """Passed to story.skip in place of Vision, to see whether it took a reward screen."""

    def __init__(self, vision):
        self._vision, self.claimed = vision, False

    def __getattr__(self, name):
        return getattr(self._vision, name)

    def find(self, frame, name):
        point = self._vision.find(frame, name)
        if point and name == "click_continue":
            self.claimed = True
        return point


def _finish_story(game, vision, entry, log):
    """Skip the open story and take its reward, until the Episode List is back."""
    global _story_open
    watch = _RewardWatch(vision)
    frame = None
    for _ in range(4):                   # skip() returns early if a load hides the story for a moment
        if not story.skip(game, watch, log):
            _fail(game, game.grab(), "the story skip timed out")
        for _ in range(STORY_POLLS):
            frame = game.grab()
            if vision.find(frame, EPISODE_LIST):
                _story_open = False
                entry["episodes"] += 1
                game.rt.bump("event episodes")
                if watch.claimed:
                    entry["rewards"] += 1
                    game.rt.bump("event story rewards")
                return
            if any(vision.find(frame, n) for n in STORY_SCREENS):
                break
            game.sleep(POLL)
        else:
            _story_open = False
            _fail(game, frame, "didn't come back to the Episode List after the story")
    _story_open = False
    _fail(game, frame, "the story or its reward wouldn't clear")


def _play(game, vision, entry, log):
    """From Episode Info: enter the episode. A battle or anything else that isn't a story stops here."""
    global _story_open
    frame = game.grab()
    point = vision.find(frame, ENTER_EPISODE)
    if not point or not vision.find(frame, EPISODE_INFO):
        _fail(game, frame, "Episode Info isn't showing Enter Episode")
    _story_open = True                   # a Stop from here on leaves a story for the next run to finish
    try:
        _press(game, vision, ENTER_EPISODE, point, lambda f: vision.find(f, "story_menu"),
               "entering the episode (it may be a battle, which this task doesn't play)", polls=STORY_POLLS)
    except EventStoryError:
        _story_open = False
        raise
    _finish_story(game, vision, entry, log)


def _row_print(vision, frame, tag):
    vision.find(frame, EPISODE_NEW)
    box = (max(0, tag[0] - 500), max(0, tag[1] - 30), tag[0] + 500, tag[1] + 130)
    grey = cv2.cvtColor(viewport.crop(frame, box, vision.shift_of(frame, EPISODE_NEW)), cv2.COLOR_BGR2GRAY)
    return cv2.resize(grey, (64, 16), interpolation=cv2.INTER_AREA).astype(np.int16)


def _clear_list(game, vision, entry, log):
    """Skip each New episode on the open Episode List, top first."""
    last = None
    for _ in range(MAX_EPISODES):
        frame = _wait(game, lambda f: vision.find(f, EPISODE_LIST), LOAD_POLLS)
        if frame is None:
            _fail(game, game.grab(), "the Episode List didn't come back")
        enter = story._enter_point(vision, frame, EPISODE_LIST)
        if not enter:
            return
        tag = vision.find(frame, EPISODE_NEW)
        row = _row_print(vision, frame, tag)
        if last is not None and last[0] == tag and float(np.abs(last[1] - row).mean()) < SAME_ROW:
            _fail(game, frame, "an episode stayed New after its skip")
        last = (tag, row)
        _press(game, vision, EPISODE_NEW, enter,
               lambda f: vision.find(f, ENTER_EPISODE) and vision.find(f, EPISODE_INFO),
               "opening an episode", zone=ROW_ZONE, center=tag)
        _play(game, vision, entry, log)
    _fail(game, game.grab(), f"more than {MAX_EPISODES} episodes in one event")


def _open_card(game, vision, card):
    _press(game, vision, CARD_DOT, card.point, lambda f: not on_story_tab(vision, f), "opening a card",
           zone=GRID, center=card.dot, polls=LOAD_POLLS)
    if _wait(game, lambda f: vision.find(f, EPISODE_LIST), LOAD_POLLS) is None:
        _fail(game, game.grab(), "this card opened something other than an Episode List")


def _resume(game, vision, report, log):
    """Finish a story an earlier Stop left open. A story this task didn't start is not ours to skip."""
    global _story_open
    frame = game.grab()
    if any(vision.find(frame, n) for n in STORY_SCREENS):
        if not _story_open:
            raise EventStoryError("event stories: a story is already open and this task didn't start it. "
                                  "Finish it, or run `ctl run skip`, then start again")
        log("event stories: finishing the story from the last run")
        entry = {"event": "resumed story", "episodes": 0, "rewards": 0, "result": "resumed"}
        report["events"].append(entry)
        _finish_story(game, vision, entry, log)
    elif label(vision, frame) in ("lobby", "event_recap", "episode_list"):
        _story_open = False


def _summary(report):
    rows = "; ".join(f"{e['event']}: {e['episodes']} episodes, {e['rewards']} rewards ({e['result']})"
                     for e in report["events"])
    return (f"event stories: {report['outcome']}, {sum(e['episodes'] for e in report['events'])} episodes, "
            f"{sum(e['rewards'] for e in report['events'])} rewards" + (f" | {rows}" if rows else ""))


def run(game, vision, limit=None, log=None):
    """Clear unfinished events (at most `limit`). Returns a report with per-event counts."""
    log = log or game.log
    vision.load_pack(PACK)
    gaps = missing(vision)
    if gaps:
        raise EventStoryError("event stories: not ready, recon still has to provide: " + ", ".join(gaps) +
                              "")
    game.rt.task = "Event stories"
    report = {"outcome": "stopped", "events": [], "cant_scroll": False}
    dead, known = [], []                 # cards with nothing to skip; (print, entry) for cards done this run
    try:
        _resume(game, vision, report, log)
        for _ in range(MAX_PASSES):
            cleared = sum(1 for _, e in known if e["episodes"])
            if limit and cleared >= limit:
                report["outcome"] = f"reached the limit of {limit}"
                break
            _open_grid(game, vision, log)
            card = _find_card(game, vision, dead, report)
            if card is None:
                report["outcome"] = ("partial: this window shape can't scroll, so cards further down weren't reached"
                                     if report["cant_scroll"] else "finished")
                break
            entry = next((e for p, e in known if _same(p, card.print)), None)
            if entry is None:
                entry = {"event": f"event {len(known) + 1}", "episodes": 0, "rewards": 0, "result": "nothing new"}
                known.append((card.print, entry))
                report["events"].append(entry)
            log(f"event stories: opening {entry['event']}")
            before = entry["episodes"]
            _open_card(game, vision, card)
            _clear_list(game, vision, entry, log)
            if entry["episodes"] == before:
                dead.append(card.print)          # no New rows: don't reopen it this run
            else:
                entry["result"] = "cleared"
                game.rt.bump("events cleared")
            log(f"event stories: {entry['event']} has {entry['episodes']} episodes, {entry['rewards']} rewards")
            nav.go_lobby(game, vision, log)
        else:
            _fail(game, game.grab(), f"still finding work after {MAX_PASSES} passes")
        nav.go_lobby(game, vision, log)
        return report
    except RuntimeError as e:            # includes EventStoryError, NavError and Game's input refusals
        e.report = report
        raise
    finally:
        log(_summary(report))
        game.rt.task = "idle"


# ---------------------------------------------------------------- read-only recon
def parse_recon(stages, at):
    """(stage, point, drag) from ctl's --stage and --at. Raises ValueError on a bad request."""
    stages = stages or ["grid"]
    if len(stages) != 1:
        raise ValueError("recon takes one --stage: grid, scroll:DY or card")
    name, _, amount = str(stages[0]).partition(":")
    if name not in ("grid", "scroll", "card"):
        raise ValueError("--stage must be grid, scroll:DY or card")
    if name == "grid":
        return name, None, None
    try:
        x, y = (int(v) for v in str(at).split(","))
    except ValueError:
        raise ValueError(f"{name} needs --at X,Y") from None
    if not _inside((x, y), PANEL):
        raise ValueError(f"--at must be inside the event panel {PANEL}")
    if name == "card":
        return name, (x, y), None
    try:
        by = int(amount)
    except ValueError:
        raise ValueError("scroll needs a distance, like --stage scroll:-300") from None
    if not 40 <= abs(by) <= 600 or not _inside((x, y + by), PANEL):
        raise ValueError("the scroll must be 40-600 px and stay inside the event panel")
    return name, (x, y), by


def _save(frame, folder, name, result):
    path = folder / f"{name}.png"
    cv2.imwrite(str(path), np.asarray(frame))
    result["frames"].append(str(path))
    canvas = getattr(frame, "canvas", None)
    if canvas is not None:               # the true-position picture, for a window that isn't 16:9
        cv2.imwrite(str(folder / f"{name}_canvas.png"), canvas)


def _need_story_tab(game, vision):
    frame = game.grab()
    if not on_story_tab(vision, frame):
        raise EventStoryError("recon: not on Event Recap's Event Story tab; run --stage grid first. Nothing clicked")
    return frame


def _panel_grey(frame):
    pixels = np.asarray(frame)[PANEL[1]:PANEL[3], PANEL[0]:PANEL[2]]
    return np.float32(cv2.cvtColor(pixels, cv2.COLOR_BGR2GRAY))


def _recon_card(game, vision, frame, point, folder, result):
    """Open one card and photograph what appears. Nothing inside it is clicked."""
    view = getattr(frame, "view_info", None)
    if view is not None and not view.native:
        dots = vision.find_all(frame, CARD_DOT, limit=MAX_VISIBLE) if vision.has(CARD_DOT) else []
        near = [d for d in dots if abs(d[0] - point[0]) <= 300 and abs(d[1] - point[1]) <= 300]
        if not near or not buttons.tie(vision, frame, CARD_DOT, *point, reach=TIE_REACH, center=near[0]):
            raise EventStoryError("recon: outside 16:9 a card click must be tied to its dot, and no es_card_dot "
                                  "is matched near that point. Nothing clicked")
    _save(frame, folder, "card_before", result)
    game.click(*point)
    result["clicked"] = list(point)
    for i, wait in enumerate((1.0, 1.5, 2.0, 2.5), 1):
        game.sleep(wait)
        frame = game.grab()
        _save(frame, folder, f"card_after_{i}", result)
    result["screen"] = label(vision, frame)
    result["episode_list"] = bool(vision.find(frame, EPISODE_LIST))
    result["new_rows"] = len(vision.find_all(frame, EPISODE_NEW)) if result["episode_list"] else 0
    result["story_open"] = bool(vision.find(frame, "story_menu"))
    result["next"] = ("a story opened: don't skip it for recon, tell ba-eventstory" if result["story_open"]
                      else "look at the frames, then leave with `ctl run lobby`")


def _recon_scroll(game, vision, frame, point, by, folder, result):
    view = getattr(frame, "view_info", None)
    if view is not None and not view.native:
        raise EventStoryError("recon: Game.drag is refused outside 16:9, so scrolling can't be tested at this "
                              "window shape. Don't resize the game for this. Nothing clicked")
    _save(frame, folder, "scroll_before", result)
    before = _panel_grey(frame)
    game.drag(point[0], point[1], point[0], point[1] + by)
    game.sleep(1.5)
    after_frame = game.grab()
    _save(after_frame, folder, "scroll_after", result)
    after = _panel_grey(after_frame)
    (dx, dy), peak = cv2.phaseCorrelate(before, after)
    result.update(changed=float(np.abs(before - after).mean()) > MOVED, moved_px=[round(dx, 1), round(dy, 1)],
                  match_quality=round(float(peak), 3), screen=label(vision, after_frame),
                  still_on_tab=on_story_tab(vision, after_frame))


def recon(game, vision, stage="grid", point=None, by=None, log=None):
    """Save what a stage shows. Clicks only navigation, one card, or one drag; never a story or a claim."""
    log = log or game.log
    vision.load_pack(PACK)
    game.rt.task = "Event story recon"
    folder = CAPTURES / f"{time.strftime('%H%M%S')}_{stage}"
    folder.mkdir(parents=True, exist_ok=True)
    result = {"stage": stage, "folder": str(folder), "frames": []}
    try:
        if stage == "grid":
            frame = _open_recap(game, vision, log)
            _save(frame, folder, "recap", result)
            frame = _open_story_tab(game, vision, frame)
            game.sleep(1.0)              # let the cards finish drawing
            frame = game.grab()
            _save(frame, folder, "story_tab", result)
            view = getattr(frame, "view_info", None)
            result["window"] = view.info() if view is not None else None
            if vision.has(CARD_DOT):
                result["dots"] = len(vision.find_all(frame, CARD_DOT, limit=MAX_VISIBLE))
            if vision.has(CARD_CHECK):
                result["checks"] = len(vision.find_all(frame, CARD_CHECK, limit=MAX_VISIBLE))
        elif stage == "scroll":
            _recon_scroll(game, vision, _need_story_tab(game, vision), point, by, folder, result)
        elif stage == "card":
            _recon_card(game, vision, _need_story_tab(game, vision), point, folder, result)
        else:
            raise EventStoryError(f"recon: unknown stage {stage!r}")
        log(f"event story recon: {stage} saved to {folder}")
        return result
    finally:
        game.rt.task = "idle"
