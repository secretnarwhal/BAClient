"""The Journal window (HUD button): every input as a timeline, with the frame it saw."""
import tkinter as tk
from tkinter import ttk

import cv2
from PIL import Image, ImageTk

from . import journal
from .hud import ACCENT, BG, DIM, FG, OK, PANEL, WARN

ROW_LIMIT = 1500                 # newest rows shown; the file on disk keeps them all
FRAME_W, FRAME_H = 960, 540      # size of saved frames; event coordinates are 1920x1080


class JournalWindow(tk.Toplevel):
    def __init__(self, hud):
        super().__init__(hud.root)
        self.hud = hud
        self.title("BAClient journal")
        self.configure(bg=BG)
        self.geometry("1320x640+1980+90")
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.day = None
        self.offset = 0
        self.events = []
        self.rows = {}
        self.selected = None
        self._photo = None
        self._resize_job = None
        self.v_day = tk.StringVar()
        self.v_logs = tk.BooleanVar(value=False)
        self.v_follow = tk.BooleanVar(value=True)
        self._build()
        self._load_days()
        self._poll_job = self.after(1500, self._poll)

    # ---------------------------------------------------------- layout
    def _build(self):
        style = ttk.Style(self)
        style.configure("Journal.Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG,
                        borderwidth=0, rowheight=22, font=("Consolas", 9))
        style.configure("Journal.Treeview.Heading", background=BG, foreground=DIM, borderwidth=0,
                        font=("Segoe UI", 9))
        style.map("Journal.Treeview", background=[("selected", "#2f3a52")],
                  foreground=[("selected", FG)])

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=12, pady=(10, 6))
        tk.Label(bar, text="Day", bg=BG, fg=DIM, font=("Segoe UI", 9)).pack(side="left", padx=(0, 6))
        self.cmb_day = ttk.Combobox(bar, textvariable=self.v_day, state="readonly", width=12)
        self.cmb_day.pack(side="left")
        self.cmb_day.bind("<<ComboboxSelected>>", lambda _: self._open_day(self.v_day.get()))
        for text, var, cmd in (("Log lines", self.v_logs, self._rebuild), ("Follow newest", self.v_follow, self._rebuild)):
            tk.Checkbutton(bar, text=text, variable=var, command=cmd, bg=BG, fg=FG, selectcolor=PANEL,
                           activebackground=BG, activeforeground=FG, bd=0,
                           font=("Segoe UI", 9)).pack(side="left", padx=(14, 0))
        self.lbl_count = tk.Label(bar, text="", bg=BG, fg=DIM, font=("Segoe UI", 9))
        self.lbl_count.pack(side="right")

        body = tk.PanedWindow(self, orient="horizontal", bg=BG, bd=0, sashwidth=6)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        left = tk.Frame(body, bg=BG)
        cols = (("time", "Time", 66), ("actor", "Actor", 58), ("path", "Screen", 190), ("action", "Action", 240))
        self.tree = ttk.Treeview(left, columns=[c[0] for c in cols], show="headings",
                                 style="Journal.Treeview", selectmode="browse")
        for key, text, width in cols:
            self.tree.heading(key, text=text, anchor="w")
            self.tree.column(key, width=width, anchor="w", stretch=key == "action")
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.tag_configure("run", foreground=ACCENT)
        self.tree.tag_configure("error", foreground=WARN)
        self.tree.tag_configure("log", foreground=DIM)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        body.add(left, minsize=420, width=600)

        right = tk.Frame(body, bg=BG)
        self.canvas = tk.Canvas(right, bg=PANEL, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self._on_resize)
        self.lbl_detail = tk.Label(right, text="", bg=BG, fg=DIM, anchor="w", justify="left",
                                   wraplength=660, font=("Consolas", 9))
        self.lbl_detail.pack(fill="x", pady=(6, 0))
        body.add(right, minsize=320)

    # ------------------------------------------------------------ data
    def _load_days(self):
        days = journal.days()
        self.cmb_day.configure(values=days)
        if days and self.day is None:
            self._open_day(days[0])

    def _open_day(self, day):
        self.day = day
        self.v_day.set(day)
        self.offset = 0
        self.events = []
        self.selected = None
        self._read()
        self._rebuild()

    def _read(self):
        if self.day is None:
            return False
        new, self.offset = journal.read_new(self.day, self.offset)
        self.events += new
        return bool(new)

    def _poll(self):
        days = journal.days()
        if list(self.cmb_day.cget("values")) != days:
            self.cmb_day.configure(values=days)
        if self.day is None and days:
            self._open_day(days[0])
        elif self.v_follow.get() and days and self.day != days[0]:
            self._open_day(days[0])
        elif self._read():
            self._rebuild()
        self._poll_job = self.after(1500, self._poll)

    def _rebuild(self):
        rows = journal.timeline(self.events, logs=self.v_logs.get())
        inputs = sum(r["kind"] in journal.INPUT_KINDS for r in rows)
        shown = rows[-ROW_LIMIT:]
        self.lbl_count.configure(text=f"{inputs} inputs" + (f"  ·  newest {ROW_LIMIT} shown" if len(rows) > ROW_LIMIT else ""))
        self.rows = {r["row"]: r for r in shown}
        self.tree.delete(*self.tree.get_children())
        for r in shown:
            kind = r["kind"]
            tag = kind if kind in ("run", "error", "log") else ""
            self.tree.insert("", "end", iid=r["row"], tags=(tag,), values=(
                r.get("time", ""), r.get("actor", ""), journal.path_text(r), journal.describe(r)))
        if self.v_follow.get() and shown:
            target = next((r["row"] for r in reversed(shown) if r.get("frame")), shown[-1]["row"])
            self.selected = target
        if self.selected in self.rows:
            self.tree.selection_set(self.selected)
            self.tree.see(self.selected)
            self._show(self.rows[self.selected])
        else:
            self.selected = None
            self._show(None)

    def _on_select(self, _=None):
        sel = self.tree.selection()
        if not sel or sel[0] == self.selected:
            return
        self.selected = sel[0]
        if sel[0] != next(iter(reversed(self.tree.get_children())), None):
            self.v_follow.set(False)     # looking back: stop jumping to the newest row
        self._show(self.rows.get(sel[0]))

    def _on_resize(self, _):
        if self._resize_job:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(80, lambda: self._show(self.rows.get(self.selected)))

    # ----------------------------------------------------------- frame
    def _show(self, row):
        c = self.canvas
        c.delete("all")
        self._photo = None
        if row is None:
            self.lbl_detail.configure(text="")
            return
        detail = [f"{row.get('time', '')}  {row.get('actor', '')}  {journal.describe(row)}"]
        if row.get("task"):
            detail.append(f"task: {row['task']}" + (f"   run: {row['run']}" if row.get("run") else ""))
        if row.get("frame"):
            detail.append(row["frame"])
        self.lbl_detail.configure(text="\n".join(detail))
        w, h = max(c.winfo_width(), 2), max(c.winfo_height(), 2)
        img = cv2.imread(str(journal.LOGS / self.day / row["frame"])) if row.get("frame") else None
        if img is None:
            text = "frame deleted by retention" if row.get("frame") else "no frame for this event"
            c.create_text(w / 2, h / 2, text=text, fill=DIM, font=("Segoe UI", 10))
            return
        scale = min(w / FRAME_W, h / FRAME_H)
        size = (max(1, int(FRAME_W * scale)), max(1, int(FRAME_H * scale)))
        rgb = cv2.cvtColor(cv2.resize(img, size, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
        self._photo = ImageTk.PhotoImage(Image.fromarray(rgb))
        ox, oy = (w - size[0]) // 2, (h - size[1]) // 2
        c.create_image(ox, oy, image=self._photo, anchor="nw")
        px = lambda x, y: (ox + x / 2 * scale, oy + y / 2 * scale)   # 1920x1080 -> canvas
        if row["kind"] == "click":
            x, y = px(row["x"], row["y"])
            c.create_oval(x - 13, y - 13, x + 13, y + 13, outline=WARN, width=3, tags="mark")
            c.create_line(x - 20, y, x + 20, y, fill=WARN, tags="mark")
            c.create_line(x, y - 20, x, y + 20, fill=WARN, tags="mark")
        elif row["kind"] == "drag":
            x1, y1 = px(row["x1"], row["y1"])
            x2, y2 = px(row["x2"], row["y2"])
            c.create_line(x1, y1, x2, y2, fill=WARN, width=3, arrow="last", arrowshape=(14, 16, 6), tags="mark")
            c.create_oval(x1 - 6, y1 - 6, x1 + 6, y1 + 6, outline=OK, width=2, tags="mark")

    def _close(self):
        self.after_cancel(self._poll_job)
        self.hud.journal_win = None
        self.destroy()
