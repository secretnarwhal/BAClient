"""Daily cafe: claim earnings, headpat students showing a marker, use the free invitation.

  python -m ba_auto.tasks.cafe                   dry run: reports what it would do, only navigates
  python -m ba_auto.tasks.cafe --go [--max-pats 1] [--no-invite]
  python -m ba_auto.tasks.cafe --recon           saves floor frames of both cafes, only navigates

Safety:
  - Every click names a kind and must land in that kind's zone. Gifts, Edit, Furniture, Presets,
    Store All, Planned and Unannounced Visit and the Bonus Invitation (purchasable) are in no zone.
  - Headpat targets are found fresh in the frame right before each click. White talk bubbles are
    ambient lines, so only templates named bubble* (cut from recon frames) count as targets.
  - The floor is a 3D scene that doesn't follow the UI's anchors. Outside 16:9 a marker is clickable
    only if it's clear of every button at every place that button's anchor could put it.
  - Until those templates exist, headpats report "not calibrated" and click nothing. The same goes
    for the invitation list. Inviting is off unless config/cafe.json turns it on.
  - Unknown popups stop the run with the popup left open and its frame in the journal.
"""
import argparse
import json
import re
import time
from collections import namedtuple

import cv2
import numpy as np

from .. import buttons, journal, nav, ocr, viewport
from ..screens import _dimmed, label
from ..vision import Vision
from .claims import (ClaimStop, Pack, centre_shift, hold_reason, is_yellow, line_in, place, press, read_text, stop,
                     words_at)

ZERO = str.maketrans("Oo", "00")
CONFIG = journal.ROOT / "config" / "cafe.json"
RECON_DIR = journal.ROOT / "captures" / "cafe_recon"
DEFAULTS = {"cafes": [1, 2], "headpat": True,
            "invite": {"enabled": False, "students": [], "fallback_lowest_bond": False}}

POLL = 0.5
LOAD_POLLS = 30
VERIFY_POLLS = 8
ODD_POLLS = 5
MAX_PATS = 12
MAX_MISSES = 2
SAME_MARKER = 40                 # px: two template hits this close are one marker
WALK_PX = 140                    # px a student can plausibly walk between two reads

ZONES = {
    "earnings": (1642, 930, 1880, 1042),
    "earn_claim": (790, 730, 1132, 870),
    "earn_close": (1440, 180, 1510, 245),
    "pill": (66, 128, 335, 190),
    "guide_close": (1350, 245, 1410, 300),
    "floor": (150, 70, 1900, 930),
    "invite": (1265, 925, 1395, 1040),
    "invite_row": (300, 150, 1700, 950),
    "invite_confirm": (500, 600, 1420, 950),
}
# Parts of the floor rectangle covered by buttons. Measured on ui-map frames, which are a 1053 px tall
# window stretched to 1080, so each is padded by how far its group can sit from there at true 16:9.
UI_MARGIN = 14
NOT_FLOOR = tuple((x1, y1 - UI_MARGIN, x2, y2 + UI_MARGIN) for x1, y1, x2, y2 in (
    (0, 0, 1920, 70), (60, 120, 790, 200), (20, 215, 190, 495), (1770, 105, 1900, 190),
    (30, 915, 900, 1080), (1095, 865, 1400, 1080), (1400, 925, 1920, 1080)))
BONUS = (1095, 865 - UI_MARGIN, 1255, 1050 + UI_MARGIN)
CARDS = ((452, 788), (796, 1132), (1140, 1476))
CARD_NAMES = ("cafe 1 credits", "cafe 1 AP", "cafe 2 credits")
EARN_TITLE_AT = (960, 210)       # the fixed boxes below are placed from where these really matched
INVITE_LABEL_AT = (1328, 1023)
EARN_STATUS = (450, 625, 1480, 705)
EARN_CLAIM = (800, 750, 1125, 850)
INVITE_TIMER = (1270, 878, 1392, 914)
TIMER_WHITE = 0.5
TIMER_DARK = (0.04, 0.4)
POPUP_BOX = (400, 170, 1520, 920)
INVITE_RECON = ("invite_ready", "invite_title", "invite_button", "invite_confirm_title", "invite_confirm")
INVITE_COST = re.compile(r"pyroxene|purchase|\bbuy\b|\bcost\b|bonus", re.I)

Marker = namedtuple("Marker", "x y shift")


def load_config(path=None):
    """config/cafe.json over the safe defaults. A broken file leaves inviting off."""
    try:
        raw = json.loads((path or CONFIG).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    invite = {**DEFAULTS["invite"], **(raw.get("invite") or {})}
    invite["enabled"] = invite["enabled"] is True
    invite["fallback_lowest_bond"] = invite["fallback_lowest_bond"] is True
    invite["students"] = [s for s in invite["students"] if isinstance(s, str) and s.strip()]
    cafes = [c for c in raw.get("cafes", DEFAULTS["cafes"]) if c in (1, 2)]
    return {"cafes": cafes, "headpat": raw.get("headpat", True) is True, "invite": invite}


def _inside(point, box):
    return box[0] <= point[0] < box[2] and box[1] <= point[1] < box[3]


def on_floor(point, view=None, shift=(0, 0)):
    """Whether a marker can be clicked. Outside 16:9, tested on the true canvas against every button
    at each place its anchor could put it."""
    if view is None or view.native:
        return _inside(point, ZONES["floor"]) and not any(_inside(point, b) for b in NOT_FLOOR)
    p = (point[0] + shift[0], point[1] + shift[1])
    if not (0 <= p[0] < view.canvas_w and 0 <= p[1] < view.canvas_h):
        return False
    return not any(_inside(p, (b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy))
                   for b in NOT_FLOOR for dx, dy in view.shifts())


def _click(game, kind, point, view=None, shift=(0, 0)):
    point = (point[0], point[1])
    if kind == "floor":
        if not on_floor(point, view, shift):
            raise ClaimStop(f"cafe: floor click at {point} is on or near a button")
    else:
        if _inside(point, BONUS):
            raise ClaimStop(f"cafe: refused a click on the Bonus Invitation at {point}")
        if not _inside(point, ZONES[kind]):
            raise ClaimStop(f"cafe: {kind} click at {point} is outside its zone")
    game.click(*point)


def clear_cafe(vision, frame):
    """The plain cafe floor, with no popup over it."""
    return label(vision, frame) == "cafe" and not nav.top_popup(vision, frame)


def which_cafe(pack, frame):
    """1 or 2, from the 'Move to Cafe No. N' pill, which names the other cafe."""
    if pack.find(frame, "pill_digit_2"):
        return 1
    if pack.find(frame, "pill_digit_1"):
        return 2
    return None


def go_to_cafe(game, vision, pack, number, report=None):
    frame = game.grab()
    if which_cafe(pack, frame) == number and clear_cafe(vision, frame):
        return frame
    name = "pill_digit_2" if number == 2 else "pill_digit_1"
    for _ in range(2):
        frame = game.grab()
        digit = pack.find(frame, name)
        if not digit or not clear_cafe(vision, frame):
            stop(game, frame, f"cafe: can't find the way to cafe {number}", report)
        _click(game, "pill", digit)                # the digit is inside the pill button
        for _ in range(LOAD_POLLS):
            game.sleep(POLL)
            frame = game.grab()
            popup = nav.top_popup(vision, frame)
            if popup and popup[0] == "Guide":      # first visit to cafe 2
                if not buttons.prove(vision, frame, "Guide", "close", *popup[1]):
                    stop(game, frame, "cafe: can't confirm the Guide's close button", report)
                _click(game, "guide_close", popup[1])
                continue
            if which_cafe(pack, frame) == number and clear_cafe(vision, frame):
                game.sleep(POLL)
                return game.grab()
    stop(game, frame, f"cafe: didn't arrive at cafe {number}", report)


# ---- earnings ----

def read_earnings(frame, shift=(0, 0), box=EARN_STATUS):
    words = words_at(frame, box, shift, scale=3)
    out = {}
    for name, (x1, x2) in zip(CARD_NAMES, CARDS):
        text = line_in(words, x1, box[1], x2, box[3])
        m = re.search(r"([\dOo,.]+)\s*/\s*([\dOo,]+)", text)          # OCR reads a lone 0 as the letter o
        out[name] = {"earned": m.group(1).translate(ZERO), "cap": m.group(2).translate(ZERO)} if m else None
    return out


def _close_earnings(game, vision, report):
    for _ in range(3):
        frame = game.grab()
        popup = nav.top_popup(vision, frame)
        if not popup:
            if clear_cafe(vision, frame):
                return
        elif popup[0] == "Cafe Earnings":
            if not buttons.prove(vision, frame, "Cafe Earnings", "close", *popup[1]):
                stop(game, frame, "cafe: can't confirm the earnings popup's X", report)
            _click(game, "earn_close", popup[1])
        else:
            stop(game, frame, f"cafe: unexpected popup {popup[0]}", report)
        game.sleep(1.0)
    stop(game, game.grab(), "cafe: Cafe Earnings didn't close", report)


def _open_earnings(game, vision, pack, report):
    frame = None
    for _ in range(2):
        frame = game.grab()
        button = pack.find(frame, "earnings_label")
        if not button or _dimmed(pack, frame, "earnings_label"):
            stop(game, frame, "cafe: can't find Cafe Earnings", report)
        _click(game, "earnings", button)
        for _ in range(12):
            game.sleep(POLL)
            frame = game.grab()
            popup = nav.top_popup(vision, frame)
            if popup and popup[0] == "Cafe Earnings":
                game.sleep(POLL)
                return game.grab()
    stop(game, frame, "cafe: Cafe Earnings didn't open", report)


def claim_earnings(game, vision, pack, claim_pack, dry_run, log, report):
    out = report["earnings"] = {"status": None, "before": None}
    frame = _open_earnings(game, vision, pack, report)
    title = vision.find(frame, "nav_cafe_earnings")
    if not title:
        stop(game, frame, "cafe: lost the earnings popup", report)
    shift = vision.shift_of(frame, "nav_cafe_earnings")
    claim_box = place(EARN_CLAIM, title, EARN_TITLE_AT)
    out["before"] = read_earnings(frame, shift, place(EARN_STATUS, title, EARN_TITLE_AT))
    log(f"cafe: earnings {out['before']}")
    if pack.find(frame, "earn_claim_off"):
        out["status"] = "nothing to claim"
    elif not is_yellow(frame, claim_box, shift) or "claim" not in read_text(frame, claim_box, shift).lower():
        stop(game, frame, "cafe: the earnings Claim button isn't readable", report)
    elif dry_run:
        out["status"] = "would claim"
    else:
        def aim(f):
            found = vision.find(f, "nav_cafe_earnings")
            if not found:
                return None
            box = place(EARN_CLAIM, found, EARN_TITLE_AT)
            if not is_yellow(f, box, vision.shift_of(f, "nav_cafe_earnings")):
                return None
            center = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
            if not _inside(center, ZONES["earn_claim"]):
                raise ClaimStop("cafe: earnings Claim is outside its zone", report)
            return center if buttons.tie(vision, f, "nav_cafe_earnings", *center) else None

        def on_screen(f):
            top = nav.top_popup(vision, f)
            return bool(top and top[0] == "Cafe Earnings") or clear_cafe(vision, f)

        def changed(f):
            return clear_cafe(vision, f) or bool(pack.find(f, "earn_claim_off"))

        reward = press(game, vision, claim_pack, aim, on_screen, changed, report, "cafe earnings")
        out["status"], out["reward"] = "claimed", reward
        frame = game.grab()
        title = vision.find(frame, "nav_cafe_earnings")
        if title:
            out["after"] = read_earnings(frame, vision.shift_of(frame, "nav_cafe_earnings"),
                                         place(EARN_STATUS, title, EARN_TITLE_AT))
    _close_earnings(game, vision, report)
    return out


# ---- headpats ----

def find_markers(pack, frame):
    """Headpat markers on the floor in this frame, from every bubble* template."""
    view = getattr(frame, "view_info", None)
    hits = []
    for name in sorted(n for n in pack.images if n.startswith("bubble")):
        if not pack.find(frame, name):
            continue
        shift = pack.shift_of(frame, name)
        for p in pack.find_all(frame, name):
            if on_floor(p, view, shift) and all(abs(p[0] - q.x) > SAME_MARKER or abs(p[1] - q.y) > SAME_MARKER
                                                for q in hits):
                hits.append(Marker(p[0], p[1], shift))
    return sorted(hits, key=lambda m: (m.y, m.x))


def _near(point, markers, radius=WALK_PX):
    best = None
    for m in markers:
        d = max(abs(m[0] - point[0]), abs(m[1] - point[1]))
        if d <= radius and (best is None or d < best[0]):
            best = (d, m)
    return best[1] if best else None


def _pat_landed(game, vision, pack, point, before, report):
    odd = 0
    for _ in range(VERIFY_POLLS):
        game.sleep(POLL)
        frame = game.grab()
        if not clear_cafe(vision, frame):
            odd += 1
            if odd >= ODD_POLLS:
                body = read_text(frame, POPUP_BOX, centre_shift(frame))[:200]
                stop(game, frame, f"cafe: popup after a headpat, left open: {body}", report)
            continue
        odd = 0
        now = find_markers(pack, frame)
        if len(now) < before or not _near(point, now):
            return True
    return False


def headpat(game, vision, pack, dry_run, log, report, out, max_pats=MAX_PATS):
    if not any(n.startswith("bubble") for n in pack.images):
        out["headpats"] = "not calibrated: no bubble templates yet"
        return
    frame = game.grab()
    view = getattr(frame, "view_info", None)
    unverified = view is not None and not view.native
    if dry_run:
        out["headpats"] = {"would_pat": len(find_markers(pack, frame))}
        if unverified:
            out["headpats"]["note"] = "marker art is unverified at this window shape"
        return
    done = misses = 0
    for _ in range(max(MAX_PATS * 2, max_pats)):
        if done >= max_pats or misses >= MAX_MISSES:
            break
        frame = game.grab()
        if not clear_cafe(vision, frame):
            stop(game, frame, "cafe: a popup covered the floor", report)
        markers = find_markers(pack, frame)
        if not markers:
            break
        target = markers[0]
        frame = game.grab()                   # students walk: read again right before clicking
        if not clear_cafe(vision, frame):
            stop(game, frame, "cafe: a popup covered the floor", report)
        fresh = find_markers(pack, frame)
        point = _near(target, fresh)
        if not point:
            continue
        _click(game, "floor", point, getattr(frame, "view_info", None), point.shift)
        if _pat_landed(game, vision, pack, point, len(fresh), report):
            done += 1
            misses = 0
            log(f"cafe: headpat {done} at {(point.x, point.y)}")
        else:
            misses += 1
            log(f"cafe: no change after a headpat at {(point.x, point.y)}")
    out["headpats"] = {"done": done, "left": len(find_markers(pack, game.grab()))}
    if misses >= MAX_MISSES:
        out["headpats"]["stopped"] = f"{MAX_MISSES} headpats in a row changed nothing"
    if unverified and not done:
        out["headpats"]["note"] = "marker art is unverified at this window shape"


# ---- invitation ----

def _norm(text):
    return " ".join(text.lower().split())


def row_has(text, name):
    """The exact student: 'Shiroko' doesn't match 'Shiroko (Riding)'."""
    return re.search(r"(?<![a-z0-9])" + re.escape(_norm(name)) + r"(?![a-z0-9]|\s*\()", _norm(text)) is not None


def choose_student(invite, rows, roster_students, exclude=()):
    """(row, why) for the student to invite, or (None, why). `rows` have a 'text' field."""
    open_rows = [r for r in rows if not any(row_has(r["text"], x) for x in exclude)]
    for name in invite["students"]:
        for row in open_rows:
            if row_has(row["text"], name):
                return {**row, "student": name}, "from config"
    if not invite["fallback_lowest_bond"]:
        return None, "no configured student on the list, and the fallback is off"
    ranked = sorted(((s["bond"], s["name"]) for s in roster_students
                     if isinstance(s.get("bond"), int) and s.get("name")), key=lambda t: (t[0], t[1]))
    for bond, name in ranked:
        for row in open_rows:
            if row_has(row["text"], name):
                return {**row, "student": name}, f"lowest bond on the list ({bond})"
    return None, "no listed student has a bond in the roster scan"


def timer_showing(frame, shift=(0, 0), box=INVITE_TIMER):
    """The white callout over the ticket. OCR misreads its digits, but its shape is steady."""
    crop = viewport.crop(frame, box, shift)
    if crop.size == 0:
        return False
    hsv = cv2.cvtColor(np.ascontiguousarray(crop), cv2.COLOR_BGR2HSV)
    white = float(((hsv[:, :, 1] < 22) & (hsv[:, :, 2] > 232)).mean())
    dark = float((hsv[:, :, 2] < 110).mean())
    return white >= TIMER_WHITE and TIMER_DARK[0] <= dark <= TIMER_DARK[1]


def invite_state(frame, pack):
    """'cooldown', 'ready' (only on a positive match), or 'unknown', which is never acted on."""
    found = pack.find(frame, "invite_label")
    if found and timer_showing(frame, pack.shift_of(frame, "invite_label"),
                               place(INVITE_TIMER, found, INVITE_LABEL_AT)):
        return "cooldown"
    return "ready" if pack.has("invite_ready") and pack.find(frame, "invite_ready") else "unknown"


def invite_rows(frame, pack):
    if not pack.find(frame, "invite_button"):
        return []
    words = words_at(frame, ZONES["invite_row"], pack.shift_of(frame, "invite_button"))
    rows = []
    for x, y in pack.find_all(frame, "invite_button"):
        if _inside((x, y), ZONES["invite_row"]):
            text = ocr.text_in(words, ZONES["invite_row"][0], y - 70, x - 60, y + 70)
            rows.append({"text": text, "at": (x, y)})
    return rows


def use_invite(game, vision, pack, config, dry_run, log, report, out, exclude):
    invite = config["invite"]
    if not invite["enabled"]:
        out["invite"] = "off in config/cafe.json"
        return None
    frame = game.grab()
    state = invite_state(frame, pack)
    if state == "cooldown":
        out["invite"] = "on cooldown"
        return None
    missing = [n for n in INVITE_RECON if not pack.has(n)]
    if missing:
        out["invite"] = f"not calibrated: {', '.join(missing)}"
        return None
    if state != "ready":
        out["invite"] = "can't tell whether the ticket is ready, left alone"
        return None
    if dry_run:
        out["invite"] = "would open the invitation list and pick from config"
        return None
    point = pack.find(frame, "invite_label")
    if not point or _dimmed(pack, frame, "invite_label"):
        stop(game, frame, "cafe: can't find Invitation", report)
    _click(game, "invite", point)
    for _ in range(LOAD_POLLS):
        game.sleep(POLL)
        frame = game.grab()
        if pack.find(frame, "invite_title"):
            break
    else:
        stop(game, frame, "cafe: the invitation list didn't open", report)
    game.sleep(POLL)
    frame = game.grab()
    from . import roster as roster_task
    row, why = choose_student(invite, invite_rows(frame, pack), roster_task.load()["students"], exclude)
    if not row:
        stop(game, frame, f"cafe: nobody to invite ({why}); list left open", report)
    log(f"cafe: inviting {row['student']} ({why})")
    _click(game, "invite_row", row["at"])
    for _ in range(12):
        game.sleep(POLL)
        frame = game.grab()
        if pack.find(frame, "invite_confirm_title"):
            break
    else:
        stop(game, frame, "cafe: no confirmation after choosing a student", report)
    body = read_text(frame, POPUP_BOX, pack.shift_of(frame, "invite_confirm_title"))
    if not row_has(body, row["student"]) or INVITE_COST.search(body) or hold_reason(body):
        stop(game, frame, f"cafe: invitation confirmation not accepted, left open: {body[:200]}", report)
    button = pack.find(frame, "invite_confirm")
    if not button:
        stop(game, frame, "cafe: no Confirm on the invitation", report)
    _click(game, "invite_confirm", button)
    for _ in range(LOAD_POLLS):
        game.sleep(POLL)
        frame = game.grab()
        if clear_cafe(vision, frame):
            out["invite"] = {"invited": row["student"], "why": why}
            return row["student"]
    stop(game, frame, "cafe: didn't return to the floor after inviting", report)


# ---- runs ----

def _pack_missing(pack):
    return [n for n in ("pill_digit_1", "pill_digit_2", "earnings_label", "invite_label", "earn_claim_off")
            if not pack.has(n)]


def run(game, vision=None, dry_run=True, config=None, headpats=None, invites=None, max_pats=MAX_PATS):
    """Returns a report. Raises ClaimStop, with `.report`, when it stops on something."""
    vision = vision or Vision(game.rt)
    pack, claim_pack = Pack("cafe", game.rt), Pack("claims", game.rt)
    config = config or load_config()
    missing = _pack_missing(pack) + ([] if claim_pack.has("reward_confirm") else ["claims/reward_confirm"])
    if missing:
        raise ClaimStop(f"cafe: templates missing: {', '.join(missing)}")
    do_pats = config["headpat"] if headpats is None else headpats
    do_invite = invites is not False
    report = {"dry_run": dry_run, "earnings": None, "cafes": {}}
    previous, game.rt.task = game.rt.task, "Cafe (dry run)" if dry_run else "Cafe"
    try:
        _enter(game, vision, pack, game.log)
        claim_earnings(game, vision, pack, claim_pack, dry_run, game.log, report)
        invited = []
        for number in config["cafes"]:
            go_to_cafe(game, vision, pack, number, report)
            out = report["cafes"][str(number)] = {}
            if do_pats:
                headpat(game, vision, pack, dry_run, game.log, report, out, max_pats)
            if do_invite:
                who = use_invite(game, vision, pack, config, dry_run, game.log, report, out, invited)
                if who:
                    invited.append(who)
        nav.go_lobby(game, vision, game.log)
    except ClaimStop as e:
        e.report = report
        raise
    finally:
        game.rt.task = previous
    return report


def _enter(game, vision, pack, log):
    nav.go_lobby(game, vision, log)
    frame = game.grab()
    for _ in range(2):
        frame = game.grab()
        point = vision.find(frame, "lobby_cafe")
        if not point:
            stop(game, frame, "cafe: can't find Cafe on the lobby")
        game.click(*point)
        for _ in range(LOAD_POLLS):
            game.sleep(POLL)
            frame = game.grab()
            if clear_cafe(vision, frame) and which_cafe(pack, frame):
                game.sleep(POLL)
                return game.grab()
        if label(vision, frame) != "lobby":
            break
    stop(game, frame, "cafe: the cafe didn't open")


def recon_image(frame):
    """A 1920x1080 image of true pixels. Outside 16:9 that's the canvas's centre, not the rebuilt frame,
    whose anchor bands would cut through students on the floor."""
    view, canvas = getattr(frame, "view_info", None), getattr(frame, "canvas", None)
    if view is None or view.native or canvas is None:
        return np.asarray(frame)
    h, w = canvas.shape[:2]
    sx, sy = max(0, (w - 1920) // 2), max(0, (h - 1080) // 2)
    part = canvas[sy:sy + 1080, sx:sx + 1920]
    board = np.zeros((1080, 1920, 3), np.uint8)
    oy, ox = (1080 - part.shape[0]) // 2, (1920 - part.shape[1]) // 2
    board[oy:oy + part.shape[0], ox:ox + part.shape[1]] = part
    return board


def recon(game, vision=None, shots=8, gap=2.5):
    """Save floor frames of both cafes, for cutting the marker templates. Only navigates."""
    vision = vision or Vision(game.rt)
    pack = Pack("cafe", game.rt)
    RECON_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%H%M%S")
    saved = []
    previous, game.rt.task = game.rt.task, "Cafe recon"
    try:
        _enter(game, vision, pack, game.log)
        for number in (1, 2):
            go_to_cafe(game, vision, pack, number)
            for i in range(shots):
                frame = game.grab()
                view = getattr(frame, "view_info", None)
                shape = "16x9" if view is None or view.native else f"{view.client[2]}x{view.client[3]}"
                path = RECON_DIR / f"{stamp}_cafe{number}_{i}_{shape}.png"
                cv2.imwrite(str(path), recon_image(frame))
                saved.append(str(path.relative_to(journal.ROOT)))
                game.sleep(gap)
        nav.go_lobby(game, vision, game.log)
    finally:
        game.rt.task = previous
    return saved


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ba_auto.tasks.cafe")
    ap.add_argument("--go", action="store_true", help="really act; without it this is a dry run")
    ap.add_argument("--recon", action="store_true", help="only save floor frames of both cafes")
    ap.add_argument("--no-headpat", action="store_true")
    ap.add_argument("--no-invite", action="store_true")
    ap.add_argument("--max-pats", type=int, default=MAX_PATS)
    a = ap.parse_args(argv)
    from ..game import Game
    game = Game()
    if a.recon:
        print("\n".join(recon(game)))
        return 0
    try:
        result = run(game, dry_run=not a.go, headpats=False if a.no_headpat else None,
                     invites=False if a.no_invite else None, max_pats=max(1, a.max_pats))
    except ClaimStop as e:
        print(json.dumps({"stopped": str(e), "report": e.report}, indent=1, ensure_ascii=False))
        return 2
    print(json.dumps(result, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
