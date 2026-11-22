"""One schema for every task the client can run, so the panel can show real controls for each.

A task in `control.TASKS` with no entry here fails `tools/test_ui.py`. The server builds a run's
options only from this schema, so the panel can't send anything else. Previews and plans stay the
first choice wherever a task has one, and anything that spends or claims needs a confirmation bound
to the exact options shown. Developer tools, raw clicks and `--allow-spend` have no entry here.
"""
import hashlib
import json
import re
from collections import namedtuple

from .. import control

GROUPS = ("Start", "Story", "Daily", "Lessons", "Missions", "Conquest", "Battle", "Recon")
BATTLEFIELD = (40, 130, 1880, 840)      # tasks.battle.FIELD
EVENT_PANEL = (330, 130, 1900, 1060)    # tasks.eventstory.PANEL
ARG_NAMES = {"limit", "timeout", "stages", "slot", "times", "at", "go", "live", "rehearse", "max_tickets", "steps",
             "rooms", "scan_ts", "seconds"}
# Commands that deliberately have no control in the panel.
LEFT_OUT = {"press": "raw button press, can reach Never buttons", "click": "raw coordinate click",
            "cut": "developer tool", "grab": "developer tool", "shot": "developer tool", "watch": "console tool",
            "journal prune": "deletes saved frames"}
_TEXT = re.compile(r"[\w .:()\-]{1,40}")

Plan = namedtuple("Plan", "task args confirm key label summary")


def num(id, label, low, high, default=None, blank=None, unit="", when=None, arg=None, required=False):
    return {"id": id, "kind": "number", "arg": arg or id, "label": label, "min": low, "max": high, "default": default,
            "blank": blank, "unit": unit, "when": when, "required": required}


def choice(id, label, options, default=None, when=None, arg=None, required=False):
    return {"id": id, "kind": "choice", "arg": arg or id, "label": label, "options": list(options), "default": default,
            "when": when, "required": required}


def point(id, label, box, when=None):
    return {"id": id, "kind": "point", "arg": id, "label": label, "box": list(box), "default": None, "when": when, "required": True}


def text(id, label, hint="", when=None):
    return {"id": id, "kind": "text", "arg": id, "label": label, "hint": hint, "default": "", "when": when, "required": True}


def opt(value, label, args=None, confirm=None, note=""):
    return {"value": value, "label": label, "args": args or {}, "confirm": confirm, "note": note}


def mode(*options):
    return {"id": "mode", "kind": "mode", "options": list(options), "default": options[0]["value"], "when": None}


def task(group, icon, desc, *fields, confirm=None, via=None, build=None):
    return {"group": group, "icon": icon, "desc": desc, "fields": list(fields), "confirm": confirm, "via": via, "build": build}


def _tickets(args):
    limit = args.get("max_tickets")
    return (f"Teaches real lessons and spends {'up to ' + str(limit) if limit else 'the planned number of'} lesson tickets. "
            "Tickets can't be refunded.")


def _claims(args):
    limit = args.get("limit")
    return f"Really claims mail and task rewards{f', at most {limit} rows per section' if limit else ''}. It never presses Claim All."


def _cafe(args):
    return (f"Really collects cafe earnings, gives up to {args.get('limit', 12)} headpats per cafe, "
            "and uses an invite if config/cafe.json allows it.")


def _conquest(args):
    steps = args.get("steps", 1)
    return f"Fights {steps} real Conquest battle{'' if steps == 1 else 's'} at 5 AP each: {steps * 5} AP."


def _sweep(args):
    from ..tasks import sweeper
    rows = control._sweep_plan(args.get("stages"))
    ap = sum(sweeper.cost(r["key"]) * r["count"] for r in rows)
    what = ", ".join(f"{r['key']} x{r['count']}" for r in rows)
    return f"Sweeps {what}. Spends {ap} AP and never refills AP."


def _smart_stage(args):
    args = dict(args)
    args["stages"] = [args.pop("stage")]
    return args


def _recon_stage(args):
    args = dict(args)
    kind = args.pop("kind", "grid")
    by = args.pop("by", None)
    args["stages"] = [f"scroll:{by}" if kind == "scroll" else kind]
    return args


COMMANDS = {
    "launch": task("Start", "power", "Open Blue Archive through Steam if it isn't running, then log in if Auto login is on."),
    "login": task("Start", "pointer", "Press Click to Start once the PC is idle. Stops on anything unknown."),
    "lobby": task("Start", "home", "Go Home, closing only known popups. Stops on anything unknown."),

    "skip": task("Story", "skip", "Skip the story on screen."),
    "episodes": task("Story", "book", "Skip New episodes in the open chapter.", num("limit", "Episodes at most", 1, 500, 50)),
    "ministories": task("Story", "sparkle", "Clear every mini story chapter tagged New."),
    "groupstories": task("Story", "team", "Clear Group Story chapters tagged New. Stops on any battle or popup.",
                         num("limit", "Chapters at most", 1, 200, 1, blank="All")),
    "event-stories": task("Story", "star", "Skip unfinished Event Recap stories and take their story rewards.",
                          num("limit", "Events at most", 1, 100, blank="All"),
                          confirm="Skips event stories and takes their story rewards. This can't be undone."),

    "daily-scan": task("Daily", "list", "Read tasks, mail, lessons, cafe and tickets for Today. Free navigation only."),
    "claims": task("Daily", "check", "Claim mail and task rewards one row at a time.",
                   mode(opt("dry", "Preview", note="Lists what could be claimed and claims nothing."),
                        opt("go", "Claim for real", {"go": True}, _claims)),
                   num("limit", "Rows per section", 1, 150, blank="All")),
    "cafe": task("Daily", "coin", "Cafe earnings, headpats and invites.",
                 mode(opt("dry", "Preview", note="Checks the cafe and claims and pats nothing."),
                      opt("go", "Do it for real", {"go": True}, _cafe)),
                 num("limit", "Headpats per cafe", 1, 12, 12)),
    "daily-pack": task("Daily", "gem", "The Free Daily Pack only. Cash Shop > General > Daily, and nothing else in the shop.",
                       mode(opt("dry", "Preview", note="Opens General > Daily, reports and claims nothing."),
                            opt("rehearse", "Rehearse", {"rehearse": True},
                                "Presses the Free Daily Pack card once and never confirms a dialog."),
                            opt("go", "Claim it", {"go": True},
                                "Claims the Free Daily Pack. The card must read exactly Free, and pyroxene must not change.")),),
    "momotalk": task("Daily", "heart", "Answer unread students and skip their relationship stories. No gifts.",
                     num("limit", "Students at most", 1, 200, blank="All"),
                     confirm="Sends the suggested replies to unread MomoTalk conversations. Replies can't be undone. No gifts are given."),

    "lesson-scan": task("Lessons", "pin", "Read every location, room and student. Never starts a lesson.",
                        num("limit", "Locations at most", 1, 30, blank="All")),
    "lesson-run": task("Lessons", "book", "Choose the best rooms automatically.",
                       mode(opt("plan", "Plan", note="Lists the rooms it would pick. Spends nothing."),
                            opt("rehearse", "Rehearse", {"rehearse": True}, note="Opens and checks one room. Spends nothing."),
                            opt("live", "Teach for real", {"live": True}, _tickets)),
                       num("max_tickets", "Tickets at most", 1, 20, blank="Plan's number", when=["plan", "live"])),
    "lesson-teach": task("Lessons", "map", "Teach the exact rooms you mark on the Lesson Atlas.", via=("lessons", "Lessons")),

    "sweep-scan": task("Missions", "radar", "Read every stage for the sweep picker. Spends nothing."),
    "sweep": task("Missions", "bolt", "Sweep the stages you pick from the last scan. Spends AP and never refills it.",
                  {"id": "stages", "kind": "sweep", "arg": "stages", "label": "Stages", "default": None, "when": None, "required": True},
                  confirm=_sweep),

    "conquest": task("Conquest", "map", "Read the map on screen and show the plan and AP cost. No input."),
    "conquest-recon": task("Conquest", "eye", "Open the next target's menu and dialog, then close them. Spends nothing."),
    "conquest-step": task("Conquest", "swords", "Fight real Conquest battles, 5 AP each.",
                          num("steps", "Battles", 1, 20, 1), confirm=_conquest),

    "battle": task("Battle", "swords", "Auto-proceed one battle.", num("timeout", "Give up after", 10, 3600, 300, unit="s"),
                   confirm="Starting a fight can use AP."),
    "ex": task("Battle", "target", "Use one EX skill in the battle on screen, once its card is ready.",
               choice("slot", "Card slot", [1, 2, 3], 1, required=True), point("at", "Target on the battlefield", BATTLEFIELD),
               num("times", "Times", 1, 50, 1), num("timeout", "Give up after", 1, 600, 60, unit="s"),
               confirm="Uses this EX skill in the battle on screen."),
    "smart-shadow": task("Battle", "eye", "Watch the battle on screen and log what it would cast. No input.",
                         num("seconds", "Watch for", 10, 600, 180, unit="s")),
    "smart-recon": task("Battle", "search", "Read each EX card's text, then cancel. Use only in a low-stakes battle.",
                        confirm="Opens each EX card in the battle on screen and cancels. Use only in a low-stakes battle."),
    "smart-battle": task("Battle", "sparkle", "Control EX skills in the battle on screen. Never starts a battle.",
                         num("timeout", "Give up after", 30, 900, 420, unit="s"),
                         confirm="Takes over EX skills in the battle on screen until it ends. F8 hands it back."),
    "smart-proof": task("Battle", "crown", "Measure one fight that's already on screen, for comparing against AUTO.",
                        mode(opt("auto", "Watch AUTO", note="Records the fight on AUTO. No input."),
                             opt("smart", "Play smart", {"live": True},
                                 "Controls EX skills in the battle on screen and records the result.")),
                        text("stage", "Stage name", "N1-1"), build=_smart_stage),

    "roster-scan": task("Recon", "students", "Read every student into the Students page. Spends nothing.",
                        num("limit", "Students at most", 1, 400, blank="All")),
    "cafe-recon": task("Recon", "window", "Save floor frames of both cafes. Navigation only."),
    "event-stories-recon": task("Recon", "search", "Read-only checks of the Event Story tab. Never enters a story or claims.",
                                choice("kind", "Check", ["grid", "scroll", "card"], "grid", required=True),
                                point("at", "Point in the event panel", EVENT_PANEL, when=None),
                                num("by", "Scroll distance", -600, 600, -300, unit="px"), build=_recon_stage),
}
for _field in COMMANDS["event-stories-recon"]["fields"]:     # these depend on the "Check" choice
    if _field["id"] == "at":
        _field["depends"] = {"kind": ["scroll", "card"]}
    elif _field["id"] == "by":
        _field["depends"] = {"kind": ["scroll"]}


def missing():
    """Tasks the client has that the panel doesn't. A test fails if this isn't empty."""
    return [name for name in control.TASKS if name not in COMMANDS]


def _whole(value):
    if isinstance(value, bool):
        raise ValueError
    if isinstance(value, str) and re.fullmatch(r"-?\d{1,6}", value.strip()):
        return int(value)
    if isinstance(value, int):
        return value
    raise ValueError


def _read(field, value):
    """One field's validated value, or None when it's blank. Raises ValueError with a readable message."""
    label, kind = field["label"], field["kind"]
    if value == "" and field.get("blank"):   # cleared on purpose, like "All"
        return None
    if value in (None, ""):
        value = field.get("default")
    if value in (None, ""):
        if field.get("required"):
            raise ValueError(f"{label} is required")
        return None
    if kind == "number":
        try:
            number = _whole(value)
        except ValueError:
            raise ValueError(f"{label} must be a whole number") from None
        if not field["min"] <= number <= field["max"]:
            raise ValueError(f"{label} must be {field['min']}-{field['max']}")
        return number
    if kind == "choice":
        for option in field["options"]:
            if str(option) == str(value) and not isinstance(value, bool):
                return option
        raise ValueError(f"{label} isn't one of the options")
    if kind == "point":
        match = re.fullmatch(r"(\d{1,4}),(\d{1,4})", str(value))
        if not match:
            raise ValueError(f"Pick {label.lower()}")
        x, y = int(match[1]), int(match[2])
        x1, y1, x2, y2 = field["box"]
        if not (x1 <= x <= x2 and y1 <= y <= y2):
            raise ValueError(f"{label} is outside the allowed area")
        return f"{x},{y}"
    if kind == "text":
        clean = str(value).strip()
        if not _TEXT.fullmatch(clean):
            raise ValueError(f"{label} has characters it can't use")
        return clean
    if kind == "sweep":
        if not isinstance(value, dict) or not 0 < len(value) <= 120:
            raise ValueError("Pick at least one stage")
        out = []
        for key, count in value.items():
            if not re.fullmatch(r"[A-Za-z]{0,2}\d{1,3}-\d{1,3}", str(key)):
                raise ValueError(f"{key} isn't a stage")
            try:
                count = _whole(count)
            except ValueError:
                raise ValueError(f"{key}: count must be a whole number") from None
            out.append(f"{key}:{count}")
        return out
    raise ValueError(f"{label} can't be read")


def _active(field, chosen):
    if field.get("when") and chosen.get("mode") not in field["when"]:
        return False
    return all(chosen.get(k) in allowed for k, allowed in (field.get("depends") or {}).items())


def plan(name, values=None):
    """Validate the panel's values into the exact run. Raises ValueError with a readable message."""
    spec = COMMANDS.get(name)
    if spec is None or name not in control.TASKS:
        raise ValueError("that task can't be started from the panel")
    if spec["via"]:
        raise ValueError(f"Use the {spec['via'][1]} page for this")
    values = {} if values is None else values
    if not isinstance(values, dict):
        raise ValueError("bad options")
    fields = spec["fields"]
    unknown = set(values) - {f["id"] for f in fields}
    if unknown:
        raise ValueError(f"unknown option: {sorted(unknown)[0]}")
    args, chosen, summary = {}, {}, []
    confirm = spec["confirm"]
    label = control.TASKS[name][0]
    for field in fields:
        if field["kind"] == "mode":
            wanted = values.get("mode", field["default"])
            option = next((o for o in field["options"] if o["value"] == wanted), None)
            if option is None:
                raise ValueError("that mode doesn't exist")
            chosen["mode"] = wanted
            args.update(option["args"])
            confirm = option["confirm"] or confirm
            label = f"{label}: {option['label']}"
            summary.append(["Mode", option["label"]])
        elif field["kind"] == "choice":
            chosen[field["id"]] = str(values.get(field["id"], field["default"]))
    for field in fields:
        if field["kind"] == "mode" or not _active(field, chosen):
            continue
        value = _read(field, values.get(field["id"]))
        if value is None:
            continue
        args[field["arg"]] = value
        shown = ", ".join(value) if isinstance(value, list) else value
        summary.append([field["label"], f"{shown}{' ' + field['unit'] if field.get('unit') else ''}"])
    if spec["build"]:
        args = spec["build"](args)
    text_ = confirm(args) if callable(confirm) else confirm
    key = hashlib.sha256(json.dumps([name, args], sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return Plan(name, args, text_, key, label, summary)


def _public(field):
    out = {k: v for k, v in field.items() if k not in ("arg", "confirm")}
    if field["kind"] == "mode":
        out["options"] = [{"value": o["value"], "label": o["label"], "note": o["note"], "confirm": bool(o["confirm"])}
                          for o in field["options"]]
    return out


def describe():
    """The schema as the page reads it."""
    rows = []
    for name, (label, _) in control.TASKS.items():
        spec = COMMANDS.get(name)
        if spec is None:
            rows.append({"name": name, "label": label, "group": "Recon", "icon": "alert", "desc": "No controls yet.",
                         "fields": [], "missing": True})
            continue
        rows.append({"name": name, "label": label, "group": spec["group"], "icon": spec["icon"], "desc": spec["desc"],
                     "fields": [_public(f) for f in spec["fields"]], "confirm": bool(spec["confirm"]),
                     "via": spec["via"][0] if spec["via"] else None})
    return {"groups": list(GROUPS), "tasks": rows}
