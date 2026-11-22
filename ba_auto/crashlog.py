"""What stopped the client, written to data/ctl/client.log.

A refused input or a failed task is a normal error: the run ends and the client stays up. This module
catches what's left: uncaught errors in the main thread or any other thread, Tk callback errors, hard
crashes (faulthandler), and how the process ended. The HUD can run with no console, so nothing here may
depend on one, and nothing here may raise.
"""
import atexit
import faulthandler
import os
import sys
import threading
import time
import traceback

_installed = False
_fault_file = None               # faulthandler needs a file that stays open


def _log_path():
    from . import control
    return control.STATE / "client.log"


def note(text):
    """One timestamped line in the client log. Under `ctl start`, stderr already is that file."""
    line = f"{time.strftime('%H:%M:%S')}  {text}\n"
    try:
        if sys.stderr is not None:
            sys.stderr.write(line)
            sys.stderr.flush()
            return
    except Exception:
        pass
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def exception(where, exc_info=None):
    """Log an error with its traceback."""
    try:
        text = "".join(traceback.format_exception(*(exc_info or sys.exc_info()))).rstrip()
    except Exception:
        text = "(no traceback)"
    note(f"{where}\n{text}")


def _thread_hook(args):
    if args.exc_type is SystemExit:
        return
    name = args.thread.name if args.thread else "unknown"
    exception(f"uncaught error in thread {name}", (args.exc_type, args.exc_value, args.exc_traceback))


def install():
    """Route every uncaught error to the client log. Safe to call twice."""
    global _installed, _fault_file
    if _installed:
        return
    _installed = True
    sys.excepthook = lambda t, v, tb: exception("uncaught error, the client is stopping", (t, v, tb))
    threading.excepthook = _thread_hook
    try:
        if sys.stderr is not None:
            faulthandler.enable(file=sys.stderr)
        else:
            path = _log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            _fault_file = open(path, "a", encoding="utf-8")
            faulthandler.enable(file=_fault_file)
    except Exception:
        pass
    pid = os.getpid()
    atexit.register(note, f"client process {pid} exiting")
    note(f"client process {pid} started")


def watch_tk(root, owner):
    """Tk callback errors go to the log, and a failed refresh can't stop the HUD's update loop."""
    root.report_callback_exception = lambda t, v, tb: exception("HUD callback error (the client keeps running)", (t, v, tb))
    inner = owner._tick

    def tick():
        try:
            inner()
        except Exception:
            exception("HUD refresh error (the client keeps running)")
            try:
                root.after(500, owner._tick)
            except Exception:    # the window is already gone
                pass
    owner._tick = tick
