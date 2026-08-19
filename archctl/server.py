"""HTTP server: static PWA on /, JSON control API on /api."""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import socket
import sys
import tempfile
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from . import __version__, actions, system
from .auth import COOKIE_NAME, CSRF_HEADER, Auth
from .config import Config

log = logging.getLogger("archctl")

WEB_ROOT = Path(__file__).parent / "web"
MAX_BODY = 2 * 1024 * 1024
PUBLIC_PATHS = {"/api/login", "/api/ping"}


class Handled(Exception):
    """Raised by helpers that have already written a response."""


def _json_bytes(payload: dict | list) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


class Router:
    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], Callable]] = []

    def add(self, method: str, pattern: str, handler: Callable) -> None:
        self.routes.append((method, re.compile(f"^{pattern}$"), handler))

    def match(self, method: str, path: str):
        allowed = False
        for route_method, pattern, handler in self.routes:
            match = pattern.match(path)
            if not match:
                continue
            if route_method == method:
                return handler, match.groupdict()
            allowed = True
        return (None, {"_method_mismatch": True}) if allowed else (None, {})


class ControlHandler(BaseHTTPRequestHandler):
    server_version = f"arch-controller/{__version__}"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # injected by make_server
    config: Config
    auth: Auth
    router: Router

    # -- plumbing ----------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        log.info("%s %s", self.client_address[0], fmt % args)

    def client_id(self) -> str:
        return self.client_address[0]

    def send_payload(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, payload: dict | list, status: int = 200, extra: dict | None = None) -> None:
        self.send_payload(status, _json_bytes(payload), "application/json; charset=utf-8", extra)

    def fail(self, status: int, message: str, extra: dict | None = None) -> None:
        self.send_json({"error": message}, status=status, extra=extra)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self.fail(413, "request body too large")
            raise Handled
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.fail(400, "body must be valid JSON")
            raise Handled from None
        if not isinstance(data, dict):
            self.fail(400, "body must be a JSON object")
            raise Handled
        return data

    # -- auth --------------------------------------------------------
    def cookie_session(self) -> str:
        raw = self.headers.get("Cookie")
        if not raw:
            return ""
        try:
            jar = SimpleCookie(raw)
        except Exception:  # malformed cookie header
            return ""
        morsel = jar.get(COOKIE_NAME)
        return morsel.value if morsel else ""

    def bearer_token(self) -> str:
        header = self.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return ""

    def authorize(self) -> bool:
        """True when the request may proceed; writes the failure otherwise."""
        token = self.bearer_token()
        if token:
            if self.auth.check_token(token):
                return True
            self.auth.record_failure(self.client_id())
            self.fail(401, "invalid token")
            return False

        if self.auth.valid_session(self.cookie_session()):
            # Cookie auth is ambient, so mutations need a header no cross-site
            # form can set.
            if self.command != "GET" and self.headers.get(CSRF_HEADER) != "1":
                self.fail(403, "missing X-Archctl header")
                return False
            return True

        self.fail(401, "authentication required")
        return False

    # -- dispatch ----------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        self.dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self.dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self.dispatch("POST")

    def dispatch(self, method: str) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            if not path.startswith("/api/"):
                self.serve_static(path)
                return

            handler, params = self.router.match(method, path)
            if handler is None:
                if params.get("_method_mismatch"):
                    self.fail(405, "method not allowed")
                else:
                    self.fail(404, "no such endpoint")
                return

            if path not in PUBLIC_PATHS and not self.authorize():
                return

            handler(self, **params)
        except Handled:
            return
        except actions.ActionError as exc:
            self.fail(exc.status, str(exc))
        except BrokenPipeError:
            return
        except Exception:
            log.exception("unhandled error serving %s %s", method, path)
            self.fail(500, "internal error")

    # -- static ------------------------------------------------------
    STATIC = {
        "/": "index.html",
        "/index.html": "index.html",
        "/app.js": "app.js",
        "/style.css": "style.css",
        "/manifest.webmanifest": "manifest.webmanifest",
        "/icon.svg": "icon.svg",
        "/sw.js": "sw.js",
    }

    def serve_static(self, path: str) -> None:
        name = self.STATIC.get(path)
        if name is None:
            self.send_payload(404, b"not found\n", "text/plain; charset=utf-8")
            return
        target = WEB_ROOT / name
        if not target.exists():
            self.send_payload(404, b"not found\n", "text/plain; charset=utf-8")
            return
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if name.endswith(".webmanifest"):
            ctype = "application/manifest+json"
        if ctype.startswith("text/") or name.endswith((".js", ".webmanifest", ".svg")):
            ctype = f"{ctype}; charset=utf-8" if "charset" not in ctype else ctype
        self.send_payload(200, target.read_bytes(), ctype, {"Cache-Control": "no-cache"})


# -- API handlers ---------------------------------------------------------


def api_ping(h: ControlHandler) -> None:
    h.send_json({"ok": True, "service": "arch-controller", "version": __version__})


def api_login(h: ControlHandler) -> None:
    wait = h.auth.retry_after(h.client_id())
    if wait > 0:
        h.fail(429, "too many attempts", {"Retry-After": str(int(wait) + 1)})
        return

    body = h.read_json()
    token = str(body.get("token", ""))
    if not h.auth.check_token(token):
        h.auth.record_failure(h.client_id())
        log.warning("failed login from %s", h.client_id())
        h.fail(401, "invalid token")
        return

    h.auth.record_success(h.client_id())
    sid = h.auth.create_session()
    max_age = int(h.auth.session_ttl)
    secure = "; Secure" if h.headers.get("X-Forwarded-Proto") == "https" else ""
    cookie = (
        f"{COOKIE_NAME}={sid}; Path=/; Max-Age={max_age}; "
        f"HttpOnly; SameSite=Strict{secure}"
    )
    h.send_json({"ok": True}, extra={"Set-Cookie": cookie})


def api_logout(h: ControlHandler) -> None:
    h.auth.drop_session(h.cookie_session())
    expired = f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"
    h.send_json({"ok": True}, extra={"Set-Cookie": expired})


def api_status(h: ControlHandler) -> None:
    h.send_json(
        {
            "system": system.snapshot(),
            "capabilities": actions.capabilities(),
            "media": actions.media_status(),
            "volume": actions.volume_status(),
            "brightness": actions.brightness_status(),
            "commands": [
                {"id": c.id, "label": c.label, "confirm": c.confirm} for c in h.config.commands
            ],
            "shell_enabled": h.config.allow_shell,
            "version": __version__,
        }
    )


def api_media(h: ControlHandler) -> None:
    body = h.read_json()
    h.send_json(actions.media(str(body.get("action", ""))))


def _level(body: dict) -> int | None:
    if "value" not in body or body["value"] is None:
        return None
    try:
        return int(body["value"])
    except (TypeError, ValueError):
        raise actions.ActionError("value must be an integer") from None


def api_volume(h: ControlHandler) -> None:
    body = h.read_json()
    h.send_json(actions.volume(str(body.get("action", "")), _level(body)))


def api_brightness(h: ControlHandler) -> None:
    body = h.read_json()
    h.send_json(actions.brightness(str(body.get("action", "")), _level(body)))


def api_power(h: ControlHandler) -> None:
    body = h.read_json()
    action = str(body.get("action", ""))
    if action in ("reboot", "poweroff", "hibernate") and not body.get("confirm"):
        h.fail(400, f"{action} requires confirm: true")
        return
    log.warning("power action %s requested by %s", action, h.client_id())
    h.send_json(actions.power(action))


def api_notify(h: ControlHandler) -> None:
    body = h.read_json()
    h.send_json(actions.notify(str(body.get("title", "")), str(body.get("body", ""))))


def api_clipboard_get(h: ControlHandler) -> None:
    h.send_json(actions.clipboard_get())


def api_clipboard_set(h: ControlHandler) -> None:
    body = h.read_json()
    h.send_json(actions.clipboard_set(str(body.get("text", ""))))


def api_open(h: ControlHandler) -> None:
    body = h.read_json()
    h.send_json(actions.open_url(str(body.get("url", ""))))


def api_screenshot(h: ControlHandler) -> None:
    with tempfile.TemporaryDirectory(prefix="archctl-") as tmp:
        dest = Path(tmp) / "screen.png"
        actions.screenshot(dest)
        h.send_payload(200, dest.read_bytes(), "image/png", {"Cache-Control": "no-store"})


def api_processes(h: ControlHandler) -> None:
    query = h.path.split("?", 1)[1] if "?" in h.path else ""
    limit = 15
    for pair in query.split("&"):
        key, _, value = pair.partition("=")
        if key == "limit" and value.isdigit():
            limit = int(value)
    h.send_json({"processes": actions.processes(limit)})


def api_kill(h: ControlHandler) -> None:
    body = h.read_json()
    try:
        pid = int(body.get("pid"))
    except (TypeError, ValueError):
        h.fail(400, "pid must be an integer")
        return
    log.warning("kill %s requested by %s", pid, h.client_id())
    h.send_json(actions.kill_process(pid, str(body.get("signal", "TERM"))))


def api_command(h: ControlHandler) -> None:
    body = h.read_json()
    command_id = str(body.get("id", ""))
    for command in h.config.commands:
        if command.id == command_id:
            log.info("running command %s for %s", command_id, h.client_id())
            h.send_json(actions.run_command(command))
            return
    h.fail(404, f"no command named {command_id!r}")


def api_shell(h: ControlHandler) -> None:
    if not h.config.allow_shell:
        h.fail(403, "shell access is disabled (set actions.allow_shell = true to enable)")
        return
    body = h.read_json()
    script = str(body.get("cmd", ""))
    log.warning("shell from %s: %s", h.client_id(), script[:200])
    h.send_json(actions.run_shell(script, h.config.shell_timeout))


def build_router() -> Router:
    router = Router()
    router.add("GET", "/api/ping", api_ping)
    router.add("POST", "/api/login", api_login)
    router.add("POST", "/api/logout", api_logout)
    router.add("GET", "/api/status", api_status)
    router.add("POST", "/api/media", api_media)
    router.add("POST", "/api/volume", api_volume)
    router.add("POST", "/api/brightness", api_brightness)
    router.add("POST", "/api/power", api_power)
    router.add("POST", "/api/notify", api_notify)
    router.add("GET", "/api/clipboard", api_clipboard_get)
    router.add("POST", "/api/clipboard", api_clipboard_set)
    router.add("POST", "/api/open", api_open)
    router.add("GET", "/api/screenshot", api_screenshot)
    router.add("GET", "/api/processes", api_processes)
    router.add("POST", "/api/kill", api_kill)
    router.add("POST", "/api/command", api_command)
    router.add("POST", "/api/shell", api_shell)
    return router


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    address_family = socket.AF_INET

    def __init__(self, config: Config):
        self.config = config
        self.auth = Auth(token=config.token, session_ttl=config.session_hours * 3600)
        self.router = build_router()
        if ":" in config.host:
            self.address_family = socket.AF_INET6

        handler = type(
            "BoundControlHandler",
            (ControlHandler,),
            {"config": config, "auth": self.auth, "router": self.router},
        )
        super().__init__((config.host, config.port), handler)


def serve(config: Config) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    server = Server(config)
    log.info("arch-controller %s listening on %s", __version__, config.base_url)
    if config.host in ("0.0.0.0", "::"):
        log.warning(
            "bound to every interface — put this behind Tailscale or a tunnel, "
            "not a port-forward on the open internet"
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        server.server_close()
    return 0


__all__ = ["Server", "serve", "build_router", "ControlHandler", "Router"]
