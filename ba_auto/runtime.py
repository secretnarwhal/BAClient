"""Shared state between the worker running a task and the HUD drawing it."""
import collections
import threading
import time

from . import journal


class Stopped(Exception):
    """Raised inside a task when the user pressed Stop."""


class TitleScreen(RuntimeError):
    """Input was refused because the game is on the title screen."""


class Runtime:
    def __init__(self):
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.logs = collections.deque(maxlen=200)
        self.matches = collections.deque(maxlen=20)   # (name, (x1,y1,x2,y2), score, time)
        self.task = "idle"
        self.screen = "-"
        self.stats = {}
        self.last_click = None                        # ((x, y), time)
        self.listeners = []

    def log(self, msg):
        line = f"{time.strftime('%H:%M:%S')}  {msg}"
        with self.lock:
            self.logs.append(line)
        journal.record("log", msg=msg, task=self.task)
        print(line, flush=True)

    def bump(self, key, n=1):
        with self.lock:
            self.stats[key] = self.stats.get(key, 0) + n

    def check(self):
        if self.stop.is_set():
            raise Stopped()

    def sleep(self, seconds):
        if self.stop.wait(seconds):
            raise Stopped()


# Default runtime for command-line use; the HUD makes its own.
DEFAULT = Runtime()
