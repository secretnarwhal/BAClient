"""The Mission Sweeper window: every stage, its rewards and Eleph, with marks and counts."""
import tkinter as tk
from tkinter import messagebox, ttk

from .tasks import sweeper

BG, PANEL, ROW, FG, DIM = "#14161c", "#1d2029", "#232733", "#e8ecf4", "#8a93a6"
ACCENT, OK, WARN, HARD, NORMAL = "#4fc3f7", "#7bd88f", "#ff6b6b", "#c8444a", "#2f4f78"
MAX_COUNT = 99


class SweeperWindow(tk.Toplevel):
    def __init__(self, hud):
        super().__init__(hud.root)
        self.hud = hud
        self.title("BAClient - Mission Sweeper")
        self.configure(bg=BG)
        self.geometry("1040x860+1980+80")
        self.minsize(860, 480)
        self.attributes("-topmost", True)
        self.data = sweeper.load()
        self.marks = sweeper.load_marks()
        self._images = {}
        self._vars = {}
        self._watching = False
        self.kind = tk.StringVar(value="All")
        self.only_ready = tk.BooleanVar(value=True)
        self.search = tk.StringVar()
        self._build()
        self.search.trace_add("write", lambda *_: self._refresh())
        self._refresh()
        self.protocol("WM_DELETE_WINDOW", self._close)

    # ------------------------------------------------------------ layout
    def _build(self):
        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", padx=14, pady=(12, 4))
        tk.Label(head, text="Mission Sweeper", bg=BG, fg=FG,
                 font=("Segoe UI Semibold", 15)).pack(side="left")
        self.lbl_scan = tk.Label(head, text="", bg=BG, fg=DIM, font=("Segoe UI", 9))
        self.lbl_scan.pack(side="left", padx=12, pady=(6, 0))
        self.btn_scan = ttk.Button(head, text="Scan missions", command=self._scan)
        self.btn_scan.pack(side="right")

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=14, pady=(2, 8))
        for value in ("All", "Hard", "Normal"):
            tk.Radiobutton(bar, text=value, value=value, variable=self.kind, indicatoron=False,
                           command=self._refresh, bg=PANEL, fg=FG, selectcolor="#34506b",
                           activebackground="#2a2f3d", activeforeground=FG, bd=0, padx=14, pady=5,
                           font=("Segoe UI", 10)).pack(side="left", padx=(0, 2))
        tk.Checkbutton(bar, text="Sweepable only", variable=self.only_ready, command=self._refresh,
                       bg=BG, fg=FG, selectcolor=PANEL, activebackground=BG, activeforeground=FG,
                       font=("Segoe UI", 10)).pack(side="left", padx=12)
        tk.Label(bar, text="Find", bg=BG, fg=DIM, font=("Segoe UI", 10)).pack(side="left")
        tk.Entry(bar, textvariable=self.search, width=8, bg=PANEL, fg=FG, insertbackground=FG,
                 bd=0, font=("Segoe UI", 10)).pack(side="left", padx=6, ipady=4)
        ttk.Button(bar, text="Clear marks", command=self._clear).pack(side="right")
        ttk.Button(bar, text="Mark all shown", command=self._mark_shown).pack(side="right", padx=4)

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=14)
        self.canvas = tk.Canvas(body, bg=BG, highlightthickness=0, bd=0)
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=BG)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>",
                        lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.bind("<MouseWheel>", self._wheel)

        foot = tk.Frame(self, bg=PANEL)
        foot.pack(fill="x", side="bottom")
        self.lbl_sum = tk.Label(foot, text="", bg=PANEL, fg=FG, font=("Segoe UI", 11))
        self.lbl_sum.pack(side="left", padx=14, pady=10)
        self.btn_sweep = ttk.Button(foot, text="Sweep marked", command=self._sweep)
        self.btn_sweep.pack(side="right", padx=14, pady=8)

    def _wheel(self, e):
        if isinstance(e.widget, tk.Spinbox):
            return
        self.canvas.yview_scroll(-e.delta // 120 * 3, "units")

    def _image(self, name):
        if not name:
            return None
        if name not in self._images:
            try:
                self._images[name] = tk.PhotoImage(file=str(sweeper.ICONS / name))
            except tk.TclError:
                self._images[name] = None
        return self._images[name]

    # ------------------------------------------------------------ list
    def _shown(self):
        needle = self.search.get().strip().lower()
        out = []
        for r in self.data["rows"]:
            if self.kind.get() != "All" and r["kind"] != self.kind.get():
                continue
            if self.only_ready.get() and r["status"] != "ready" and r["key"] not in self.marks:
                continue
            if needle and needle not in r["id"].lower():
                continue
            out.append(r)
        return out

    def _max(self, r):
        return (r["remaining"] or 0) if r["kind"] == "Hard" else MAX_COUNT

    def _default(self, r):
        return max(1, self._max(r)) if r["kind"] == "Hard" else 1

    def _refresh(self):
        for w in self.inner.winfo_children():
            w.destroy()
        self._vars = {}
        when = self.data.get("scanned_at")
        ap = self.data.get("ap")
        self.lbl_scan.configure(text=f"Last scan {when}  ·  AP {ap}" if when else "Not scanned yet")
        rows = self._shown()
        if not rows:
            msg = ("Open the game on the lobby, Campaign or Mission screen, then press Scan missions."
                   if not self.data["rows"] else "No stages match these filters.")
            tk.Label(self.inner, text=msg, bg=BG, fg=DIM, font=("Segoe UI", 11)).pack(pady=50)
        area = None
        for r in rows:
            if r["area"] != area:
                area = r["area"]
                tk.Label(self.inner, text=f"Area {area}", bg=BG, fg=ACCENT, anchor="w",
                         font=("Segoe UI Semibold", 11)).pack(fill="x", pady=(9, 3))
            self._row(r)
        self.canvas.yview_moveto(0)
        self._summary()

    def _row(self, r):
        ready = r["status"] == "ready"
        fg = FG if ready else DIM
        line = tk.Frame(self.inner, bg=ROW)
        line.pack(fill="x", pady=1)

        on = tk.BooleanVar(value=r["key"] in self.marks and ready)
        count = tk.StringVar(value=str(min(self.marks.get(r["key"], self._default(r)),
                                           max(1, self._max(r)))))
        self._vars[r["key"]] = (on, count, r)
        tk.Checkbutton(line, variable=on, bg=ROW, fg=OK, selectcolor=PANEL, activebackground=ROW,
                       activeforeground=OK, state="normal" if ready else "disabled",
                       command=lambda: self._toggle(r["key"])).pack(side="left", padx=(8, 2))
        tk.Label(line, text=r["id"], width=6, bg=ROW, fg=fg, anchor="w",
                 font=("Segoe UI Semibold", 12)).pack(side="left")
        tk.Label(line, text=r["kind"], width=7, bg=HARD if r["kind"] == "Hard" else NORMAL, fg="#ffffff",
                 font=("Segoe UI", 9)).pack(side="left", padx=(0, 8))

        cell = tk.Frame(line, bg=ROW, width=66, height=66)
        cell.pack(side="left", padx=(0, 8))
        cell.pack_propagate(False)
        eleph = self._image(r.get("eleph")) if r["kind"] == "Hard" else None
        if eleph:
            tk.Label(cell, image=eleph, bg=ROW, bd=0).pack(expand=True)

        strip = self._image(r.get("strip"))
        if strip:
            tk.Label(line, image=strip, bg=ROW, bd=0).pack(side="left", pady=4)

        box = tk.Frame(line, bg=ROW)
        box.pack(side="right", padx=10)
        if ready:
            tk.Label(box, text="Sweeps", bg=ROW, fg=DIM, font=("Segoe UI", 9)).pack(side="left", padx=(0, 4))
            spin = tk.Spinbox(box, from_=1, to=max(1, self._max(r)), width=3, textvariable=count,
                              bg=PANEL, fg=FG, buttonbackground=PANEL, insertbackground=FG, bd=0,
                              font=("Segoe UI", 10), command=lambda: self._count(r["key"]))
            spin.pack(side="left")
            spin.bind("<KeyRelease>", lambda _: self._count(r["key"]))
            spin.bind("<FocusOut>", lambda _: self._normalize(r["key"]))
        if r["kind"] == "Hard" and r["remaining"] is not None:
            left = f"{r['remaining']}/{r['limit'] or 3} left"
        elif r["status"] == "locked":
            left = "locked"
        else:
            left = "∞"
        if r["status"] == "locked":
            left = "locked"
        tk.Label(line, text=left, width=9, bg=ROW, fg=OK if ready else DIM,
                 font=("Segoe UI", 10)).pack(side="right")

    # ------------------------------------------------------------ marks
    def _read(self, k):
        on, count, r = self._vars[k]
        try:
            n = int(count.get())
        except ValueError:
            return None
        return max(1, min(n, max(1, self._max(r))))

    def _toggle(self, k):
        on, _count, _r = self._vars[k]
        if on.get():
            self.marks[k] = self._read(k) or self._default(_r)
        else:
            self.marks.pop(k, None)
        sweeper.save_marks(self.marks)
        self._summary()

    def _count(self, k):
        on, _c, _r = self._vars[k]
        n = self._read(k)
        if on.get() and n:
            self.marks[k] = n
            sweeper.save_marks(self.marks)
        self._summary()

    def _normalize(self, k):
        if k not in self._vars:
            return
        on, count, r = self._vars[k]
        count.set(str(self._read(k) or self._default(r)))
        self._count(k)

    def _mark_shown(self):
        for k, (on, _count, r) in self._vars.items():
            if r["status"] == "ready":
                on.set(True)
                self.marks[k] = self._read(k) or self._default(r)
        sweeper.save_marks(self.marks)
        self._summary()

    def _clear(self):
        self.marks = {}
        sweeper.save_marks(self.marks)
        for on, _count, _r in self._vars.values():
            on.set(False)
        self._summary()

    def _plan(self):
        by_key = {r["key"]: r for r in self.data["rows"]}
        plan = []
        for k, n in self.marks.items():
            r = by_key.get(k)
            if r and r["status"] == "ready":
                plan.append({"key": k, "count": min(n, max(1, self._max(r)))})
        return sorted(plan, key=lambda p: sweeper.sort_key(by_key[p["key"]]))

    def _summary(self):
        plan = self._plan()
        ap = sum(sweeper.cost(p["key"]) * p["count"] for p in plan)
        have = self.data.get("ap")
        over = have is not None and ap > have
        text = f"{len(plan)} stages marked  ·  {sum(p['count'] for p in plan)} sweeps  ·  {ap} AP"
        if have is not None:
            text += f" of {have}"
        self.lbl_sum.configure(text=text, fg=WARN if over else FG)
        busy = self.hud.worker is not None and self.hud.worker.is_alive()
        self.btn_sweep.state(["disabled"] if busy or not plan or over else ["!disabled"])
        self.btn_scan.state(["disabled"] if busy else ["!disabled"])

    # ------------------------------------------------------------ actions
    def _scan(self):
        if self.hud.worker and self.hud.worker.is_alive():
            return
        self.hud._start("Scan missions", lambda: sweeper.scan(self.hud.game, self.hud.vision))
        self._watch()

    def _sweep(self):
        plan = self._plan()
        if not plan or (self.hud.worker and self.hud.worker.is_alive()):
            return
        ap = sum(sweeper.cost(p["key"]) * p["count"] for p in plan)
        names = ", ".join(f"{p['key'][1:]}{' Hard' if p['key'][0] == 'H' else ''} x{p['count']}"
                          for p in plan[:12])
        if len(plan) > 12:
            names += f", and {len(plan) - 12} more"
        if not messagebox.askyesno("Sweep marked stages",
                                   f"Sweep {len(plan)} stages for {ap} AP?\n\n{names}\n\n"
                                   "Press F8 to stop. AP is never refilled.", parent=self):
            return
        self.hud._start("Sweep missions", lambda: sweeper.run(self.hud.game, self.hud.vision, plan))
        self._watch()

    def _watch(self):
        """Refresh the list when the running task ends."""
        self._summary()
        if not self._watching:
            self._watching = True
            self.after(500, self._poll)

    def _poll(self):
        if self.hud.worker and self.hud.worker.is_alive():
            self.after(500, self._poll)
            return
        self._watching = False
        self.data = sweeper.load()
        self._images = {}
        ready = {r["key"] for r in self.data["rows"] if r["status"] == "ready"}
        for k in [k for k in self.marks if k not in ready]:
            del self.marks[k]
        sweeper.save_marks(self.marks)
        self._refresh()

    def _close(self):
        self.hud.sweeper = None
        self.destroy()
