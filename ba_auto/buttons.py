"""Proof that a known fixed button is really on screen, for layouts other than 16:9.

At 16:9 the catalog's coordinates are verified. At any other shape Game only clicks a point tied
to a template matched in the current frame. These helpers tie a catalog button to its own small
template (built by tools/cut_button_templates.py from real captures).
"""
import re

from .viewport import TARGETS

# Targets with no art of their own, placed relative to something stable that is matched.
DERIVED = {("title", "click_to_start"): (("title_copyright", (0, -105)), ("title/menu", (862, -20)))}


def key(scope, name):
    return f"btn__{re.sub(r'[^a-z0-9]+', '_', scope.lower())}__{name}"


def _is_native(frame):
    view = getattr(frame, "view_info", None)
    return view is None or view.native


def tie(vision, frame, template, x, y, anchor=None, reach=700, center=None):
    """Allow a click at (x, y) because `template` is matched in this frame. True if it is.

    The point moves with the template's anchor, or with `anchor` ("top", "center" or "bottom")
    when the point belongs to a different UI group. `center` names one of several matches (from
    find_all), which must still be there; by default it's the best match. Call it right before
    Game.click."""
    best = vision.find(frame, template)
    if not best:
        return False
    if center is None:
        center = best
    elif not vision.matches_at(frame, template, center, vision.shift_of(frame, template), 8):
        return False
    if abs(center[0] - x) > reach * 2 or abs(center[1] - y) > reach:
        return False
    if _is_native(frame):
        return True
    shift = vision.shift_of(frame, template)
    if anchor is not None:
        view = frame.view_info
        shift = (shift[0], {"top": 0, "center": round(view.dy / 2), "bottom": round(view.dy)}[anchor])
    # The proof stays the template at its own place; only the click point takes the anchor.
    TARGETS.note(vision, template, center, point=(x, y), shift=vision.shift_of(frame, template), click_shift=shift)
    return True


def prove(vision, frame, scope, name, x, y):
    """True if (x, y) may be clicked in this frame. Always true at 16:9."""
    if _is_native(frame):
        return True
    if (scope, name) in DERIVED:
        for template, (ox, oy) in DERIVED[(scope, name)]:
            center = vision.find(frame, template)
            if center and abs(center[0] + ox - x) <= 24 and abs(center[1] + oy - y) <= 24:
                TARGETS.note(vision, template, center, point=(x, y), shift=vision.shift_of(frame, template))
                return True
        return False
    for candidate in (key(scope, name), key("top", name)):
        if not vision.has(candidate):
            continue
        center = vision.find(frame, candidate)
        if center and abs(center[0] - x) <= 16 and abs(center[1] - y) <= 16:
            TARGETS.note(vision, candidate, center, point=(x, y), shift=vision.shift_of(frame, candidate))
            return True
    return False
