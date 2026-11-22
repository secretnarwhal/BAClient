"""Which client window opens: the new web panel, or the classic Tk HUD.

  python -m ba_auto ctl ui              show the current choice
  python -m ba_auto ctl ui classic      go back to the classic HUD at the next client start
  python -m ba_auto ctl ui new          use the new client at the next client start

Both run the same engine (`hud.Hud`): one worker, Stop and F8, the watchdog, the journal, `ctl`, the
launcher and every task. Only what's drawn differs. A running client is never changed by this.
"""
import json
import os

from . import control, crashlog, journal

CHOICES = ("new", "classic")


def _file():
    return control.STATE / "ui.json"


def choice():
    """The saved choice. New unless someone chose classic."""
    try:
        value = json.loads(_file().read_text(encoding="utf-8")).get("ui")
    except (OSError, ValueError, AttributeError):
        value = None
    return value if value in CHOICES else "new"


def set_choice(name, by="user"):
    if name not in CHOICES:
        raise ValueError(f"ui must be one of {', '.join(CHOICES)}")
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"ui": name}), encoding="utf-8")
    os.replace(tmp, path)
    journal.record("ui_choice", ui=name, by=by)
    return name


def _classic():
    from .hud import main as classic_main
    return classic_main()


def main():
    """Open the chosen client. If the new one can't even load, the classic HUD opens instead."""
    if choice() == "classic":
        return _classic()
    try:
        from .ui import app
    except Exception:
        crashlog.exception("the new client couldn't load, so the classic HUD is opening")
        return _classic()
    real_open = app.open_panel

    def safe_open(url, position=None):
        """No panel is not an emergency: ctl, F8 and the overlay keep working without it."""
        try:
            return real_open(url, position)
        except Exception as e:
            crashlog.note(f"the client's panel couldn't open ({e!r}); ctl and F8 still work")
            return False

    app.open_panel = safe_open
    return app.main()
