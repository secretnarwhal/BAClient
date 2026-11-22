"""Auto-MomoTalk: answer every student with unread messages and skip the stories they unlock.

Lobby > MomoTalk > Messages tab, then for each unread row, a small state machine (see classify):

  waiting for messages <-> more messages (typing) -> reply needed -> waiting ...
  waiting -> relationship event offered -> entering it -> story skip -> reward -> back in MomoTalk

A conversation can have any number of replies, or none. It counts as finished only after showing no
reply, no event and no typing for SETTLE_SECONDS (set from a real recording, see `analyze`) and after
that student's row has lost its notification badge.

How the game behaves: a student's texting pauses when you switch away, so the task
stays on one student until they're finished and never clicks another row meanwhile. A badge always
marks a student with something to send or read, and no badge means nothing, so badges are the queue.

Safety:
  - Every click names a kind and must land inside that kind's zone (ZONES). Nothing else is clickable,
    so gifts, shops and anything outside MomoTalk can't be reached by this task.
  - Every click is tied to a template matched in a fresh frame, so it works at any window size.
  - It stops, with the frame in the journal, on anything it doesn't recognise: another popup, a battle,
    a click that changes nothing, a story that doesn't come back, or an unread count that doesn't drop.
  - Templates are the `momotalk` pack (tools/cut_pack.py, vision.load_pack). Until the conversation
    templates, REPLY_OFFSET and SETTLE_SECONDS exist, the task refuses to start, before any click.

Recording, for the player. `record` and `analyze` send no input:
  python -m ba_auto.tasks.momotalk record DIR [--seconds N]
  python -m ba_auto.tasks.momotalk open|tab DIR
  python -m ba_auto.tasks.momotalk click|enter X Y DIR [--note TEXT]
  python -m ba_auto.tasks.momotalk shot DIR [--note TEXT]
  python -m ba_auto.tasks.momotalk record-stop DIR
  python -m ba_auto.tasks.momotalk analyze DIR
  python -m ba_auto.tasks.momotalk fit-regions
  python -m ba_auto.tasks.momotalk set-values --reply-offset DX,DY --settle-from DIR
"""
import argparse
import json
import math
import re
import time
from pathlib import Path

import cv2
import numpy as np

from .. import buttons, journal, nav, ocr, viewport
from ..screens import _dimmed
from ..vision import TEMPLATE_DIR
from . import story

PACK = "momotalk"
VALUES = TEMPLATE_DIR / PACK / "values.json"   # reply_offset and settle_seconds, set from a recording
PACK_INDEX = TEMPLATE_DIR / PACK / "index.json"
TITLE = "nav_momotalk"
LOBBY_BUTTON = "btn__lobby__momotalk"    # shared, cut from native frames
MESSAGES_TAB = "momotalk/mt_tab_messages_off"
UNREAD_TITLE = "momotalk/mt_unread_title"
SORT_UNREAD = "momotalk/mt_sort_unread"
UNREAD_BADGE = "momotalk/mt_unread_badge"
REPLY_HEADER = "momotalk/mt_reply_header"
STORY_PROMPT = "momotalk/mt_story_prompt"
TYPING = "momotalk/mt_typing"            # the indicator that a student is typing
STORY_ENTER = "momotalk/mt_story_enter"  # optional: a popup between the prompt and the story
BASE = (LOBBY_BUTTON, MESSAGES_TAB, UNREAD_TITLE, SORT_UNREAD)
RECON = (UNREAD_BADGE, TYPING, REPLY_HEADER, STORY_PROMPT)
REPLY_OFFSET = None                      # (dx, dy) from the reply header to its top option; see set_values
SETTLE_SECONDS = None                    # no reply, event or typing for this long = finished; see set_values

# Conversation states
WAITING, MORE, REPLY, EVENT = "waiting", "more_messages", "reply_needed", "event_offered"
ENTERING, STORY, REWARD, UNKNOWN = "entering_event", "story", "reward", "unknown"
CHAT = (WAITING, MORE, REPLY, EVENT)

# Popup coordinates at native 16:9. The ui-map shots were a 1053-high window stretched to 1080.
LIST = (323, 300, 1024, 940)
PANEL = (1024, 225, 1726, 940)
ZONES = {
    "open": (150, 160, 260, 280),
    "tab": (192, 345, 322, 478),
    "row": LIST,
    "reply": PANEL,
    "story": PANEL,
    "story_enter": (400, 200, 1520, 900),
}
SEARCH_AREAS = {UNREAD_BADGE: (700, 300, 1024, 940), REPLY_HEADER: PANEL, STORY_PROMPT: PANEL, TYPING: PANEL,
                STORY_ENTER: ZONES["story_enter"]}
PANEL_FROM_TITLE = (664, 48, 1354, 762)  # the conversation, inside the popup's edges, from the title's centre
LIST_FROM_TITLE = (-43, 47, 658, 762)    # the student list with its unread count, from the title's centre
COUNT_FROM_TITLE = (-103, -26, 167, 24)  # "Unread Messages (N)", from that label's centre
ROW_X = 660
NAME_BOX = (450, -45, 920, 5)       # relative to the row's centre y
UNREAD_NAME = "(unread name)"
AVATAR_BOX = (365, -30, 425, 30)    # the portrait's middle, relative to the row's centre y
AVATAR_SLACK = 12                   # a re-sorted row can sit a few pixels away
AVATAR_MATCH = 0.9
POPUP_REACH = 900                   # the popup's title to its lowest row

POLL = 0.6
CLICK_POLLS = 8                     # how long a click gets to show an effect
STORY_POLLS = 30                    # loading into a story, or back out of it
ODD_POLLS = 3                       # frames off MomoTalk in a row before it's more than a transition
TYPING_POLLS = 200                  # a typing bubble that lasts this long is stuck
MAX_POLLS = 1500                    # per conversation
MAX_REPLIES = 40                    # per conversation
MAX_STUDENTS = 200
BADGE_ROUNDS = 3                    # settle periods a quiet student may keep their badge before it's a stop
MAX_VISITS = 3                      # times one student's badge may send the task back to them in a run
AGREE_POLLS = 4                     # reads for the unread count and the badges to agree
PANEL_DIFF = 0.4                    # mean grey change on the quarter-size panel that counts as new content
MOVED_PX = 12

RECORD_PERIOD = 0.25                # the recorder's steady sample rate
RECORD_SECONDS = 600
RECORD_MAX_SECONDS = 1800
RECORD_MAX_FRAMES = 700             # images; the timeline keeps going past this
RECORD_MINOR_EVERY = 1.0            # small changes (typing dots) are saved at most this often
RECORD_HEARTBEAT = 5.0
MINOR_CELLS = 4                     # quarter-size cells changed by more than MINOR_LEVEL = a small change
MINOR_LEVEL = 20
MIN_SETTLE = 20                     # never trust a shorter wait, whatever one recording showed


class MomoTalkError(nav.NavError):
    """The task met something it won't click through. `.report` holds what it finished."""


def load_values():
    """Read the values measured from a recording. Without the file the task stays not ready."""
    global REPLY_OFFSET, SETTLE_SECONDS
    try:
        data = json.loads(VALUES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if data.get("reply_offset"):
        REPLY_OFFSET = tuple(int(v) for v in data["reply_offset"])
    if data.get("settle_seconds"):
        SETTLE_SECONDS = max(MIN_SETTLE, int(data["settle_seconds"]))


def set_values(reply_offset=None, settle_seconds=None):
    """Save measured values next to the templates, and use them from now on."""
    try:
        data = json.loads(VALUES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if reply_offset is not None:
        data["reply_offset"] = [int(v) for v in reply_offset]
    if settle_seconds is not None:
        if settle_seconds < MIN_SETTLE:
            raise MomoTalkError(f"the finish wait can't be under {MIN_SETTLE} s")
        data["settle_seconds"] = int(settle_seconds)
    VALUES.parent.mkdir(parents=True, exist_ok=True)
    tmp = VALUES.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(VALUES)
    load_values()
    return data


def fit_regions():
    """Give freshly cut conversation templates their full search areas: a badge can be on any row, and
    a bubble anywhere in the chat, while cut_pack only pads around the one place it was cut from."""
    meta = json.loads(PACK_INDEX.read_text(encoding="utf-8"))
    changed = []
    for full, box in SEARCH_AREAS.items():
        name = full.split("/")[1]
        if name in meta and meta[name]["region"] != list(box):
            meta[name]["region"] = list(box)
            changed.append(name)
    tmp = PACK_INDEX.with_suffix(".fit.tmp")
    tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    tmp.replace(PACK_INDEX)
    return changed


def missing(vision):
    """What still has to be cut or measured before the task may run."""
    gaps = [n for n in BASE + RECON if not vision.has(n)]
    return gaps + [n for n, v in (("REPLY_OFFSET", REPLY_OFFSET), ("SETTLE_SECONDS", SETTLE_SECONDS)) if not v]


class _RewardWatch:
    """Passed to story.skip in place of Vision, to see whether it claimed a reward."""

    def __init__(self, vision, game):
        self._vision, self._game, self.claimed = vision, game, False

    def __getattr__(self, name):
        return getattr(self._vision, name)

    def find(self, frame, name):
        p = self._vision.find(frame, name)
        if p and name == "click_continue" and not self.claimed:
            self.claimed = True
            journal.record("log", frame=frame, msg="momotalk: story reward", task=self._game.rt.task)
        return p


def _fail(game, frame, why):
    journal.record("log", frame=frame, msg=f"momotalk stopped: {why}", task=game.rt.task)
    raise MomoTalkError(f"momotalk: {why} (frame in the journal)")


def _click(game, kind, point):
    x1, y1, x2, y2 = ZONES[kind]
    if not (x1 <= point[0] < x2 and y1 <= point[1] < y2):
        raise MomoTalkError(f"momotalk: {kind} click at {point} is outside its zone")
    game.click(*point)


def _press(game, vision, kind, point, proof, done, what, polls=CLICK_POLLS, retries=1, still=None):
    """Click `point`, tied to template `proof` in a fresh frame, and wait for `done(frame)`.
    A first click after focus can be swallowed, so one retry, only if the target is still there."""
    frame = None
    for _ in range(retries + 1):
        frame = game.grab()
        if not buttons.tie(vision, frame, proof, *point, reach=POPUP_REACH) or (still and not still(frame)):
            _fail(game, frame, f"{what}: its target is no longer on screen")
        _click(game, kind, point)
        for _ in range(polls):
            game.sleep(POLL)
            frame = game.grab()
            if done(frame):
                return frame
    _fail(game, frame, f"{what}: nothing changed after {retries + 1} click(s)")


def _in_momotalk(vision, frame):
    popup = nav.top_popup(vision, frame)
    return bool(popup and popup[0] == "MomoTalk")


def classify(vision, frame, story_done=False):
    """Which conversation state this frame shows. A prompt left in the history after its story is ignored."""
    if vision.find(frame, "reward_acquired"):
        return REWARD
    if vision.find(frame, "skip_question") or vision.find(frame, "story_menu"):
        return STORY
    if vision.find(frame, STORY_ENTER):
        return ENTERING
    if not _in_momotalk(vision, frame):
        return UNKNOWN
    if vision.find(frame, REPLY_HEADER):
        return REPLY
    if not story_done and vision.find(frame, STORY_PROMPT):
        return EVENT
    if vision.find(frame, TYPING):
        return MORE
    return WAITING


def _to_popup(vision, frame, name, point):
    """`point`, matched for `name`, in the popup's own reference position. A template with a roomy
    search region can match at the wrong anchor offset at other window shapes; the title's is reliable."""
    vision.find(frame, TITLE)
    found, title = vision.shift_of(frame, name), vision.shift_of(frame, TITLE)
    return point[0] + found[0] - title[0], point[1] + found[1] - title[1]


def _locate(vision, frame, name):
    """Where `name` is inside the popup, or None."""
    point = vision.find(frame, name)
    return _to_popup(vision, frame, name, point) if point else None


def _badges(vision, frame):
    """Unread rows, top first, in popup coordinates."""
    if not vision.find(frame, UNREAD_BADGE):
        return []
    return [_to_popup(vision, frame, UNREAD_BADGE, p) for p in vision.find_all(frame, UNREAD_BADGE)]


def _panel(vision, frame):
    """A small grey picture of the conversation, or of the whole frame when MomoTalk isn't showing."""
    title = vision.find(frame, TITLE)
    if title:
        x1, y1, x2, y2 = PANEL_FROM_TITLE
        box = (title[0] + x1, title[1] + y1, title[0] + x2, title[1] + y2)
        pixels = viewport.crop(frame, box, vision.shift_of(frame, TITLE))
    else:
        pixels = np.asarray(frame)
    grey = cv2.cvtColor(pixels, cv2.COLOR_BGR2GRAY)
    return cv2.resize(grey, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA).astype(np.int16)


def _list_view(vision, frame):
    """A small grey picture of the student list, or None when MomoTalk isn't showing."""
    title = vision.find(frame, TITLE)
    if not title:
        return None
    x1, y1, x2, y2 = LIST_FROM_TITLE
    box = (title[0] + x1, title[1] + y1, title[0] + x2, title[1] + y2)
    grey = cv2.cvtColor(viewport.crop(frame, box, vision.shift_of(frame, TITLE)), cv2.COLOR_BGR2GRAY)
    return cv2.resize(grey, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA).astype(np.int16)


def _moved(a, b):
    return a.shape != b.shape or float(np.abs(a - b).mean()) > PANEL_DIFF


def _small_change(a, b):
    """A change too small for _moved, such as a typing dot or a badge."""
    return a.shape == b.shape and np.count_nonzero(np.abs(a - b) > MINOR_LEVEL) >= MINOR_CELLS


def _avatar(vision, frame, y):
    """The middle of a row's portrait. It tells students apart where read names can differ by one letter."""
    vision.find(frame, TITLE)
    x1, y1, x2, y2 = AVATAR_BOX
    return viewport.crop(frame, (x1, y + y1, x2, y + y2), vision.shift_of(frame, TITLE)).copy()


def _same_student(vision, frame, y, avatar):
    """Whether the row at `y` shows this portrait."""
    vision.find(frame, TITLE)
    x1, y1, x2, y2 = AVATAR_BOX
    slack = AVATAR_SLACK
    band = viewport.crop(frame, (x1 - slack, y + y1 - slack, x2 + slack, y + y2 + slack), vision.shift_of(frame, TITLE))
    if band.shape[0] < avatar.shape[0] or band.shape[1] < avatar.shape[1]:
        return False
    return float(cv2.matchTemplate(band, avatar, cv2.TM_CCOEFF_NORMED).max()) >= AVATAR_MATCH


def _has_badge(vision, frame, who):
    """Whether this student's row still has a notification, wherever the list has re-sorted it."""
    return any(_same_student(vision, frame, b[1], who["avatar"]) for b in _badges(vision, frame))


def parse_count(text):
    """N from 'Unread Messages (N)'. OCR reads a lone 0 as the letter O."""
    m = re.search(r"\(\s*([0-9OoIl|]+)\s*\)", text)
    if not m:
        return None
    digits = m.group(1).translate(str.maketrans("OoIl|", "00111"))
    return int(digits)


def _read(vision, frame, box):
    vision.find(frame, UNREAD_TITLE)
    pixels = viewport.crop(frame, box, vision.shift_of(frame, UNREAD_TITLE))
    return " ".join(w.text for w in ocr.read(pixels, None, scale=2)).strip()


def read_count(vision, frame):
    label = vision.find(frame, UNREAD_TITLE)
    if not label:
        return None
    x1, y1, x2, y2 = COUNT_FROM_TITLE
    return parse_count(_read(vision, frame, (label[0] + x1, label[1] + y1, label[0] + x2, label[1] + y2)))


def row_name(vision, frame, y):
    x1, top, x2, bottom = NAME_BOX
    return _read(vision, frame, (x1, max(0, y + top), x2, y + bottom)) or UNREAD_NAME


def _open(game, vision, log):
    nav.go_lobby(game, vision, log)
    frame = game.grab()
    button = vision.find(frame, LOBBY_BUTTON)
    if not button:
        _fail(game, frame, "can't see the MomoTalk button on the lobby")
    _press(game, vision, "open", button, LOBBY_BUTTON, lambda f: _in_momotalk(vision, f), "opening MomoTalk")


def _messages(game, vision):
    """The Messages tab, sorted by Unread, as a current frame."""
    frame = game.grab()
    if not _in_momotalk(vision, frame):
        _fail(game, frame, "MomoTalk isn't the top screen")
    if not vision.find(frame, UNREAD_TITLE):
        tab = _locate(vision, frame, MESSAGES_TAB)
        if not tab:
            _fail(game, frame, "can't find the Messages tab")
        frame = _press(game, vision, "tab", tab, TITLE, lambda f: vision.find(f, UNREAD_TITLE), "Messages tab")
    if not vision.find(frame, SORT_UNREAD):
        _fail(game, frame, "the Messages list isn't sorted by Unread; set it once by hand")
    return frame


def _story(game, vision, point, report, log):
    """Relationship event offered -> entering it -> story skip -> reward -> back in MomoTalk."""
    started = lambda f: classify(vision, f, story_done=True) in (ENTERING, STORY)
    frame = _press(game, vision, "story", point, TITLE, started, "relationship story prompt",
                   polls=STORY_POLLS, retries=0)
    if classify(vision, frame, story_done=True) == ENTERING:
        enter = vision.find(frame, STORY_ENTER)
        _press(game, vision, "story_enter", enter, STORY_ENTER,
               lambda f: classify(vision, f, story_done=True) == STORY, "entering the story",
               polls=STORY_POLLS, retries=0)
    watch = _RewardWatch(vision, game)
    for _ in range(3):                   # skip() returns early if a load hides the story for a moment
        if not story.skip(game, watch, log):
            _fail(game, game.grab(), "the story skip timed out")
        for _ in range(STORY_POLLS):
            frame = game.grab()
            state = classify(vision, frame, story_done=True)
            if state in CHAT:
                report["stories"] += 1
                game.rt.bump("momotalk stories")
                if watch.claimed:
                    report["rewards"] += 1
                    game.rt.bump("momotalk rewards")
                log(f"momotalk: story skipped{', reward claimed' if watch.claimed else ', no reward seen'}")
                return
            if state in (STORY, REWARD):
                break
            game.sleep(POLL)
        else:
            _fail(game, frame, "didn't come back to MomoTalk after the story")
    _fail(game, game.grab(), "the story or its reward wouldn't clear")


def _converse(game, vision, report, log, who):
    """Stay on the open student until they're finished: quiet for SETTLE_SECONDS with nothing pending,
    and their badge gone. It never clicks another row, because their texting pauses if you switch away."""
    name = who["name"]
    settle = math.ceil(SETTLE_SECONDS / POLL)
    before, replies, story_done, quiet, odd, typing, rounds = None, 0, False, 0, 0, 0, 0
    for _ in range(MAX_POLLS):
        frame = game.grab()
        state = classify(vision, frame, story_done)
        if state not in CHAT:
            odd += 1
            if odd >= ODD_POLLS:
                _fail(game, frame, f"something other than MomoTalk came up during a conversation ({state})")
            game.sleep(POLL)
            continue
        odd = 0

        if state == REPLY:
            if replies >= MAX_REPLIES:
                _fail(game, frame, f"more than {MAX_REPLIES} replies in one conversation")
            header = _locate(vision, frame, REPLY_HEADER)
            def answered(f):
                now = _locate(vision, f, REPLY_HEADER)
                return not now or abs(now[1] - header[1]) > MOVED_PX
            option = (header[0] + REPLY_OFFSET[0], header[1] + REPLY_OFFSET[1])
            _press(game, vision, "reply", option, TITLE, answered, "sending a reply")
            replies += 1
            report["replies"] += 1
            game.rt.bump("momotalk replies")
            before, quiet, typing, rounds = None, 0, 0, 0
            continue

        if state == EVENT:
            _story(game, vision, _locate(vision, frame, STORY_PROMPT), report, log)
            story_done = True
            before, quiet, typing, rounds = None, 0, 0, 0
            continue

        typing = typing + 1 if state == MORE else 0
        if typing >= TYPING_POLLS:
            _fail(game, frame, f"{name}'s typing indicator never finished")
        panel = _panel(vision, frame)
        if state == MORE or before is None or _moved(before, panel):
            quiet = 0
            if state == MORE or before is not None:
                rounds = 0               # they're still talking
        else:
            quiet += 1
            if quiet >= settle:
                if not _has_badge(vision, frame, who):
                    return replies
                rounds += 1
                if rounds >= BADGE_ROUNDS:
                    _fail(game, frame, f"{name} went quiet but kept their notification")
                log(f"momotalk: {name} is quiet but still has a notification; staying on them")
                quiet = 0
        before = panel
        game.sleep(POLL)
    _fail(game, game.grab(), "the conversation never settled")


def _queue(game, vision):
    """The Messages tab with its count and badges, once they agree. Badges are the work queue."""
    frame, count, badges = None, None, []
    for _ in range(AGREE_POLLS):
        frame = _messages(game, vision)
        count, badges = read_count(vision, frame), _badges(vision, frame)
        if count is not None and bool(count) == bool(badges):
            return frame, count, badges
        game.sleep(POLL)
    if count is None:
        _fail(game, frame, "couldn't read the unread count")
    if count:
        _fail(game, frame, f"{count} unread but no unread row is visible")
    _fail(game, frame, f"{len(badges)} row(s) have a notification but the unread count is 0")


def run(game, vision, limit=None, log=None):
    """Work through students with a notification (at most `limit`). Returns a report; raises MomoTalkError
    on a stop. A student left mid-conversation keeps their badge, so the next run picks them up."""
    log = log or game.log
    vision.load_pack(PACK)
    load_values()
    gaps = missing(vision)
    if gaps:
        raise MomoTalkError("momotalk: not ready, still needs: " + ", ".join(gaps) + "")
    game.rt.task = "MomoTalk"
    report = {"students": 0, "names": [], "replies": 0, "stories": 0, "rewards": 0, "unread_left": None}
    try:
        _open(game, vision, log)
        students, current = [], None
        while True:
            frame, count, badges = _queue(game, vision)
            report["unread_left"] = count
            if not badges or (limit and report["students"] >= limit):
                break                    # no badge anywhere means nobody has anything to send
            y = badges[0][1]
            who = next((s for s in students if _same_student(vision, frame, y, s["avatar"])), None)
            if who is None:
                who = {"avatar": _avatar(vision, frame, y), "name": row_name(vision, frame, y), "visits": 0}
                students.append(who)
            who["visits"] += 1
            name = who["name"]
            if who["visits"] > MAX_VISITS:
                _fail(game, frame, f"{name}'s notification keeps coming back")
            if who is not current:       # a student who's already open is never clicked again
                log(f"momotalk: opening {name} ({count} unread)")
                start = _panel(vision, frame)
                same_row = lambda f: any(abs(b[1] - y) <= MOVED_PX for b in _badges(vision, f))
                _press(game, vision, "row", (ROW_X, y), TITLE, lambda f: _moved(start, _panel(vision, f)),
                       f"opening {name}", still=same_row)
                current = who
            replies = _converse(game, vision, report, log, who)
            if who["visits"] == 1:
                report["students"] += 1
                report["names"].append(name)
                game.rt.bump("momotalk students")
            log(f"momotalk: {name} done, {replies} replies")
        nav.go_lobby(game, vision, log)
        return report
    except RuntimeError as e:            # includes MomoTalkError, NavError and Game's input refusals
        e.report = report
        raise
    finally:
        log(f"momotalk: {report['students']} students, {report['replies']} replies, {report['stories']} stories, "
            f"{report['rewards']} rewards, unread left: {report['unread_left']}")
        game.rt.task = "idle"


# ---------------------------------------------------------------- recording
def reference(vision, frame):
    """The frame with MomoTalk at its true reference position, for cutting templates. A rebuilt
    non-16:9 frame splits the popup at the anchor bands, so this copies it from the canvas instead."""
    view, canvas = getattr(frame, "view_info", None), getattr(frame, "canvas", None)
    if view is None or view.native or canvas is None or not vision.find(frame, TITLE):
        return np.asarray(frame)
    dx, dy = vision.shift_of(frame, TITLE)
    ch, cw = canvas.shape[:2]
    x0, y0, x1, y1 = max(0, dx), max(0, dy), min(cw, viewport.REF_W + dx), min(ch, viewport.REF_H + dy)
    out = np.zeros((viewport.REF_H, viewport.REF_W, 3), np.uint8)
    out[y0 - dy:y1 - dy, x0 - dx:x1 - dx] = canvas[y0:y1, x0:x1]
    return out


def _write(path, vision, frame):
    if not cv2.imwrite(str(path), reference(vision, frame), [cv2.IMWRITE_PNG_COMPRESSION, 1]):
        raise MomoTalkError(f"recording: couldn't write {path}")


def request_stop(out):
    Path(out, "stop").touch()


def record(game, vision, out, seconds=RECORD_SECONDS, clock=time.time):
    """Read-only: sample at a steady rate, save frames that changed, and log a timeline. Never clicks."""
    out = Path(out)
    manifest = out / "manifest.jsonl"
    if manifest.exists():
        raise MomoTalkError(f"recording: {out} already holds a recording; use a new folder")
    out.mkdir(parents=True, exist_ok=True)
    seconds = min(seconds, RECORD_MAX_SECONDS)
    start = clock()
    previous, saved, samples, last_save, last_line, last_state, last_list = None, 0, 0, -1e9, -1e9, None, None
    with open(manifest, "a", encoding="utf-8") as lines:
        while clock() - start < seconds and not (out / "stop").exists():
            tick = clock()
            frame = game.grab()
            state = classify(vision, frame)
            panel = _panel(vision, frame)
            major = previous is None or state != last_state or _moved(previous, panel)
            minor = not major and _small_change(previous, panel)
            listed = _list_view(vision, frame)    # a badge clearing changes the list, not the conversation
            list_changed = listed is not None and last_list is not None and _small_change(last_list, listed)
            previous, last_state, last_list = panel, state, listed
            samples += 1
            name = None
            if (major or list_changed or (minor and tick - last_save >= RECORD_MINOR_EVERY)) and saved < RECORD_MAX_FRAMES:
                saved += 1
                name = f"f{saved:04d}_{tick - start:07.2f}s.png"
                _write(out / name, vision, frame)
                last_save = tick
            if major or minor or list_changed or name or tick - last_line >= RECORD_HEARTBEAT:
                lines.write(json.dumps({"t": round(tick, 3), "s": round(tick - start, 2), "state": state,
                                        "active": bool(major or state == MORE), "minor": bool(minor),
                                        "list": bool(list_changed), "file": name}) + "\n")
                lines.flush()
                last_line = tick
            game.sleep(max(0.0, RECORD_PERIOD - (clock() - tick)))
    return {"samples": samples, "saved": saved, "seconds": round(clock() - start, 1), "capped": saved >= RECORD_MAX_FRAMES}


def recon_click(game, vision, x, y):
    """One click inside the open MomoTalk list or conversation, tied to its title."""
    frame = game.grab()
    if not _in_momotalk(vision, frame):
        raise MomoTalkError("recon: MomoTalk isn't the top screen; nothing clicked")
    if not any(x1 <= x < x2 and y1 <= y < y2 for x1, y1, x2, y2 in (LIST, PANEL)):
        raise MomoTalkError(f"recon: ({x}, {y}) isn't in the MomoTalk list or conversation")
    if not buttons.tie(vision, frame, TITLE, x, y, reach=POPUP_REACH):
        raise MomoTalkError("recon: can't tie the click to MomoTalk's title")
    game.click(x, y)


def recon_enter(game, vision, x, y):
    """One click on a popup that opened over MomoTalk, after the player has read it."""
    frame = game.grab()
    if not vision.find(frame, TITLE) or not _dimmed(vision, frame, TITLE):
        raise MomoTalkError("recon: there's no popup over MomoTalk; nothing clicked")
    x1, y1, x2, y2 = ZONES["story_enter"]
    if not (x1 <= x < x2 and y1 <= y < y2):
        raise MomoTalkError(f"recon: ({x}, {y}) is outside the popup area")
    if not buttons.tie(vision, frame, TITLE, x, y, reach=POPUP_REACH):
        raise MomoTalkError("recon: can't tie the click to MomoTalk's title")
    game.click(x, y)


def recon_template(game, vision, name):
    """Click a template that's on screen now (the lobby button or the Messages tab)."""
    frame = game.grab()
    point = vision.find(frame, name)
    if not point:
        raise MomoTalkError(f"recon: {name} isn't on screen; nothing clicked")
    game.click(*point)


def step(game, vision, out, note="", act=None, point=None, wait=1.5, clock=time.time):
    """Save a frame, optionally do one click, save the frame after it, and log it beside the recording."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    log_file = out / "clicks.jsonl"
    n = 1 + (len(log_file.read_text(encoding="utf-8").splitlines()) if log_file.exists() else 0)
    frame = game.grab()
    entry = {"n": n, "note": note, "t": round(clock(), 3), "state_before": classify(vision, frame)}
    entry["before"] = f"step{n:03d}_before.png" if act else f"step{n:03d}_shot.png"
    _write(out / entry["before"], vision, frame)
    if act:
        act()
        entry.update(kind="click", x=point[0] if point else None, y=point[1] if point else None)
        game.sleep(wait)
        frame = game.grab()
        entry["state_after"] = classify(vision, frame)
        entry["after"] = f"step{n:03d}_after.png"
        _write(out / entry["after"], vision, frame)
    else:
        entry["kind"] = "shot"
    with open(log_file, "a", encoding="utf-8") as lines:
        lines.write(json.dumps(entry) + "\n")
    return entry


def chat_gaps(samples, clicks):
    """How long the game itself went quiet in a conversation. The clock restarts at each click, so
    time the player spent deciding isn't counted, and it's dropped during a story or another screen."""
    events = [(s["t"], 1, s) for s in samples] + [(c["t"], 0, c) for c in clicks if c.get("kind") == "click"]
    gaps, last = [], None
    for t, is_sample, e in sorted(events, key=lambda ev: (ev[0], ev[1])):
        if not is_sample:
            last = t
        elif e["state"] not in CHAT:
            last = None
        elif e.get("active"):
            if last is not None:
                gaps.append(round(t - last, 2))
            last = t
    return gaps


def recommended_settle(gaps):
    """Twice the longest quiet spell plus 5 s, never under MIN_SETTLE. None without any evidence."""
    return max(MIN_SETTLE, math.ceil(max(gaps) * 2) + 5) if gaps else None


def _jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def cut_hints(samples, clicks):
    """Which saved frame to cut each missing template from, using the notes on the player's clicks."""
    hints = {}
    for word, template, side in (("messages tab", UNREAD_BADGE, "after"), ("reply", REPLY_HEADER, "before"),
                                 ("event button", STORY_PROMPT, "before"), ("enter event", STORY_ENTER, "before")):
        c = next((c for c in clicks if c.get("kind") == "click" and word in (c.get("note") or "").lower()), None)
        if c:
            hints[template.split("/")[1]] = {"file": c[side], "clicked": [c.get("x"), c.get("y")]}
    opened = next((c for c in clicks if "open student" in (c.get("note") or "").lower()), None)
    if opened:
        typing = [s["file"] for s in samples if s["t"] > opened["t"] and s.get("minor") and s.get("file")]
        hints[TYPING.split("/")[1]] = {"files": typing[:6]}
    return hints


def verify(vision, out, samples, clicks):
    """Replay the recording against the templates and values. Returns what doesn't line up."""
    problems = []
    for c in (c for c in clicks if c.get("kind") == "click"):
        note = (c.get("note") or "").lower()
        frame = cv2.imread(str(Path(out) / c["before"]))
        if frame is None:
            problems.append(f"step {c['n']}: can't read {c['before']}")
            continue
        state = classify(vision, frame)
        if "reply" in note:
            if state != REPLY:
                problems.append(f"step {c['n']} ({note}): the reply box read as {state}")
            elif REPLY_OFFSET:
                head = _locate(vision, frame, REPLY_HEADER)
                aim = (head[0] + REPLY_OFFSET[0], head[1] + REPLY_OFFSET[1])
                if abs(aim[0] - c["x"]) > 200 or abs(aim[1] - c["y"]) > 25:
                    problems.append(f"step {c['n']} ({note}): the task would click {aim}, the player clicked "
                                    f"({c['x']}, {c['y']})")
        elif "event button" in note:
            prompt = _locate(vision, frame, STORY_PROMPT)
            if state != EVENT or not prompt or abs(prompt[1] - c["y"]) > 40:
                problems.append(f"step {c['n']} ({note}): the event button read as {state} at {prompt}")
        elif "open student" in note and not any(abs(b[1] - c["y"]) <= 60 for b in _badges(vision, frame)):
            problems.append(f"step {c['n']} ({note}): no badge on the row that was opened")
    files = [s["file"] for s in samples if s.get("file")]
    if not any(classify(vision, f) == MORE for f in (cv2.imread(str(Path(out) / n)) for n in files) if f is not None):
        problems.append("the typing indicator never matched in this recording")
    return problems


def analyze(vision, out):
    """Read a recording: states, when badges clear, quiet gaps, the wait they support, and what to cut."""
    out = Path(out)
    vision.load_pack(PACK)
    load_values()
    samples, clicks = _jsonl(out / "manifest.jsonl"), _jsonl(out / "clicks.jsonl")
    files = [s for s in samples if s.get("file")]
    timeline, scores, badge_timeline = [], {}, []
    names = [n for n in vision.images if n.startswith(PACK + "/")] + [TITLE, "btn__momotalk__close"]
    every = max(1, len(files) // 60)
    for i, s in enumerate(files):
        frame = cv2.imread(str(out / s["file"]))
        if frame is None:
            continue
        state = classify(vision, frame)
        if not timeline or timeline[-1]["state"] != state:
            timeline.append({"s": s["s"], "state": state, "file": s["file"]})
        if vision.has(UNREAD_BADGE) and vision.find(frame, UNREAD_TITLE):   # when does the badge clear?
            rows = [b[1] for b in _badges(vision, frame)]
            if not badge_timeline or badge_timeline[-1]["rows"] != rows:
                badge_timeline.append({"s": s["s"], "rows": rows, "unread": read_count(vision, frame),
                                       "state": state, "file": s["file"]})
        if i % every == 0:
            for n in names:
                score, at = vision.match(frame, n)
                if n not in scores or score > scores[n]["score"]:
                    scores[n] = {"score": round(score, 3), "at": at, "file": s["file"],
                                 "threshold": vision.meta[n].get("threshold", 0.85)}
    gaps = chat_gaps(samples, clicks)
    return {"samples": len(samples), "saved": len(files), "clicks": sum(c.get("kind") == "click" for c in clicks),
            "timeline": timeline, "badge_timeline": badge_timeline, "click_times": [
                {"s": round(c["t"] - samples[0]["t"], 2), "note": c.get("note")} for c in clicks
                if c.get("kind") == "click" and samples],
            "gaps": gaps, "longest_gap": max(gaps) if gaps else None,
            "settle_seconds": recommended_settle(gaps), "template_scores": scores, "cut_from": cut_hints(samples, clicks),
            "problems": verify(vision, out, samples, clicks), "missing": missing(vision)}


def _cli(argv=None):
    from ..game import Game
    from ..vision import Vision
    ap = argparse.ArgumentParser(prog="ba_auto.tasks.momotalk")
    sub = ap.add_subparsers(dest="op", required=True)
    for op in ("record", "record-stop", "analyze", "open", "tab", "shot"):
        p = sub.add_parser(op)
        p.add_argument("dir")
        p.add_argument("--note", default="")
        if op == "record":
            p.add_argument("--seconds", type=int, default=RECORD_SECONDS)
    for op in ("click", "enter"):
        p = sub.add_parser(op)
        p.add_argument("x", type=int)
        p.add_argument("y", type=int)
        p.add_argument("dir")
        p.add_argument("--note", default="")
    sub.add_parser("fit-regions")
    p = sub.add_parser("set-values")
    p.add_argument("--reply-offset", metavar="DX,DY")
    p.add_argument("--settle", type=int)
    p.add_argument("--settle-from", metavar="DIR", help="use the wait this recording supports")
    a = ap.parse_args(argv)
    if a.op == "record-stop":
        request_stop(a.dir)
        print(f"asked the recorder in {a.dir} to stop")
        return
    if a.op == "fit-regions":
        print("search areas set for:", ", ".join(fit_regions()) or "nothing new")
        return
    if a.op == "set-values":
        offset = tuple(int(v) for v in a.reply_offset.split(",")) if a.reply_offset else None
        settle = a.settle
        if a.settle_from:
            folder = Path(a.settle_from)
            settle = recommended_settle(chat_gaps(_jsonl(folder / "manifest.jsonl"), _jsonl(folder / "clicks.jsonl")))
            if settle is None:
                raise SystemExit("that recording shows no gaps to set the wait from")
        print(json.dumps(set_values(offset, settle)))
        return
    vision = Vision()
    vision.load_pack(PACK)
    if a.op == "analyze":
        print(json.dumps(analyze(vision, a.dir), indent=1))
        return
    game = Game()
    if a.op == "record":
        print(json.dumps(record(game, vision, a.dir, a.seconds)))
    elif a.op == "shot":
        print(json.dumps(step(game, vision, a.dir, a.note)))
    elif a.op == "open":
        print(json.dumps(step(game, vision, a.dir, a.note or "open MomoTalk",
                              lambda: recon_template(game, vision, LOBBY_BUTTON))))
    elif a.op == "tab":
        print(json.dumps(step(game, vision, a.dir, a.note or "Messages tab",
                              lambda: recon_template(game, vision, MESSAGES_TAB))))
    else:
        click = recon_click if a.op == "click" else recon_enter
        print(json.dumps(step(game, vision, a.dir, a.note, lambda: click(game, vision, a.x, a.y), (a.x, a.y))))


if __name__ == "__main__":
    _cli()
