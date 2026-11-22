"""Mission sweeper: read every stage from the Multi-Sweep list, then sweep the marked ones.

The Multi-Sweep popup lists all stages with their type, remaining count and rewards,
so it is the single source for both scanning and sweeping.

Spending rules, enforced here rather than in the UI:
  * Only AP is ever spent. If a dialog mentions pyroxene, buying or a refill, the task
    stops without clicking anything.
  * The AP preview must equal the exact cost of the marked stages.
  * The game's "Sweep Content" dialog must list exactly the marked stages and counts,
    otherwise it is cancelled.
  * Presets are never saved. Work happens on preset tab 7.
"""
import json
import os
import re
import threading
import time
from pathlib import Path

import cv2

from .. import ocr

ROOT = Path(__file__).resolve().parent.parent.parent
DATA = ROOT / "data" / "sweeper"
ICONS = DATA / "icons"
MISSIONS = DATA / "missions.json"
MARKS = DATA / "marks.json"

LIST_TOP, LIST_BOTTOM = 396, 807
CONTENT_TOP, CONTENT_BOTTOM = 368, 816
STRIP_X = {0: 80, 12: 92}         # layout shift (no scrollbar = +12) -> x of the row's colour strip
SLOT0_X, SLOT_PITCH, SLOT_COUNT = 842, 95, 8
AP_COST = {"Hard": 20, "Normal": 10}

OPEN_MULTI = (800, 890)
CAMPAIGN_MISSION = (1150, 300)
MISSION_INFO_CLOSE = (1691, 157)
HARD_TAB = (1595, 229)
AREA_PREV, AREA_NEXT = (65, 540), (1858, 540)
TAB_7 = (1613, 236)
CLOSE = (1832, 140)
UNDO_ALL = (1728, 340)
PLUS, MINUS = (1127, 898), (899, 898)
SWEEP = (1668, 897)
CONTENT_CANCEL, CONTENT_CONFIRM = (770, 888), (1150, 888)
COMPLETE_CONFIRM = (960, 885)
CHECK_X = 1750
HARD_ROW_Y = (373, 549, 725)      # Mission screen, Hard tab: row centres

ID_RE = re.compile(r"(\d{1,2})\s*[-_.~–—]\s*([0-9A-Ca-c])")
REMAIN_RE = re.compile(r"(\d)\s*/\s*(\d)")
FORBIDDEN = ("pyroxene", "purchase", "recharge", "refill", "insufficient", "buy")
MAX_VIEWS = 160


# ------------------------------------------------------------------ reading the list
def _row_spans(frame, list_top=LIST_TOP, list_bottom=LIST_BOTTOM):
    """[(top, bottom, shift)] of rows fully visible in a list."""
    for shift, x in STRIP_X.items():
        strip = frame[list_top:list_bottom, x:x + 3]
        on = cv2.cvtColor(strip, cv2.COLOR_BGR2HSV)[:, :, 1].mean(axis=1) > 60
        spans, start = [], None
        for i, v in enumerate(list(on) + [False]):
            if v and start is None:
                start = i
            elif not v and start is not None:
                if i - start >= 105 and start > 1 and i < len(on) - 1:
                    spans.append((list_top + start, list_top + i, shift))
                start = None
        if spans:
            return spans
    return []


def _slots(frame, top, shift):
    """One entry per reward slot: (x1, y1, x2, y2, badged). Empty slots are left out."""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    out = []
    for i in range(SLOT_COUNT):
        cx = SLOT0_X + shift + SLOT_PITCH * i
        tile = cv2.cvtColor(frame[top + 30:top + 90, cx - 34:cx + 34], cv2.COLOR_BGR2GRAY)
        if tile.std() < 7:
            continue
        band = hsv[top + 10:top + 17, cx - 40:cx + 40, 1]
        out.append((cx - 47, top + 8, cx + 47, top + 106, bool((band > 90).mean() > 0.12)))
    return out


def _kind(frame, top, shift):
    h = cv2.cvtColor(frame[top + 72:top + 84, 270 + shift:285 + shift], cv2.COLOR_BGR2HSV)[:, :, 0]
    return "Normal" if 90 <= float(cv2.mean(h)[0]) <= 135 else "Hard"


def parse_view(frame):
    """Every fully visible row in a Multi-Sweep frame."""
    rows = []
    for top, bottom, shift in _row_spans(frame):
        dark = frame[top + 60, 92 + shift].mean() < 120     # greyed out: used up or locked
        view = cv2.convertScaleAbs(frame, alpha=2.8) if dark else frame
        words = ocr.read(view, (100 + shift, top, 640 + shift, top + 60), scale=3)
        m = ID_RE.search(ocr.text_in(words, 100 + shift, top, 255 + shift, top + 58).replace(" ", ""))
        if not m:
            continue
        kind = _kind(frame, top, shift)
        rem = REMAIN_RE.search(ocr.text_in(words, 260 + shift, top, 640 + shift, top + 58))
        text = " ".join(w.text for w in ocr.read(frame, (700, top, 1600, bottom))) if dark else ""
        locked = "Required" in text
        remaining = int(rem.group(1)) if rem else None
        used = "Used" in text or (kind == "Hard" and remaining == 0)
        slots = _slots(frame, top, shift)
        eleph = None
        if kind == "Hard" and not locked:
            eleph = next((i for i, s in enumerate(slots) if not s[4]), None)
        rows.append({
            "id": f"{int(m.group(1))}-{m.group(2).upper()}", "area": int(m.group(1)),
            "stage": m.group(2).upper(), "kind": kind,
            "remaining": remaining if kind == "Hard" else None,
            "limit": int(rem.group(2)) if rem and kind == "Hard" else None,
            "status": "locked" if locked else "used" if used else "ready",
            "top": top, "shift": shift, "slots": slots, "eleph": eleph,
        })
    return rows


def parse_content(frame):
    """{key: sweep count} for the rows visible in the Sweep Content dialog."""
    out = {}
    for top, _bottom, shift in _row_spans(frame, CONTENT_TOP, CONTENT_BOTTOM):
        words = ocr.read(frame, (100 + shift, top, 640 + shift, top + 60), scale=3)
        m = ID_RE.search(ocr.text_in(words, 100 + shift, top, 255 + shift, top + 58).replace(" ", ""))
        c = re.search(r"Count\D*(\d+)", ocr.text_in(words, 260 + shift, top, 640 + shift, top + 58))
        if m:
            kind = _kind(frame, top, shift)
            out[f"{kind[0]}{int(m.group(1))}-{m.group(2).upper()}"] = int(c.group(1)) if c else None
    return out


def key(row):
    return f"{row['kind'][0]}{row['id']}"


def cost(k):
    return AP_COST["Hard" if k.startswith("H") else "Normal"]


def sort_key(row):
    stage = row["stage"]
    return row["area"], row["kind"] != "Normal", (int(stage) if stage.isdigit() else 100 + ord(stage))


def _title(frame):
    return " ".join(w.text for w in ocr.read(frame, (500, 90, 1420, 200)))


def _forbidden(frame):
    text = " ".join(w.text for w in ocr.read(frame, (250, 90, 1670, 990))).lower()
    return next((w for w in FORBIDDEN if w in text), None)


def _header_ap(frame):
    m = re.search(r"(\d+)\s*/\s*\d+", " ".join(w.text for w in ocr.read(frame, (755, 12, 890, 60), scale=3)))
    return int(m.group(1)) if m else None


def _number_reads(frame, box):
    """Readings of a small number area. OCR drops short text near an image edge, so the
    crop gets a margin, and several sizes are tried."""
    x1, y1, x2, y2 = box
    gray = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
    padded = cv2.cvtColor(cv2.copyMakeBorder(gray, 30, 30, 30, 30, cv2.BORDER_REPLICATE), cv2.COLOR_GRAY2BGR)
    for scale in (3, 2, 4, 5):
        yield " ".join(w.text for w in ocr.read(padded, scale=scale))


def _preview(frame):
    """(AP now, AP after) from the bubble over the Multi-Sweep button."""
    for text in _number_reads(frame, (1680, 815, 1830, 855)):
        nums = re.findall(r"\d+", text)
        if len(nums) == 2 and int(nums[1]) <= int(nums[0]):
            return int(nums[0]), int(nums[1])
    # the arrow between the numbers can break a whole-bubble read: read each side alone
    sides = []
    for box in ((1682, 815, 1740, 855), (1770, 815, 1830, 855)):
        side = next((n for t in _number_reads(frame, box) if len(n := re.findall(r"\d+", t)) == 1), None)
        if side is None:
            return None
        sides.append(int(side[0]))
    return tuple(sides) if sides[1] <= sides[0] else None


def _area(frame):
    """The area number on the Mission screen, from the stage labels (OCR misses lone digits)."""
    for y in HARD_ROW_Y:
        m = _hard_row_id(frame, y)
        if m:
            return int(m.group(1))
    return None


def _hard_row_id(frame, y):
    """Regex match of the stage label on a Mission screen row."""
    for text in _number_reads(frame, (1035, y - 45, 1130, y + 10)):
        m = ID_RE.search(text.replace(" ", ""))
        if m:
            return m
    return None


def _ticked(frame, row):
    x, y = CHECK_X + row["shift"], row["top"] + 58
    box = cv2.cvtColor(frame[y - 25:y + 25, x - 25:x + 25], cv2.COLOR_BGR2HSV)
    return cv2.inRange(box, (85, 90, 150), (110, 255, 255)).mean() / 255 > 0.05


# ------------------------------------------------------------------ navigation
def goto_multi_sweep(game, vision, log):
    """Open the Multi-Sweep popup from the lobby, Campaign or Mission screen."""
    unknown = 0
    for _ in range(14):
        f = game.grab()
        t = _title(f)
        if "Multi-Sweep" in t:
            return True
        if "Sweep Complete" in t:
            game.click(*COMPLETE_CONFIRM)
        elif "Sweep Content" in t:
            game.click(*CONTENT_CANCEL)
        elif "Mission Info" in t:
            game.click(*MISSION_INFO_CLOSE)
        elif not t.strip() and "Mission" in " ".join(w.text for w in ocr.read(f, (150, 5, 420, 70))):
            game.click(*OPEN_MULTI)
        elif vision.find(f, "campaign_title"):
            game.click(*CAMPAIGN_MISSION)
        elif (p := vision.find(f, "lobby_campaign")):
            game.click(*p)
        else:
            unknown += 1
            if unknown >= 4:
                log("sweeper: open the lobby, Campaign or Mission screen first")
                return False
            game.sleep(1)
            continue
        game.sleep(2.5)
    log("sweeper: could not open Multi-Sweep")
    return False


def _open_preset(game, log):
    for _ in range(3):
        if game.grab()[236, 1540].mean() < 110:         # the selected tab is dark
            return True
        game.click(*TAB_7)
        game.sleep(1.2)
    if game.grab()[236, 1540].mean() < 110:
        return True
    log("sweeper: could not select preset tab 7")
    return False


def _filter_off(frame):
    return "OFF" in " ".join(w.text for w in ocr.read(frame, (1250, 315, 1480, 365))).upper()


def _scroll(game, x=735, y1=720, y2=480):
    game.drag(x, y1, x, y2)
    game.sleep(0.6)


def _changed(a, b, top=LIST_TOP, bottom=LIST_BOTTOM):
    return cv2.absdiff(a[top:bottom, 70:1830], b[top:bottom, 70:1830]).mean() >= 1.0


# ------------------------------------------------------------------ scanning
def load():
    if MISSIONS.exists():
        return json.loads(MISSIONS.read_text(encoding="utf-8"))
    return {"scanned_at": None, "ap": None, "rows": []}


def _write(path, data):
    """The whole file or nothing: a torn file would stop the sweeper from loading."""
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _save(data):
    _write(MISSIONS, data)


def load_marks():
    if MARKS.exists():
        return {k: int(v) for k, v in json.loads(MARKS.read_text(encoding="utf-8")).items()}
    return {}


def save_marks(marks):
    _write(MARKS, marks)


def _resized(img, height):
    return cv2.resize(img, (round(img.shape[1] * height / img.shape[0]), height), interpolation=cv2.INTER_AREA)


def _record(frame, row, old):
    """A row as stored on disk. A used-up row's icons are covered by a banner, so the
    clean icons from an earlier scan are kept when there are any."""
    k = key(row)
    clean = row["status"] == "ready"
    rec = {n: row[n] for n in ("id", "area", "stage", "kind", "remaining", "limit", "status")}
    rec["key"] = k
    rec["rewards"] = len(row["slots"])
    strip_path, eleph_path = ICONS / f"{k}.png", ICONS / f"{k}_eleph.png"
    if row["slots"] and (clean or not (old or {}).get("icons_clean")):
        y1, y2 = row["slots"][0][1], row["slots"][0][3]
        x1, x2 = row["slots"][0][0], row["slots"][-1][2]
        cv2.imwrite(str(strip_path), _resized(frame[y1:y2, x1:x2], 56))
    rec["icons_clean"] = clean or bool((old or {}).get("icons_clean"))
    if row["eleph"] is not None and (clean or not (old or {}).get("eleph_clean")):
        x1, y1, x2, y2, _ = row["slots"][row["eleph"]]
        cv2.imwrite(str(eleph_path), _resized(frame[y1:y2, x1:x2], 64))
    rec["eleph_clean"] = (row["eleph"] is not None and clean) or bool((old or {}).get("eleph_clean"))
    rec["strip"] = strip_path.name if strip_path.exists() else None
    rec["eleph"] = eleph_path.name if row["kind"] == "Hard" and eleph_path.exists() else None
    return rec


def _fill_elephs(game, records, log):
    """Used-up Hard rows hide their Eleph under a banner, so read those from the Mission screen."""
    areas = sorted({r["area"] for r in records.values()
                    if r["kind"] == "Hard" and r["status"] == "used" and not r.get("eleph_clean")})
    if not areas:
        return
    game.click(*CLOSE)
    game.sleep(2)
    game.click(*HARD_TAB)
    game.sleep(1.5)
    for area in areas:
        for _ in range(40):
            now = _area(game.grab())
            if now == area or now is None:
                break
            game.click(*(AREA_PREV if now > area else AREA_NEXT))
            game.sleep(1.3)
        if now != area:
            log(f"sweeper: could not reach area {area} for its Eleph icons")
            continue
        game.sleep(0.5)
        f = game.grab()
        for y in HARD_ROW_Y:
            m = _hard_row_id(f, y)
            if not m:
                continue
            k = f"H{int(m.group(1))}-{m.group(2).upper()}"
            if k in records:
                cv2.imwrite(str(ICONS / f"{k}_eleph.png"), _resized(f[y - 55:y + 55, 1150:1250], 64))
                records[k]["eleph"] = f"{k}_eleph.png"
                records[k]["eleph_clean"] = True


def scan(game, vision, log=None):
    """Read every stage from Multi-Sweep into data/sweeper/missions.json. Spends nothing."""
    log = log or game.log
    game.rt.task = "Scan missions"
    ICONS.mkdir(parents=True, exist_ok=True)
    old = {r["key"]: r for r in load()["rows"]}
    if not goto_multi_sweep(game, vision, log) or not _open_preset(game, log):
        return None
    f = game.grab()
    if not _filter_off(f):
        log("sweeper: turn the Multi-Sweep Filter off, then scan again")
        return None
    ap = _header_ap(f)
    records, prev, same, locked_views = {}, None, 0, 0
    for _ in range(MAX_VIEWS):
        f = game.grab()
        rows = parse_view(f)
        for row in rows:
            k = key(row)
            if k not in records or (row["status"] == "ready" and records[k]["status"] != "ready"):
                records[k] = _record(f, row, old.get(k))
        locked_views = locked_views + 1 if rows and all(r["status"] == "locked" for r in rows) else 0
        same = same + 1 if prev is not None and not _changed(f, prev) else 0
        prev = f
        if same >= 2 or locked_views >= 5:
            break
        _scroll(game)
        game.rt.stats["stages found"] = len(records)
    _fill_elephs(game, records, log)
    ordered = sorted(records.values(), key=sort_key)
    data = {"scanned_at": time.strftime("%Y-%m-%d %H:%M"), "ap": ap, "rows": ordered}
    _save(data)
    ready = sum(r["status"] == "ready" for r in ordered)
    log(f"sweeper: scan done - {len(ordered)} stages, {ready} ready to sweep")
    game.rt.task = "idle"
    return data


# ------------------------------------------------------------------ sweeping
def _select(game, wanted, count, log):
    """Tick the wanted rows and untick any others seen. Returns (selected, lowered)."""
    selected, lowered, resolved = set(), {}, set()
    prev, same = None, 0
    for _ in range(MAX_VIEWS):
        f = game.grab()
        rows = parse_view(f)
        clicked = []
        for row in rows:
            k = key(row)
            on = _ticked(f, row)
            want = False
            if k in wanted and k not in resolved:
                resolved.add(k)
                if row["status"] != "ready":
                    log(f"sweeper: {k[1:]} ({row['kind']}) is {row['status']}, skipping")
                elif row["kind"] == "Hard" and row["remaining"] is None:
                    log(f"sweeper: could not read the remaining count of {k[1:]}, skipping")
                elif row["kind"] == "Hard" and row["remaining"] < count:
                    lowered[k] = row["remaining"]
                    log(f"sweeper: {k[1:]} has {row['remaining']} left, lowering from {count}")
                else:
                    want = True
                    selected.add(k)
            elif k in selected:
                want = True
            if want != on:
                game.click(CHECK_X + row["shift"], row["top"] + 58)
                game.sleep(0.45)
                clicked.append((row, want))
        if clicked:
            game.sleep(0.4)
            f = game.grab()
            for row, want in clicked:
                if _ticked(f, row) != want:             # a swallowed first click
                    game.click(CHECK_X + row["shift"], row["top"] + 58)
                    game.sleep(0.6)
            f = game.grab()
            for row, want in clicked:
                if _ticked(f, row) != want:
                    log(f"sweeper: could not set the checkbox on {key(row)[1:]}")
                    return None, lowered
        if resolved >= wanted:
            return selected, lowered
        same = same + 1 if prev is not None and not _changed(f, prev) else 0
        prev = f
        if same >= 2:
            break
        _scroll(game)
    for k in wanted - resolved:
        log(f"sweeper: {k[1:]} is not in the list, skipping")
    return selected, lowered


def _set_count(game, per_run, count, log):
    """Step the count until the AP preview is exactly right. Returns current AP or None."""
    target = per_run * count
    for _ in range(count + 8):
        game.sleep(0.45)
        p = _preview(game.grab())
        if not p:
            continue
        delta = p[0] - p[1]
        if delta == target:
            return p[0]
        if delta % per_run:
            log(f"sweeper: preview shows {delta} AP, which does not fit {per_run} AP per run; stopping")
            return None
        game.click(*(PLUS if delta < target else MINUS))
    log(f"sweeper: could not set the sweep count to {count}")
    return None


def _wait_title(game, want, timeout, retry=None, retry_while=None, log=None):
    """Wait for a dialog title. Stops on any dialog about spending something other than AP.

    `retry` is clicked again (for a swallowed click) only while the title still says
    `retry_while`, so an unrecognised screen never receives a click.
    """
    end = time.time() + timeout
    next_retry = time.time() + 3
    while time.time() < end:
        f = game.grab()
        bad = _forbidden(f)
        if bad:
            cv2.imwrite(str(ROOT / "captures" / "sweeper_stopped.png"), f)
            if log:
                log(f"sweeper: STOPPED, a dialog mentions '{bad}'. Nothing more was clicked. Please check the game")
            return None
        title = _title(f)
        if want in title:
            return f
        if retry and retry_while in title and time.time() >= next_retry:
            game.click(*retry)
            next_retry = time.time() + 3
        game.sleep(0.4)
    return None


def _still_on(game, title, log):
    """Right before a fixed-position Confirm: this dialog is on top, and nothing on it mentions another cost."""
    f = game.grab()
    bad = _forbidden(f)
    if bad or title not in _title(f):
        cv2.imwrite(str(ROOT / "captures" / "sweeper_stopped.png"), f)
        log(f"sweeper: STOPPED before Confirm: expected '{title}'" + (f", and a dialog mentions '{bad}'" if bad else ""))
        return False
    return True


def _check_content(game, expected, need, frame, log):
    """True only if the game's confirmation shows exactly the expected stages, counts and AP."""
    m = re.search(r"Use\s*([\d,]+)\s*AP", " ".join(w.text for w in ocr.read(frame, (560, 235, 1360, 310))))
    shown = int(m.group(1).replace(",", "")) if m else None
    if shown != need:
        log(f"sweeper: the game asks for {shown} AP but {need} was expected; cancelling")
        return False
    seen, prev, same = {}, None, 0
    for _ in range(40):
        f = game.grab()
        seen.update(parse_content(f))
        if seen == expected:
            return True
        same = same + 1 if prev is not None and not _changed(f, prev, CONTENT_TOP, CONTENT_BOTTOM) else 0
        prev = f
        if same >= 1 or set(seen) - set(expected):
            break
        game.drag(640, 720, 640, 480)
        game.sleep(0.6)
    log(f"sweeper: the game's stage list does not match the marked stages; cancelling "
        f"(shown {sorted(seen)}, expected {sorted(expected)})")
    return False


class Unconfirmed(int):
    """AP that a pressed Confirm may have spent: the game's result screen was never seen."""


def _sweep_group(game, selected, count, budget_left, log):
    """Sweep the ticked stages `count` times. Returns the AP spent, or None to stop the run."""
    per_run = sum(cost(k) for k in selected)
    need = per_run * count
    ap = _set_count(game, per_run, count, log)
    if ap is None:
        return None
    if need > budget_left:
        log(f"sweeper: {need} AP is over the approved budget of {budget_left}; stopping")
        return None
    if ap < need:
        log(f"sweeper: need {need} AP but only {ap} available; stopping (AP is never refilled)")
        return None
    if _forbidden(game.grab()):
        log("sweeper: unexpected dialog on screen; stopping")
        return None
    game.click(*SWEEP)
    f = _wait_title(game, "Sweep Content", 10, retry=SWEEP, retry_while="Multi-Sweep", log=log)
    if f is None:
        log("sweeper: the Sweep Content confirmation did not appear; stopping")
        return None
    if not _check_content(game, {k: count for k in selected}, need, f, log):
        game.click(*CONTENT_CANCEL)
        game.sleep(1.2)
        return None
    if not _still_on(game, "Sweep Content", log):
        return None
    game.click(*CONTENT_CONFIRM)                      # this press spends AP, so it is never repeated
    if _wait_title(game, "Sweep Complete", 30, log=log) is None:
        log(f"sweeper: no Sweep Complete screen after Confirm. It may have spent up to {need} AP; please check the game")
        return Unconfirmed(-need)
    game.sleep(0.8)
    if not _still_on(game, "Sweep Complete", log):
        return -need                                  # spent, but the run cannot continue
    game.click(*COMPLETE_CONFIRM)
    if _wait_title(game, "Multi-Sweep", 12, retry=COMPLETE_CONFIRM, retry_while="Sweep Complete",
                   log=log) is None:
        log(f"sweeper: swept {len(selected)} stage(s) x{count}, but a screen after the rewards "
            "is not recognised; stopping")
        return -need                                  # spent, but the run cannot continue
    return need


def _record_results(swept):
    data = load()
    for row in data["rows"]:
        n = swept.get(row["key"])
        if n and row["kind"] == "Hard" and row["remaining"] is not None:
            row["remaining"] = max(0, row["remaining"] - n)
            if row["remaining"] == 0:
                row["status"] = "used"
    if data["rows"]:
        _save(data)


def run(game, vision, plan, log=None):
    """Sweep a plan of [{"key": "H11-3", "count": 3}, ...]. Returns {key: times swept}."""
    log = log or game.log
    game.rt.task = "Sweep missions"
    queue = {p["key"]: int(p["count"]) for p in plan if int(p["count"]) > 0}
    budget = sum(cost(k) * c for k, c in queue.items())
    spent, swept, unconfirmed = 0, {}, 0
    log(f"sweeper: {len(queue)} stages, up to {budget} AP")
    try:
        while queue:
            count = max(queue.values())
            group = {k for k, c in queue.items() if c == count}
            if not goto_multi_sweep(game, vision, log) or not _open_preset(game, log):
                break
            if not _filter_off(game.grab()):
                log("sweeper: turn the Multi-Sweep Filter off, then run again")
                break
            game.click(*UNDO_ALL)
            game.sleep(1.0)
            if "Multi-Sweep" not in _title(game.grab()):
                log("sweeper: unexpected dialog after clearing the selection; stopping")
                break
            selected, lowered = _select(game, group, count, log)
            if selected is None:
                break
            for k in group:
                del queue[k]
            queue.update({k: c for k, c in lowered.items() if c > 0})
            if not selected:
                game.click(*CLOSE)
                game.sleep(1.5)
                continue
            result = _sweep_group(game, selected, count, budget - spent, log)
            if result is None:
                break
            if isinstance(result, Unconfirmed):
                unconfirmed = abs(result)
                break
            spent += abs(result)
            for k in selected:
                swept[k] = swept.get(k, 0) + count
            game.rt.bump("stages swept", len(selected))
            log(f"sweeper: swept {', '.join(sorted(k[1:] for k in selected))} x{count} ({abs(result)} AP)")
            if result < 0:
                break
            game.click(*CLOSE)
            game.sleep(1.5)
        else:
            f = game.grab()
            if "Multi-Sweep" in _title(f):
                game.click(*CLOSE)
    finally:                                          # F8 or a refusal mustn't lose what was already swept
        try:
            _record_results(swept)
        except Exception as e:
            log(f"sweeper: couldn't save the sweep results: {e!r}")
    log(f"sweeper: finished - {len(swept)} stages swept, {spent} AP spent")
    game.rt.task = "idle"
    if unconfirmed:
        raise RuntimeError(f"a sweep's Confirm was pressed but its result wasn't seen: it may have spent up to "
                           f"{unconfirmed} AP. Check the game before sweeping again")
    return swept
