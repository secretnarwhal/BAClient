"""Screen identification. A screen matches when all of its templates are found.
Order matters: the first match wins, so more specific screens come first."""

SCREENS = [
    ("formation",     ["formation_title", "mobilize"]),
    ("alliance_formation", ["alliance_title", "mobilize"]),
    ("mobilize_unit", ["mobilize_unit_title"]),    # the story's multi-unit formation
    ("reward",        ["reward_acquired"]),    # drawn over Battle Complete, so it comes first
    ("battle_tip",    ["battle_tip"]),         # drawn over Defeat
    ("battle_result", ["battle_complete"]),
    ("battle_result", ["result_confirm", "result_stats"]),   # the title shows the stage behind it, these don't
    ("defeat",        ["battle_failed"]),
    ("story",         ["story_menu"]),
    ("occupy_dialog", ["conquest/start_battle", "conquest/occupy_base"]),
    ("conquest_map",  ["conquest/title", "conquest/manage_ark"]),
    ("in_battle",     ["bs_pause"]),
    ("in_battle",     ["battle_auto"]),
]


def _result_clear(vision, frame):
    """False when a popup (say, the date-change Notice) is dimming Battle Complete's buttons."""
    for name in ("result_stats", "result_confirm"):
        if vision.has(name) and vision.find(frame, name):
            return not _dimmed(vision, frame, name)
    return True


def detect(vision, frame):
    for screen, needed in SCREENS:
        if all(vision.has(n) for n in needed) and all(vision.find(frame, n) for n in needed):
            if screen == "battle_result" and not _result_clear(vision, frame):
                continue
            return screen
    return "unknown"


def _first(vision, frame, table):
    for screen, needed in table:
        if all(vision.has(n) for n in needed) and all(vision.find(frame, n) for n in needed):
            return screen
    return None


# Names for logs and the HUD only. Tasks branch on detect(), which these never change.
# The title screen, where a click logs in. Its art and the fading CLICK TO START change, so it's
# recognised by the Notice and Menu buttons, which don't.
TITLE = [("title", ["title/notice", "title/menu"]), ("title", ["title_menu", "title_copyright"])]
DIALOGS = TITLE + [("skip_dialog", ["skip_question"])]    # drawn over a screen detect() would name
MENUS = [
    ("lobby",          ["lobby_cafe", "lobby_campaign"]),
    ("campaign",       ["campaign_title"]),
    ("ministory_list", ["ministory_title"]),
    ("episode_info",   ["episode_info_title"]),
    ("episode_list",   ["episode_list_title"]),
    ("story_hub",      ["story_hub_mini"]),
    ("student_detail", ["roster_basic_tab"]),
    ("students",       ["roster_list_title"]),
    # by top-bar title; longer titles first, since "Event" is a prefix of two others
    *[(n, [f"title_{n}"]) for n in (
        "event_recap", "event_tasks", "event", "work_onboarding", "items", "attendance", "cafe", "lesson",
        "lesson_location", "formation_menu", "crafting", "shop", "recruit", "cash_shop", "tasks", "mail",
        "mission", "main_story", "group_story", "replay", "bounty", "commissions", "scrimmage",
        "total_assault", "joint_firing_drill", "grand_assault", "tactical_challenge", "final_restriction")],
]


DIMMABLE = {"formation": "formation_title", "alliance_formation": "alliance_title", "mobilize_unit": "mobilize_unit_title"}


def _dimmed(vision, frame, name):
    """A popup darkens what's behind it. Template scores ignore brightness, so compare it:
    across the saved frames, clear screens measure 1.0 and popups 0.28-0.48."""
    crop = vision.matched_crop(frame, name)
    if crop is None or crop.size == 0:
        return False
    return float(crop.mean()) < float(vision.images[name].mean()) * 0.75


def is_title(vision, frame):
    return bool(_first(vision, frame, TITLE))


def label(vision, frame):
    """The most specific name we have for this frame, for reading logs."""
    dialog = _first(vision, frame, DIALOGS)
    if dialog:
        return dialog
    screen = detect(vision, frame)
    if screen != "unknown":
        title = DIMMABLE.get(screen)     # these have a Mobilize button, and matching can't see a dialog over it
        return f"{screen}+popup" if title and vision.has(title) and _dimmed(vision, frame, title) else screen
    menu = _first(vision, frame, MENUS)
    if menu is None:
        return "unknown"
    return f"{menu}+popup" if _dimmed(vision, frame, dict(MENUS)[menu][0]) else menu


def scores(vision, frame):
    return {n: round(vision.match(frame, n)[0], 3) for n in sorted(vision.images)}
