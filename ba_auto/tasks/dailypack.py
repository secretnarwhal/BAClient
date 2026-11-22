"""Claim the Cash Shop's Free Daily Pack. This is the only thing the client may do in the Cash Shop.

  python -m ba_auto.tasks.dailypack              dry run: opens General > Daily, reports, claims nothing
  python -m ba_auto.tasks.dailypack --recon      presses the card once, never confirms a dialog
  python -m ba_auto.tasks.dailypack --go         claims it

Safety:
  - Every click goes through Route, which allows only: the lobby's Cash Shop, General, this card, Home,
    and the Confirm of a Reward Acquired popup. The other shop tabs and the top bar's buttons are refused.
  - The card is pressed only if one fresh frame shows General > Daily and a card reading exactly
    "Free Daily Pack", priced exactly "Free", with at least 1 claim left today, and not dimmed.
  - It is pressed at most once per run and never retried.
  - No confirm dialog is accepted until its exact text has been recorded in DIALOG_TEXTS. Anything else
    stops the run, after pressing Cancel if there is plainly one.
  - Afterwards the counter must read 0, pyroxene must be unchanged, and AP and credits must have risen.
    A pyroxene change halts all input until a person clears it.
"""
import argparse
import json
import re
import time

import cv2
import numpy as np

from .. import buttons, journal, nav, resources, viewport, watchdog
from ..screens import label
from ..vision import Vision
from . import claims
from .claims import ClaimStop, Pack, centre_shift, place, read_text, stop, words_at

PACK_NAME = "Free Daily Pack"
PRICE = "Free"
CAPTURES = journal.ROOT / "captures" / "daily_pack"

# Exact dialog text, lower case with single spaces, added only after a recon has recorded it.
DIALOG_TEXTS = ()
PAYMENT = re.compile(r"usd|eur\b|gbp|jpy|krw|[$€£¥₩]|\d+[.,]\d{2}\b|pyroxene|payment|\bpay\b|steam|wallet|billing"
                     r"|\bprice\b|\bcost\b|\bcharge|\btax\b|credit card|checkout", re.I)
COUNTER = re.compile(r"can purchase (\S+) time ?\(s\) a day")     # OCR sometimes spaces "time (s)"

POLL = 0.5
LOAD_POLLS = 24
AFTER_POLLS = 16
ODD_POLLS = 4
VERIFY_POLLS = 16
SLOT_TOLERANCE = 30              # px the card may sit from its captured place
TITLE_BRIGHT = 200               # the claimed, dimmed card measures 129
PRICE_BRIGHT = 170               # the claimed, dimmed card measures 119

TITLE_AT = (577, 552)            # where card_title was cut; the boxes below move with it
COUNTER_BOX = (405, 468, 750, 500)
TITLE_BOX = (405, 520, 750, 585)
PRICE_BOX = (405, 608, 750, 662)
DIALOG_BOX = (300, 120, 1620, 960)
REWARD_BAND = (200, 170, 1720, 760)

ZONES = {
    "lobby_cash": (150, 360, 262, 425),
    "general": (44, 438, 350, 522),
    "pack": (399, 590, 755, 700),
    "confirm": (500, 500, 1420, 960),
    "cancel": (500, 500, 1420, 960),
}
HOME = (1820, 5, 1890, 70)
REWARD_CONFIRM = (560, 900, 1360, 1060)
# Never clickable once in the shop: every other side tab, Weekly and Monthly, and the top bar's buttons.
FORBIDDEN = ((30, 118, 356, 438), (30, 522, 356, 860), (1446, 110, 1900, 190),
             (920, 10, 970, 65), (1610, 10, 1660, 65), (1715, 10, 1770, 65))
TEMPLATES = ("lobby_cash", "general_off", "general_on", "daily_on", "weekly_off", "monthly_off", "card_title")


def _inside(point, box):
    return box[0] <= point[0] < box[2] and box[1] <= point[1] < box[3]


class Route:
    """The only way this task clicks. nav and the reward popup get Home and the reward's Confirm."""

    def __init__(self, game):
        self.game, self.rt = game, game.rt
        self.pack_pressed = self.confirm_pressed = self.reward_open = False

    def grab(self):
        return self.game.grab()

    def sleep(self, seconds):
        self.game.sleep(seconds)

    def log(self, message):
        self.game.log(message)

    def click(self, x, y):
        point = (x, y)
        if any(_inside(point, b) for b in FORBIDDEN):
            raise ClaimStop(f"daily pack: refused a click on a forbidden control at {point}")
        if _inside(point, HOME) or (self.reward_open and _inside(point, REWARD_CONFIRM)):
            self.game.click(x, y)
            return
        raise ClaimStop(f"daily pack: refused a click outside the route at {point}")

    def press(self, kind, point):
        point = (point[0], point[1])
        if not _inside(point, ZONES[kind]):
            raise ClaimStop(f"daily pack: {kind} click at {point} is outside its zone")
        if kind != "lobby_cash" and any(_inside(point, b) for b in FORBIDDEN):
            raise ClaimStop(f"daily pack: refused a click on a forbidden control at {point}")
        if kind == "pack":
            if self.pack_pressed:
                raise ClaimStop("daily pack: the card may be pressed only once per run")
            self.pack_pressed = True
        elif kind == "confirm":
            if self.confirm_pressed or not self.pack_pressed:
                raise ClaimStop("daily pack: refused a second or unprompted Confirm")
            self.confirm_pressed = True
        self.game.click(*point)


# ---- reading ----

def read_resources(frame):
    return resources.read(frame)


def parse_counter(text):
    """Claims left today. None unless the whole line reads as the daily counter."""
    m = COUNTER.fullmatch(" ".join(text.lower().split()))
    if not m:
        return None
    token = m.group(1).replace("o", "0")      # OCR reads a lone 0 as the letter O
    return int(token) if token.isdigit() else None


def on_daily_tab(frame, vision, pack):
    """General > Daily, clear of popups, with the other two period tabs visibly off."""
    if label(vision, frame) != "cash_shop" or nav.top_popup(vision, frame):
        return False
    return all(pack.find(frame, n) for n in ("general_on", "daily_on", "weekly_off", "monthly_off"))


def _brightness(frame, box, shift):
    crop = viewport.crop(frame, box, shift)
    if crop.size == 0:
        return 0.0
    return float(np.median(cv2.cvtColor(np.ascontiguousarray(crop), cv2.COLOR_BGR2HSV)[:, :, 2]))


def read_card(frame, pack):
    """Everything about the card, from this one frame."""
    found = pack.find(frame, "card_title")
    if not found:
        return {"found": False}
    if max(abs(found[0] - TITLE_AT[0]), abs(found[1] - TITLE_AT[1])) > SLOT_TOLERANCE:
        return {"found": False}
    shift = pack.shift_of(frame, "card_title")
    title_box, price_box = place(TITLE_BOX, found, TITLE_AT), place(PRICE_BOX, found, TITLE_AT)
    prices = [" ".join(w.text for w in words_at(frame, price_box, shift, scale)) for scale in (2, 3)]
    prices = [p for p in prices if p]
    return {"found": True, "title": read_text(frame, title_box, shift),
            "price": prices[0] if prices and all(p == prices[0] for p in prices) else (" | ".join(prices) or None),
            "counter_text": read_text(frame, place(COUNTER_BOX, found, TITLE_AT), shift),
            "title_brightness": _brightness(frame, title_box, shift),
            "price_brightness": _brightness(frame, price_box, shift),
            "press_at": ((price_box[0] + price_box[2]) // 2, (price_box[1] + price_box[3]) // 2)}


def judge(card):
    """('claimable' | 'claimed' | 'refused', why)."""
    if not card.get("found"):
        return "refused", "the Free Daily Pack card isn't in its slot"
    if card["title"] != PACK_NAME:
        return "refused", f"the card's title reads {card['title']!r}, not {PACK_NAME!r}"
    left = parse_counter(card["counter_text"])
    if left is None:
        return "refused", f"can't read the daily counter: {card['counter_text']!r}"
    if left == 0:
        return "claimed", "already claimed today"
    if card["price"] != PRICE:
        return "refused", f"the price reads {card['price']!r}, not exactly {PRICE!r}"
    if card["title_brightness"] < TITLE_BRIGHT or card["price_brightness"] < PRICE_BRIGHT:
        return "refused", "the card is dimmed"
    return "claimable", f"{left} claim(s) left today"


def dialog_ok(text):
    """Only a recorded dialog that names this pack and has no payment wording."""
    clean = " ".join(text.lower().split())
    return clean in DIALOG_TEXTS and PACK_NAME.lower() in clean and not PAYMENT.search(clean)


def _public(card):
    return {k: v for k, v in card.items() if k != "press_at"}


def _save(frame, tag):
    CAPTURES.mkdir(parents=True, exist_ok=True)
    path = CAPTURES / f"{tag}_{time.strftime('%Y%m%d_%H%M%S')}.png"
    cv2.imwrite(str(path), np.asarray(frame))
    return str(path.relative_to(journal.ROOT))


def _left_number(ap):
    m = re.fullmatch(r"(\d+)/\d+", str(ap))
    return int(m.group(1)) if m else None


# ---- route ----

def open_daily(route, vision, pack, report):
    nav.go_lobby(route.game, vision, route.log)         # its own allowlist: Home and known popups' X
    frame = route.grab()
    for _ in range(2):
        frame = route.grab()
        point = pack.find(frame, "lobby_cash")
        if label(vision, frame) != "lobby" or nav.top_popup(vision, frame) or not point:
            stop(route, frame, "daily pack: can't find Cash Shop on a clear lobby", report)
        route.press("lobby_cash", point)
        for _ in range(LOAD_POLLS):
            route.sleep(POLL)
            frame = route.grab()
            if label(vision, frame) == "cash_shop" and not nav.top_popup(vision, frame):
                break
        else:
            if label(vision, frame) == "lobby":
                continue                                # a swallowed click
            stop(route, frame, "daily pack: the Cash Shop didn't open cleanly", report)
        break
    else:
        stop(route, frame, "daily pack: the Cash Shop didn't open", report)
    route.sleep(POLL)
    frame = route.grab()
    if not pack.find(frame, "general_on"):
        for _ in range(2):
            frame = route.grab()
            point = pack.find(frame, "general_off")
            if not point or label(vision, frame) != "cash_shop":
                stop(route, frame, "daily pack: can't find the General tab", report)
            route.press("general", point)
            for _ in range(12):
                route.sleep(POLL)
                frame = route.grab()
                if pack.find(frame, "general_on"):
                    break
            else:
                continue
            break
        else:
            stop(route, frame, "daily pack: General didn't open", report)
    for _ in range(12):
        route.sleep(POLL)
        frame = route.grab()
        if on_daily_tab(frame, vision, pack):
            route.sleep(POLL)
            frame = route.grab()
            if on_daily_tab(frame, vision, pack):
                return frame
    stop(route, frame, "daily pack: this isn't General > Daily, and only that tab may be used", report)


def _cancel_if_plain(route, frame):
    """Press Cancel if the dialog plainly has exactly one. Needs a true 16:9 frame to prove the point."""
    view = getattr(frame, "view_info", None)
    if view is not None and not view.native:
        return False
    words = [w for w in words_at(frame, ZONES["cancel"], centre_shift(frame)) if w.text == "Cancel"]
    if len(words) != 1:
        return False
    word = words[0]
    route.press("cancel", (int(word.x + word.w / 2), int(word.y + word.h / 2)))
    return True


def _dialog(route, vision, pack, frame, mode, report):
    """An unexpected or recorded dialog. Returns True only if it was confirmed."""
    text = read_text(frame, DIALOG_BOX, centre_shift(frame))
    report["dialog"] = {"text": text[:400], "frame": _save(frame, "dialog")}
    if mode == "go" and not route.confirm_pressed and dialog_ok(text) and pack.find(frame, "dialog_title"):
        button = pack.find(frame, "dialog_confirm")
        if button:
            route.press("confirm", button)
            return True
    report["dialog"]["cancelled"] = _cancel_if_plain(route, frame)
    what = "recon: dialog recorded" if mode == "recon" else "unrecorded dialog"
    stop(route, frame, f"daily pack: {what}, not confirmed: {text[:200]}", report)


def _reward(route, vision, claim_pack, frame, report):
    text = read_text(frame, REWARD_BAND, vision.shift_of(frame, "reward_acquired"))
    report["reward"] = text[:300]
    if PAYMENT.search(text):
        report["dialog"] = {"text": text[:400], "frame": _save(frame, "reward_payment")}
        stop(route, frame, f"daily pack: payment or pyroxene wording on the reward: {text[:200]}", report)
    route.reward_open = True
    try:
        claims.acknowledge(route, vision, claim_pack, frame, report, "daily pack")
    finally:
        route.reward_open = False


def _await_result(route, vision, pack, claim_pack, mode, report):
    odd = 0
    for _ in range(AFTER_POLLS):
        route.sleep(POLL)
        frame = route.grab()
        if vision.find(frame, "reward_acquired"):
            _reward(route, vision, claim_pack, frame, report)
            return
        if on_daily_tab(frame, vision, pack):
            odd = 0
            card = read_card(frame, pack)
            if card.get("found") and parse_counter(card["counter_text"]) == 0:
                return
            continue
        odd += 1
        if odd >= ODD_POLLS:
            _dialog(route, vision, pack, frame, mode, report)
            odd = 0
    stop(route, route.grab(), "daily pack: the press changed nothing. Not retrying", report)


def _verify(route, vision, pack, before, report):
    after, last, frame = None, None, None
    for _ in range(VERIFY_POLLS):
        route.sleep(POLL)
        frame = route.grab()
        if not on_daily_tab(frame, vision, pack):
            continue
        now = read_resources(frame)
        if now.get("pyroxene") is not None and now["pyroxene"] != before["pyroxene"] and now == last:
            after = now
            break
        if all(v is not None for v in now.values()) and now == last:
            after = now
            break
        last = now
    frame = route.grab() if frame is None else frame
    report["after"] = after
    if after is None:
        stop(route, frame, "daily pack: couldn't read the resources after claiming, so the claim is unverified", report)
    if after["pyroxene"] != before["pyroxene"]:
        reason = f"daily pack: pyroxene changed from {before['pyroxene']} to {after['pyroxene']}"
        watchdog.halt(reason, task="daily-pack")
        stop(route, frame, reason, report)
    card = read_card(frame, pack)
    if not card.get("found") or parse_counter(card["counter_text"]) != 0:
        stop(route, frame, f"daily pack: the counter isn't 0 after claiming: {card.get('counter_text')!r}", report)
    if None in (after["ap"], after["credits"]) or _left_number(after["ap"]) <= _left_number(before["ap"]) \
            or after["credits"] <= before["credits"]:
        stop(route, frame, f"daily pack: AP and credits didn't both rise: {before} -> {after}", report)


def run(game, vision=None, mode="dry"):
    """mode: 'dry', 'recon' or 'go'. Returns a report; raises ClaimStop, with `.report`, on any stop."""
    if mode not in ("dry", "recon", "go"):
        raise ValueError("daily pack mode must be dry, recon or go")
    vision = vision or Vision(game.rt)
    pack, claim_pack = Pack("dailypack", game.rt), Pack("claims", game.rt)
    missing = [n for n in TEMPLATES if not pack.has(n)] + ([] if claim_pack.has("reward_confirm") else ["reward_confirm"])
    if missing:
        raise ClaimStop(f"daily pack: templates missing: {', '.join(missing)}")
    report = {"mode": mode, "state": None, "why": None, "card": None, "pressed": False, "result": None}
    route = Route(game)
    previous, game.rt.task = game.rt.task, f"Daily pack ({mode})"
    try:
        frame = open_daily(route, vision, pack, report)
        card = read_card(frame, pack)
        report["card"] = _public(card)
        report["state"], report["why"] = judge(card)
        game.log(f"daily pack: {report['state']}: {report['why']}")
        if report["state"] == "refused":
            report["frame"] = _save(frame, "refused")
            stop(route, frame, f"daily pack: {report['why']}", report)
        if report["state"] == "claimed":
            report["result"] = "already claimed today"
        elif mode == "dry":
            report["frame"] = _save(frame, "unclaimed")
            report["result"] = "would claim"
        else:
            frame = route.grab()                        # every check again, on the frame being acted on
            card = read_card(frame, pack)
            state, why = judge(card)
            if not on_daily_tab(frame, vision, pack) or state != "claimable":
                stop(route, frame, f"daily pack: changed before pressing: {why}", report)
            before = read_resources(frame)
            report["before"] = before
            if any(v is None for v in before.values()) or _left_number(before["ap"]) is None:
                stop(route, frame, "daily pack: can't read AP, credits and pyroxene, so a claim couldn't be verified",
                     report)
            if not buttons.tie(pack, frame, "card_title", *card["press_at"]):
                stop(route, frame, "daily pack: can't prove the card's position", report)
            report["frame"] = _save(frame, "before_press")
            report["pressed"] = True
            game.log("daily pack: pressing the card, once")
            route.press("pack", card["press_at"])
            _await_result(route, vision, pack, claim_pack, mode, report)
            _verify(route, vision, pack, before, report)
            report["result"] = "claimed"
        nav.go_lobby(route, vision, game.log)
    except ClaimStop as e:
        e.report = report
        raise
    finally:
        game.rt.task = previous
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ba_auto.tasks.dailypack")
    ap.add_argument("--go", action="store_true", help="really claim; without it this is a dry run")
    ap.add_argument("--recon", action="store_true", help="press the card once and never confirm a dialog")
    a = ap.parse_args(argv)
    if a.go and a.recon:
        ap.error("choose --go or --recon")
    from ..game import Game
    try:
        result = run(Game(), mode="go" if a.go else "recon" if a.recon else "dry")
    except ClaimStop as e:
        print(json.dumps({"stopped": str(e), "report": e.report}, indent=1, ensure_ascii=False))
        return 2
    print(json.dumps(result, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
