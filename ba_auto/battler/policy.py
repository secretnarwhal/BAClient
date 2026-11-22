"""What to cast, on what, and why. Pure: no game, vision or clock, so every rule is unit-tested.

decide(obs, skills, memory, config) returns a Decision and the next memory. The rules:
  - AoE waits until enough enemies are inside its shape.
  - High single-target damage goes on the boss or the biggest enemy, and waits if only small ones are up.
  - Buffs and debuffs go just before a damage skill that can actually follow them.
  - Heals and shields wait for need.
  - Cost is reserved for a more valuable skill that's nearly affordable.
  - Nothing is held forever: cost cap, a hold limit, the battle timer and a final wave all release.
  - A skill whose effect isn't known is never guessed at. If it blocks spending, control goes back to AUTO.
"""
import math
import statistics
from dataclasses import dataclass, field

TARGETS = ("single_enemy", "aoe_enemy", "ally_buff", "heal", "shield", "debuff", "cc", "summon", "self", "unique",
           "unknown")

DEFAULTS = {
    "cost_cap": 10,
    "near_cap": 9,               # holding here wastes regeneration
    "cost_per_second": 0.7,      # measured on saved fights; only used to judge "nearly affordable"
    "reserve_seconds": 4.0,
    "aoe_min_hits": 3,
    "aoe_radius": 190,           # field pixels, used when a skill's real size isn't known yet
    "y_squash": 0.6,             # the field is drawn at an angle, so vertical distance counts for more
    "cc_min_hits": 2,
    "max_hold": 18.0,            # seconds a ready card may wait while enemies are up
    "heal_at": 0.5,
    "heal_group_at": 0.7,
    "heal_group_count": 2,
    "shield_at": 0.75,
    "big_ratio": 1.5,            # an enemy this much bigger than the median counts as high-HP
    "timer_pressure": 25.0,      # seconds left at which everything is released
    "unsure_grace": 2.5,         # seconds of unreadable state before handing back to AUTO
    "cast_gap": 0.5,             # the game needs a moment between casts
}


@dataclass(frozen=True)
class Skill:
    student: str
    target: str = "unknown"
    shape: str = None            # circle, fan, line or all
    radius: float = None
    role: str = "unknown"
    cost: int = None
    confidence: float = 0.0
    source: str = "none"


@dataclass(frozen=True)
class Enemy:
    x: int
    y: int
    size: float = 1.0            # chunks x fill, a rough relative HP
    boss: bool = False


@dataclass(frozen=True)
class Ally:
    x: int
    y: int
    hp: float = None
    shield: bool = False


@dataclass(frozen=True)
class Card:
    slot: int
    student: str = None
    cost: int = None
    ready: bool = False


@dataclass
class Obs:
    t: float
    cost: int = None
    cards: tuple = ()
    enemies: tuple = ()
    allies: tuple = ()
    wave: int = 0
    wave_age: float = 0.0
    timer: float = None          # seconds left on the battle clock
    final_wave: bool = None      # None until a stage's wave count can be read
    sure: bool = True
    targeting: bool = False


@dataclass
class Decision:
    kind: str                    # cast, wait or fallback
    reason: str
    slot: int = None
    student: str = None
    point: tuple = None
    target: str = None
    released_by: str = None      # which anti-stall rule let a held skill go
    held: tuple = ()             # (student, why) for cards deliberately held this frame


@dataclass
class Memory:
    ready_since: dict = field(default_factory=dict)
    unsure_since: float = None
    last_cast: float = None


# ---------------------------------------------------------------- geometry
def _dist(ax, ay, bx, by, squash):
    return math.hypot(ax - bx, (ay - by) / squash)


def best_circle(enemies, radius, squash=0.6):
    """(centre, hits): the circle of `radius` covering the most enemies."""
    if not enemies:
        return None, 0
    candidates = [(e.x, e.y) for e in enemies]
    for i, a in enumerate(enemies):
        for b in enemies[i + 1:]:
            candidates.append(((a.x + b.x) // 2, (a.y + b.y) // 2))
    best, hits = None, -1
    for cx, cy in candidates:
        n = sum(_dist(e.x, e.y, cx, cy, squash) <= radius for e in enemies)
        if n > hits:
            best, hits = (cx, cy), n
    return best, hits


def top_enemy(enemies):
    """The boss if there is one, otherwise the enemy with the most HP left."""
    if not enemies:
        return None
    bosses = [e for e in enemies if e.boss]
    pool = bosses or list(enemies)
    return max(pool, key=lambda e: (e.size, -e.y))


def is_big(enemy, enemies, ratio):
    if enemy.boss or len(enemies) == 1:
        return True
    others = [e.size for e in enemies if e is not enemy]
    return enemy.size >= ratio * statistics.median(others)


# ---------------------------------------------------------------- what each skill wants
def _want(skill, obs, cfg, in_hand):
    """(score, point, reason), or (None, point, why it's held). point is where it would land."""
    enemies, allies = obs.enemies, obs.allies
    kind = skill.target
    if kind in ("unknown", "unique"):
        return None, None, f"{kind} skill: never guessed at"
    if kind == "aoe_enemy":
        radius = skill.radius or cfg["aoe_radius"]
        point, hits = best_circle(enemies, radius, cfg["y_squash"])
        if any(e.boss for e in enemies):
            target = top_enemy(enemies)
            return 50 + hits * 5, (target.x, target.y), f"AoE on the boss, {hits} in range"
        if hits >= cfg["aoe_min_hits"]:
            return 50 + hits * 5, point, f"AoE covers {hits} enemies"
        return None, point, f"AoE held: only {hits} in range (wants {cfg['aoe_min_hits']})"
    if kind == "single_enemy":
        target = top_enemy(enemies)
        if target is None:
            return None, None, "no target"
        if is_big(target, enemies, cfg["big_ratio"]):
            label = "boss" if target.boss else f"biggest enemy (size {target.size:.1f})"
            return 60, (target.x, target.y), f"single-target on {label}"
        return None, (target.x, target.y), "single-target held: only small enemies up"
    if kind in ("heal", "shield"):
        known = [a for a in allies if a.hp is not None]
        if not known:
            return None, None, f"{kind} held: ally HP unreadable"
        low = min(known, key=lambda a: a.hp)
        if kind == "heal":
            hurt = sum(a.hp < cfg["heal_group_at"] for a in known)
            if low.hp < cfg["heal_at"] or hurt >= cfg["heal_group_count"]:
                return 100, (low.x, low.y), f"heal: lowest ally at {low.hp:.0%}, {hurt} hurt"
            return None, (low.x, low.y), f"heal held: lowest ally at {low.hp:.0%}"
        bare = [a for a in known if not a.shield]
        if bare and min(a.hp for a in bare) < cfg["shield_at"]:
            pick = min(bare, key=lambda a: a.hp)
            return 90, (pick.x, pick.y), f"shield: unshielded ally at {pick.hp:.0%}"
        return None, (low.x, low.y), "shield held: nobody needs it"
    if kind in ("ally_buff", "debuff"):
        follow = _burst_after(skill, obs, cfg, in_hand)
        if kind == "debuff":
            target = top_enemy(enemies)
            point = (target.x, target.y) if target else None
        else:
            point = _front_ally(allies, enemies)
        if point is None:
            return None, None, f"{kind} held: no target"
        if follow:
            return 70, point, f"{kind} just before {follow}"
        return None, point, f"{kind} held: no damage skill can follow it yet"
    if kind == "cc":
        radius = skill.radius or cfg["aoe_radius"]
        point, hits = best_circle(enemies, radius, cfg["y_squash"])
        if hits >= cfg["cc_min_hits"] or any(e.boss for e in enemies):
            return 65, point, f"crowd control on {hits}"
        return None, point, f"crowd control held: {hits} in range"
    if kind == "summon":
        point, _ = best_circle(enemies, cfg["aoe_radius"], cfg["y_squash"])
        return 40, point, "summon while enemies are up"
    if kind == "self":
        point = _front_ally(allies, enemies)
        return (35, point, "self skill") if point else (None, None, "self skill held: no ally position")
    return None, None, f"unhandled target type {kind}"


def _front_ally(allies, enemies):
    if not allies:
        return None
    if enemies:
        ex = sum(e.x for e in enemies) / len(enemies)
        a = min(allies, key=lambda a: abs(a.x - ex))
    else:
        a = max(allies, key=lambda a: a.x)
    return a.x, a.y


def _burst_after(skill, obs, cfg, in_hand):
    """Name of a damage skill in hand that's worth casting and affordable right after `skill`."""
    spend = _cost(skill, in_hand)
    if spend is None or obs.cost is None:
        return None
    for card, other in in_hand:
        if other.student == skill.student or other.target not in ("aoe_enemy", "single_enemy"):
            continue
        if card.cost is None or card.cost + spend > obs.cost:
            continue
        if _want(other, obs, cfg, ())[0] is not None:
            return other.student
    return None


def _cost(skill, in_hand):
    for card, other in in_hand:
        if other.student == skill.student:
            return card.cost
    return skill.cost


# ---------------------------------------------------------------- anti-stall
def _pressure(student, obs, mem, cfg):
    if obs.timer is not None and obs.timer <= cfg["timer_pressure"]:
        return f"timer ({obs.timer:.0f}s left)"
    if obs.cost is not None and obs.cost >= cfg["near_cap"]:
        return f"cost near cap ({obs.cost})"
    since = mem.ready_since.get(student)
    if since is not None and obs.t - since >= cfg["max_hold"]:
        return f"held {obs.t - since:.0f}s"
    if obs.final_wave and len(obs.enemies) <= 2:
        return "final wave nearly clear"
    return None


def _released_point(skill, obs, cfg):
    """Where a skill goes when anti-stall lets it go without its ideal condition."""
    if skill.target in ("aoe_enemy", "cc", "summon"):
        return best_circle(obs.enemies, skill.radius or cfg["aoe_radius"], cfg["y_squash"])[0]
    if skill.target in ("single_enemy", "debuff"):
        target = top_enemy(obs.enemies)
        return (target.x, target.y) if target else None
    if skill.target == "heal" or skill.target == "shield":
        known = [a for a in obs.allies if a.hp is not None]
        if known:
            low = min(known, key=lambda a: a.hp)
            return low.x, low.y
        return None
    if skill.target in ("ally_buff", "self"):
        return _front_ally(obs.allies, obs.enemies)
    return None


# ---------------------------------------------------------------- the decision
def decide(obs, skills, memory=None, config=None):
    """(Decision, next Memory). `skills` maps a card's student to a Skill."""
    cfg = {**DEFAULTS, **(config or {})}
    mem = memory or Memory()
    ready_now = {c.student for c in obs.cards if c.ready and c.student}
    mem = Memory({s: mem.ready_since.get(s, obs.t) for s in ready_now}, mem.unsure_since, mem.last_cast)

    if not obs.sure:
        mem.unsure_since = mem.unsure_since if mem.unsure_since is not None else obs.t
        waited = obs.t - mem.unsure_since
        if waited >= cfg["unsure_grace"]:
            return Decision("fallback", f"battle state unreadable for {waited:.1f}s"), mem
        return Decision("wait", "battle state unreadable"), mem
    mem.unsure_since = None

    if obs.targeting:
        return Decision("wait", "a card is already selected"), mem
    if mem.last_cast is not None and obs.t - mem.last_cast < cfg["cast_gap"]:
        return Decision("wait", "letting the last cast land"), mem
    if not obs.enemies:
        return Decision("wait", "no enemies on the field"), mem

    hand = [(c, skills.get(c.student) or Skill(c.student or "?")) for c in obs.cards if c.cost is not None and c.student]
    ready = [(c, s) for c, s in hand if c.ready]
    if not ready:
        if obs.cost is not None and obs.cost >= cfg["near_cap"] and any(c.ready and not c.student for c in obs.cards):
            return Decision("fallback", "cost is capped and a ready card can't be identified"), mem
        return Decision("wait", "no card ready"), mem

    options, held = [], []
    for card, skill in ready:
        score, point, why = _want(skill, obs, cfg, hand)
        if score is not None and point is not None:
            options.append((score, card, skill, point, why, None))
            continue
        pressure = _pressure(skill.student, obs, mem, cfg) if skill.target not in ("unknown", "unique") else None
        late = _released_point(skill, obs, cfg) if pressure else None
        if pressure and late is not None:
            options.append((20, card, skill, late, f"{why}; released", pressure))
        else:
            held.append((skill.student, why))

    # Keep cost for a better skill that's nearly affordable, unless something is forcing a spend.
    plan = _plan(hand, obs, cfg)
    if plan and options:
        kept = []
        for opt in options:
            score, card, skill, *_ = opt
            spare = obs.cost - card.cost if obs.cost is not None else 0
            if score < plan[0] and spare < plan[1].cost and opt[5] is None and not _capped(obs, cfg):
                held.append((skill.student, f"saving cost for {plan[2].student}"))
            else:
                kept.append(opt)
        options = kept

    if not options:
        unknown = [s.student for c, s in ready if s.target in ("unknown", "unique")]
        if unknown and _capped(obs, cfg):
            return Decision("fallback", f"cost is capped and only unverified skills are ready: {', '.join(unknown)}",
                            held=tuple(held)), mem
        return Decision("wait", "holding: " + "; ".join(f"{s}: {w}" for s, w in held), held=tuple(held)), mem

    score, card, skill, point, why, pressure = max(options, key=lambda o: (o[0], -(o[1].cost or 0)))
    mem.last_cast = obs.t
    mem.ready_since.pop(skill.student, None)
    reason = f"{why} [released: {pressure}]" if pressure else why
    return Decision("cast", reason, card.slot, skill.student, tuple(point), skill.target, pressure, tuple(held)), mem


def _capped(obs, cfg):
    return obs.cost is not None and obs.cost >= cfg["near_cap"]


def _plan(hand, obs, cfg):
    """(score, card, skill) of the best skill that isn't affordable yet but soon will be."""
    if obs.cost is None:
        return None
    best = None
    for card, skill in hand:
        if card.ready or card.cost is None or card.cost <= obs.cost:
            continue
        if (card.cost - obs.cost) / cfg["cost_per_second"] > cfg["reserve_seconds"]:
            continue
        score = _want(skill, obs, cfg, hand)[0]
        if score is not None and (best is None or score > best[0]):
            best = (score, card, skill)
    return best


class WaveTracker:
    """Numbers waves from the enemy count: a new wave is enemies returning after a clear field."""

    def __init__(self, clear_after=1.5, window=3):
        self.clear_after, self.window = clear_after, window
        self.recent = []
        self.wave, self.started, self.peak = 0, None, 0
        self.empty_since = None

    def update(self, t, count):
        """(wave number, seconds since it started, smoothed enemy count)."""
        self.recent = (self.recent + [count])[-self.window:]
        now = int(statistics.median(self.recent))
        if now == 0:
            self.empty_since = self.empty_since if self.empty_since is not None else t
        else:
            if self.wave == 0 or (self.empty_since is not None and t - self.empty_since >= self.clear_after):
                self.wave += 1
                self.started, self.peak = t, 0
            self.empty_since = None
            self.peak = max(self.peak, now)
        return self.wave, (t - self.started if self.started is not None else 0.0), now
