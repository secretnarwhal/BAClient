"""Team advisor: ranks the roster against an enemy's armor and attack type. Recommends only.

Changing a saved team is never automatic, so nothing here clicks. The only type facts
used are the four strong matchups. Terrain isn't scored until its face colours are confirmed.
"""
import json

from . import ocr
from .tasks import roster

STRONG = {"Explosive": "Light", "Piercing": "Heavy", "Mystic": "Special", "Sonic": "Elastic"}
BEATS_ARMOR = {armor: attack for attack, armor in STRONG.items()}
BOSS_ATTACK, BOSS_ARMOR = (1140, 295, 1236, 328), (1288, 295, 1386, 328)
BOSS_NAME = (540, 292, 1000, 346)
HURT = 0.7                       # a student whose armor the enemy's attack is strong against


def read_enemy(frame):
    """Type info from an open Boss Info popup."""
    def word(box):
        return " ".join(w.text for w in ocr.read(frame, box, scale=2)).strip().title() or None
    name = " ".join(w.text for w in ocr.read(frame, BOSS_NAME, scale=2)).strip() or None
    return {"boss": name, "attack": word(BOSS_ATTACK), "armor": word(BOSS_ARMOR)}


def _known(value, table):
    value = (value or "").strip().title()
    return value if value in table else None


def score(student, armor=None, attack=None):
    """(score, reasons) for one student against this enemy."""
    base = (student.get("stats") or {}).get("atk") or (student.get("level") or 1) * 10
    value, why = float(base), []
    if armor and STRONG.get(student.get("attack")) == armor:
        value *= 2
        why.append(f"{student['attack']} hits {armor} hard")
    if attack and student.get("armor") == STRONG.get(attack):
        value *= HURT
        why.append(f"{student['armor']} armor takes extra from {attack}")
    return value, why


def _pick(pool, count, taken):
    out = []
    for row in pool:
        if len(out) >= count:
            break
        if row["name"] not in taken:
            out.append(row)
            taken.add(row["name"])
    return out


def advise(students, armor=None, attack=None):
    armor, attack = _known(armor, BEATS_ARMOR), _known(attack, STRONG)
    rows = []
    for s in students:
        if not s.get("name"):
            continue
        value, why = score(s, armor, attack)
        rows.append({"name": s["name"], "class": s.get("class"), "role": s.get("role"), "position": s.get("position"),
                     "attack": s.get("attack"), "armor": s.get("armor"), "level": s.get("level"),
                     "score": round(value), "why": why})
    rows.sort(key=lambda r: (-r["score"], r["name"]))
    strikers = [r for r in rows if r["class"] == "Striker"]
    specials = [r for r in rows if r["class"] == "Special"]
    taken, team = set(), []
    team += _pick([r for r in strikers if r["role"] == "Tank"], 1, taken)
    team += _pick([r for r in strikers if r["role"] == "Healer"], 1, taken)
    team += _pick([r for r in strikers if r["role"] not in ("Tank", "Healer")], 4 - len(team), taken)
    team += _pick(strikers, 4 - len(team), taken)
    support = _pick([r for r in specials if r["role"] in ("Healer", "Support")], 1, taken)
    support += _pick(specials, 2 - len(support), taken)
    want = BEATS_ARMOR.get(armor)
    notes = []
    if armor is None:
        notes.append("enemy armor unknown or Normal: ranked by stats only")
    elif want and not any(r["attack"] == want for r in team):
        notes.append(f"no {want} student made the team; {armor} armor takes double from {want}")
    return {"enemy": {"armor": armor, "attack": attack}, "team": team + support,
            "best_attackers": [r for r in rows if r["role"] not in ("Healer", "Support", "Tank")][:8], "notes": notes}


def show(armor=None, attack=None, as_json=False, enemy=None):
    data = roster.load()
    if not data["students"]:
        print("no roster yet: ctl run roster-scan")
        return 1
    result = advise(data["students"], armor, attack)
    if enemy:
        result["enemy"]["read"] = enemy
    if as_json:
        print(json.dumps(result, indent=1, ensure_ascii=False))
        return 0
    e = result["enemy"]
    print(f"Enemy armor: {e['armor'] or '?'}   attack: {e['attack'] or '?'}"
          + (f"   ({enemy.get('boss')})" if enemy and enemy.get("boss") else "")
          + ("   [roster is partial]" if not data.get("complete") else ""))
    print("Suggested team (recommendation only; changing a saved team needs the user's OK):")
    for r in result["team"]:
        print(f"  {r['name'][:24]:25}{str(r['class']):8}{str(r['role']):9}{str(r['attack']):10}{str(r['armor']):8}"
              f"Lv.{str(r['level']):4}{'; '.join(r['why'])}")
    for note in result["notes"]:
        print("Note:", note)
    return 0
