"""Get back to the lobby from anywhere, closing known popups on the way.

Safety rules:
  - The only things ever clicked are the Home button and a known popup's X or Cancel.
    Confirm, Claim, Start and every other button are never touched.
  - A popup is closed only if its title is on the allowlist below and it is the top layer
    (not dimmed behind another popup). Anything else stops the task for a person to read.
  - Story scenes, battles and result screens are left alone: leaving them isn't navigation.
"""
from . import buttons, inputguard, reconnect, watchdog
from .runtime import TitleScreen
from .screens import _dimmed, label

HOME = "nav_home"
HOME_WAIT = 4.0                  # screens swallow clicks while they load
POPUP_WAIT = 1.5
UNKNOWN_RETRIES = 4              # a frame mid-transition can look unknown for a moment
NOTICE_BODY = (590, 300, 1330, 700)
# "Notice" is a generic title. Only these bodies may be closed; anything else, or text that
# can't be read, is left for a person. Duplicate login is handled harder by watchdog.py.
SAFE_NOTICES = ("to be updated", "you can send an invite after the cooldown",
                "you will be taken to another user")

# (name, title template, where its X or Cancel is)
POPUPS = [
    ("Menu Tab",        "nav_menu_tab",      (1274, 266)),
    ("Select Roster",   "nav_select_roster", (770, 865)),    # Cancel
    ("MomoTalk",        "nav_momotalk",      (1680, 167)),
    ("Notice web view", "nav_web_notice",    (1662, 170)),
    ("Battle Pass",     "nav_battle_pass",   (1863, 62)),
    ("Notice",          "nav_notice",        (1328, 240)),
    ("Guide",           "nav_guide",         (1380, 272)),
    ("Lesson Ticket",   "nav_ticket_info",   (1321, 221)),
    ("Location Info",   "nav_location_info", (1444, 162)),
    ("Mission Info",    "nav_mission_info",  (1691, 157)),
    ("Multi-Sweep",     "nav_multi_sweep",   (1832, 140)),
    ("Cafe Earnings",   "nav_cafe_earnings", (1474, 212)),
    ("Planned Visit",   "nav_planned_visit", (1474, 128)),
    ("Inventory",       "nav_inventory",     (1713, 150)),
    ("Presets",         "nav_presets",       (1594, 158)),
    ("All Locations",   "nav_all_locations", (1706, 147)),
    ("Relationship Rank Buff", "nav_relationship", (1529, 264)),
    ("Boss Info",       "nav_boss_info",     (1383, 147)),
]

# Never navigated away from: a person or a task has to finish these first.
HANDS_OFF = {"title", "story", "skip_dialog", "in_battle", "battle_result", "reward", "defeat", "battle_tip",
             "occupy_dialog", "conquest_map"}


class NavError(RuntimeError):
    """Navigation stopped on something it won't touch. The message says what."""


class OnTitle(NavError, TitleScreen):
    """Navigation found the title screen, which the launcher handles."""


def top_popup(vision, frame):
    """(name, close point) of the known popup on top, or None."""
    for name, template, close in POPUPS:
        if vision.find(frame, template) and not _dimmed(vision, frame, template):
            return name, close
    return None


def _notice_text(frame):
    """What a Notice says, for the log. It's game text: recorded, never acted on."""
    try:
        from . import ocr
        return " ".join(w.text for w in ocr.read(frame, NOTICE_BODY))[:300]
    except Exception:
        return ""


def notice_is_safe(text):
    text = " ".join(text.lower().split())
    return any(s in text for s in SAFE_NOTICES)


def go_lobby(game, vision, log=None, max_steps=16):
    """Return to the lobby. Returns how many clicks it took; raises NavError if it can't."""
    log = log or game.log
    clicks = unknown = 0
    for _ in range(max_steps):
        frame = game.grab()
        here = label(vision, frame)
        if here == "lobby":
            log(f"nav: at the lobby ({clicks} clicks)")
            return clicks
        popup = top_popup(vision, frame)
        if popup:
            body = _notice_text(frame) if popup[0] == "Notice" else ""
            if popup[0] == "Notice" and watchdog.is_date_notice(body):
                raise OnTitle("the date changed and the game is returning to the title: log in first")
            if popup[0] == "Notice" and watchdog.is_network_notice(body):
                reconnect.answer(game, frame, log)
                clicks += 1
                unknown = 0
                continue
            if popup[0] == "Notice" and not notice_is_safe(body):
                raise NavError(f"unrecognised Notice, not closing it: {body or '(unreadable)'}")
            log(f"nav: closing {popup[0]}" + (f": {body}" if body else ""))
            if not buttons.prove(vision, frame, popup[0], "close", *popup[1]):
                raise NavError(f"can't confirm {popup[0]}'s close button at this window shape")
            if not inputguard.try_click(game, *popup[1]):    # the journal keeps this frame too
                continue
            clicks += 1
            unknown = 0
            game.sleep(POPUP_WAIT)
            continue
        if here == "title":
            raise OnTitle("on 'title': log in first")
        if here in HANDS_OFF:
            raise NavError(f"on '{here}': finish or skip it first")
        home = vision.find(frame, HOME)
        if home and not _dimmed(vision, frame, HOME):
            log(f"nav: Home from {here}")
            if not inputguard.try_click(game, *home):
                continue
            clicks += 1
            unknown = 0
            game.sleep(HOME_WAIT)
            continue
        unknown += 1
        if unknown >= UNKNOWN_RETRIES:
            raise NavError(f"no safe way back from this screen ({here}): "
                           "an unrecognised popup or a screen without Home")
        game.sleep(POPUP_WAIT)
    raise NavError(f"did not reach the lobby in {max_steps} steps")
