"""Known buttons per screen and popup, each with what pressing it costs.

  nav    opens or closes something, costs nothing
  claim  collects a reward (allowed)
  spend  uses an allowed resource such as AP or a ticket; `press` needs --allow-spend
  ask    the user must decide first (never automatic)
  never  on the Never list; `press` always refuses

A button that isn't listed here can't be pressed through `press` at all.
"""

NAV, CLAIM, SPEND, ASK, NEVER = "nav", "claim", "spend", "ask", "never"

TOP_BAR = {
    "back": (85, 55, NAV),
    "home": (1855, 37, NAV),
    "ap_refill": (943, 36, NEVER),
    "pyroxene_refill": (1634, 36, NEVER),
    "settings": (1742, 37, NEVER),
}

_SCREENS = {
    "campaign": {
        "mission": (1150, 300, NAV), "story": (1740, 300, NAV), "bounty": (1110, 590, NAV),
        "commissions": (1075, 775, NAV), "scrimmage": (1060, 935, NAV), "total_assault": (1350, 610, NAV),
        "joint_firing_drill": (1545, 610, NAV), "grand_assault": (1745, 610, NAV),
        "tactical_challenge": (1305, 900, NAV), "final_restriction": (1520, 860, NAV), "event": (155, 235, NAV),
    },
    "mission": {
        "normal_tab": (1200, 229, NAV), "hard_tab": (1595, 229, NAV), "multi_sweep": (800, 890, NAV),
        "prev_area": (65, 540, NAV), "next_area": (1858, 540, NAV),
        "enter_1": (1678, 368, NAV), "enter_2": (1678, 545, NAV), "enter_3": (1678, 720, NAV),
    },
    "story_hub": {"main_story": (490, 500, NAV), "mini_story": (1170, 430, NAV),
                  "group_story": (1590, 430, NAV), "replay": (1690, 830, NAV)},
    "ministory_list": {"next_page": (1868, 538, NAV)},
    "group_story": {"next_page": (1868, 538, NAV)},
    "replay": {"relationship": (410, 580, NAV), "task_stories": (970, 580, NAV), "event_stories": (1530, 580, NAV)},
    "lesson": {"ticket_info": (1744, 167, NAV), "schale_office": (1300, 290, NAV)},
    "lesson_location": {"all_locations": (1746, 992, NAV), "prev_floor": (990, 810, NAV), "next_floor": (1890, 810, NAV)},
    "cafe": {
        "earnings": (1760, 985, NAV), "move_cafe": (205, 158, NAV),
        "invitation": (1325, 980, SPEND), "unannounced_visit": (105, 425, ASK), "store_all": (714, 960, NEVER),
    },
    "tasks": {
        "all_tab": (860, 168, NAV), "daily_tab": (1086, 168, NAV), "weekly_tab": (1314, 168, NAV),
        "achievement_tab": (1544, 168, NAV), "challenges_tab": (1768, 168, NAV),
        "daily_bonus_claim": (1458, 1000, CLAIM), "claim_all": (1724, 1002, CLAIM),
    },
    "mail": {"claim_all": (1700, 1002, CLAIM)},
    "bounty": {"overpass": (1420, 330, NAV), "desert_railroad": (1420, 515, NAV), "classroom": (1420, 705, NAV)},
    "commissions": {"base_defense": (1420, 320, NAV), "item_retrieval": (1420, 480, NAV)},
    "scrimmage": {"trinity": (1420, 315, NAV), "gehenna": (1420, 480, NAV), "millennium": (1420, 645, NAV)},
    "tactical_challenge": {"time_reward_claim": (528, 583, CLAIM), "daily_reward_claim": (528, 705, CLAIM)},
    "student_detail": {"next_student": (1873, 578, NAV), "prev_student": (45, 578, NAV)},
    "event": {"story_tab": (1218, 168, NAV), "quest_tab": (1610, 168, NAV), "event_tasks": (598, 1043, NAV),
              "event_shop": (416, 1043, ASK)},
    "event_tasks": {"claim_all": (1725, 1002, CLAIM)},
    "shop": {"cash_shop": (106, 1016, NEVER)},
}
for _name in ("event_recap", "work_onboarding", "items", "attendance", "formation_menu", "crafting", "recruit",
              "main_story", "total_assault", "joint_firing_drill", "grand_assault", "final_restriction", "students"):
    _SCREENS.setdefault(_name, {})

SCREENS = {name: {**TOP_BAR, **buttons} for name, buttons in _SCREENS.items()}
_MOBILIZE = {"mobilize": (1765, 988, SPEND)}    # starts a fight, and most fights cost AP
SCREENS["formation"] = {**TOP_BAR, **_MOBILIZE}
SCREENS["alliance_formation"] = {**TOP_BAR, **_MOBILIZE}
SCREENS["battle_result"] = {"confirm": (1755, 995, CLAIM)}
SCREENS["reward"] = {"confirm": (1158, 988, CLAIM), "go_to_lobby": (762, 988, CLAIM)}
SCREENS["story_reward"] = {"continue": (960, 945, CLAIM)}
SCREENS["defeat"] = {"confirm": (960, 987, CLAIM)}
SCREENS["battle_tip"] = {"confirm": (960, 987, CLAIM)}
SCREENS["in_battle"] = {"auto": (1815, 1015, CLAIM)}
SCREENS["title"] = {"click_to_start": (960, 938, ASK)}    # logs in, which can kick another device
SCREENS["cash_shop"] = {"back": TOP_BAR["back"], "home": TOP_BAR["home"]}
SCREENS["lobby"] = {
    "cafe": (150, 1030, NAV), "lesson": (318, 1030, NAV), "students": (490, 1030, NAV),
    "formation": (657, 1030, NAV), "social": (825, 1030, NAV), "crafting": (988, 1030, NAV),
    "shop": (1153, 1030, NAV), "campaign": (1800, 1015, NAV), "mail": (1730, 55, NAV),
    "menu_grid": (1838, 55, NAV), "notice": (75, 205, NAV), "momotalk": (205, 205, NAV),
    "attendance": (333, 205, NAV), "tasks": (75, 360, NAV), "event_recap": (1593, 960, NAV),
    "recruit": (1322, 1030, NEVER), "cash_shop": (205, 360, NEVER), "battle_pass": (528, 800, NEVER),
    "ap_refill": (945, 54, NEVER), "pyroxene_refill": (1523, 54, NEVER),
}

# Popups, keyed by the names in nav.POPUPS. "close" is added from there.
POPUPS = {
    "Mission Info": {
        "sweep_min": (1182, 495, NAV), "sweep_minus": (1285, 495, NAV), "sweep_plus": (1520, 495, NAV),
        "sweep_max": (1622, 495, NAV), "start_sweep": (1400, 657, SPEND), "start_mission": (1400, 865, SPEND),
    },
    "Cafe Earnings": {"claim": (960, 800, CLAIM)},
    "Boss Info": {"enter": (962, 868, SPEND)},
    "All Locations": {"theater": (446, 360, NAV), "gym": (962, 360, NAV), "library": (1476, 360, NAV),
                      "classroom": (446, 590, NAV), "laboratory": (962, 590, NAV)},
    "Location Info": {"start_lesson": (960, 832, SPEND)},
    "Menu Tab": {"items": (1184, 508, NAV), "options": (842, 384, NEVER), "account": (1184, 384, NEVER),
                 "restore_purchases": (1184, 632, NEVER)},
    "Battle Pass": {"unlock_growth": (260, 950, NEVER)},
}


def buttons_for(screen, popup):
    """The pressable buttons on the top layer: the open popup's if there is one."""
    if popup:
        from . import nav
        close = next((p[2] for p in nav.POPUPS if p[0] == popup), None)
        table = dict(POPUPS.get(popup, {}))
        if close:
            table["close"] = (*close, NAV)
        return table
    return SCREENS.get(screen, {})
