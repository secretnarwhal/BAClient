"""Names the screen before and after each input, so the journal reads as a path.

"Before" comes from the frame already captured for the journal. "After" is checked by a
background thread once the game has had time to react. If another input arrives first,
that input's "before" serves as the answer and the pending check is dropped. All of this
is read-only and a failure here never affects the input itself.
"""
import atexit
import threading
import time

from . import journal
from .runtime import Runtime
from .screens import label
from .vision import Vision

SETTLE = 1.2                     # seconds for a screen transition to finish
SLOW_RETRIES = (1.5, 2.0)        # further waits while the screen still reads unknown


class Trail:
    def __init__(self):
        self._cv = threading.Condition()
        self._pending = None     # (event id, actor, run, due time, probe)
        self._thread = None
        self._vision = None
        self._probes = {}

    def name(self, frame):
        try:
            if self._vision is None:
                self._vision = Vision()       # no runtime: stays off the overlay
            return label(self._vision, frame)
        except Exception:
            return None

    def after(self, game, event_id):
        """Queue an "after" check for this input, replacing any check still waiting."""
        if not event_id:
            return
        try:
            cls = type(game)
            probe = self._probes.setdefault(cls, cls(Runtime()))     # never stopped by F8
            run = getattr(journal._context, "run", None)
            with self._cv:
                self._pending = (event_id, journal.actor(), run, time.monotonic() + SETTLE, probe)
                if self._thread is None:
                    self._thread = threading.Thread(target=self._loop, daemon=True)
                    self._thread.start()
                self._cv.notify_all()
        except Exception:
            pass

    def _take_due(self):
        with self._cv:
            while True:
                if self._pending is None:
                    self._cv.wait()
                    continue
                wait = self._pending[3] - time.monotonic()
                if wait <= 0:
                    item, self._pending = self._pending, None
                    return item
                self._cv.wait(wait)

    def _newer_input(self, seconds):
        """Wait, returning True early if another input has been queued."""
        with self._cv:
            if self._pending is None:
                self._cv.wait(seconds)
            return self._pending is not None

    def _check(self, item, retries=SLOW_RETRIES):
        event_id, actor, run, _, probe = item
        try:
            journal.set_context(actor=actor, run=run)
            screen = self.name(probe.grab())
            for delay in retries:        # slow screens show a loading frame first
                if screen != "unknown" or self._newer_input(delay):
                    break
                screen = self.name(probe.grab())
            journal.record("after", of=event_id, screen=screen)
        except Exception:
            pass

    def _loop(self):
        while True:
            self._check(self._take_due())

    def flush(self):
        """At process exit, finish the last check so a one-shot tool still logs where it landed."""
        with self._cv:
            item, self._pending = self._pending, None
        if item:
            time.sleep(max(0.0, item[3] - time.monotonic()))
            self._check(item, retries=())


TRAIL = Trail()
atexit.register(TRAIL.flush)
