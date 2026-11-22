"""`look`: the game's state in one read. `press`: click a known button by name, checked.

look only captures. press is refused for anything on the Never list, needs a flag for buttons
that spend, and never repeats a click that could spend or claim twice.
"""
import json
import time
from pathlib import Path

import cv2

from . import inputguard, battlestate, buttons, catalog, nav, resources, viewport, watchdog
from .screens import label

ROOT = Path(__file__).resolve().parent.parent
LOOK_FRAME = ROOT / "captures" / "look.png"
CHANGE_POLLS = 16                # about 4 s for a press to show on screen


class PressError(RuntimeError):
    """The press was refused. Nothing was clicked."""


def state(vision, frame):
    popup = nav.top_popup(vision, frame)
    screen = label(vision, frame)
    if screen == "reward" and not vision.find(frame, "reward_confirm"):
        screen = "story_reward"          # the story's version has no buttons, only "continue"
    return screen, popup[0] if popup else None


def describe(vision, frame, save=True, window=None):
    if window:                           # a stretched frame can't be read: say so instead of guessing
        out = {"screen": "view_not_identified", "popup": None,
               "resources": {"ap": None, "credits": None, "pyroxene": None}, "buttons": [], "warning": window}
        halt = watchdog.halted()
        if halt:
            out["halted"] = halt
        return out
    screen, popup = state(vision, frame)
    base = screen.replace("+popup", "")
    buttons = catalog.buttons_for(base, popup)
    notice = nav._notice_text(frame) if popup == "Notice" else None
    unsafe_notice = popup == "Notice" and not nav.notice_is_safe(notice or "")
    if unsafe_notice:
        buttons = {}
    known = bool(buttons) and base != "unknown" if not popup else True
    path = None
    if save:
        LOOK_FRAME.parent.mkdir(exist_ok=True)
        cv2.imwrite(str(LOOK_FRAME), frame)
        path = str(LOOK_FRAME.relative_to(ROOT))
    out = {
        "screen": screen,
        "popup": popup,
        "resources": resources.read_any(frame),
        "buttons": [{"name": n, "x": b[0], "y": b[1], "class": b[2]} for n, b in buttons.items()]
        if not screen.endswith("+popup") or popup else [],
        "frame": path,
    }
    view = getattr(frame, "view_info", None)
    if view is not None:
        out["layout"] = view.info()
        if not view.native:
            out["layout"]["note"] = "unverified shape: only buttons matched on screen can be pressed"
    if screen == "in_battle":
        out["battle"] = battlestate.read(vision, frame, text=True)
    if notice is not None:
        out["popup_text"] = notice
    if unsafe_notice:
        out["warning"] = "this Notice isn't on the safe list: no buttons offered, the user decides"
    elif "+popup" in screen and not popup:
        out["warning"] = "an unrecognised popup is open: read the frame before clicking"
    elif not known:
        out["warning"] = "unrecognised screen: read the frame before clicking"
    halt = watchdog.halted()
    if halt:
        out["halted"] = halt
    return out


def _moved(a, b):
    small_a, small_b = (cv2.resize(x, (480, 270), interpolation=cv2.INTER_AREA) for x in (a, b))
    return float(cv2.absdiff(small_a, small_b).mean()) >= 2.0


def _wait_change(game, vision, before_frame, before_state):
    for _ in range(CHANGE_POLLS):
        game.sleep(0.25)
        frame = game.grab()
        if state(vision, frame) != before_state or _moved(before_frame, frame):
            game.sleep(0.5)              # let the transition finish before reading it
            return True, game.grab()
    return False, game.grab()


def press(game, vision, name, allow_spend=False, user_approved=False):
    """Press a known button on the current screen. Returns the new look plus what happened."""
    problem = getattr(game, "window_problem", lambda: None)()
    if problem:
        raise PressError(f"nothing was clicked: {problem}")
    frame = game.grab()
    before_state = state(vision, frame)
    screen, popup = before_state
    base = screen.replace("+popup", "")
    if "+popup" in screen and not popup:
        raise PressError("an unrecognised popup is open: nothing was clicked")
    if popup == "Notice":
        text = nav._notice_text(frame)
        if not nav.notice_is_safe(text):
            raise PressError(f"this Notice isn't on the safe list, nothing was clicked: {text or '(unreadable)'}")
    table = catalog.buttons_for(base, popup)
    where = popup or screen
    if not table:
        raise PressError(f"no known buttons on '{where}': nothing was clicked")
    if name not in table:
        raise PressError(f"'{name}' is not a button on '{where}'. Known: {', '.join(sorted(table))}")
    x, y, kind = table[name]
    if kind == catalog.NEVER:
        raise PressError(f"'{name}' is on the Never list: refused")
    if kind == catalog.ASK and not (user_approved and screen == "title"):
        raise PressError(f"'{name}' needs the user's decision first: refused")
    if kind == catalog.SPEND and not allow_spend:
        raise PressError(f"'{name}' spends a resource: pass --allow-spend if that's intended")

    scope = popup or base
    if not buttons.prove(vision, frame, scope, name, x, y):
        raise PressError(f"'{name}' couldn't be matched on screen at this window shape: nothing was clicked")
    before = resources.read_any(frame)
    if kind == catalog.ASK:
        hold = watchdog.login_hold()
        if hold:
            raise PressError(f"{hold.get('reason')}: on hold for the user. After they decide: "
                             "python -m ba_auto ctl clear-login-hold --note WHY")
        game.allow_title_once = True
    try:
        game.click(x, y)
    except inputguard.NotSent as e:
        raise PressError(f"nothing was clicked: {e}") from e
    finally:
        game.allow_title_once = False
    changed, after_frame = _wait_change(game, vision, frame, before_state)
    retried = False
    if not changed and kind == catalog.NAV:      # first clicks are sometimes swallowed
        if state(vision, after_frame) == before_state and buttons.prove(vision, after_frame, scope, name, x, y):
            if inputguard.try_click(game, x, y):
                retried = True
                changed, after_frame = _wait_change(game, vision, frame, before_state)
    result = describe(vision, after_frame)
    reason = watchdog.compare(before, result["resources"])
    if reason:
        result["halted"] = watchdog.halt(reason, values=result["resources"], button=name)
    result["pressed"] = {"name": name, "class": kind, "from": where, "changed": changed, "retried": retried}
    return result


def view_check(game, vision, out_path=None):
    """Read-only: where the game's picture is, and where CLICK TO START would land."""
    raw, left, top = game._capture()
    view = viewport.detect(raw, left, top)
    frame = view.compose(raw)
    out = {**view.info(), "canvas": [round(view.canvas_w), round(view.canvas_h)], "reason": view.reason}
    screen, popup = state(vision, frame) if view.confident else ("view_not_identified", None)
    out.update(screen=screen, popup=popup)
    marked = raw.copy()
    x, y, w, h = view.rect
    cv2.rectangle(marked, (x, y), (x + w - 1, y + h - 1), (0, 255, 0), 2)
    if screen == "title":
        px, py, _ = catalog.SCREENS["title"]["click_to_start"]
        proven = buttons.prove(vision, frame, "title", "click_to_start", px, py)
        from .viewport import TARGETS
        hit = TARGETS.confirm(frame, px, py) if proven and not view.native else None
        shift = hit[1] if hit else vision.shift_of(frame, "title_copyright")
        sx, sy = view.to_screen(px, py, shift)
        out["click_to_start"] = {"proven": proven, "screen": [sx, sy], "inside_picture": view.contains_screen(sx, sy),
                                 "game_is_under_point": game._point_is_game(sx, sy)}
        cv2.drawMarker(marked, (sx - left, sy - top), (0, 0, 255), cv2.MARKER_CROSS, 40, 2)
    path = Path(out_path) if out_path else ROOT / "captures" / "view_check.png"
    path.parent.mkdir(exist_ok=True)
    cv2.imwrite(str(path), marked)
    out["marked_frame"] = str(path)
    return out


def show(obj):
    print(json.dumps(obj, indent=1, ensure_ascii=False))
    return 0
