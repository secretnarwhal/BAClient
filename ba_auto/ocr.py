"""Text reading through Windows' built-in OCR engine (see ocr_win.ps1).

One PowerShell helper stays running so each read takes a fraction of a second.
Coordinates returned are in the frame's own (1920x1080 reference) space.

A helper that stops answering is killed and replaced, so a stuck read can't hold a task, F8 or the
watchdog forever. A read that fails raises instead of looking like a frame with no words on it.
"""
import json
import os
import queue
import subprocess
import tempfile
import threading
from collections import namedtuple
from pathlib import Path

import cv2

Word = namedtuple("Word", "text x y w h")
SCRIPT = Path(__file__).with_name("ocr_win.ps1")
START_TIMEOUT = 30.0             # the helper loading Windows' OCR engine
READ_TIMEOUT = 10.0              # one read; they normally take well under a second

_lock = threading.Lock()
_proc = None
_lines = None                    # what this helper has printed, one queue per helper
_count = 0
_tmp = os.path.join(tempfile.gettempdir(), f"baclient_ocr_{os.getpid()}")


def _command():
    return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT)]


def _pump(proc, lines):
    """Hand the helper's output over a queue, so waiting for it can time out."""
    try:
        for line in proc.stdout:
            lines.put(line)
    except (OSError, ValueError):
        pass
    lines.put(None)


def _drop():
    """Get rid of a helper that died or stopped answering."""
    global _proc, _lines
    proc, _proc, _lines = _proc, None, None
    if proc is not None:
        try:
            proc.kill()
        except OSError:
            pass


def _next_line(timeout):
    try:
        line = _lines.get(timeout=timeout)
    except queue.Empty:
        _drop()
        raise RuntimeError(f"Windows OCR didn't answer within {timeout:g} s") from None
    if line is None:
        _drop()
        raise EOFError("the Windows OCR helper stopped")
    return line


def _helper():
    global _proc, _lines
    if _proc is None or _proc.poll() is not None:
        _drop()
        _proc = subprocess.Popen(
            _command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            encoding="utf-8", errors="replace", bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW)
        _lines = queue.Queue()
        threading.Thread(target=_pump, args=(_proc, _lines), daemon=True, name="ocr-helper").start()
        try:
            ready = _next_line(START_TIMEOUT).strip() == "ready"
        except EOFError:
            ready = False
        if not ready:
            _drop()
            raise RuntimeError("Windows OCR helper failed to start")
    return _proc


def _ask(path):
    """The helper's answer for this file. A helper that died gets one replacement."""
    for last in (False, True):
        p = _helper()
        try:
            p.stdin.write(path + "\n")
            p.stdin.flush()
            return _next_line(READ_TIMEOUT)
        except (EOFError, OSError, ValueError) as e:
            _drop()
            if last:
                raise RuntimeError(f"the Windows OCR helper stopped ({e})") from e


def read(frame, box=None, scale=2):
    """Words in `frame` (or inside box=(x1, y1, x2, y2)), in frame coordinates."""
    x1, y1 = (box[0], box[1]) if box else (0, 0)
    img = frame[box[1]:box[3], box[0]:box[2]] if box else frame
    if scale != 1:
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    global _count
    with _lock:
        _count += 1                    # a fresh name each read: Windows caches files by path
        path = f"{_tmp}.{_count}.png"
        cv2.imwrite(path, img)
        try:
            line = _ask(path)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
    if line.strip() == "ERROR":
        raise RuntimeError("Windows OCR couldn't read that image")
    try:
        raw = json.loads(line)
    except ValueError:
        raise RuntimeError("Windows OCR gave an answer that can't be parsed") from None
    return [Word(w["t"], x1 + w["x"] / scale, y1 + w["y"] / scale, w["w"] / scale, w["h"] / scale)
            for w in raw]


def text_in(words, x1, y1, x2, y2):
    """Words whose centre is inside the box, joined left to right."""
    hits = [w for w in words if x1 <= w.x + w.w / 2 <= x2 and y1 <= w.y + w.h / 2 <= y2]
    return " ".join(w.text for w in sorted(hits, key=lambda w: (round(w.y / 12), w.x)))
