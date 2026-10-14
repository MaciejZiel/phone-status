"""Tiny read-only HTTP server (stdlib) with a cached JSON API and static assets."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from .admin import AdminAuth, client_key

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
}
ICON_FILES = {"favicon.svg", "favicon.ico", "favicon-32x32.png", "apple-touch-icon.png"}
STATIC_FILES = {"app.css", "app.js"} | ICON_FILES
# Browsers ask for these at the site root without reading the page, so they are served there too.
ROOT_ICONS = {"/favicon.ico": "favicon.ico", "/apple-touch-icon.png": "apple-touch-icon.png"}
ICON_CACHE = "public, max-age=86400"
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'self'; form-action 'none'; frame-ancestors 'none'"
)
# The administrator pages post a login form to themselves, so they (and only they) may submit forms.
ADMIN_CSP = CSP.replace("form-action 'none'", "form-action 'self'")
COOKIE_NAME = "__Secure-ss_admin"
MAX_FORM_BYTES = 1024
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=()",
}


# A connection that sends nothing for this long is closed, so idle or slow clients
# (slowloris) cannot pin worker threads. Behind nginx the proxy absorbs slow clients anyway.
REQUEST_TIMEOUT = 10.0
MAX_CONNECTIONS = 64


class StatusHTTPServer(ThreadingHTTPServer):
    """Thread-per-connection server with a hard cap on concurrent connections."""

    daemon_threads = True
    request_queue_size = 64

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        max_connections: int = MAX_CONNECTIONS,
    ) -> None:
        super().__init__(address, handler)
        self._slots = threading.BoundedSemaphore(max_connections)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            log.warning("connection limit reached, dropping %s", client_address[0])
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class CachedJson:
    """Serialises the status document at most once per `ttl` seconds."""

    def __init__(self, build: Callable[[], dict[str, Any]], ttl: float = 5.0) -> None:
        self._build = build
        self._ttl = ttl
        self._lock = threading.Lock()
        self._at = 0.0
        self._body = b"{}"

    def get(self) -> bytes:
        with self._lock:
            if time.monotonic() - self._at >= self._ttl or self._at == 0.0:
                self._body = json.dumps(self._build(), separators=(",", ":")).encode()
                self._at = time.monotonic()
            return self._body


def make_server(
    host: str,
    port: int,
    base_path: str,
    build: Callable[[], dict[str, Any]],
    title: str,
    max_connections: int = MAX_CONNECTIONS,
    admin: AdminAuth | None = None,
    build_admin: Callable[[], dict[str, Any]] | None = None,
) -> StatusHTTPServer:
    """`admin` and `build_admin` together switch the administrator view on; without them it is a 404."""
    api = CachedJson(build)
    admin_api = CachedJson(build_admin) if admin and build_admin else None
    admin_enabled = admin_api is not None
    assets = {name: (STATIC_DIR / name).read_bytes() for name in STATIC_FILES}
    # A content hash in the asset URLs: a browser or CDN that cached an older app.js or app.css
    # under the same address would otherwise keep running it for hours after a deploy.
    assets_version = hashlib.sha256(assets["app.js"] + assets["app.css"]).hexdigest()[:10]

    def render(name: str) -> str:
        page = (STATIC_DIR / name).read_text().replace("{{base}}", base_path + "/")
        page = page.replace('href="static/app.css"', f'href="static/app.css?v={assets_version}"')
        page = page.replace('src="static/app.js"', f'src="static/app.js?v={assets_version}"')
        return page.replace("{{title}}", html.escape(title))

    admin_pages = {}
    if admin_enabled:
        for name in ("admin.html", "login.html"):
            admin_pages[name] = render(name)
    cookie_path = base_path or "/"
    index_bytes = render("index.html").encode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "status"
        sys_version = ""
        protocol_version = "HTTP/1.1"
        timeout = REQUEST_TIMEOUT

        def log_message(self, format: str, *args: Any) -> None:
            log.debug("%s - %s", self.address_string(), format % args)

        def _send(
            self,
            status: int,
            body: bytes,
            ctype: str,
            cache: str = "no-store",
            head: bool = False,
            extra: dict[str, str] | None = None,
            csp: str = CSP,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            for k, v in {**SECURITY_HEADERS, "Content-Security-Policy": csp}.items():
                self.send_header(k, v)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def _path(self) -> str:
            path = self.path.split("?", 1)[0]
            if base_path and (path == base_path or path.startswith(base_path + "/")):
                path = path[len(base_path) :] or "/"
            return path

        # ---- administrator view ------------------------------------------------------------
        def _session(self) -> str | None:
            for part in self.headers.get("Cookie", "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == COOKIE_NAME:
                    return value
            return None

        def _cookie(self, value: str, max_age: int) -> dict[str, str]:
            return {
                "Set-Cookie": f"{COOKIE_NAME}={value}; Max-Age={max_age}; Path={cookie_path}; "
                "HttpOnly; Secure; SameSite=Strict"
            }

        def _admin_send(self, status: int, body: bytes, ctype: str, head: bool = False, **kw: Any) -> None:
            self._send(status, body, ctype, head=head, csp=ADMIN_CSP, **kw)

        def _redirect(self, target: str, extra: dict[str, str] | None = None) -> None:
            headers = {"Location": base_path + target, **(extra or {})}
            self._admin_send(303, b"", "text/plain; charset=utf-8", extra=headers)

        def _login_page(self, status: int, message: str = "", extra: dict[str, str] | None = None) -> None:
            page = admin_pages["login.html"].replace("{{message}}", html.escape(message))
            self._admin_send(status, page.encode(), CONTENT_TYPES[".html"], extra=extra)

        def _admin_get(self, path: str, head: bool) -> None:
            authed = admin.valid(self._session()) if admin else False
            if path == "/admin/login":
                if authed:
                    self._redirect("/admin")
                else:
                    self._login_page(200)
            elif path in ("/admin", "/admin/"):
                if authed:
                    page = admin_pages["admin.html"].encode()
                    self._admin_send(200, page, CONTENT_TYPES[".html"], head)
                else:
                    self._redirect("/admin/login")
            elif path == "/api/admin/status":
                if not authed:
                    self._admin_send(401, b"authentication required\n", "text/plain; charset=utf-8", head)
                    return
                try:
                    body = admin_api.get() if admin_api else b"{}"
                except Exception:
                    log.exception("building the administrator document failed")
                    self._admin_send(503, b"status unavailable\n", "text/plain; charset=utf-8", head)
                    return
                self._admin_send(200, body, "application/json", head)
            else:
                self._admin_send(404, b"not found\n", "text/plain; charset=utf-8", head)

        def _read_form(self) -> dict[str, list[str]] | None:
            """The urlencoded body, or None after an error response was sent."""
            self.close_connection = True
            site = self.headers.get("Sec-Fetch-Site")
            if site not in (None, "same-origin", "none"):
                self._admin_send(403, b"forbidden\n", "text/plain; charset=utf-8")
                return None
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._admin_send(411, b"length required\n", "text/plain; charset=utf-8")
                return None
            if not 0 <= length <= MAX_FORM_BYTES:
                self._admin_send(413, b"too large\n", "text/plain; charset=utf-8")
                return None
            raw = self.rfile.read(length).decode("utf-8", "replace")
            return parse_qs(raw, max_num_fields=8)

        def _admin_post(self, path: str) -> None:
            assert admin is not None
            form = self._read_form()
            if form is None:
                return
            if path == "/admin/logout":
                admin.logout(self._session())
                self._redirect("/admin/login", self._cookie("", 0))
                return
            client = client_key(self.headers.get("CF-Connecting-IP"), self.client_address[0])
            outcome, detail = admin.login(client, (form.get("token") or [""])[0])
            if outcome == "ok":
                self._redirect("/admin", self._cookie(str(detail), admin.session_ttl))
            elif outcome == "limited":
                wait = str(int(float(detail)) + 1)
                self._login_page(429, "Too many attempts. Try again later.", {"Retry-After": wait})
            else:
                self._login_page(401, "That token was not accepted.")

        def _route(self, head: bool) -> None:
            path = self._path()
            if path.startswith(("/admin", "/api/admin")):
                if admin_enabled:
                    self._admin_get(path, head)
                else:
                    self._send(404, b"not found\n", "text/plain; charset=utf-8", head=head)
                return
            if path == "/healthz":
                self._send(200, b"ok\n", "text/plain; charset=utf-8", head=head)
            elif path == "/api/status":
                try:
                    body = api.get()
                except Exception:
                    log.exception("building the status document failed")
                    self._send(503, b"status unavailable\n", "text/plain; charset=utf-8", head=head)
                    return
                self._send(200, body, "application/json", "public, max-age=5", head)
            elif path in ("/", "/index.html"):
                self._send(200, index_bytes, CONTENT_TYPES[".html"], "public, max-age=60", head)
            elif path.startswith("/static/") and path[8:] in assets:
                name = path[8:]
                ctype = CONTENT_TYPES[Path(name).suffix]
                cache = ICON_CACHE if name in ICON_FILES else "public, max-age=300"
                self._send(200, assets[name], ctype, cache, head)
            elif path in ROOT_ICONS:
                name = ROOT_ICONS[path]
                self._send(200, assets[name], CONTENT_TYPES[Path(name).suffix], ICON_CACHE, head)
            else:
                self._send(404, b"not found\n", "text/plain; charset=utf-8", head=head)

        def do_GET(self) -> None:
            self._route(head=False)

        def do_HEAD(self) -> None:
            self._route(head=True)

        def _deny(self) -> None:
            # The request body is never read, so the connection cannot be reused safely.
            self.close_connection = True
            self.send_response(405)
            self.send_header("Allow", "GET, HEAD")
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()

        def do_POST(self) -> None:
            path = self._path()
            if path.startswith(("/admin", "/api/admin")):
                if not admin_enabled:
                    self.close_connection = True
                    self._send(
                        404, b"not found\n", "text/plain; charset=utf-8", extra={"Connection": "close"}
                    )
                elif path in ("/admin/login", "/admin/logout"):
                    self._admin_post(path)
                else:
                    self._deny()
            else:
                self._deny()

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _deny

    return StatusHTTPServer((host, port), Handler, max_connections)
