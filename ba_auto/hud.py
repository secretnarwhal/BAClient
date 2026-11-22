"""BAClient HUD: a control panel plus a click-through overlay drawn over the game.

The overlay is an ordinary top-most transparent window placed over the game's
client area. It is click-through (WS_EX_TRANSPARENT) and excluded from screen
capture (WDA_EXCLUDEFROMCAPTURE), so the game never receives its clicks and the
bot's own screenshots never contain it. Nothing is injected into the game.
"""
import ctypes
import ctypes.wintypes
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

from . import battlelog, control, crashlog, daily, journal, launcher, panelguard, viewport, watchdog
from .game import Game                   # imports DPI awareness first
from .runtime import Runtime
from .screens import label
from .vision import Vision

user32 = ctypes.windll.user32
GWL_EXSTYLE = -20
WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x80000, 0x20
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80, 0x08000000
WDA_EXCLUDEFROMCAPTURE = 0x11
VK_F8, WM_HOTKEY = 0x77, 0x0312
QUIT_WAIT = 3.0                  # seconds a closing client waits for the worker to let go of the input

KEY = "#010203"                  # transparent colour key for the overlay
SHADOW = "#07080b"               # near the key, so text edges blend into dark rather than a bright fringe
OUTLINE = ((-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1))
BG, PANEL, FG, DIM = "#14161c", "#1d2029", "#e8ecf4", "#8a93a6"
ACCENT, OK, WARN = "#4fc3f7", "#7bd88f", "#ff6b6b"
LOG_TEXT = "#c4cbd8"             # the overlay's log lines: DIM is too faint without a panel behind it


def outlined_text(canvas, x, y, text, fill, font, anchor="nw"):
    """Text with a thin dark outline, readable on any game screen with nothing behind it."""
    for dx, dy in OUTLINE:
        canvas.create_text(x + dx, y + dy, text=text, anchor=anchor, font=font, fill=SHADOW)
    canvas.create_text(x, y, text=text, anchor=anchor, font=font, fill=fill)


class Hud:
    def __init__(self):
        crashlog.install()
        self.rt = Runtime()
        self.game = Game(self.rt)
        self.vision = Vision(self.rt)
        self.watch = watchdog.Watchdog(self.rt)
        self.halt = None
        self.view = None
        self.battles = battlelog.BattleWatcher(self.rt)
        self.daily = daily.DailyWatcher()
        self.ctl = control.Controller(self.rt, self.game, self.vision, self.watch)
        self.title_watch = launcher.TitleWatcher(self.ctl, self.game, self.vision, self.rt.log)
        self.sweeper = None
        self.journal_win = None
        self.lessons_win = None
        self.overlay_on = True

        self.root = tk.Tk()
        self.root.title("BAClient")
        self.root.configure(bg=BG)
        self.root.geometry("+1960+60")     # goes always-on-top only once the guard sees it's clear of the game
        self._build_panel()
        self._build_overlay()
        threading.Thread(target=self._monitor, daemon=True).start()
        threading.Thread(target=self._hotkey, daemon=True).start()
        threading.Thread(target=self._housekeeping, daemon=True).start()
        self.root.protocol("WM_DELETE_WINDOW", self._window_closed)
        crashlog.watch_tk(self.root, self)
        self.panel_guards = [panelguard.PanelGuard(self._root_hwnd, self.rt.log)]
        self._guard_error = False
        self.root.after(60, self._check_panels)
        self.root.after(100, self._tick)

    @property
    def worker(self):
        """The running task's thread, or None when idle."""
        return self.ctl.thread if self.ctl.busy else None

    # ------------------------------------------------------------ panel
    def _build_panel(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TButton", background=PANEL, foreground=FG, borderwidth=0,
                        focuscolor=PANEL, padding=(10, 7), font=("Segoe UI", 10))
        style.map("TButton", background=[("active", "#2a2f3d"), ("disabled", BG)],
                  foreground=[("disabled", "#555c6b")])
        style.configure("Stop.TButton", background="#3a1f24", foreground=WARN)
        style.map("Stop.TButton", background=[("active", "#52262d")])

        tk.Label(self.root, text="BAClient", bg=BG, fg=FG,
                 font=("Segoe UI Semibold", 15)).pack(anchor="w", padx=14, pady=(12, 0))
        self.lbl_alert = tk.Label(self.root, text="", bg="#5a1a22", fg="#ffffff", justify="left",
                                  wraplength=430, font=("Segoe UI Semibold", 10))
        self.lbl_task = tk.Label(self.root, text="", bg=BG, fg=ACCENT, font=("Segoe UI", 10))
        self.lbl_task.pack(anchor="w", padx=14)
        self.lbl_screen = tk.Label(self.root, text="", bg=BG, fg=DIM, font=("Segoe UI", 9))
        self.lbl_screen.pack(anchor="w", padx=14)
        self.lbl_stats = tk.Label(self.root, text="", bg=BG, fg=OK, font=("Segoe UI", 9),
                                  justify="left")
        self.lbl_stats.pack(anchor="w", padx=14, pady=(0, 8))

        grid = tk.Frame(self.root, bg=BG)
        grid.pack(fill="x", padx=10)
        self.task_buttons = []
        for i, name in enumerate(("ministories", "skip", "episodes", "battle")):
            label = control.TASKS[name][0]
            b = ttk.Button(grid, text=label, command=lambda l=label, n=name: self._start(
                l, control.build_task(n, {}, self.game, self.vision), n))
            b.grid(row=i // 2, column=i % 2, sticky="ew", padx=3, pady=3)
            self.task_buttons.append(b)
        ttk.Button(grid, text="Mission sweeper...", command=self._open_sweeper).grid(
            row=2, column=0, sticky="ew", padx=3, pady=3)
        ttk.Button(grid, text="Journal...", command=self._open_journal).grid(
            row=2, column=1, sticky="ew", padx=3, pady=3)
        ttk.Button(grid, text="Lessons: who's where...", command=self._open_lessons).grid(
            row=3, column=0, sticky="ew", padx=3, pady=3)
        ttk.Button(grid, text="Lesson Atlas (all maps)", command=self._open_atlas).grid(
            row=3, column=1, sticky="ew", padx=3, pady=3)
        launch = ttk.Button(grid, text="Launch Blue Archive", command=lambda: self._start(
            control.TASKS["launch"][0], control.build_task("launch", {}, self.game, self.vision), "launch"))
        launch.grid(row=4, column=0, sticky="ew", padx=3, pady=3)
        self.task_buttons.append(launch)
        self.btn_login = ttk.Button(grid, text=self._login_text(), command=self._toggle_login)
        self.btn_login.grid(row=4, column=1, sticky="ew", padx=3, pady=3)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        row = tk.Frame(self.root, bg=BG)
        row.pack(fill="x", padx=10, pady=(4, 6))
        self.btn_stop = ttk.Button(row, text="Stop  (F8)", style="Stop.TButton", command=self._stop)
        self.btn_stop.pack(side="left", fill="x", expand=True, padx=3)
        self.btn_overlay = ttk.Button(row, text="Overlay: on", command=self._toggle_overlay)
        self.btn_overlay.pack(side="left", fill="x", expand=True, padx=3)

        self.logbox = tk.Text(self.root, height=14, width=58, bg=PANEL, fg=FG, bd=0,
                              font=("Consolas", 9), insertbackground=FG, wrap="word")
        self.logbox.pack(fill="both", expand=True, padx=13, pady=(0, 13))
        self.logbox.configure(state="disabled")
        self._log_seen = 0

    @staticmethod
    def _login_text():
        return f"Auto login: {'on' if launcher.auto_login() else 'off'}"

    def _toggle_login(self):
        self.ctl.handle({"op": "auto-login", "on": not launcher.auto_login(), "actor": "user"})
        self.btn_login.configure(text=self._login_text())

    def _start(self, label, fn, name=None):
        """A run started from this window: the person at the HUD is the actor."""
        self.ctl.start(label, fn, actor="user", name=name)

    def _open_sweeper(self):
        if self.sweeper is not None and self.sweeper.winfo_exists():
            self.sweeper.deiconify()
            self.sweeper.lift()
            return
        from .sweeper_ui import SweeperWindow
        self.sweeper = SweeperWindow(self)

    def _open_atlas(self):
        try:
            from . import atlas
            atlas.open_page()
        except Exception as e:
            self.rt.log(f"atlas: {e!r}")

    def _open_lessons(self):
        if self.lessons_win is not None and self.lessons_win.winfo_exists():
            self.lessons_win.refresh()
            self.lessons_win.deiconify()
            self.lessons_win.lift()
            return
        from .lessons_ui import LessonsWindow
        self.lessons_win = LessonsWindow(self)

    def _open_journal(self):
        if self.journal_win is not None and self.journal_win.winfo_exists():
            self.journal_win.deiconify()
            self.journal_win.lift()
            return
        from .journal_ui import JournalWindow
        self.journal_win = JournalWindow(self)

    def _stop(self):
        self.ctl.stop("user")

    def _toggle_overlay(self):
        self.overlay_on = not self.overlay_on
        self.btn_overlay.configure(text=f"Overlay: {'on' if self.overlay_on else 'off'}")
        (self.ov.deiconify if self.overlay_on else self.ov.withdraw)()

    # ---------------------------------------------------------- overlay
    def _build_overlay(self):
        self.ov = tk.Toplevel(self.root)
        self.ov.overrideredirect(True)
        self.ov.attributes("-topmost", True)
        self.ov.attributes("-transparentcolor", KEY)
        self.ov.configure(bg=KEY)
        self.canvas = tk.Canvas(self.ov, bg=KEY, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.ov.update_idletasks()
        hwnd = user32.GetParent(self.ov.winfo_id()) or self.ov.winfo_id()
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                              ex | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
        self._ov_rect = None

    def _draw_overlay(self):
        try:
            x, y, w, h = self.game.client_rect()
        except Exception:
            self.ov.withdraw()
            return
        if not self.overlay_on:
            return
        if (x, y, w, h) != self._ov_rect:
            self.ov.geometry(f"{w}x{h}+{x}+{y}")
            self._ov_rect = (x, y, w, h)
        view = self.view if self.view is not None and self.view.client == (x, y, w, h) else viewport.from_rect(x, y, w, h)
        c = self.canvas
        c.delete("all")
        now = time.time()

        # recent template matches
        for name, (x1, y1, x2, y2), score, t, *rest in list(self.rt.matches):
            if now - t > 1.2:
                continue
            shift = rest[0] if rest else (0, 0)
            ax, ay = view.to_client(x1, y1, shift)
            bx2, by2 = view.to_client(x2, y2, shift)
            c.create_rectangle(ax, ay, bx2, by2, outline=OK, width=2)
            c.create_text(ax + 2, ay - 2, text=f"{name} {score:.2f}", anchor="sw",
                          fill=OK, font=("Consolas", 9, "bold"))
        # last click
        if self.rt.last_click and now - self.rt.last_click[1] < 1.0:
            _, t, (cx, cy) = self.rt.last_click
            r = 14 + 30 * (now - t)
            c.create_oval(cx - r, cy - r, cx + r, cy + r, outline=WARN, width=3)

        # status lines, bottom-left, with no box: the game shows through
        running = self.ctl.busy
        lines = [f"BAClient  ·  {self.rt.task if running else 'idle'}",
                 f"screen: {self.rt.screen}"]
        stats = dict(self.rt.stats)      # the worker adds keys while this draws
        lines += [f"{k}: {v}" for k, v in stats.items()]
        lines += [l[10:] for l in list(self.rt.logs)[-3:]]
        vx, vy, _, vh = view.rect            # the card stays inside the game's picture
        bx, by = vx + 16, vy + vh - 22 - 18 * len(lines)
        if self.halt:
            outlined_text(c, bx, by - 30, f"HALTED: {self.halt.get('reason')}", WARN, ("Consolas", 12, "bold"))
        for i, line in enumerate(lines):
            outlined_text(c, bx, by + i * 18, line, ACCENT if i == 0 else (FG if i < 2 + len(stats) else LOG_TEXT),
                          ("Consolas", 10, "bold"))

    # ----------------------------------------------------------- loops
    def _tick(self):
        if self.ctl.quit_requested:
            self._quit()
            return
        running = self.ctl.busy
        if self.halt:
            self.lbl_alert.configure(text=f"HALTED: {self.halt.get('reason')}. All game input is "
                                          "refused. After checking: ctl clear-halt")
            if not self.lbl_alert.winfo_ismapped():
                self.lbl_alert.pack(fill="x", padx=14, pady=(4, 4), before=self.lbl_task)
        elif self.lbl_alert.winfo_ismapped():
            self.lbl_alert.pack_forget()
        self.lbl_task.configure(text=f"Task: {self.rt.task if running else 'idle'}")
        self.lbl_screen.configure(text=f"Screen: {self.rt.screen}")
        self.lbl_stats.configure(text="   ".join(f"{k}: {v}" for k, v in dict(self.rt.stats).items()))
        for b in self.task_buttons:
            b.state(["disabled"] if running else ["!disabled"])
        logs = list(self.rt.logs)
        if len(logs) != self._log_seen:
            self.logbox.configure(state="normal")
            self.logbox.delete("1.0", "end")
            self.logbox.insert("end", "\n".join(logs[-80:]))
            self.logbox.see("end")
            self.logbox.configure(state="disabled")
            self._log_seen = len(logs)
        self._draw_overlay()
        self.root.after(100, self._tick)

    def _monitor(self):
        """Read-only screen identification for the HUD, once a second."""
        g = Game(Runtime())              # own runtime: never stopped by the Stop button
        v = Vision(self.rt)
        said = {}                        # the same error every second would flood the journal
        while True:
            try:
                self.halt = watchdog.halted()
                frame = g.grab()
                self.view = frame.view_info
                if not self.view.confident:  # unreadable frames would feed the watchers nonsense
                    self.rt.screen = "game picture not identified"
                    time.sleep(1)
                    continue
                self.rt.screen = label(v, frame)
                try:
                    self.watch.observe(frame)
                    said.pop("watchdog", None)
                except Exception as e:
                    self._log_once(said, "watchdog", e)
                try:
                    self.title_watch.observe(self.rt.screen, frame)
                    said.pop("title watcher", None)
                except Exception as e:
                    self._log_once(said, "title watcher", e)
                try:
                    self.battles.observe(v, frame, self.rt.screen)
                    self.daily.observe(frame, self.rt.screen)
                except Exception as e:
                    self.rt.log(f"battle recorder error: {e!r}")
            except Exception:
                self.rt.screen = "game not found"
            time.sleep(1)

    def _log_once(self, said, name, error):
        text = repr(error)
        if said.get(name) != text:
            said[name] = text
            self.rt.log(f"{name} error: {text}")

    def _housekeeping(self):
        """Keep logs/ bounded: old frames go, the event log stays."""
        while True:
            try:
                deleted, freed = journal.prune()
                if deleted:
                    self.rt.log(f"journal: deleted {deleted} old frames ({freed} MB)")
            except Exception:
                pass
            time.sleep(1800)

    def _hotkey(self):
        if not user32.RegisterHotKey(None, 1, 0, VK_F8):
            self.rt.log("F8 hotkey unavailable (another app owns it) - use the Stop button")
            return
        msg = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_HOTKEY:
                self._stop()

    # ---------------------------------------------------------- staying off the game
    def _root_hwnd(self):
        return user32.GetAncestor(self.root.winfo_id(), 2)

    def _game_picture(self, desktop):
        """(game window, picture rect on screen), or (None, None) if the game isn't showing."""
        hwnd = desktop.game()
        if not hwnd:
            return None, None
        x, y, w, h = self.game.client_rect()
        view = self.view
        if view is not None and tuple(view.client) == (x, y, w, h):
            vx, vy, vw, vh = view.rect
            return hwnd, (x + vx, y + vy, x + vx + vw, y + vy + vh)
        return hwnd, (x, y, x + w, y + h)

    def _check_panels(self):
        """Twice a second, on the Tk thread. A builder process never changes a real window."""
        try:
            if not journal.read_only():
                hwnd, picture = self._game_picture(self.panel_guards[0].desktop)
                notes = [g.check(picture, hwnd) for g in self.panel_guards]
                self.ctl.panel_note = next((n for n in notes if n), None)
        except Exception as e:
            if not self._guard_error:    # once: this runs twice a second
                self._guard_error = True
                self.rt.log(f"panel guard error: {e!r}")
        try:
            self.root.after(500, self._check_panels)
        except Exception:                # the window is closing
            pass

    def _window_closed(self):
        crashlog.note("the HUD window was closed (its X, Alt+F4, or another program closing it)")
        self._quit()

    def _quit(self):
        self.rt.stop.set()
        worker = self.worker             # a press in flight still has to release and hand the cursor back
        if worker is not None and worker is not threading.current_thread():
            worker.join(QUIT_WAIT)
        self.root.destroy()

    def run(self):
        self.ctl.serve()
        self.rt.log("HUD ready - game window " + ("found" if self._game_ok() else "NOT found"))
        try:
            self.root.mainloop()
        except BaseException:
            crashlog.exception("the HUD's main loop stopped")
            raise
        finally:
            self.ctl.close()

    def _game_ok(self):
        try:
            self.game.find()
            return True
        except Exception:
            return False


def main():
    lock = control.acquire_lock()    # held until this process exits
    if lock is None:
        text = "BAClient is already running."
        if sys.stdout is None:       # double-clicked: there is no console to print to
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo("BAClient", text)
            root.destroy()
        else:
            print(text, flush=True)
        return
    Hud().run()
