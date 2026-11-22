"""Claim mail and completed tasks, one row at a time, reading each row and popup first.

  python -m ba_auto.tasks.claims                 dry run: opens the screens and reports, claims nothing
  python -m ba_auto.tasks.claims --go [--limit 1] [--only mail|tasks]

Safety:
  - "Claim All" is never pressed, because it would claim rows nobody has read.
  - A row is held, not claimed, if its text mentions a reward choice or something that can expire,
    or if its text can't be read.
  - After a claim only a "Reward Acquired" popup with no such text is confirmed. Any other popup
    stops the run with the popup left open and its frame in the journal.
  - A claim that changes nothing stops the run instead of repeating.
  - Every click is re-found in a fresh frame, so it carries the converter's proof at any window size.
"""
import argparse
import json
import re

import cv2
import numpy as np

from .. import buttons, journal, nav, ocr, viewport
from ..screens import label
from ..vision import TEMPLATE_DIR, Vision

POLL = 0.5
OPEN_POLLS = 24
SETTLE_POLLS = 14
GRACE_POLLS = 3                  # a list can reflow just before the reward popup appears
ODD_POLLS = 5                    # polls off the expected screen before it counts as a popup
MAX_CLAIMS = 150
CHANGED = 4.0                    # mean grey difference that counts as the list having moved
SAME_ROW = 24                    # px: the same button when it's found again
LOW_RES = 0.8                    # below this window scale, OCR reads a larger copy

YELLOW = ((20, 110, 170), (38, 255, 255))
BLUE = ((88, 70, 180), (108, 255, 255))
ACTIVE_SHARE = 0.35

CHOICE = re.compile(r"\b(select(ion|or)?|choose|choice|pick one)\b", re.I)
EXPIRY = re.compile(r"\b(expir\w*|time[- ]limited|limited[- ]time|temporary|trial)\b", re.I)

TASK_TAG = re.compile(r"^(daily|weekly|achievement|challenges?)\b\s*", re.I)

POPUP_BOX = (400, 170, 1520, 920)
REWARD_BAND = (200, 170, 1720, 760)
MAIL_COUNT = (1560, 90, 1900, 170)
MAIL_LIST = (430, 165, 1870, 855)
TASK_LIST = (770, 225, 1850, 915)
TASK_FOOTER = (755, 960, 1285, 1000)
TASK_BONUS = (1395, 960, 1535, 1042)
TASK_CLAIM_ALL = (1575, 960, 1880, 1050)
TASK_FOOTER_AT = (945, 983)      # where task_footer sits in the frames the boxes above were measured on


class ClaimStop(nav.NavError):
    """The run met something it won't click through. `.report` holds what it finished."""

    def __init__(self, message, report=None):
        super().__init__(message)
        self.report = report


class Pack(Vision):
    """A Vision over templates/<pack>/, so the shared index is never rewritten."""

    def __init__(self, pack, rt=None):
        self.pack_dir = TEMPLATE_DIR / pack
        super().__init__(rt)

    def reload(self):
        index = self.pack_dir / "index.json"
        self.meta = json.loads(index.read_text(encoding="utf-8")) if index.exists() else {}
        self.images = {}
        self._soft = {}
        for name, m in self.meta.items():
            img = cv2.imread(str(self.pack_dir / m["file"]))
            if img is not None:
                self.images[name] = img

    def save(self, *_, **__):
        raise RuntimeError("cut pack templates with tools/cut_pack.py")


def stop(game, frame, why, report=None):
    journal.record("log", frame=frame, msg=f"stopped: {why}", task=game.rt.task)
    raise ClaimStop(f"{why} (frame in the journal)", report)


def hold_reason(text):
    """Why this text must be left for a person, or None."""
    if CHOICE.search(text):
        return "mentions a reward choice"
    if EXPIRY.search(text):
        return "mentions something that can expire"
    return None


# ---- reading fixed boxes at any window shape ----

def _native(frame):
    view = getattr(frame, "view_info", None)
    return view is None or view.native or getattr(frame, "canvas", None) is None


def shifts(frame):
    """Every place a UI group can sit in this frame."""
    return [(0, 0)] if _native(frame) else frame.view_info.shifts()


def centre_shift(frame):
    """Where the centred group sits. Popups are one centred group."""
    if _native(frame):
        return 0, 0
    view = frame.view_info
    return round(view.dx) // 2, round(view.dy / 2)


def words_at(frame, box, shift=(0, 0), scale=2):
    """OCR words in a reference box on an element at anchor offset `shift`, in reference coordinates."""
    image = viewport.crop(frame, box, shift)
    if image.size == 0:
        return []
    if not _native(frame) and frame.view_info.scale < LOW_RES:
        scale = max(scale, 3)
    ox, oy = (box[0], box[1]) if _native(frame) else (max(0, box[0] + shift[0]) - shift[0],
                                                      max(0, box[1] + shift[1]) - shift[1])
    return [w._replace(x=w.x + ox, y=w.y + oy) for w in ocr.read(np.ascontiguousarray(image), None, scale)]


def read_text(frame, box, shift=(0, 0)):
    return " ".join(w.text for w in words_at(frame, box, shift))


def colour_share(frame, box, colour, shift=(0, 0)):
    image = viewport.crop(frame, box, shift)
    if image.size == 0:
        return 0.0
    hsv = cv2.cvtColor(np.ascontiguousarray(image), cv2.COLOR_BGR2HSV)
    return float((cv2.inRange(hsv, np.array(colour[0]), np.array(colour[1])) > 0).mean())


def is_yellow(frame, box, shift=(0, 0)):
    return colour_share(frame, box, YELLOW, shift) >= ACTIVE_SHARE


def is_blue(frame, box, shift=(0, 0)):
    return colour_share(frame, box, BLUE, shift) >= ACTIVE_SHARE


def place(box, found, expected):
    """A fixed box moved with its template. ui-map frames are a 1053 px window stretched to 1080, so
    at true 16:9 a group can sit up to about 26 px from where the box was measured."""
    dx, dy = found[0] - expected[0], found[1] - expected[1]
    return box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy


def line_in(words, x1, y1, x2, y2):
    """One line of text, left to right. Slightly uneven word heights can't reorder it."""
    hits = [w for w in words if x1 <= w.x + w.w / 2 <= x2 and y1 <= w.y + w.h / 2 <= y2]
    return " ".join(w.text for w in sorted(hits, key=lambda w: w.x))


def moved(a, b, box, shift=(0, 0)):
    ca, cb = viewport.crop(a, box, shift), viewport.crop(b, box, shift)
    if ca.size == 0 or ca.shape != cb.shape:
        return ca.shape != cb.shape
    ga = cv2.cvtColor(np.ascontiguousarray(ca), cv2.COLOR_BGR2GRAY).astype(np.int16)
    gb = cv2.cvtColor(np.ascontiguousarray(cb), cv2.COLOR_BGR2GRAY).astype(np.int16)
    return float(np.abs(ga - gb).mean()) > CHANGED


# ---- pressing ----

def open_from_lobby(game, vision, pack, button, screen, log):
    """Lobby, then the template `button`, until `screen` is on top. One retry for a swallowed click."""
    nav.go_lobby(game, vision, log)
    frame = game.grab()
    for _ in range(2):
        frame = game.grab()
        point = pack.find(frame, button)
        if not point:
            stop(game, frame, f"can't find {button} on the lobby")
        game.click(*point)
        for _ in range(OPEN_POLLS):
            game.sleep(POLL)
            frame = game.grab()
            if label(vision, frame) == screen and not nav.top_popup(vision, frame):
                game.sleep(POLL)
                return game.grab()
        if label(vision, frame) != "lobby":
            break
    stop(game, frame, f"{screen} didn't open")


def _reward_button(frame, pack):
    """The yellow Confirm. Go to Lobby, beside it on battle rewards, is blue and never matches."""
    button = pack.find(frame, "reward_confirm")
    if not button:
        return None
    box = (button[0] - 70, button[1] - 22, button[0] + 70, button[1] + 22)
    return button if is_yellow(frame, box, pack.shift_of(frame, "reward_confirm")) else None


def acknowledge(game, vision, pack, frame, report, what):
    """Read a Reward Acquired popup and press its Confirm. Returns what it listed."""
    for _ in range(OPEN_POLLS):            # rewards animate in, and Confirm comes last
        if _reward_button(frame, pack):
            break
        game.sleep(POLL)
        frame = game.grab()
    else:
        stop(game, frame, f"{what}: no Confirm on the reward popup", report)
    vision.find(frame, "reward_acquired")
    text = read_text(frame, REWARD_BAND, vision.shift_of(frame, "reward_acquired"))
    why = hold_reason(text)
    if why:
        stop(game, frame, f"{what}: the reward popup {why}: {text}", report)
    for _ in range(2):
        frame = game.grab()
        button = _reward_button(frame, pack)
        if not button:
            if not vision.find(frame, "reward_acquired"):
                return text
            stop(game, frame, f"{what}: lost the reward popup's Confirm", report)
        game.click(*button)
        for _ in range(SETTLE_POLLS):
            game.sleep(POLL)
            frame = game.grab()
            if not vision.find(frame, "reward_acquired"):
                return text
    stop(game, frame, f"{what}: the reward popup didn't close", report)


def settle(game, vision, pack, on_screen, changed, report, what):
    """Wait for a claim to land. Returns the reward text ('' if no popup), None if nothing happened."""
    odd = 0
    frame = None
    for _ in range(SETTLE_POLLS):
        game.sleep(POLL)
        frame = game.grab()
        if vision.find(frame, "reward_acquired"):
            return acknowledge(game, vision, pack, frame, report, what)
        if on_screen(frame):
            odd = 0
            if changed(frame):
                for _ in range(GRACE_POLLS):
                    game.sleep(POLL)
                    frame = game.grab()
                    if vision.find(frame, "reward_acquired"):
                        return acknowledge(game, vision, pack, frame, report, what)
                return ""
            continue
        odd += 1
        if odd >= ODD_POLLS:
            body = read_text(frame, POPUP_BOX, centre_shift(frame))[:200]
            stop(game, frame, f"{what}: unrecognised popup, left open: {body}", report)
    return None


def press(game, vision, pack, aim, on_screen, changed, report, what):
    """Find the button with aim(frame), click, settle. Clicks once more only if nothing changed."""
    for attempt in range(2):
        frame = game.grab()
        if changed(frame):                 # a reflowed list can put another row's Claim at this spot
            if not attempt:
                stop(game, frame, f"{what}: the list changed after it was read", report)
            result = settle(game, vision, pack, on_screen, changed, report, what)   # the first click landed late
            return "" if result is None else result
        point = aim(frame)
        if not point:
            stop(game, frame, f"{what}: its button is gone" + (" with no visible change, so the claim is unverified"
                                                               if attempt else ""), report)
        game.click(*point)
        result = settle(game, vision, pack, on_screen, changed, report, what)
        if result is not None:
            return result
    stop(game, game.grab(), f"{what}: nothing changed after 2 clicks", report)


def _button_box(point):
    return point[0] - 80, point[1] - 28, point[0] + 80, point[1] + 28


def _claim_buttons(frame, pack, name, active, min_x):
    """(active Claim buttons in this list, top first; their anchor offset)."""
    if not pack.find(frame, name):
        return [], (0, 0)
    shift = pack.shift_of(frame, name)
    points = [p for p in pack.find_all(frame, name) if p[0] > min_x and active(frame, _button_box(p), shift)]
    return sorted(points, key=lambda p: p[1]), shift


def _same_button(pack, name, where, active, min_x):
    def aim(frame):
        points, _ = _claim_buttons(frame, pack, name, active, min_x)
        for p in points:
            if abs(p[0] - where[0]) <= SAME_ROW and abs(p[1] - where[1]) <= SAME_ROW:
                return p
        return None
    return aim


def _row_title(frame, words, box, shift, good):
    """A row's title from the list's OCR. If that missed it, read just this strip larger before giving up."""
    title = line_in(words, *box)
    if good(title):
        return title
    return line_in(words_at(frame, box, shift, scale=3), *box) or title


def _public(row):
    return {k: v for k, v in row.items() if k not in ("at", "hold", "shift") and v} | (
        {"why": row["hold"]} if row.get("hold") else {})


# ---- mail ----

def mail_count(frame):
    for shift in shifts(frame):
        m = re.search(r"(\d+)\s*/\s*200", read_text(frame, MAIL_COUNT, shift))
        if m:
            return int(m.group(1))
    return None


def mail_rows(frame, pack):
    """Visible claimable mail rows, top first."""
    points, shift = _claim_buttons(frame, pack, "mail_claim", is_blue, 1600)
    if not points:
        return []
    words = words_at(frame, MAIL_LIST, shift)
    rows = []
    for x, y in points:
        title = _row_title(frame, words, (650, y - 130, 1500, y - 65), shift, lambda t: bool(t))
        period = line_in(words, 650, y + 5, 1490, y + 55).replace("Receipt Period", "").strip()
        rows.append({"title": title, "period": period, "amount": line_in(words, 1500, y + 5, 1615, y + 60),
                     "at": (x, y), "shift": shift,
                     "hold": hold_reason(title) or (None if title else "title can't be read")})
    return rows


def claim_mail(game, vision, pack, dry_run=True, limit=None, log=None, report=None):
    log = log or game.log
    out = {"waiting": None, "claimed": [], "would_claim": [], "held": [], "left": None}
    if report is not None:
        report["mail"] = out
    frame = open_from_lobby(game, vision, pack, "lobby_mail", "mail", log)
    out["waiting"] = out["left"] = mail_count(frame)
    log(f"mail: {out['waiting']} waiting")
    for _ in range(MAX_CLAIMS):
        frame = game.grab()
        if vision.find(frame, "reward_acquired"):      # a slow popup from the last claim
            acknowledge(game, vision, pack, frame, report, "mail")
            frame = game.grab()
        if label(vision, frame) != "mail" or nav.top_popup(vision, frame):
            stop(game, frame, "mail: left the mailbox unexpectedly", report)
        rows = mail_rows(frame, pack)
        out["held"] = [_public(r) for r in rows if r["hold"]]
        ready = [r for r in rows if not r["hold"]]
        if dry_run:
            out["would_claim"] = [_public(r) for r in ready]
            break
        if not ready or (limit is not None and len(out["claimed"]) >= limit):
            break
        row, before, base = ready[0], out["left"], frame
        log(f"mail: claiming {row['title']} {row['amount']}")
        reward = press(game, vision, pack, _same_button(pack, "mail_claim", row["at"], is_blue, 1600),
                       lambda f: label(vision, f) == "mail",
                       lambda f: moved(base, f, MAIL_LIST, row["shift"]), report, f"mail '{row['title']}'")
        game.sleep(POLL)
        out["left"] = mail_count(game.grab())
        if before is not None and out["left"] is not None and out["left"] >= before:
            stop(game, game.grab(), f"mail: count stayed at {out['left']} after claiming '{row['title']}'", report)
        out["claimed"].append({**_public(row), "reward": reward})
    return out


# ---- tasks ----

def task_title_problem(title):
    """A row where OCR caught only the Daily/Achievement tag hasn't really been read."""
    return None if len(TASK_TAG.sub("", title).strip()) >= 4 else "title can't be read"


def task_rows(frame, pack):
    """Visible claimable task rows, top first."""
    points, shift = _claim_buttons(frame, pack, "task_claim", is_yellow, 1590)
    if not points:
        return []
    words = words_at(frame, TASK_LIST, shift)
    rows = []
    for x, y in points:
        title = _row_title(frame, words, (783, y - 125, 1840, y - 68), shift, lambda t: not task_title_problem(t))
        rows.append({"title": title, "count": line_in(words, 895, y - 28, 1120, y + 22),
                     "amount": line_in(words, 1495, y + 5, 1600, y + 55), "at": (x, y), "shift": shift,
                     "hold": hold_reason(title) or task_title_problem(title)})
    return rows


def _all_tab(game, pack, frame, report):
    if pack.find(frame, "tab_all_on"):
        return frame
    for _ in range(2):
        frame = game.grab()
        point = pack.find(frame, "tab_all_off")
        if not point:
            stop(game, frame, "tasks: can't find the All tab", report)
        game.click(*point)
        for _ in range(8):
            game.sleep(POLL)
            frame = game.grab()
            if pack.find(frame, "tab_all_on"):
                game.sleep(POLL)
                return game.grab()
    stop(game, frame, "tasks: the All tab didn't open", report)


def _bonus_ready(frame, shift=(0, 0), box=TASK_BONUS):
    return is_yellow(frame, box, shift) and "claim" in read_text(frame, box, shift).lower()


def footer_boxes(frame, pack):
    """The footer's fixed boxes where its label really matched in this frame, or None."""
    found = pack.find(frame, "task_footer")
    if not found:
        return None
    return {"shift": pack.shift_of(frame, "task_footer"), "text": place(TASK_FOOTER, found, TASK_FOOTER_AT),
            "bonus": place(TASK_BONUS, found, TASK_FOOTER_AT), "all": place(TASK_CLAIM_ALL, found, TASK_FOOTER_AT)}


def _claim_bonus(game, vision, pack, frame, dry_run, limit, out, log, report):
    boxes = footer_boxes(frame, pack)
    if not boxes:
        out["bonus"] = "footer not found, left alone"
        return
    if not _bonus_ready(frame, boxes["shift"], boxes["bonus"]):
        return
    footer = read_text(frame, boxes["text"], boxes["shift"])
    if hold_reason(footer):
        out["bonus"] = f"held: {footer}"
    elif dry_run:
        out["bonus"] = f"would claim: {footer}"
    elif limit is None or len(out["claimed"]) < limit:
        def aim(f):
            now = footer_boxes(f, pack)
            if not now or not _bonus_ready(f, now["shift"], now["bonus"]):
                return None
            x1, y1, x2, y2 = now["bonus"]
            center = ((x1 + x2) // 2, (y1 + y2) // 2)
            return center if buttons.tie(pack, f, "task_footer", *center) else None

        def changed(f):
            now = footer_boxes(f, pack)
            return bool(now) and not is_yellow(f, now["bonus"], now["shift"])

        log(f"tasks: claiming the daily bonus ({footer})")
        reward = press(game, vision, pack, aim, lambda f: label(vision, f) == "tasks", changed, report, "daily bonus")
        out["bonus"] = {"text": footer, "reward": reward}


def claim_tasks(game, vision, pack, dry_run=True, limit=None, log=None, report=None):
    log = log or game.log
    out = {"claimed": [], "would_claim": [], "held": [], "bonus": None, "more_claimable": False}
    if report is not None:
        report["tasks"] = out
    frame = open_from_lobby(game, vision, pack, "lobby_tasks", "tasks", log)
    frame = _all_tab(game, pack, frame, report)
    last = None
    for _ in range(MAX_CLAIMS):
        frame = game.grab()
        if vision.find(frame, "reward_acquired"):      # a slow popup from the last claim
            acknowledge(game, vision, pack, frame, report, "tasks")
            frame = game.grab()
        if label(vision, frame) != "tasks" or nav.top_popup(vision, frame):
            stop(game, frame, "tasks: left the Tasks screen unexpectedly", report)
        rows = task_rows(frame, pack)
        out["held"] = [_public(r) for r in rows if r["hold"]]
        ready = [r for r in rows if not r["hold"]]
        if dry_run:
            out["would_claim"] = [_public(r) for r in ready]
            break
        if last and any(r["title"] == last for r in ready):
            stop(game, frame, f"tasks: '{last}' is still claimable after claiming it", report)
        if not ready or (limit is not None and len(out["claimed"]) >= limit):
            break
        row, base = ready[0], frame
        log(f"tasks: claiming {row['title']} {row['amount']}")
        reward = press(game, vision, pack, _same_button(pack, "task_claim", row["at"], is_yellow, 1590),
                       lambda f: label(vision, f) == "tasks",
                       lambda f: moved(base, f, TASK_LIST, row["shift"]), report, f"task '{row['title']}'")
        out["claimed"].append({**_public(row), "reward": reward})
        last = row["title"]

    _claim_bonus(game, vision, pack, game.grab(), dry_run, limit, out, log, report)
    frame = game.grab()
    # Read only: Claim All is never pressed.
    boxes = footer_boxes(frame, pack)
    out["more_claimable"] = is_yellow(frame, boxes["all"], boxes["shift"]) if boxes else None
    return out


def run(game, vision=None, dry_run=True, only=("mail", "tasks"), limit=None):
    """Returns a report. Raises ClaimStop, with `.report`, when it stops on something."""
    vision = vision or Vision(game.rt)
    pack = Pack("claims", game.rt)
    missing = [n for n in ("lobby_mail", "lobby_tasks", "mail_claim", "task_claim", "tab_all_on", "tab_all_off",
                           "task_footer", "reward_confirm") if not pack.has(n)]
    if missing:
        raise ClaimStop(f"claims: templates missing: {', '.join(missing)}")
    report = {"dry_run": dry_run, "mail": None, "tasks": None}
    previous, game.rt.task = game.rt.task, "Claims (dry run)" if dry_run else "Claims"
    try:
        if "mail" in only:
            claim_mail(game, vision, pack, dry_run, limit, game.log, report)
        if "tasks" in only:
            claim_tasks(game, vision, pack, dry_run, limit, game.log, report)
        nav.go_lobby(game, vision, game.log)
    except ClaimStop as e:
        e.report = report
        raise
    finally:
        game.rt.task = previous
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ba_auto.tasks.claims")
    ap.add_argument("--go", action="store_true", help="really claim; without it this is a dry run")
    ap.add_argument("--only", choices=["mail", "tasks"])
    ap.add_argument("--limit", type=int, help="claim at most N rows in each section")
    a = ap.parse_args(argv)
    from ..game import Game
    game = Game()
    try:
        result = run(game, dry_run=not a.go, only=(a.only,) if a.only else ("mail", "tasks"), limit=a.limit)
    except ClaimStop as e:
        print(json.dumps({"stopped": str(e), "report": e.report}, indent=1, ensure_ascii=False))
        return 2
    print(json.dumps(result, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
