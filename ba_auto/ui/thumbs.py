"""Small copies of the Collection art, so the Students grid never decodes full portraits.

Made on this PC from assets/game, kept in data/ui/thumbs, and never uploaded. A missing thumbnail is
made when first asked for, one at a time at low priority. `python -m ba_auto.ui.thumbs` makes them
all in one idle-priority pass. Game files are only read.

  $env:BA_ACTOR='builder'; python -m ba_auto.ui.thumbs
"""
import ctypes
import hashlib
import os
import sys
import threading
import time

from PIL import Image

from .. import journal
from . import art

ROOT = journal.ROOT / "data" / "ui" / "thumbs"
SIZES = (112, 208)
QUALITY = 78
THREAD_PRIORITY_LOWEST = -2
IDLE_PRIORITY_CLASS = 0x40
_lock = threading.Lock()


def _path(source, size):
    # Student names can differ only by case, which is the same file on Windows.
    name = hashlib.sha1(str(source).lower().encode("utf-8")).hexdigest()[:20]
    return ROOT / str(size) / f"{name}.webp"


def _fresh(out, source):
    try:
        return out.stat().st_mtime_ns >= source.stat().st_mtime_ns
    except OSError:
        return False


def _make(source, out, size):
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with Image.open(source) as image:
            image.load()
            height = round(size * image.height / image.width)
            small = image.convert("RGBA").resize((size, height), Image.LANCZOS, reducing_gap=2.0)
        small.save(tmp, "WEBP", quality=QUALITY, method=3)
        os.replace(tmp, out)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def get(dev, size):
    """The thumbnail file, made if needed, or None if this student has no Collection art."""
    if size not in SIZES:
        return None
    source = art.portrait_file(dev)
    if source is None:
        return None
    out = _path(source, size)
    if _fresh(out, source):
        return out
    kernel = ctypes.windll.kernel32
    thread = kernel.GetCurrentThread()
    before = kernel.GetThreadPriority(thread)
    kernel.SetThreadPriority(thread, THREAD_PRIORITY_LOWEST)
    try:
        with _lock:
            if not _fresh(out, source):
                _make(source, out, size)
    except OSError:
        return None
    finally:
        kernel.SetThreadPriority(thread, before)
    return out


def url(dev, size):
    """A URL that changes when the source art changes, or None."""
    source = art.portrait_file(dev)
    if source is None:
        return None
    return f"/thumb/{size}/{dev}?v={source.stat().st_mtime_ns:x}"


def build_all():
    """One idle-priority pass over every student with Collection art."""
    kernel = ctypes.windll.kernel32
    kernel.SetPriorityClass(kernel.GetCurrentProcess(), IDLE_PRIORITY_CLASS)
    started, made, failed, total = time.perf_counter(), 0, 0, 0
    devs = [d for d, e in art._load()["students"].items() if e.get("collection")]
    for dev in devs:
        source = art.portrait_file(dev)
        if source is None:
            failed += 1
            continue
        for size in SIZES:
            out = _path(source, size)
            if not _fresh(out, source):
                try:
                    _make(source, out, size)
                    made += 1
                except OSError:
                    failed += 1
                    continue
            total += out.stat().st_size
    return {"students": len(devs), "made": made, "failed": failed, "mb": round(total / 1048576, 1),
            "seconds": round(time.perf_counter() - started, 1)}


if __name__ == "__main__":
    print(build_all(), flush=True)
    sys.exit(0)
