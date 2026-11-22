"""Find a student's art from a dev name or a name read on screen. Never guesses: no exact match
means no art, because showing the wrong student is worse than showing a placeholder."""
import json
import re
import threading

from .. import portraits

ART = portraits.ART
_PLAYABLE = re.compile(r"^(CH\d+|[A-Z][a-z]+(_[A-Za-z0-9]+)*)$")
# "full" is the upgrade path: once assembled full-body art is listed in the manifest, the Students viewer uses it.
KINDS = {"portrait": "collection", "icon": "icon", "small": "icon_small", "battle": "battle", "full": "full"}
_lock = threading.Lock()
_cache = {"key": None, "students": {}, "names": {}, "index": {}}


def _clean(name):
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def _stamp(path):
    try:
        s = path.stat()
        return s.st_mtime_ns, s.st_size
    except OSError:
        return None


def _display(dev, learned):
    if dev in learned:
        return learned[dev]
    if dev.startswith("CH"):
        return dev
    base, _, variant = dev.partition("_")
    return f"{base} ({variant.replace('_', ' ').title()})" if variant else base


def _load():
    key = (_stamp(ART / "manifest.json"), _stamp(portraits.NAMES))
    with _lock:
        if _cache["key"] == key:
            return _cache
        try:
            students = json.loads((ART / "manifest.json").read_text(encoding="utf-8"))["students"]
        except (OSError, ValueError, KeyError):
            students = {}
        learned = portraits.names()
        names = {d: _display(d, learned) for d in students}
        index, clashes = {}, set()
        for dev, name in names.items():
            for form in {_clean(dev), _clean(name)}:
                if form in index and index[form] != dev:
                    clashes.add(form)
                index.setdefault(form, dev)
        for dev, name in learned.items():      # a name learned from the game outranks a dev name
            if dev in students:
                index[_clean(name)] = dev
                clashes.discard(_clean(name))
        for form in clashes:            # one student filed twice in different case is safe to merge
            devs = [d for d in names if form in (_clean(d), _clean(names[d]))]
            if len({d.lower() for d in devs}) == 1:
                index[form] = max(devs, key=lambda d: (bool(students[d].get("collection")), len(students[d])))
            else:
                index.pop(form, None)
        _cache.update(key=key, students=students, names=names, index=index)
        return _cache


def resolve(name):
    """The dev name for a name or dev name, or None."""
    return _load()["index"].get(_clean(name)) if name else None


def display(dev):
    return _load()["names"].get(dev) or dev


def urls(dev):
    """{portrait, icon, small, battle} art URLs that exist for this student."""
    entry = _load()["students"].get(dev) or {}
    return {k: f"/art/{entry[v]}" for k, v in KINDS.items() if entry.get(v)}


def portrait_file(dev):
    """The Collection art file for this student, or None. Stays inside the art folder."""
    relative = (_load()["students"].get(dev) or {}).get("collection")
    if not relative:
        return None
    root = ART.resolve()
    try:
        path = (root / relative).resolve()
        path.relative_to(root)
    except (ValueError, OSError):
        return None
    return path if path.is_file() else None


def person(name=None, dev=None):
    """A student as the panel shows them. `art` is empty when no art is known."""
    dev = dev or resolve(name)
    return {"name": name or (display(dev) if dev else None), "dev": dev, "art": urls(dev) if dev else {}}


def catalog():
    """Students with a large portrait, for the featured picker. Named students sort first."""
    data = _load()
    rows = [{"dev": d, "name": data["names"][d], "art": urls(d)} for d, e in data["students"].items()
            if e.get("collection") and _PLAYABLE.match(d) and "guard" not in d.lower()]
    rows.sort(key=lambda r: (r["name"].startswith("CH"), r["name"].lower()))
    return rows


def known(dev):
    return dev in _load()["students"]
