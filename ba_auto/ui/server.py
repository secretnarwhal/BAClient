"""The panel's local web server. Loopback only, and every request needs this launch's key.

A web page in any browser on this PC can send requests to 127.0.0.1, so commands are protected
against that: the Host header must be ours (stops DNS rebinding), a session cookie marked
SameSite=Strict is required, and POSTs need a custom header and a same-origin Origin.
"""
import hmac
import json
import mimetypes
import re
import secrets
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .. import atlas, journal
from . import art, thumbs

WEB = Path(__file__).with_name("web")
COOKIE = "baclient_panel"
ART_TYPES = {".png", ".webp", ".jpg", ".html", ".js", ".css", ".json"}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; frame-src 'self'; base-uri 'none'; form-action 'none'")
MAX_BODY = 4096


class Refused(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def safe_under(root, relative):
    """The file at root/relative, or None if it escapes root or doesn't exist."""
    root = Path(root).resolve()
    try:
        target = (root / relative).resolve()
        target.relative_to(root)
    except (ValueError, OSError):
        return None
    return target if target.is_file() else None


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        """Chrome closing a kept-alive connection is normal, and pythonw has no console for it."""


class PanelServer:
    def __init__(self, store, actions=None, key=None):
        self.store, self.actions = store, actions     # actions=None means read-only preview
        self.key = key or secrets.token_urlsafe(24)
        panel = self

        class Handler(_Handler):
            server_panel = panel

        self.httpd = _QuietServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self._thread = None

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/?k={self.key}"

    def start(self):
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="ui-server")
        self._thread.start()
        return self

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30
    server_panel = None

    def log_message(self, *args):       # pythonw has no console, and polling would flood one
        pass

    # ---- guards ----
    def _host_ok(self):
        port = self.server_panel.port
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _authed(self):
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie") or "")
        except Exception:
            return False
        morsel = jar.get(COOKIE)
        return bool(morsel) and hmac.compare_digest(morsel.value.encode(), self.server_panel.key.encode())

    def _same_origin(self):
        origin = self.headers.get("Origin")
        port = self.server_panel.port
        if origin is not None and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            return False
        return self.headers.get("Sec-Fetch-Site", "same-origin") in ("same-origin", "none")

    # ---- replies ----
    def _send(self, code, body, ctype, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, value, code=200):
        self._send(code, json.dumps(value, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", [("Cache-Control", "no-store")])

    def _file(self, path, cache="private, max-age=604800", csp=False):
        stat = path.stat()
        tag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
        extra = [("ETag", tag), ("Cache-Control", cache)]
        if csp:
            extra.append(("Content-Security-Policy", CSP))
        if self.headers.get("If-None-Match") == tag:
            self.send_response(304)
            for k, v in extra:
                self.send_header(k, v)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self._send(200, path.read_bytes(), ctype, extra)

    # ---- routes ----
    def do_GET(self):
        try:
            self._get()
        except Refused as e:
            self._json({"ok": False, "error": str(e)}, e.code)
        except (BrokenPipeError, ConnectionError):
            self.close_connection = True
        except Exception as e:
            self._json({"ok": False, "error": repr(e)}, 500)

    do_HEAD = do_GET

    def _get(self):
        if not self._host_ok():
            raise Refused(403, "wrong host")
        url = urlsplit(self.path)
        path, query = url.path, parse_qs(url.query)
        panel = self.server_panel
        if path == "/" and "k" in query:
            if not hmac.compare_digest(query["k"][0].encode(), panel.key.encode()):
                raise Refused(403, "wrong key")
            cookie = f"{COOKIE}={panel.key}; Path=/; HttpOnly; SameSite=Strict"
            to = (query.get("to") or [""])[0]
            where = f"/#{to}" if re.fullmatch(r"[a-z]{1,12}", to) else "/"
            self._send(302, b"", "text/plain", [("Location", where), ("Set-Cookie", cookie),
                                                ("Cache-Control", "no-store")])
            return
        if not self._authed():
            raise Refused(403, "open the panel from BAClient")
        if path == "/":
            self._file(WEB / "index.html", cache="no-cache", csp=True)
        elif path.startswith("/web/"):
            target = safe_under(WEB, unquote(path[5:]))
            if target is None:
                raise Refused(404, "not found")
            self._file(target, cache="no-cache")
        elif path.startswith("/art/"):
            target = safe_under(art.ART, unquote(path[5:]))
            if target is None or target.suffix.lower() not in ART_TYPES:
                raise Refused(404, "not found")
            self._file(target, cache="no-cache" if target.suffix in (".html", ".js") else "private, max-age=604800")
        elif path.startswith("/thumb/"):
            m = re.fullmatch(r"/thumb/(\d{3})/([\w.-]{1,80})", path)
            target = thumbs.get(m.group(2), int(m.group(1))) if m and art.known(m.group(2)) else None
            if target is None:
                raise Refused(404, "not found")
            self._file(target)
        elif path.startswith("/frame/"):
            m = re.fullmatch(r"/frame/(\d{4}-\d{2}-\d{2})/frames/([\w.-]+\.jpg)", path)
            target = safe_under(journal.LOGS / m.group(1) / "frames", m.group(2)) if m else None
            if target is None:
                raise Refused(404, "not found")
            self._file(target)
        elif path == "/api/state":
            self._json(panel.store.state())
        elif path == "/api/roster":
            self._json(panel.store.roster())
        elif path == "/api/students":
            rev = (query.get("rev") or [None])[0]
            self._json(panel.store.students(rev if rev and re.fullmatch(r"[0-9a-f]{16}", rev) else None))
        elif path == "/api/commands":
            self._json(panel.store.commands())
        elif path == "/api/sweep":
            self._json(panel.store.sweep())
        elif path == "/api/halt":
            self._json(panel.store.halt_view())
        elif path == "/api/halt/frame":
            target = panel.store.halt_frame()
            if target is None:
                raise Refused(404, "not found")
            self._file(target, cache="no-store")
        elif path == "/api/client":
            self._json({"guard": panel.store.guard(), "ui": panel.store.client_choice()})
        elif path == "/api/battles":
            self._json(panel.store.battles())
        elif path == "/api/lessons":
            rev = (query.get("rev") or [None])[0]
            self._json(panel.store.lessons(rev if rev and re.fullmatch(r"[0-9a-f]{16}", rev) else None))
        elif path == "/api/atlas/maps":
            self._json(atlas.geometry())
        elif path == "/api/tasks":
            self._json(panel.store.tasks())
        elif path == "/api/advise":
            self._json(panel.store.advise((query.get("armor") or [None])[0], (query.get("attack") or [None])[0]))
        elif path == "/api/art":
            self._json({"students": art.catalog()})
        elif path == "/api/journal":
            day = (query.get("day") or [None])[0]
            if day is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                raise Refused(400, "bad day")
            self._json(panel.store.journal(day))
        else:
            raise Refused(404, "not found")

    def _drain(self):
        """Read a refused request's small body, so Windows delivers the refusal instead of resetting the connection."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if 0 < length <= MAX_BODY:
                self.connection.settimeout(1)
                self.rfile.read(length)
        except (ValueError, OSError):
            pass

    def do_POST(self):
        self._body_read = False
        try:
            self._post()
        except Refused as e:
            self.close_connection = True
            if not self._body_read:
                self._drain()
            self._json({"ok": False, "error": str(e)}, e.code)
        except (BrokenPipeError, ConnectionError):
            self.close_connection = True
        except Exception as e:
            self._json({"ok": False, "error": repr(e)}, 500)

    def _post(self):
        if not (self._host_ok() and self._authed() and self._same_origin()
                and self.headers.get("X-BAClient") == "1"):
            raise Refused(403, "refused")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise Refused(400, "bad length") from None
        if not 0 <= length <= MAX_BODY:
            raise Refused(413, "too large")
        try:
            raw = self.rfile.read(length)
            self._body_read = True
            body = json.loads(raw or b"{}")
        except ValueError:
            raise Refused(400, "bad json") from None
        if not isinstance(body, dict):
            raise Refused(400, "bad json")
        panel, path = self.server_panel, urlsplit(self.path).path
        if path == "/api/prefs":
            if panel.actions is None and not panel.store.sample:
                raise Refused(403, "this is a preview: nothing is saved")
            try:
                panel.store.set_featured(str(body.get("featured")))
            except ValueError as e:
                raise Refused(400, str(e)) from None
            self._json({"ok": True})
            return
        if path in ("/api/lessons/mark", "/api/lessons/clear", "/api/lessons/remark"):
            if panel.actions is None and not panel.store.sample:
                raise Refused(403, "this is a preview: nothing is saved")
            if path.endswith("/clear"):
                reply = panel.store.desk.clear()
            elif path.endswith("/remark"):
                reply = panel.store.desk.remark_skipped()
            else:
                room, on = body.get("room"), body.get("on")
                if not isinstance(room, str) or not 0 < len(room) <= 200 or not isinstance(on, bool):
                    raise Refused(400, "room and on are required")
                reply = panel.store.desk.mark(room, on)
            self._json(reply, 200 if reply.get("ok") else 409)
            return
        if panel.actions is None:
            raise Refused(403, "this is a preview: commands are off")
        if path == "/api/command":
            task, values, confirm = body.get("task"), body.get("values", {}), body.get("confirm")
            if not isinstance(task, str) or not isinstance(values, dict) or not (confirm is None or isinstance(confirm, str)):
                raise Refused(400, "task and values are required")
            self._json(panel.actions.command(task, values, confirm))
        elif path == "/api/halt/clear":
            self._json(panel.actions.clear_halt(body.get("note"), body.get("expect")))
        elif path == "/api/hold/clear":
            self._json(panel.actions.clear_hold(body.get("note"), body.get("expect"), body.get("phrase")))
        elif path == "/api/restart":
            self._json(panel.actions.restart(body.get("confirmed") is True))
        elif path == "/api/ui-choice":
            self._json(panel.actions.ui_choice(body.get("choice")))
        elif path == "/api/inspect":
            self._json(panel.actions.inspect(str(body.get("view"))))
        elif path == "/api/lessons/teach":
            rooms, scan_ts = body.get("rooms"), body.get("scan_ts")
            rehearse = body.get("rehearse", False)
            if (not isinstance(rooms, list) or not 0 < len(rooms) <= 60 or not all(isinstance(r, str) and len(r) <= 200 for r in rooms)
                    or isinstance(scan_ts, bool) or not isinstance(scan_ts, (int, float)) or not isinstance(rehearse, bool)):
                raise Refused(400, "rooms and scan_ts are required")
            reply = panel.actions.teach(rooms, scan_ts, rehearse=rehearse, confirmed=body.get("confirmed") is True)
            self._json(reply, 200 if reply.get("ok") else 409)
        elif path == "/api/run":
            self._json(panel.actions.run(str(body.get("task")), confirmed=body.get("confirmed") is True))
        elif path == "/api/stop":
            self._json(panel.actions.stop())
        elif path == "/api/open":
            self._json(panel.actions.open(str(body.get("what"))))
        elif path == "/api/overlay":
            self._json(panel.actions.overlay(bool(body.get("on"))))
        elif path == "/api/autologin":
            if not isinstance(body.get("on"), bool):
                raise Refused(400, "on must be true or false")
            self._json(panel.actions.auto_login(body["on"]))
        elif path == "/api/quit":
            self._json(panel.actions.quit())
        else:
            raise Refused(404, "not found")
