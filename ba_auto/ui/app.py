"""The new client window: `Hud`'s engine with the web panel and the small overlay card.

Everything that keeps the game safe stays in `Hud` and `Controller`: the one worker, Stop, the F8
hotkey, the watchdog, the journal and `ctl`. This class replaces only what's drawn. Closing the
panel leaves the client and F8 running; use Quit in the panel or `ctl quit` to end it.
"""
import json
import os
import queue
import subprocess
import sys
import time
from pathlib import Path

import win32api

from .. import control, journal, viewport, watchdog
from ..hud import Hud, OK, WARN
from . import commands, controls, overlay, panelwin, slots
from .server import PanelServer
from .state import PANEL_TASKS, LiveClient, Store

try:
    from . import tray                  # Windows only. Without it the client simply has no tray icon
except ImportError:
    tray = None

PANEL_FILE = journal.ROOT / "data" / "ui" / "panel.json"
PROFILE = journal.ROOT / "data" / "ui" / "chrome-profile"
CHROME = (Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
          Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe")
PANEL_SIZE = (1500, 940)
OPENABLE = ("sweeper", "journal", "lessons")
EDGE_PX = 5
NOTICE_SECONDS = 6.0
TRAY_NOTES = {"elsewhere": "Panel is on another workspace", "no_room": "No free monitor for the panel",
              "failed": "Couldn't open the panel"}


def panel_position(game):
    """(position, safe). Position is on a monitor without the game. It isn't safe to open
    automatically when the game fills the only monitor, because a tiling window manager would tile the panel
    beside it and resize the game."""
    try:
        x, y, w, h = game.client_rect()
    except Exception:
        return None, True               # no game window to disturb
    try:
        cx, cy = x + w // 2, y + h // 2
        for _, _, (left, top, right, bottom) in win32api.EnumDisplayMonitors():
            if not (left <= cx < right and top <= cy < bottom):
                return (left + 40, top + 50), True
    except Exception:
        pass
    return None, False


def open_panel(url, position=None):
    """Open the panel as its own app window, without taking focus. Returns True if it launched."""
    exe = next((p for p in CHROME if p.exists()), None)
    if exe is None:
        return False
    args = [str(exe), f"--app={url}", f"--user-data-dir={PROFILE}", "--no-first-run",
            "--no-default-browser-check", f"--window-size={PANEL_SIZE[0]},{PANEL_SIZE[1]}"]
    if position:
        args.append(f"--window-position={position[0]},{position[1]}")
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 4                # SW_SHOWNOACTIVATE
    PROFILE.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(args, startupinfo=info, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=subprocess.DETACHED_PROCESS)
    return True


class Actions:
    """What the panel may ask for. Runs go through the same path as `ctl run`."""

    def __init__(self, hud):
        self.hud = hud
        self.inspector = None           # made on first use, so an unused Inspect tab costs nothing

    def command(self, task, values, confirm=None):
        """A task with its options from the schema. Anything that spends runs only for the exact options confirmed."""
        try:
            plan = commands.plan(task, values)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if plan.confirm and confirm != plan.key:
            return {"ok": False, "confirm": plan.confirm, "key": plan.key, "label": plan.label, "summary": plan.summary,
                    "error": plan.confirm}
        if task == "launch" and self._game_open():
            return {"ok": False, "error": "Blue Archive is already running"}
        reply = self.hud.ctl.handle({"op": "run", "task": task, "actor": "user", "args": plan.args})
        if reply.get("ok") and reply.get("id"):
            self.hud.panel_labels[reply["id"]] = plan.label
        return reply

    def clear_halt(self, note, expect):
        """The same op as `ctl clear-halt`, for the exact halt the person reviewed."""
        try:
            note = controls.clean_note(note)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        current = watchdog.halted()
        if current is None:
            return {"ok": True, "cleared": None}
        if controls.record_key(current) != expect:
            return {"ok": False, "changed": True, "error": "The halt changed while this was open. Review it again"}
        return self.hud.ctl.handle({"op": "clear-halt", "actor": "user", "note": note})

    def clear_hold(self, note, expect, phrase):
        """The same call as `ctl clear-login-hold`. Someone else may be on the account, so it asks for more."""
        if journal.read_only("user"):
            return {"ok": False, "error": "the builder session is read-only: the player runs the game"}
        try:
            note = controls.clean_note(note)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if phrase != controls.HOLD_PHRASE:
            return {"ok": False, "error": f"Type {controls.HOLD_PHRASE} to confirm"}
        current = watchdog.login_hold()
        if current is None:
            return {"ok": True, "cleared": None}
        if controls.record_key(current) != expect:
            return {"ok": False, "changed": True, "error": "The login hold changed while this was open. Review it again"}
        watchdog.clear_login_hold("user", note)
        self.hud.rt.log(f"login hold cleared by user: {note}")
        return {"ok": True, "cleared": True}

    def restart(self, confirmed=False):
        if confirmed is not True:
            return {"ok": False, "error": "Confirm before restarting"}
        if journal.read_only("user"):
            return {"ok": False, "error": "the builder session is read-only: the player runs the game"}
        if self.hud.ctl.busy:
            return {"ok": False, "error": "A run is active. Stop it first"}
        try:
            helper = controls.spawn_restart(os.getpid())
        except Exception as e:
            return {"ok": False, "error": f"Couldn't prepare the restart: {e}"}
        reply = self.hud.ctl.handle({"op": "quit", "actor": "user"})
        if not reply.get("ok"):         # e.g. a run started just now: leave this client alone
            helper.terminate()
        return reply

    def ui_choice(self, choice):
        from .. import client
        if journal.read_only("user"):
            return {"ok": False, "error": "the builder session is read-only: the player runs the game"}
        try:
            return {"ok": True, "ui": client.set_choice(choice, "user")}
        except (ValueError, TypeError) as e:
            return {"ok": False, "error": str(e)}

    def inspect(self, view):
        hud = self.hud
        if self.inspector is None:
            self.inspector = controls.Inspector(hud.game, lambda: hud.ctl.busy, advise=lambda armor, attack: hud.store.advise(armor, attack))
        return self.inspector.run(view)

    def run(self, task, confirmed=False):
        meta = PANEL_TASKS.get(task)
        if meta is None or task not in control.TASKS:
            return {"ok": False, "error": "that task can't be started from the panel"}
        if meta.get("spends") and not confirmed:
            return {"ok": False, "confirm": meta["spends"], "error": meta["spends"]}
        if task == "launch" and self._game_open():
            return {"ok": False, "error": "Blue Archive is already running"}
        reply = self.hud.ctl.handle({"op": "run", "task": task, "actor": "user", "args": {}})
        if reply.get("ok") and reply.get("id") and meta.get("label"):
            self.hud.panel_labels[reply["id"]] = meta["label"]
        return reply

    def teach(self, rooms, scan_ts, rehearse=False, confirmed=False):
        """Only from a real selection on the Atlas. Teaching spends tickets, so it needs a confirmed request."""
        if not rehearse and confirmed is not True:
            return {"ok": False, "confirm": True, "error": "Confirm before spending tickets"}
        return self.hud.store.desk.start(self.hud.ctl, rooms, scan_ts, rehearse=rehearse)

    def _game_open(self):
        """The launcher's own process-and-window check. If it can't tell, don't risk a second launch."""
        from .. import launcher
        try:
            return bool(launcher.game_running())
        except Exception:
            return True

    def auto_login(self, on):
        """Saved by the client, so the panel never shows a setting it doesn't really have."""
        return self.hud.ctl.handle({"op": "auto-login", "on": bool(on), "actor": "user"})

    def stop(self):
        return {"ok": True, "stopping": self.hud.ctl.stop("user")}

    def open(self, what):
        if what not in OPENABLE:
            return {"ok": False, "error": "unknown window"}
        self.hud.ui_queue.put(("open", what))
        return {"ok": True}

    def overlay(self, on):
        self.hud.ui_queue.put(("overlay", on))
        return {"ok": True}

    def quit(self):
        return self.hud.ctl.handle({"op": "quit", "actor": "user"})


class ArtHud(Hud):
    def __init__(self):
        self.ui_queue = queue.Queue()
        self.panel_labels = {}          # run id -> the name of a dry run the panel started
        self.panel = None
        self.card = None
        self._marks_drawn = False
        self._card_error_logged = False
        self._team_key = None
        self._team = []
        self.tray = None
        self._restorer = None
        self._notice = None             # (text, monotonic time it ends), set by the tray thread
        super().__init__()
        self.store = Store(LiveClient(self))
        self.panel_guards.append(panelwin.ChromeGuards(self._chrome_hwnds, self.rt.log))

    def _chrome_hwnds(self):
        """This client's panel windows, found by its Chrome profile and never by title."""
        return panelwin.panel_windows(PROFILE)

    # ---- what Hud builds ----
    def _build_panel(self):
        self.root.withdraw()            # the web panel replaces it; Tk stays for the overlay and tool windows

    def _build_overlay(self):
        super()._build_overlay()
        self.card = overlay.CardWindow()

    def _toggle_overlay(self):
        self._set_overlay(not self.overlay_on)

    def _set_overlay(self, on):
        self.overlay_on = bool(on)
        if self.overlay_on:
            self.ov.deiconify()
        else:
            self.ov.withdraw()
            self.card.hide()

    # ---- drawing ----
    def _status(self):
        if self.halt:
            return "halted", f"Halted: {self.halt.get('reason')}"
        current = self.ctl.current
        if current is not None:
            return "running", self.panel_labels.get(current.id) or str(self.rt.task)
        return "idle", "BAClient idle"

    def _card_team(self):
        names = list(self.battles.team)
        if names != self._team_key:
            self._team_key = names
            self._team = self.store.team(names)["members"]
        return self._team

    def _current_view(self):
        """The converter's latest view of the game's picture, if it still fits the window."""
        client = tuple(self.game.client_rect())
        view = self.view
        return view if view is not None and view.client == client else viewport.from_rect(*client)

    def _draw_overlay(self):
        try:
            view = self._current_view()
        except Exception:
            self.ov.withdraw()
            self.card.hide()
            self._ov_rect = None
            return
        if not self.overlay_on:
            return
        if view.client != self._ov_rect:
            x, y, w, h = view.client
            self.ov.deiconify()
            self.ov.geometry(f"{w}x{h}+{x}+{y}")
            self._ov_rect = view.client
        state = self._status()
        self._draw_marks(view, state[0])
        rect = slots.card_rect(self.rt.screen, view)
        if rect is None:
            self.card.hide()
        else:
            stats = dict(self.rt.stats) if self.ctl.busy else None
            info = overlay.card_info(state, self.rt.screen, self._card_team(), stats)
            notice = self._notice
            if notice and time.monotonic() < notice[1]:
                info["sub"] = notice[0]         # the title line keeps showing a halt
            self.card.show(info, rect)
            if self.card.error and not self._card_error_logged:
                self._card_error_logged = True
                self.rt.log(f"overlay card off: {self.card.error}")

    def _draw_marks(self, view, state):
        """Matched templates, the last click, and the status line. Outlines only."""
        c, now = self.canvas, time.time()
        matches = [m for m in list(self.rt.matches) if now - m[3] <= 1.2]
        click = self.rt.last_click if self.rt.last_click and now - self.rt.last_click[1] < 1.0 else None
        edge = {"running": overlay.COLORS["running"], "halted": overlay.COLORS["halted"]}.get(state)
        if not (matches or click or edge or self._marks_drawn):
            return
        c.delete("all")
        for name, (x1, y1, x2, y2), score, _, *rest in matches:
            shift = rest[0] if rest else (0, 0)
            ax, ay = view.to_client(x1, y1, shift)
            bx, by = view.to_client(x2, y2, shift)
            c.create_rectangle(ax, ay, bx, by, outline=OK, width=2)
            c.create_text(ax + 2, ay - 2, text=f"{name} {score:.2f}", anchor="sw",
                          fill=OK, font=("Segoe UI Semibold", 9))
        if click:
            _, t, (cx, cy) = click      # the converter stores the click in client pixels
            r = 14 + 30 * (now - t)
            c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=WARN, width=3)
        if edge:                        # the game's own controls stop short of the bottom edge
            vx, vy, vw, vh = view.rect
            c.create_rectangle(vx, vy + vh - EDGE_PX, vx + vw, vy + vh, fill="#%02x%02x%02x" % edge, width=0)
        self._marks_drawn = bool(matches or click or edge)

    # ---- loop ----
    def _tick(self):
        if self.ctl.quit_requested:
            self._quit()
            return
        while True:
            try:
                kind, value = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                if kind == "overlay":
                    self._set_overlay(value)
                elif value == "sweeper":
                    self._open_sweeper()
                elif value == "journal":
                    self._open_journal()
                elif value == "lessons":
                    self._open_lessons()
            except Exception as e:
                self.rt.log(f"panel: {kind} {value}: {e!r}")
        try:
            self._draw_overlay()
        except Exception as e:          # a drawing problem must never take the client down
            self.rt.log(f"overlay: {e!r}")
        self.root.after(100, self._tick)

    # ---- tray ----
    def _start_tray(self):
        """A failure here costs only the icon. A builder process never creates one."""
        if tray is None or journal.read_only():
            return
        try:
            desktop = panelwin.RestoreDesktop()
            self._restorer = panelwin.PanelRestorer(lambda: panelwin.panel_states(PROFILE), self._open_from_tray,
                                                    lambda: self._game_picture(desktop), desktop)
            self.tray = tray.TrayIcon(self._tray_click, self.rt.log).start()
        except Exception as e:
            self.tray = None
            self.rt.log(f"tray: off: {e!r}")

    def _stop_tray(self):
        try:
            if self.tray:
                self.tray.stop()
        except Exception as e:
            self.rt.log(f"tray: {e!r}")

    def _open_from_tray(self):
        return open_panel(self.panel.url, panel_position(self.game)[0])

    def _tray_click(self):
        """On the tray thread, right after the user clicked the icon."""
        text = TRAY_NOTES.get(self._restorer.bring_back())
        if text:
            self._notice = (text, time.monotonic() + NOTICE_SECONDS)
            self.rt.log(f"tray: {text}")

    # ---- lifetime ----
    def run(self):
        self.panel = PanelServer(self.store, Actions(self)).start()
        _write_panel_file(self.panel)
        self._start_tray()
        position, safe = panel_position(self.game)
        if not safe:
            self.rt.log("panel: not opened, the game is on the only monitor. Double-click BAClient.pyw to open it")
        elif not open_panel(self.panel.url, position):
            self.rt.log("panel: Chrome not found. The address is in data/ui/panel.json")
        try:
            super().run()
        finally:
            self._stop_tray()
            _remove_panel_file()
            self.panel.close()


def _write_panel_file(panel):
    PANEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PANEL_FILE.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"pid": os.getpid(), "url": panel.url}), encoding="utf-8")
    os.replace(tmp, PANEL_FILE)


def _remove_panel_file():
    try:
        if json.loads(PANEL_FILE.read_text(encoding="utf-8")).get("pid") == os.getpid():
            PANEL_FILE.unlink()
    except (OSError, ValueError):
        pass


def reopen_panel():
    """The client is already running: bring its panel back. False if it has no web panel."""
    try:
        info = json.loads(PANEL_FILE.read_text(encoding="utf-8"))
        running = json.loads((control.STATE / "client.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if info.get("pid") != running.get("pid"):
        return False
    from ..game import Game             # a person asked for it, so it opens even on one monitor
    return open_panel(info["url"], panel_position(Game())[0])


def main():
    lock = control.acquire_lock()       # held until this process exits
    if lock is None:
        if not reopen_panel() and sys.stdout is not None:
            print("BAClient is already running.", flush=True)
        return
    ArtHud().run()
