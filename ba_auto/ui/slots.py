"""Where the overlay card may sit, per game screen, in 1920x1080 reference pixels.

Every slot was checked by eye against that screen's saved frame in ui-map/ and captures/, and
tools/test_ui.py checks each one against the catalog's known buttons. A screen that isn't listed
gets no card: popups, battles, unknown screens and busy lists show only a thin status line along
the bottom edge of the game's picture while a run is active or input is halted.
"""

CARD_W, CARD_H = 320, 64
MIN_VIEW_W = 1280               # below this the card would be too small to read
BUTTON_MARGIN = 40              # the catalog stores button centres

SLOTS = {
    "alliance_formation": (8, 112), "battle_result": (8, 96), "battle_tip": (8, 224),
    "cafe": (8, 624), "campaign": (1584, 96), "defeat": (1584, 104),
    "episode_info": (8, 112), "episode_list": (8, 112), "event_recap": (8, 632),
    "formation_menu": (8, 112), "grand_assault": (1296, 176), "group_story": (8, 112),
    "items": (8, 208), "lesson": (8, 200), "lobby": (392, 176), "mail": (8, 512),
    "main_story": (8, 520), "ministory_list": (8, 112), "mission": (8, 112),
    "replay": (8, 112), "reward": (8, 96), "story": (8, 1000), "story_hub": (8, 984),
    "student_detail": (1584, 96), "students": (8, 104), "tactical_challenge": (8, 112),
    "tasks": (8, 104), "work_onboarding": (8, 976),
}


def card_rect(screen, view):
    """(x, y, w, h) in screen pixels for this game screen, or None for no card.

    `view` is the converter's viewport.View. Slots were checked on 16:9 frames. At any other
    shape the game moves its widgets, so the card can't promise to miss them and stays hidden.
    """
    slot = SLOTS.get(screen)
    if slot is None or view is None or not view.confident or not view.native:
        return None
    vw, vh = view.rect[2:]
    if vw < MIN_VIEW_W or vh <= 0:
        return None
    x, y = view.canvas_to_client(*slot)
    return (round(view.client[0] + x), round(view.client[1] + y), round(CARD_W * vw / 1920), round(CARD_H * vh / 1080))
