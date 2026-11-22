"""ba-auto command line.

  python -m ba_auto detect                     which screen is showing + template scores
  python -m ba_auto shot [file.png]            save the current frame (reference space)
  python -m ba_auto grab NAME x1 y1 x2 y2      cut a template from the live frame
  python -m ba_auto cut FILE NAME x1 y1 x2 y2  cut a template from a saved 1920x1080 png
  python -m ba_auto battle [--timeout 300]     auto-proceed through one battle
  python -m ba_auto watch                      print screen changes (no clicking)
  python -m ba_auto ctl status|tasks|start|run TASK|wait|stop|quit   control the running client
  python -m ba_auto journal [--last N] [--day D] [--logs]            read the action timeline
  python -m ba_auto journal prune              delete old frames now (also runs in the HUD)
  python -m ba_auto roster [--armor X] [--attack Y] [--role Z] [--class C] [--sort atk]  query the roster
  python -m ba_auto battles [--boss X] [--last N]  every recorded fight
  python -m ba_auto advise [--armor X --attack Y | --screen]  suggest a team (recommendation only)
  python -m ba_auto todo                       what's left today and what to do next
  python -m ba_auto lessons [--student X] [--owned]  every location, room and student from the last scan
  python -m ba_auto atlas                      open the zoomable map of every location and student
  python -m ba_auto who                        name the students on a list, room or result screen
  python -m ba_auto look                       screen, popup, resources and known buttons as JSON
  python -m ba_auto press NAME [--allow-spend]  click a known button by name, checked
  python -m ba_auto launch                     start the game through Steam if needed, then log in if Auto login is on
  python -m ba_auto click X Y --from FILE      one checked manual click at a point in a fresh `shot` frame
  python -m ba_auto view                       read-only: where the game's picture is in its window
"""
import argparse
import sys
import time

import cv2

from .game import Game
from .screens import detect, scores
from .vision import Vision


def main():
    if sys.argv[1:2] == ["ctl"]:
        from .control import main as ctl_main
        sys.exit(ctl_main(sys.argv[2:]))
    ap = argparse.ArgumentParser(prog="ba_auto")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("detect")
    s = sub.add_parser("shot"); s.add_argument("file", nargs="?", default="shot.png")
    g = sub.add_parser("grab"); g.add_argument("name"); g.add_argument("box", type=int, nargs=4)
    g.add_argument("--threshold", type=float, default=0.85); g.add_argument("--pad", type=int, default=60)
    c = sub.add_parser("cut"); c.add_argument("file"); c.add_argument("name"); c.add_argument("box", type=int, nargs=4)
    c.add_argument("--threshold", type=float, default=0.85); c.add_argument("--pad", type=int, default=60)
    b = sub.add_parser("battle"); b.add_argument("--timeout", type=int, default=300)
    sub.add_parser("watch")
    sub.add_parser("skip")
    sub.add_parser("ministories")
    gs = sub.add_parser("groupstories"); gs.add_argument("--limit", type=int)
    sub.add_parser("hud")
    sub.add_parser("sweep-scan")
    e = sub.add_parser("episodes"); e.add_argument("--limit", type=int, default=50)
    j = sub.add_parser("journal"); j.add_argument("action", nargs="?", choices=["show", "prune"], default="show")
    j.add_argument("--day"); j.add_argument("--last", type=int, default=40); j.add_argument("--logs", action="store_true")
    r = sub.add_parser("roster")
    r.add_argument("--name"); r.add_argument("--armor"); r.add_argument("--attack"); r.add_argument("--role")
    r.add_argument("--class", dest="klass"); r.add_argument("--position")
    r.add_argument("--terrain", help="urban, outdoor or indoor, optionally =colour")
    r.add_argument("--min-level", type=int)
    r.add_argument("--sort", default="level", choices=["level", "stars", "bond", "atk", "hp", "def", "healing", "name"])
    r.add_argument("--last", type=int, default=40); r.add_argument("--json", action="store_true")
    br = sub.add_parser("battles"); br.add_argument("--boss"); br.add_argument("--stage")
    br.add_argument("--last", type=int, default=20); br.add_argument("--json", action="store_true")
    ad = sub.add_parser("advise"); ad.add_argument("--armor"); ad.add_argument("--attack")
    ad.add_argument("--screen", action="store_true", help="read the open Boss Info popup"); ad.add_argument("--json", action="store_true")
    td = sub.add_parser("todo"); td.add_argument("--json", action="store_true")
    sub.add_parser("who")
    at = sub.add_parser("atlas"); at.add_argument("--no-open", action="store_true")
    ls = sub.add_parser("lessons"); ls.add_argument("--student"); ls.add_argument("--owned", action="store_true")
    ls.add_argument("--json", action="store_true")
    sub.add_parser("look")
    p = sub.add_parser("press"); p.add_argument("name"); p.add_argument("--allow-spend", action="store_true")
    p.add_argument("--user-approved", action="store_true", help="the user OKed this exact login click")
    sub.add_parser("view")
    sub.add_parser("launch", help="start Blue Archive through Steam if needed, then log in if Auto login is on")
    mc = sub.add_parser("click", help="one checked click at a point in a saved `shot` frame")
    mc.add_argument("x", type=int); mc.add_argument("y", type=int); mc.add_argument("--from", dest="from_file", required=True)
    a = ap.parse_args()

    if a.cmd == "hud":
        from .client import main as hud_main
        return hud_main()
    if a.cmd == "advise":
        from . import advisor
        enemy = None
        if a.screen:
            enemy = advisor.read_enemy(Game().grab())
        sys.exit(advisor.show(a.armor or (enemy or {}).get("armor"), a.attack or (enemy or {}).get("attack"), a.json, enemy))
    if a.cmd == "todo":
        from . import daily
        try:
            frame = Game().grab()
        except Exception:
            frame = None
        sys.exit(daily.show(frame, a.json))
    if a.cmd == "atlas":
        from . import atlas
        if a.no_open:
            atlas.write()
            print(atlas.PAGE)
        else:
            print(atlas.open_page())
        return
    if a.cmd == "lessons":
        from .tasks import lessons
        sys.exit(lessons.show(a.student, a.owned, a.json))
    if a.cmd == "who":
        import json as _json
        from . import nav, portraits
        from .screens import label
        vision, frame = Vision(), Game().grab()
        screen, popup = label(vision, frame), nav.top_popup(vision, frame)
        layout = "rooms" if popup and popup[0] == "All Locations" else screen if screen in portraits.LAYOUTS else None
        if layout is None:
            print(_json.dumps({"error": "open the Students list, All Locations or a battle result", "screen": screen}))
            sys.exit(5)
        print(_json.dumps({"layout": layout, "students": portraits.who(frame, layout)}, indent=1, ensure_ascii=False))
        return
    if a.cmd == "battles":
        from . import battlelog
        sys.exit(battlelog.show(a.boss or a.stage, a.last, a.json))
    if a.cmd == "launch":
        from . import control, launcher, look
        try:
            control.call("ping")
        except control.NoClient:
            try:
                return look.show(launcher.launch(Game(), Vision()))
            except RuntimeError as e:
                look.show({"stopped": str(e)})
                sys.exit(1)
        sys.exit(control.main(["run", "launch", "--wait", "600"]))
    if a.cmd == "click":
        from . import look, manual
        try:
            return look.show(manual.click(Game(), Vision(), a.x, a.y, a.from_file))
        except RuntimeError as e:        # every refusal, from this command or from Game
            look.show({"refused": str(e)})
            sys.exit(5)
    if a.cmd == "view":
        from . import look
        return look.show(look.view_check(Game(), Vision()))
    if a.cmd in ("look", "press"):
        from . import look
        vision, game = Vision(), Game()
        if a.cmd == "look":
            return look.show(look.describe(vision, game.grab(), window=game.window_problem()))
        try:
            return look.show(look.press(game, vision, a.name, a.allow_spend, a.user_approved))
        except look.PressError as e:
            look.show({"refused": str(e)})
            sys.exit(5)
    if a.cmd == "roster":
        from .tasks import roster
        sys.exit(roster.show(a))
    if a.cmd == "journal":
        from . import journal
        if a.action == "prune":
            print("deleted %d frames, freed %s MB" % journal.prune())
        else:
            journal.show(a.day, a.last, a.logs)
        return
    vision = Vision()
    if a.cmd == "cut":
        vision.save(cv2.imread(a.file), a.name, a.box, a.pad, a.threshold)
        print(f"saved template {a.name}")
        return
    game = Game()
    if a.cmd == "detect":
        f = game.grab()
        print(detect(vision, f))
        print(scores(vision, f))
    elif a.cmd == "shot":
        cv2.imwrite(a.file, game.grab()); print(a.file)
    elif a.cmd == "grab":
        vision.save(game.grab(), a.name, a.box, a.pad, a.threshold); print(f"saved template {a.name}")
    elif a.cmd == "battle":
        print(__import__("ba_auto.tasks.battle", fromlist=["run"]).run(game, vision, a.timeout))
    elif a.cmd == "ministories":
        from .tasks import ministory
        print(ministory.clear_all(game, vision), "episodes skipped")
    elif a.cmd == "groupstories":
        from .tasks import ministory
        print(ministory.clear_group(game, vision, limit=a.limit))
    elif a.cmd == "sweep-scan":
        from .tasks import sweeper
        data = sweeper.scan(game, vision)
        print(len(data["rows"]) if data else 0, "stages")
    elif a.cmd == "skip":
        from .tasks import story
        print(story.skip(game, vision))
    elif a.cmd == "episodes":
        from .tasks import story
        print(story.episodes(game, vision, limit=a.limit), "episodes skipped")
    elif a.cmd == "watch":
        last = None
        while True:
            s = detect(vision, game.grab())
            if s != last:
                print(time.strftime("%H:%M:%S"), s, flush=True); last = s
            time.sleep(1)


if __name__ == "__main__":
    main()
