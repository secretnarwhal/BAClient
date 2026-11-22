"""The Lessons window (HUD button): every location, room and student in one place."""
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageDraw, ImageTk

from . import portraits
from .hud import ACCENT, BG, DIM, FG, OK, PANEL, WARN
from .tasks import lessons

FACE = 58
COLUMNS = 3


class LessonsWindow(tk.Toplevel):
    def __init__(self, hud):
        super().__init__(hud.root)
        self.hud = hud
        self.title("BAClient lessons")
        self.configure(bg=BG)
        self.geometry("1120x760+1980+90")
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.v_find = tk.StringVar()
        self.v_owned = tk.BooleanVar(value=False)
        self._faces = {}
        self._build()
        self.refresh()

    def _build(self):
        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=14, pady=(12, 6))
        tk.Label(bar, text="Lessons", bg=BG, fg=FG, font=("Segoe UI Semibold", 15)).pack(side="left")
        self.lbl_state = tk.Label(bar, text="", bg=BG, fg=DIM, font=("Segoe UI", 9))
        self.lbl_state.pack(side="left", padx=12)
        ttk.Button(bar, text="Reload", command=self.refresh).pack(side="right")
        tk.Checkbutton(bar, text="Only students I own", variable=self.v_owned, command=self.refresh, bg=BG, fg=FG,
                       selectcolor=PANEL, activebackground=BG, activeforeground=FG, bd=0,
                       font=("Segoe UI", 9)).pack(side="right", padx=10)
        entry = tk.Entry(bar, textvariable=self.v_find, bg=PANEL, fg=FG, insertbackground=FG, bd=0, width=20,
                         font=("Segoe UI", 10))
        entry.pack(side="right", ipady=4)
        tk.Label(bar, text="Find student", bg=BG, fg=DIM, font=("Segoe UI", 9)).pack(side="right", padx=6)
        self.v_find.trace_add("write", lambda *_: self.refresh())

        self.lbl_best = tk.Label(self, text="", bg=BG, fg=OK, anchor="w", justify="left", font=("Segoe UI", 9),
                                 wraplength=1080)
        self.lbl_best.pack(fill="x", padx=14, pady=(0, 6))
        wrap = tk.Frame(self, bg=BG)
        wrap.pack(fill="both", expand=True, padx=(14, 4), pady=(0, 12))
        self.canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0, bd=0)
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.body = tk.Frame(self.canvas, bg=BG)
        self._win = self.canvas.create_window(0, 0, anchor="nw", window=self.body)
        self.body.bind("<Configure>", lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-e.delta // 120, "units"))

    def _face(self, dev, owned):
        key = (dev, owned)
        if key not in self._faces:
            img = None
            entry = portraits.art_entry(dev)
            for kind in ("icon", "collection", "battle"):
                if entry.get(kind):
                    img = Image.open(portraits.ART / entry[kind]).convert("RGB")
                    break
            if img is None:
                img = Image.new("RGB", (FACE, FACE), PANEL)
            side = min(img.size)
            img = img.crop(((img.width - side) // 2, 0, (img.width + side) // 2, side)).resize((FACE, FACE), Image.LANCZOS)
            draw = ImageDraw.Draw(img)
            draw.rectangle((0, 0, FACE - 1, FACE - 1), outline="#ff8fb1" if owned else "#3a4050", width=2)
            self._faces[key] = ImageTk.PhotoImage(img)
        return self._faces[key]

    def refresh(self):
        for child in self.body.winfo_children():
            child.destroy()
        data = lessons.load()
        needle = self.v_find.get().strip().lower()
        only_owned = self.v_owned.get()
        if not data["locations"]:
            self.lbl_state.configure(text="")
            self.lbl_best.configure(text="")
            tk.Label(self.body, text="No lesson scan yet. Run: python -m ba_auto ctl run lesson-scan", bg=BG, fg=DIM,
                     font=("Segoe UI", 11)).pack(pady=40)
            return
        stale = lessons.is_stale(data)
        state = "complete" if data.get("complete") else "partial"
        self.lbl_state.configure(text=f"scanned {data['scanned_at']}, {state}" + ("  ·  STALE: before today's reset" if stale else ""),
                                 fg=WARN if stale else DIM)
        best = lessons.best_rooms(data, 3)
        self.lbl_best.configure(text="Best rooms:  " + "   |   ".join(
            f"{b['location']} > {b['room']}: {', '.join(b['students'])}" for b in best) if best else "")
        for loc in data["locations"]:
            rooms = []
            for room in loc["rooms"]:
                people = [s for s in room["students"] if (s["owned"] or not only_owned)
                          and (not needle or needle in str(s.get("student")).lower())]
                if people or not (needle or only_owned):
                    rooms.append((room, people))
            if not rooms:
                continue
            title = loc["location"] + (f"   Rank {loc['rank']}" if loc.get("rank") else "")
            tk.Label(self.body, text=title, bg=BG, fg=ACCENT, anchor="w", font=("Segoe UI Semibold", 11)).pack(
                fill="x", pady=(10, 3))
            grid = tk.Frame(self.body, bg=BG)
            grid.pack(fill="x")
            for c in range(COLUMNS):
                grid.columnconfigure(c, weight=1, uniform="room")
            for i, (room, people) in enumerate(rooms):
                self._room(grid, room, people).grid(row=i // COLUMNS, column=i % COLUMNS, sticky="nsew", padx=3, pady=3)

    def _room(self, parent, room, people):
        card = tk.Frame(parent, bg=PANEL)
        head = room["room"] + ("  (locked)" if room["locked"] else "")
        tk.Label(card, text=head, bg=PANEL, fg=DIM if room["locked"] else FG, anchor="w",
                 font=("Segoe UI Semibold", 9)).pack(fill="x", padx=8, pady=(6, 2))
        row = tk.Frame(card, bg=PANEL)
        row.pack(fill="x", padx=6, pady=(0, 6))
        for s in people:
            cell = tk.Frame(row, bg=PANEL)
            cell.pack(side="left", padx=3)
            face = self._face(s.get("dev"), s["owned"])
            tk.Label(cell, image=face, bg=PANEL, bd=0).pack()
            name = (s.get("student") or "?").replace(" (", "\n(")
            bond = f"  ♥{s['bond']}" if s.get("bond") else ""
            tk.Label(cell, text=name + bond, bg=PANEL, fg=FG if s["owned"] else DIM, width=11, justify="center",
                     font=("Segoe UI", 7)).pack()
        if not people:
            tk.Label(row, text="can't be used yet" if room["locked"] else "empty", bg=PANEL, fg=DIM, font=("Segoe UI", 8)).pack(side="left", padx=4, pady=14)
        return card

    def _close(self):
        self.hud.lessons_win = None
        self.destroy()
