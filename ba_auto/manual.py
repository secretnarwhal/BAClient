"""A journaled manual click for the player: "click what I just looked at in that saved frame".

    python -m ba_auto shot captures\\x.png
    python -m ba_auto click X Y --from captures\\x.png

X, Y are pixels in the saved frame. This relaxes one rule, for this command only: the point needn't be a
matched template. Instead the saved frame must be seconds old and still match the live screen, overall and
around the point. Every other Game check still applies (read-only actor, confirmed picture, inside the
picture, game under the point, title screen, blocking popups, halts, the client's run lock, journaling).
Known spending and Never/Ask controls are refused. Drags are not available here.
"""
import re
import time
from pathlib import Path

import cv2
import numpy as np

from . import catalog, nav, ocr, watchdog
from .screens import label

MAX_AGE = 12.0                   # seconds; about long enough to look at a frame and answer
PATCH = (70, 45)                 # half-size of the area around the point that must be unchanged
PATCH_DIFF = 14.0                # mean grey-level change allowed in that area
SCREEN_DIFF = 30                 # a thumbnail pixel changed by more than this counts as changed
SCREEN_CHANGED = 0.18            # share of changed pixels that means a different screen
TOP_BAR_NEVER = ((943, 36), (1634, 36), (1742, 37), (945, 54), (1523, 54))
SPEND_SCREENS = {"shop", "recruit", "cash_shop", "crafting"}
BIG_BUTTONS = {"start_sweep": (260, 60), "start_mission": (280, 70), "start_lesson": (210, 60),
               "enter": (210, 65), "mobilize": (140, 65), "unlock_growth": (170, 60)}
NEVER_WORDS = re.compile(r"\b(purchase|buy|recruit|recharge|refill|level up|sell|dismantle|discard|unlock)\b", re.I)
SPEND_WORDS = re.compile(r"\b(purchase|buy|recruit|recharge|refill|level up|sell|dismantle|discard|"
                         r"start sweep|start mission|mobilize|multi-sweep|unlock)\b", re.I)
DIALOG_WORDS = re.compile(r"\b(purchases?|buy|recruit|recharge|refill|level up|sell|dismantle|discard|unlock|"
                          r"pyroxenes?|payments?|prices?)\b", re.I)   # anywhere in a dialog's body
CLOSE_REACH = 30                 # how close to a popup's X counts as closing it


class ManualRefused(RuntimeError):
    """The manual click was refused. Nothing was clicked."""


def load(path):
    p = Path(path)
    try:
        age = time.time() - p.stat().st_mtime
    except OSError:
        raise ManualRefused(f"can't read {path}") from None
    if age > MAX_AGE:
        raise ManualRefused(f"{path} is {age:.0f} s old (limit {MAX_AGE:.0f} s): take a new shot and look again")
    saved = cv2.imread(str(p))
    if saved is None or saved.shape[:2] != (1080, 1920):
        raise ManualRefused(f"{path} isn't a 1920x1080 frame from `shot`")
    return saved


def _gray(image):
    return cv2.cvtColor(np.asarray(image), cv2.COLOR_BGR2GRAY)


def screen_changed(saved, live, x, y):
    """Why the live screen no longer matches the saved frame at this point, or None."""
    a, b = _gray(saved), _gray(live)
    small = cv2.absdiff(cv2.resize(a, (240, 135), interpolation=cv2.INTER_AREA),
                        cv2.resize(b, (240, 135), interpolation=cv2.INTER_AREA))
    share = float(np.count_nonzero(small > SCREEN_DIFF)) / small.size
    if share > SCREEN_CHANGED:
        return f"the screen has changed since that frame ({share:.0%} of it differs)"
    hx, hy = PATCH
    x1, y1, x2, y2 = max(0, x - hx), max(0, y - hy), min(1920, x + hx), min(1080, y + hy)
    diff = float(cv2.absdiff(a[y1:y2, x1:x2], b[y1:y2, x1:x2]).mean())
    if diff > PATCH_DIFF:
        return f"the area around ({x}, {y}) has changed since that frame (difference {diff:.0f})"
    return None


def _dialog_problem(frame, popup, shown, x, y):
    """Why this point can't be pressed in the popup on top, or None. Its X always can."""
    if popup and popup[0] == "Notice":
        if abs(x - popup[1][0]) <= CLOSE_REACH and abs(y - popup[1][1]) <= CLOSE_REACH:
            return None
        text = nav._notice_text(frame)
        if not nav.notice_is_safe(text) and not watchdog.is_date_notice(text) and not watchdog.is_network_notice(text):
            return f"a Notice that isn't on the safe list: {text or '(unreadable)'}"
    elif not popup and (shown.endswith("+popup") or shown == "unknown"):
        words = " ".join(w.text for w in ocr.read(np.asarray(frame), nav.NOTICE_BODY))
        hit = DIALOG_WORDS.search(words)
        if hit:
            return f"an unrecognised popup mentioning '{hit.group(0)}'"
    return None


def forbidden(vision, frame, x, y, allow_spend=False):
    """The known spending or Never/Ask control at this point, or None. `allow_spend` admits only the
    catalog's allowed spends (AP, tickets). Never and Ask controls, shops and unsafe popups stay refused."""
    for bx, by in TOP_BAR_NEVER:
        if abs(x - bx) <= 40 and abs(y - by) <= 30:
            return "an AP/pyroxene refill or Settings button"
    popup = nav.top_popup(vision, frame)
    shown = label(vision, frame)
    screen = shown.replace("+popup", "")
    problem = _dialog_problem(frame, popup, shown, x, y)
    if problem:
        return problem
    if not popup and screen in SPEND_SCREENS and not (y < 110 and (x < 170 or x > 1800)):
        return f"the {screen} screen (only Back and Home are allowed there)"
    table = catalog.buttons_for(screen, popup[0] if popup else None)
    refused = (catalog.NEVER, catalog.ASK) if allow_spend else (catalog.NEVER, catalog.ASK, catalog.SPEND)
    for name, (bx, by, kind) in table.items():
        if kind in refused:
            hx, hy = BIG_BUTTONS.get(name, (75, 55))
            if abs(x - bx) <= hx and abs(y - by) <= hy:
                return f"'{name}' ({kind})"
    if not popup and screen == "student_detail":
        from .tasks import roster
        for name, (x1, y1, x2, y2) in roster.DANGER.items():
            if x1 <= x <= x2 and y1 <= y <= y2:
                return f"'{name}' on the student page"
    box = (max(0, x - 110), max(0, y - 45), min(1920, x + 110), min(1080, y + 45))
    words = " ".join(w.text for w in ocr.read(np.asarray(frame), box, scale=2))
    hit = (NEVER_WORDS if allow_spend else SPEND_WORDS).search(words)
    if hit:
        return f"a control reading '{hit.group(0)}'"
    return None


def verify(saved, live, vision, x, y):
    """Called by Game with the live frame it is about to act on."""
    changed = screen_changed(saved, live, x, y)
    if changed:
        raise ManualRefused(changed)
    bad = forbidden(vision, live, x, y)
    if bad:
        raise ManualRefused(f"({x}, {y}) is on {bad}")


def click(game, vision, x, y, path):
    if not (0 <= x < 1920 and 0 <= y < 1080):
        raise ManualRefused(f"({x}, {y}) is outside the 1920x1080 frame")
    saved = load(path)
    game.click_manual(saved, vision, x, y)   # raises if it wasn't sent
    view = game._local.view
    return {"clicked": [x, y], "target": "manual", "screen_xy": list(game._local.aim), "layout": view.info()}
