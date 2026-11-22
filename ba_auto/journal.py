"""Automatic action journal.

Every click, drag and key sent through Game, and every runtime log line, lands in
logs/YYYY-MM-DD/events.jsonl. Input events also save the frame the actor was looking
at when it acted to logs/YYYY-MM-DD/frames/, and name the screen before and after.
Set BA_ACTOR to say who is acting ("player", "user", ...). Journal failures never
interrupt the game.

Retention (`prune`) only ever deletes old frame images. events.jsonl is kept, and so is
any frame named in a project note.
"""
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"
KEEP_DAYS = 14
MAX_FRAMES_MB = 2048
INPUT_KINDS = ("click", "drag", "key")
KEY_NAMES = {0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x1B: "Esc", 0x20: "Space"}
_lock = threading.Lock()
_seq = 0
_context = threading.local()     # per-thread actor and run id, set for client-managed runs


def set_context(actor=None, run=None):
    """Attribute this thread's events to `actor` and `run` (used by control.py)."""
    _context.actor = actor
    _context.run = run


def actor():
    return getattr(_context, "actor", None) or os.environ.get("BA_ACTOR", "client")


def read_only(name=None):
    """True when input must be refused for `name` (default: this thread's actor). The
    process's own BA_ACTOR always counts, so a client launched as the builder can never
    send input, whoever asks it to."""
    return "builder" in (name or actor(), os.environ.get("BA_ACTOR"))


def _day_dir():
    d = LOGS / time.strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    return d


def record(kind, frame=None, **fields):
    """Append one event and return its id. `frame` (BGR, 1920x1080) is saved at half size."""
    global _seq
    try:
        with _lock:
            _seq += 1
            seq = _seq
        now = time.time()
        event = {
            "id": f"{os.getpid()}-{seq}",
            "ts": round(now, 3),
            "time": time.strftime("%H:%M:%S", time.localtime(now)),
            "actor": actor(),
            "pid": os.getpid(),
            "cmd": " ".join(os.path.basename(a) if i == 0 else a for i, a in enumerate(sys.argv)),
            "kind": kind,
            **fields,
        }
        run = getattr(_context, "run", None)
        if run:
            event["run"] = run
        day = _day_dir()
        if frame is not None:
            frames = day / "frames"
            frames.mkdir(exist_ok=True)
            name = f"{time.strftime('%H%M%S', time.localtime(now))}_{os.getpid()}_{seq:05d}_{kind}.jpg"
            small = cv2.resize(frame, (960, 540), interpolation=cv2.INTER_AREA)
            cv2.imwrite(str(frames / name), small, [cv2.IMWRITE_JPEG_QUALITY, 75])
            event["frame"] = f"frames/{name}"
        line = json.dumps(event, ensure_ascii=False) + "\n"
        with _lock:
            with open(day / "events.jsonl", "a", encoding="utf-8") as f:
                f.write(line)
        return event["id"]
    except Exception:
        return None


# ------------------------------------------------------------------ reading
def days():
    """Days with a journal, newest first."""
    if not LOGS.exists():
        return []
    found = [d.name for d in LOGS.iterdir()
             if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name) and (d / "events.jsonl").exists()]
    return sorted(found, reverse=True)


def read_new(day, offset=0):
    """Events after byte `offset`, and the new offset. A line still being written is left."""
    path = LOGS / day / "events.jsonl"
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return [], offset
    end = data.rfind(b"\n") + 1
    events = []
    for line in data[:end].splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events, offset + end


def timeline(events, logs=False):
    """Rows for reading: each input gets `after`, the screen it led to. That comes from its
    own check when there was time for one, else from the next input's starting screen."""
    by_id, rows, last_input = {}, [], None
    for i, e in enumerate(events):
        kind = e.get("kind")
        if kind == "after":
            target = by_id.get(e.get("of"))
            if target is not None and not (e.get("screen") == "unknown" and target.get("after")):
                target["after"] = e.get("screen")    # never replace a known screen with unknown
            continue
        if kind == "log" and not logs:
            continue
        row = {**e, "row": str(i)}
        if kind in INPUT_KINDS:
            if last_input is not None and last_input.get("after") in (None, "unknown") and row.get("screen"):
                last_input["after"] = row["screen"]
            last_input = row
        if e.get("id"):
            by_id[e["id"]] = row
        rows.append(row)
    return rows


def describe(row):
    kind = row.get("kind")
    if kind == "click":
        return f"click ({row.get('x')}, {row.get('y')})"
    if kind == "drag":
        return f"drag ({row.get('x1')}, {row.get('y1')}) -> ({row.get('x2')}, {row.get('y2')})"
    if kind == "key":
        vk = row.get("vk", 0)
        return f"key {KEY_NAMES.get(vk, hex(vk))}"
    if kind == "run":
        if row.get("phase") == "start":
            return f"run start: {row.get('task')}"
        return f"run {row.get('outcome')}: {row.get('task')}" + (f" {row['error']}" if row.get("error") else "")
    if kind == "error":
        return f"error in {row.get('task')}"
    return str(row.get("msg", kind))


def path_text(row):
    before, after = row.get("screen"), row.get("after")
    if not before:
        return ""
    return f"{before} -> {after}" if after and after != before else before


def show(day=None, last=40, logs=False):
    """Print the end of a day's timeline. Returns the number of rows printed."""
    day = day or (days() or [None])[0]
    if day is None:
        print("no journal yet")
        return 0
    rows = timeline(read_new(day)[0], logs=logs)[-last:]
    print(f"--- {day}")
    for r in rows:
        print(f"{r.get('time', ''):8}  {r.get('actor', ''):7}  {path_text(r):34}  {describe(r)}"
              + (f"  [{r['frame']}]" if r.get("frame") else ""))
    return len(rows)


# ------------------------------------------------------------------ retention
def _cited():
    """Frame files that a project note points at."""
    names = set()
    for doc in ():
        try:
            names |= set(re.findall(r"[\w-]+\.jpg", (ROOT / "docs" / doc).read_text(encoding="utf-8")))
        except OSError:
            pass
    return names


def prune(keep_days=KEEP_DAYS, max_mb=MAX_FRAMES_MB):
    """Delete frames older than `keep_days`, then the oldest ones until all frames fit in
    `max_mb`. Returns (frames deleted, MB freed)."""
    cited = _cited()
    files = []
    for path in LOGS.glob("*/frames/*.jpg"):
        try:
            st = path.stat()
            files.append((st.st_mtime, st.st_size, path))
        except OSError:
            continue
    files.sort(key=lambda f: f[0])
    total = sum(f[1] for f in files)
    limit, cutoff = max_mb * 1024 * 1024, time.time() - keep_days * 86400
    deleted, freed = 0, 0
    for mtime, size, path in files:
        if mtime >= cutoff and total <= limit:
            break
        if path.name in cited:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        freed += size
        deleted += 1
    return deleted, round(freed / (1024 * 1024), 1)
