"""What each student's EX does, and how we know.

data/skills.json holds what was seen in the game, and config/battler.json holds the user's overrides.
Nothing is filled in from memory or an outside dataset: an entry stays `unknown` until the game's own
skill text or a targeting shape has been read. The policy never guesses at an `unknown` skill.
"""
import json
import os
import re
from pathlib import Path

from .. import portraits
from .policy import Skill, TARGETS

ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / "data" / "skills.json"
CONFIG = ROOT / "config" / "battler.json"

ROLE_OF = {"single_enemy": "burst", "aoe_enemy": "clear", "ally_buff": "setup", "debuff": "setup", "heal": "sustain",
           "shield": "sustain", "cc": "control", "summon": "utility", "self": "utility", "unique": "utility",
           "unknown": "unknown"}
CONFIDENCE = {"none": 0.0, "text": 0.6, "shape": 0.7, "text+shape": 0.9, "user": 1.0}

_RULES = (
    ("heal", r"\b(recover|recovers|restore|restores|heal|heals)\b.*\bhp\b|\bheals?\b"),
    ("shield", r"\bshields?\b|\bbarrier\b"),
    ("summon", r"\b(summons?|deploys?|installs?|places? a)\b"),
    ("cc", r"\b(stuns?|knocks?|taunts?|immobili[sz]es?|confus\w+|fear)\b"),
    ("aoe_enemy", r"damage\b.*\b(enemies|area|circular|fan|cone|line|range)\b"),
    ("single_enemy", r"damage\b.*\b(an? enemy|one enemy|single enemy|target|enemy)\b"),
    ("debuff", r"\b(reduces?|decreases?|lowers?)\b.*\b(enem\w+|target)\b"),
    ("ally_buff", r"\b(increases?|boosts?|raises?)\b.*\b(all(y|ies)|students?|atk|attack|crit\w*)\b"),
)


def classify_text(text):
    """The EX's main effect from the game's own skill text, or 'unknown'."""
    low = re.sub(r"\s+", " ", (text or "").lower())
    if not low.strip():
        return "unknown"
    for kind, pattern in _RULES:
        if re.search(pattern, low):
            return kind
    return "unknown"


def _read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _clean(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


class Catalog:
    def __init__(self, skills_path=SKILLS, config_path=CONFIG):
        self.skills_path, self.config_path = Path(skills_path), Path(config_path)
        self.reload()

    def reload(self):
        self.data = _read(self.skills_path) or {"version": 1, "students": {}}
        self.config = _read(self.config_path)
        self.students = {}
        for name, entry in self.data.get("students", {}).items():
            self.students[name] = dict(entry)
        for name, extra in (self.config.get("students") or {}).items():
            merged = dict(self.students.get(name, {}))
            ex = {**(merged.get("ex") or {}), **{k: v for k, v in extra.items() if k != "dev"}}
            if ex.get("target"):
                ex.setdefault("source", "user")
                ex["confidence"] = CONFIDENCE["user"]
            merged["ex"] = ex
            if extra.get("dev"):
                merged["dev"] = extra["dev"]
            self.students[name] = merged

    @property
    def policy(self):
        return self.config.get("policy") or {}

    @property
    def team(self):
        return list(self.config.get("team") or self.data.get("team") or [])

    def skill(self, name):
        entry = self.students.get(name) or {}
        ex = entry.get("ex") or {}
        target = ex.get("target") if ex.get("target") in TARGETS else "unknown"
        return Skill(name, target, ex.get("shape"), ex.get("radius"), ex.get("role") or ROLE_OF[target], ex.get("cost"),
                     float(ex.get("confidence") or 0.0), ex.get("source") or "none")

    def dev_of(self, name):
        """The art pack's name for a student, from the catalog, learned names or a plain match."""
        dev = (self.students.get(name) or {}).get("dev")
        if dev:
            return dev
        for known, shown in portraits.names().items():
            if _clean(shown) == _clean(name):
                return known
        wanted = _clean(name)
        for candidate in _art_names():
            if _clean(candidate) == wanted:
                return candidate
        return None

    def card_map(self, team=None):
        """{art name: student} for the team, including each student's alternate card art."""
        out = {}
        names = _art_names()
        for name in team or self.team:
            dev = self.dev_of(name)
            if not dev:
                continue
            out[dev] = name
            for other in names:                 # e.g. CH0230_01: a card the skill swaps in
                if other.startswith(dev + "_") and re.fullmatch(r"_\d+", other[len(dev):]):
                    out[other] = f"{name}#{other[len(dev) + 1:]}"
        return out

    def skills_for(self, team=None):
        """{card student: Skill}. A swapped-in card has its own entry, so it stays unknown until seen."""
        return {student: self.skill(student) for student in self.card_map(team).values()}

    def coverage(self, team=None):
        rows = []
        for name in team or self.team:
            skill = self.skill(name)
            rows.append({"student": name, "dev": self.dev_of(name), "target": skill.target, "role": skill.role,
                         "cost": skill.cost, "confidence": skill.confidence, "source": skill.source})
        return rows

    def observe(self, name, text=None, cost=None, shape=None, radius=None, skill_name=None):
        """Record something read from the game. Knowledge only ever gets firmer, never weaker."""
        students = self.data.setdefault("students", {})
        entry = students.setdefault(name, {"ex": {"target": "unknown", "confidence": 0.0, "source": "none"}})
        ex = entry.setdefault("ex", {})
        if cost is not None:
            seen = set(ex.get("costs_seen") or [])
            seen.add(int(cost))
            ex["costs_seen"] = sorted(seen)
            ex["cost"] = max(seen)               # the badge can show a temporary discount
        if skill_name:
            ex["name"] = skill_name
        if text:
            ex["text"] = text
        if shape:
            ex["shape"] = shape
        if radius:
            ex["radius"] = radius
        kind = classify_text(ex.get("text"))
        source = "text+shape" if ex.get("text") and ex.get("shape") else "text" if ex.get("text") else \
            "shape" if ex.get("shape") else "none"
        confidence = CONFIDENCE[source] if kind != "unknown" else 0.0
        if kind != "unknown" and confidence >= float(ex.get("confidence") or 0.0):
            ex.update(target=kind, role=ROLE_OF[kind], confidence=confidence, source=source)
        _write(self.skills_path, self.data)
        self.reload()
        return self.skill(name)


_art = None


def _art_names():
    global _art
    if _art is None:
        try:
            _art = list(json.loads((portraits.ART / "manifest.json").read_text(encoding="utf-8"))["students"])
        except (OSError, ValueError):
            _art = []
    return _art
