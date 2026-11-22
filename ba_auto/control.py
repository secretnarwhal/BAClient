"""Control the one running client (the HUD) from other processes.

  python -m ba_auto ctl start                  open the client if it isn't running
  python -m ba_auto ctl status                 current run, last result, screen, recent log
  python -m ba_auto ctl tasks                  what can be run
  python -m ba_auto ctl run TASK [options]     start a run (--wait [S] to block)
  python -m ba_auto ctl wait [ID] [--timeout S]  block until a run ends
  python -m ba_auto ctl stop                   same as pressing F8
  python -m ba_auto ctl quit                   close the client (refused during a run)
  python -m ba_auto ctl clear-halt --note WHY  resume after a watchdog halt, once a person has looked
  python -m ba_auto ctl clear-login-hold --note WHY  allow logging in again after a duplicate login
  python -m ba_auto ctl auto-login on|off      the Auto login setting (default on)
  python -m ba_auto ctl ui [new|classic]       which client window opens at the next start

Exit codes: 0 done, 1 error, 2 stopped, 3 still running, 4 no client, 5 refused.

The client listens on 127.0.0.1 on a random port. The port and a random token are in
data/ctl/client.json, so only processes that can read the project folder can talk to it.
Tasks run inside the client and all their input still goes through Game, so they are
journaled, drawn on the overlay and stopped by F8. Journal events carry the actor who
asked for the run.
"""
import argparse
import collections
import hmac
import json
import msvcrt
import os
import secrets
import socket
import socketserver
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from . import journal, watchdog
from .runtime import Stopped, TitleScreen

STATE = journal.ROOT / "data" / "ctl"
EXIT_OK, EXIT_ERROR, EXIT_STOPPED, EXIT_RUNNING, EXIT_NO_CLIENT, EXIT_REFUSED = range(6)
OUTCOME_EXIT = {"done": EXIT_OK, "error": EXIT_ERROR, "stopped": EXIT_STOPPED}
NOT_SENT_RESTARTS = 5            # a task that is safe to repeat starts over this many times
MAX_SWEEPS = 99                 # the most one Multi-Sweep row accepts

TASKS = {
    "launch": ("Launch Blue Archive", "through Steam if it isn't running, to the title, then log in if Auto login is on"),
    "login": ("Log in", "press Click to Start, waiting while you're in the game or another game is open, then go to the lobby; stops on anything unknown"),
    "lobby": ("Return to lobby", "Home and known popups' X/Cancel only; stops on anything unknown"),
    "skip": ("Skip this story", "skip the story on screen"),
    "episodes": ("Skip New episodes (chapter)", "skip New episodes; --limit N (default 50)"),
    "ministories": ("Clear all mini stories", "clear every chapter tagged New"),
    "groupstories": ("Clear all group stories", "clear Group Story chapters tagged New; stops on any battle or popup; --limit N chapters"),
    "battle": ("Auto-proceed battle", "one battle; --timeout S (default 300)"),
    "ex": ("Use EX skill", "in battle: --slot 1-3 --at X,Y [--times N] [--timeout S]; waits for the card to be ready"),
    "roster-scan": ("Roster scan", "read every student into data/roster.json (spends nothing); --limit N"),
    "lesson-scan": ("Lesson scan", "read every location, room and student (never starts a lesson); --limit N locations"),
    "momotalk": ("Auto MomoTalk", "answer unread students, skip their relationship stories (no gifts); --limit N students"),
    "lesson-run": ("Run lessons", "plan only by default; --rehearse checks one room; --live spends tickets; --max-tickets N"),
    "lesson-teach": ("Teach marked lessons", "teach exactly the rooms marked in the Atlas; --room ID (repeatable) --scan-ts T [--rehearse]"),
    "claims": ("Claim mail and tasks", "dry run unless --go; --limit N rows per section; never presses Claim All"),
    "cafe": ("Daily cafe", "dry run unless --go; earnings, headpats, invite per config/cafe.json; --limit N headpats per cafe"),
    "cafe-recon": ("Cafe recon", "save floor frames of both cafes (navigation only)"),
    "conquest": ("Conquest plan", "dry run: prints the plan and AP cost; no input"),
    "conquest-recon": ("Conquest recon", "opens the next target's menu and dialog, then closes; spends nothing"),
    "conquest-step": ("Conquest step", "real battles: --steps N (default 1), 5 AP each"),
    "event-stories": ("Clear event stories", "skip unfinished Event Recap stories, take their story rewards; --limit N events; refuses until recon is done"),
    "event-stories-recon": ("Event story recon", "read-only: --stage grid | scroll:DY --at X,Y | card --at X,Y; never enters a story or claims"),
    "smart-shadow": ("Smart battle shadow", "watch the battle on screen and log what it would cast; no input; --seconds S"),
    "smart-recon": ("Smart battle recon", "read each EX card's text and cancel; low-stakes battles only"),
    "smart-battle": ("Smart battle", "control EX skills in the battle on screen; never starts one; --timeout S"),
    "smart-proof": ("Smart battle proof", "measure one fight: --stage NAME, AUTO baseline, or --live for smart"),
    "daily-pack": ("Free Daily Pack", "dry run unless --go; --rehearse presses the card once and never confirms a dialog; only Cash Shop > General > Daily"),
    "daily-scan": ("Daily scan", "read tasks, mail, lessons, cafe and tickets for `todo` (free navigation only)"),
    "sweep-scan": ("Scan missions", "read every stage into data/sweeper (spends nothing)"),
    "sweep": ("Sweep missions", "spends AP, never refills it; --stage KEY:COUNT, repeatable (N1-1:2, H11-3:1)"),
}


def _sweep_plan(specs):
    """Validate --stage KEY:COUNT against the last scan. Raises ValueError."""
    from .tasks import sweeper
    if not specs:
        raise ValueError("sweep needs at least one --stage KEY:COUNT")
    data = sweeper.load()
    rows = {r["key"]: r for r in data["rows"]}
    if not rows:
        raise ValueError("no scan data: run sweep-scan first")
    plan = {}
    for spec in specs:
        key, _, n = str(spec).partition(":")
        key = key.strip().upper()
        try:
            count = int(n or 1)
        except ValueError:
            raise ValueError(f"bad count in {spec!r}") from None
        row = rows.get(key)
        if row is None:
            raise ValueError(f"unknown stage {key} (keys look like N1-1 or H11-3)")
        if row["status"] != "ready":
            raise ValueError(f"{key} is not sweepable: {row['status']}")
        limit = (row["remaining"] or 0) if row["kind"] == "Hard" else MAX_SWEEPS
        if key in plan or not 1 <= count <= limit:
            raise ValueError(f"{key}: count must be 1-{limit}, once per stage")
        plan[key] = count
    ap = sum(sweeper.cost(k) * c for k, c in plan.items())
    if data.get("ap") is not None and ap > data["ap"]:
        raise ValueError(f"plan costs {ap} AP but the last scan saw {data['ap']}")
    return [{"key": k, "count": c} for k, c in plan.items()]


def _int_arg(args, name, default, low, high):
    value = args.get(name)
    if value is None:
        return default
    if not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be {low}-{high}")
    return value


def build_task(name, args, game, vision):
    """The function that runs task `name`. Raises ValueError on a bad request."""
    from . import nav
    from . import daily
    from .tasks import battle, lessons, ministory, roster, story, sweeper
    if name == "lobby":
        return lambda: nav.go_lobby(game, vision)
    if name in ("launch", "login"):
        from . import launcher
        return (lambda: launcher.launch(game, vision)) if name == "launch" else (lambda: launcher.log_in(game, vision))
    # Lane tasks import here, one at a time, so a lane's work in progress can't break other tasks.
    if name == "momotalk":
        from .tasks import momotalk
        limit = _int_arg(args, "limit", None, 1, momotalk.MAX_STUDENTS)
        return lambda: momotalk.run(game, vision, limit=limit)
    if name == "lesson-run":
        from .tasks import lesson_run
        limit = _int_arg(args, "max_tickets", None, 1, lesson_run.MAX_TICKETS)
        return lambda: lesson_run.run(game, vision, live=bool(args.get("live")),
                                      rehearse=bool(args.get("rehearse")), max_tickets=limit)
    if name == "lesson-teach":
        from .tasks import lesson_run
        return lesson_run.teach_task(game, vision, args)
    if name == "claims":
        from .tasks import claims
        limit = _int_arg(args, "limit", None, 1, claims.MAX_CLAIMS)
        return lambda: claims.run(game, vision, dry_run=args.get("go") is not True, limit=limit)
    if name == "cafe":
        from .tasks import cafe
        limit = _int_arg(args, "limit", cafe.MAX_PATS, 1, cafe.MAX_PATS)
        return lambda: cafe.run(game, vision, dry_run=args.get("go") is not True, max_pats=limit)
    if name == "cafe-recon":
        from .tasks import cafe
        return lambda: cafe.recon(game, vision)
    if name == "conquest":
        from .tasks import conquest
        return lambda: conquest.run(game, vision)
    if name == "conquest-recon":
        from .tasks import conquest
        return lambda: conquest.recon(game, vision)
    if name == "conquest-step":
        from .tasks import conquest
        steps = _int_arg(args, "steps", 1, 1, 20)
        return lambda: conquest.run(game, vision, execute=True, steps=steps)
    if name == "event-stories":
        from .tasks import eventstory
        limit = _int_arg(args, "limit", None, 1, eventstory.MAX_EVENTS)
        return lambda: eventstory.run(game, vision, limit=limit)
    if name == "event-stories-recon":
        from .tasks import eventstory
        stage, point, by = eventstory.parse_recon(args.get("stages"), args.get("at"))
        return lambda: eventstory.recon(game, vision, stage, point, by)
    if name == "smart-shadow":
        from .battler import shadow
        seconds = _int_arg(args, "seconds", 180, 10, 600)
        return lambda: shadow.run(game, vision, seconds)
    if name == "smart-recon":
        from .tasks import smartbattle
        return lambda: smartbattle.recon(game, vision)
    if name == "smart-battle":
        from .tasks import smartbattle
        timeout = _int_arg(args, "timeout", 420, 30, 900)
        return lambda: smartbattle.run(game, vision, timeout=timeout)
    if name == "smart-proof":
        from .battler import proof
        stages = args.get("stages") or []       # a free label here, so no sweep validation
        if len(stages) != 1 or not str(stages[0]).strip():
            raise ValueError("smart-proof needs one --stage NAME")
        stage = str(stages[0]).strip()
        return lambda: proof.run(game, vision, stage, "smart" if args.get("live") else "auto")
    if name == "lesson-scan":
        limit = _int_arg(args, "limit", None, 1, lessons.MAX_AREAS)
        return lambda: lessons.scan(game, vision, limit=limit)
    if name == "daily-scan":
        return lambda: daily.scan(game, vision)
    if name == "roster-scan":
        limit = _int_arg(args, "limit", None, 1, roster.MAX_STUDENTS)
        return lambda: roster.scan(game, vision, limit=limit)
    if name == "skip":
        return lambda: story.skip(game, vision)
    if name == "episodes":
        limit = _int_arg(args, "limit", 50, 1, 500)
        def run_episodes():
            n = story.episodes(game, vision, limit=limit)
            return {"skipped": n, "note": "no New episodes on this list" if n == 0 else None}
        return run_episodes
    if name == "ministories":
        return lambda: ministory.clear_all(game, vision)
    if name == "groupstories":
        limit = _int_arg(args, "limit", None, 1, ministory.MAX_CHAPTERS)
        return lambda: ministory.clear_group(game, vision, limit=limit)
    if name == "battle":
        timeout = _int_arg(args, "timeout", 300, 10, 3600)
        return lambda: battle.run(game, vision, timeout)
    if name == "ex":
        slot = _int_arg(args, "slot", None, 1, 3)
        if slot is None:
            raise ValueError("ex needs --slot 1-3")
        times = _int_arg(args, "times", 1, 1, 50)
        timeout = _int_arg(args, "timeout", 60, 1, 600)
        try:
            x, y = (int(v) for v in str(args.get("at")).split(","))
        except ValueError:
            raise ValueError("ex needs --at X,Y") from None
        if not (battle.FIELD[0] <= x <= battle.FIELD[2] and battle.FIELD[1] <= y <= battle.FIELD[3]):
            raise ValueError(f"--at must be on the battlefield {battle.FIELD}")
        return lambda: battle.use_ex(game, vision, slot, (x, y), times=times, timeout=timeout)
    if name == "sweep-scan":
        return lambda: sweeper.scan(game, vision)
    if name == "sweep":
        plan = _sweep_plan(args.get("stages"))
        return lambda: sweeper.run(game, vision, plan)
    if name == "daily-pack":
        from .tasks import dailypack
        if args.get("go") and args.get("rehearse"):
            raise ValueError("daily-pack: choose --go or --rehearse")
        mode = "go" if args.get("go") is True else "recon" if args.get("rehearse") is True else "dry"
        return lambda: dailypack.run(game, vision, mode=mode)
    raise ValueError(f"unknown task {name!r}; one of: {', '.join(TASKS)}")


# ---------------------------------------------------------------- inside the client
class Run:
    def __init__(self, run_id, label, name, actor):
        self.id, self.label, self.name, self.actor = run_id, label, name, actor
        self.started, self.ended = time.time(), None
        self.outcome = self.result = self.error = self.stopped_by = None
        self.stats = {}
        self.resources = {}

    def public(self):
        return {
            "id": self.id, "task": self.name or self.label, "label": self.label, "actor": self.actor,
            "started": time.strftime("%H:%M:%S", time.localtime(self.started)),
            "seconds": round((self.ended or time.time()) - self.started, 1),
            "outcome": self.outcome, "result": self.result, "error": self.error,
            "stopped_by": self.stopped_by, "stats": self.stats, "resources": self.resources,
        }


def _jsonable(value):
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


class Controller:
    """Owns the client's single worker. The HUD's buttons and `ctl` both start runs here."""

    def __init__(self, rt, game, vision, watch=None):
        self.rt, self.game, self.vision, self.watch = rt, game, vision, watch
        self.token = secrets.token_hex(16)
        self.thread = None
        self.current = None
        self.history = collections.deque(maxlen=30)
        self.quit_requested = False
        self._lock = threading.Lock()
        self._count = 0
        self._server = None
        self.panel_note = None           # why BAClient's panel is behind the game, if it is

    @property
    def busy(self):
        return self.current is not None

    def start(self, label, fn, actor="user", name=None):
        """Start a run, or return None if one is already going or input is halted."""
        halt = watchdog.halted()
        if halt:
            self.rt.log(f"not starting {label}: halted ({halt.get('reason')})")
            return None
        problem = getattr(self.game, "window_problem", lambda: None)()
        if problem:
            self.rt.log(f"not starting {label}: {problem}")
            return None
        with self._lock:
            if self.current is not None:
                return None
            self._count += 1
            run = Run(f"{time.strftime('%H%M%S')}-{self._count}", label, name, actor)
            self.rt.stop.clear()
            self.rt.task = label
            self.current = run
            self.history.append(run)
            self.thread = threading.Thread(target=self._work, args=(run, fn), daemon=True)
            self.thread.start()
        return run

    def _work(self, run, fn):
        journal.set_context(actor=run.actor, run=run.id)
        before = dict(self.rt.stats)
        held = dict(self.watch.values) if self.watch else {}
        journal.record("run", phase="start", task=run.label, resources=held)
        outcome, result, error = "error", None, None
        try:
            self.rt.log(f"start: {run.label} ({run.actor})")
            attempt = lambda: self._retrying(run, fn)
            try:
                result = attempt()
            except TitleScreen as e:
                result = self._after_title(run, attempt, e)
            outcome = "done"
            self.rt.log(f"done: {run.label}")
        except Stopped:
            outcome = "stopped"
            self.rt.log(f"stopped: {run.label}")
        except Exception as e:          # keep the client alive and show what broke
            error = repr(e)
            journal.record("error", task=run.label, trace=traceback.format_exc())
            self.rt.log(f"error in {run.label}: {e!r}")
        finally:
            run.stats = {k: v - before.get(k, 0) for k, v in self.rt.stats.items()
                         if v != before.get(k, 0)}
            run.resources = {"before": held, "after": dict(self.watch.values) if self.watch else {}}
            journal.record("run", phase="end", task=run.label, outcome=outcome, error=error,
                           stats=run.stats, result=_jsonable(result), resources=run.resources)
            with self._lock:
                run.result, run.error, run.ended = _jsonable(result), error, time.time()
                run.outcome = outcome
                self.current = None
                self.rt.task = "idle"
                self.rt.stop.clear()

    def _retrying(self, run, fn):
        """Input that wasn't sent because the screen moved: a task that is safe to repeat looks again.
        Anything that can spend or claim stops instead."""
        from . import inputguard, launcher
        for restarts in range(NOT_SENT_RESTARTS + 1):
            try:
                return fn()
            except inputguard.NotSent as e:
                if run.name not in launcher.RESTARTABLE or restarts == NOT_SENT_RESTARTS:
                    raise
                self.rt.log(f"{run.label}: {e}; starting it over")

    def _after_title(self, run, fn, error):
        """A run hit the title: log back in. Only tasks that are safe to repeat start over."""
        from . import launcher
        if run.name in ("launch", "login") or not launcher.auto_login():
            raise error
        self.rt.log(f"{run.label}: the game is on the title screen, logging in")
        launcher.log_in(self.game, self.vision, self.rt.log)
        if run.name not in launcher.RESTARTABLE:
            raise RuntimeError(f"logged back in, but '{run.label}' isn't restarted automatically. "
                               "Check what it finished, then run it again")
        self.rt.log(f"{run.label}: logged back in, starting it over")
        return fn()

    def stop(self, by="user"):
        with self._lock:
            if self.current is None:
                return False
            self.current.stopped_by = by
            self.rt.stop.set()
        self.rt.log(f"stop requested by {by}")
        return True

    # ---- requests ----
    def _find(self, run_id):
        if run_id:
            return next((r for r in self.history if r.id == run_id), None)
        return self.current or (self.history[-1] if self.history else None)

    def _status(self, req):
        current = self.current
        ended = [r for r in self.history if r.outcome]
        found = self._find(req.get("id"))
        return {
            "ok": True, "pid": os.getpid(), "busy": current is not None,
            "run": current.public() if current else None,
            "last": ended[-1].public() if ended else None,
            "requested": found.public() if found else None,
            "screen": self.rt.screen,
            "resources": dict(self.watch.values) if self.watch else None,
            "halt": watchdog.halted(),
            "launcher": self._launcher(),
            "panel": self.panel_note,
            "log": list(self.rt.logs)[-12:],
        }

    def _launcher(self):
        from . import launcher
        return launcher.status(self.rt.screen)

    def handle(self, req):
        op = req.get("op")
        actor = str(req.get("actor") or "client")
        if op == "ping":
            return {"ok": True, "pid": os.getpid()}
        if op == "status":
            return self._status(req)
        if op in ("run", "stop", "quit", "clear-halt", "auto-login") and journal.read_only(actor):
            return {"ok": False, "error": "the builder session is read-only: the player runs the game"}
        if op == "auto-login":
            from . import launcher
            on = launcher.set_auto_login(bool(req.get("on")), actor)
            self.rt.log(f"auto login {'on' if on else 'off'} ({actor})")
            return {"ok": True, "auto_login": on}
        if op == "clear-halt":
            was = watchdog.clear(actor, str(req.get("note") or ""))
            if was and self.watch:
                self.watch.rebase()
            if was:
                self.rt.log(f"halt cleared by {actor}: {req.get('note') or 'no note'}")
            return {"ok": True, "cleared": was}
        if op == "run":
            halt = watchdog.halted()
            if halt:
                return {"ok": False, "halted": halt, "error": f"halted: {halt.get('reason')}"}
            problem = getattr(self.game, "window_problem", lambda: None)()
            if problem:
                return {"ok": False, "error": problem}
            name = req.get("task")
            try:
                fn = build_task(name, req.get("args") or {}, self.game, self.vision)
            except ValueError as e:
                return {"ok": False, "error": str(e)}
            run = self.start(TASKS[name][0], fn, actor=actor, name=name)
            if run is None:
                return {"ok": False, "busy": True, "error": f"busy: {self.current.label if self.current else 'a run'}"}
            return {"ok": True, "id": run.id, "task": name}
        if op == "stop":
            return {"ok": True, "stopping": self.stop(by=actor)}
        if op == "quit":
            if self.busy:
                return {"ok": False, "busy": True, "error": "a run is active: ctl stop first"}
            self.rt.log(f"quit requested by {actor}")
            self.quit_requested = True
            return {"ok": True}
        return {"ok": False, "error": f"unknown op {op!r}"}

    # ---- server ----
    def serve(self):
        ctl = self

        class Handler(socketserver.StreamRequestHandler):
            timeout = 5

            def handle(self):
                try:
                    req = json.loads(self.rfile.readline(65536))
                    token = str(req.get("token", "")).encode()
                    if not hmac.compare_digest(token, ctl.token.encode()):
                        reply = {"ok": False, "error": "bad token"}
                    else:
                        reply = ctl.handle(req)
                except Exception as e:
                    reply = {"ok": False, "error": repr(e)}
                self.wfile.write((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = False

        self._server = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        info = {"pid": os.getpid(), "port": self._server.server_address[1], "token": self.token}
        tmp = STATE / f"client.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(info), encoding="utf-8")
        os.replace(tmp, STATE / "client.json")

    def close(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            try:
                info = _info()
                if info and info.get("pid") == os.getpid():
                    (STATE / "client.json").unlink()
            except OSError:
                pass


def acquire_lock():
    """Hold this for the client's lifetime. None means another client is already open.
    The OS releases it if the process dies, so a crash never blocks the next start."""
    STATE.mkdir(parents=True, exist_ok=True)
    f = open(STATE / "client.lock", "a+b")
    try:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        f.close()
        return None
    return f


# ---------------------------------------------------------------- outside the client
class NoClient(Exception):
    pass


def _info():
    try:
        return json.loads((STATE / "client.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def call(op, timeout=3.0, **fields):
    info = _info()
    if not info:
        raise NoClient()
    req = {"op": op, "token": info["token"], "actor": journal.actor(), **fields}
    try:
        with socket.create_connection(("127.0.0.1", info["port"]), timeout=timeout) as s:
            s.sendall((json.dumps(req) + "\n").encode("utf-8"))
            with s.makefile("rb") as f:
                line = f.readline()
        return json.loads(line)
    except (OSError, ValueError):       # dead client, or a stale port now used by something else
        raise NoClient() from None


def active_run():
    """Label of the client's current run, or None if no client is running. A client that's alive
    but doesn't answer counts as busy, so two mice can never run at once. Never raises."""
    info = _info()
    if not info:
        return None
    try:
        reply = call("status", timeout=2.0)
        return reply["run"]["label"] if reply.get("run") else None
    except Exception:
        from . import launcher
        try:
            alive = launcher.processes().get(int(info.get("pid", -1))) in ("python.exe", "pythonw.exe")
        except Exception:
            alive = True
        return "a client that isn't answering" if alive else None


def spawn_client(args=("-m", "ba_auto", "hud")):
    """Open the HUD as its own process so it outlives the shell that started it."""
    STATE.mkdir(parents=True, exist_ok=True)
    exe = Path(sys.executable).with_name("pythonw.exe")
    if not exe.exists():
        exe = Path(sys.executable)
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    log = STATE / "client.log"
    try:                                 # keep the last client's log: it may explain why it ended
        if log.exists():
            os.replace(log, STATE / "client.prev.log")
    except OSError:                      # the old client still has it open: carry on in the same file
        pass
    with open(log, "a", encoding="utf-8") as out:
        for extra in (subprocess.CREATE_BREAKAWAY_FROM_JOB, 0):   # not every job allows breakaway
            try:
                return subprocess.Popen([str(exe), *args], cwd=str(journal.ROOT),
                                        stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                        creationflags=flags | extra)
            except OSError:
                if not extra:
                    raise


def _show(obj):
    print(json.dumps(obj, indent=1, ensure_ascii=False))


def _wait(run_id, timeout):
    deadline = time.monotonic() + timeout
    while True:
        try:
            reply = call("status", id=run_id)
        except NoClient:
            _show({"waiting": run_id, "error": "the client is gone"})
            return EXIT_NO_CLIENT
        run = reply.get("requested")
        if run is None:
            if run_id:
                _show({"error": f"no run {run_id} in this client"})
                return EXIT_ERROR
            _show({"note": "nothing has run yet"})
            return EXIT_OK
        if run["outcome"]:
            _show(run)
            return OUTCOME_EXIT[run["outcome"]]
        if time.monotonic() >= deadline:
            _show({"still_running": run, "screen": reply["screen"], "log": reply["log"][-5:]})
            return EXIT_RUNNING
        time.sleep(0.4)


def _start():
    try:
        _show({"already_running": call("ping")["pid"]})
        return EXIT_OK
    except NoClient:
        pass
    if journal.read_only():
        _show({"error": "the builder session cannot open the client"})
        return EXIT_REFUSED
    proc = spawn_client()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        time.sleep(0.3)
        try:
            _show({"started": call("ping")["pid"]})
            return EXIT_OK
        except NoClient:
            if proc.poll() is not None:
                break
    _show({"error": "the client did not come up", "log": str(STATE / "client.log")})
    return EXIT_ERROR


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ba_auto ctl")
    sub = ap.add_subparsers(dest="op", required=True)
    sub.add_parser("start")
    sub.add_parser("status")
    sub.add_parser("tasks")
    sub.add_parser("stop")
    sub.add_parser("quit")
    r = sub.add_parser("run")
    r.add_argument("task", choices=list(TASKS))
    r.add_argument("--limit", type=int)
    r.add_argument("--timeout", type=int)
    r.add_argument("--stage", action="append", metavar="KEY:COUNT")
    r.add_argument("--slot", type=int)
    r.add_argument("--times", type=int)
    r.add_argument("--at", metavar="X,Y")
    r.add_argument("--go", action="store_true", help="claims, cafe: act instead of a dry run")
    r.add_argument("--live", action="store_true", help="lesson-run: spend tickets")
    r.add_argument("--rehearse", action="store_true", help="lesson-run: check one room, spend nothing")
    r.add_argument("--max-tickets", type=int)
    r.add_argument("--room", action="append", metavar="LOCATION|ROOM", help="lesson-teach: a marked room, repeatable")
    r.add_argument("--scan-ts", type=float, help="lesson-teach: the ts of the scan the rooms were marked from")
    r.add_argument("--steps", type=int, help="conquest-step: battles to fight")
    r.add_argument("--seconds", type=int, help="smart-shadow: how long to watch")
    r.add_argument("--wait", type=float, nargs="?", const=110.0, metavar="SECONDS")
    h = sub.add_parser("clear-halt")
    h.add_argument("--note", default="")
    lh = sub.add_parser("clear-login-hold")
    lh.add_argument("--note", default="")
    ui = sub.add_parser("ui")
    ui.add_argument("choice", nargs="?", choices=("new", "classic"))
    al = sub.add_parser("auto-login")
    al.add_argument("state", choices=("on", "off"))
    w = sub.add_parser("wait")
    w.add_argument("id", nargs="?")
    w.add_argument("--timeout", type=float, default=110.0)
    a = ap.parse_args(argv)

    if a.op == "tasks":
        _show({k: v[1] for k, v in TASKS.items()})
        return EXIT_OK
    if a.op == "start":
        return _start()
    if a.op == "wait":
        return _wait(a.id, a.timeout)
    if a.op == "ui":
        from . import client
        if a.choice:
            if journal.read_only():
                _show({"error": "the builder session is read-only: the player runs the game"})
                return EXIT_REFUSED
            client.set_choice(a.choice, journal.actor())
        _show({"ui": client.choice(), "note": "takes effect the next time the client starts"})
        return EXIT_OK
    if a.op in ("run", "stop", "quit", "clear-halt", "clear-login-hold", "auto-login") and journal.read_only():
        _show({"error": "the builder session is read-only: the player runs the game"})
        return EXIT_REFUSED
    if a.op == "clear-login-hold":       # a file, so it works without a client
        _show({"ok": True, "cleared": watchdog.clear_login_hold(journal.actor(), a.note)})
        return EXIT_OK
    if a.op == "auto-login":
        try:
            reply = call("auto-login", on=a.state == "on")
        except NoClient:
            from . import launcher
            reply = {"ok": True, "auto_login": launcher.set_auto_login(a.state == "on", journal.actor())}
        _show(reply)
        return EXIT_OK
    if a.op == "clear-halt":
        try:
            reply = call("clear-halt", note=a.note)
        except NoClient:                # the halt is a file, so it clears without a client too
            reply = {"ok": True, "cleared": watchdog.clear(journal.actor(), a.note)}
        _show(reply)
        return EXIT_OK
    try:
        if a.op == "status":
            _show(call("status"))
            return EXIT_OK
        if a.op == "quit":
            reply = call("quit")
        elif a.op == "stop":
            reply = call("stop")
            if reply.get("stopping"):
                return _wait(None, 15.0)
        else:
            args = {"limit": a.limit, "timeout": a.timeout, "stages": a.stage, "slot": a.slot,
                    "times": a.times, "at": a.at, "go": a.go or None, "live": a.live or None,
                    "rehearse": a.rehearse or None, "max_tickets": a.max_tickets, "steps": a.steps,
                    "rooms": a.room, "scan_ts": a.scan_ts, "seconds": a.seconds}
            reply = call("run", task=a.task, args={k: v for k, v in args.items() if v is not None})
            if reply.get("ok") and a.wait is not None:
                return _wait(reply["id"], a.wait)
    except NoClient:
        _show({"error": "no client running (python -m ba_auto ctl start)"})
        return EXIT_NO_CLIENT
    _show(reply)
    return EXIT_OK if reply.get("ok") else EXIT_REFUSED
