"""Tiny read-only HTTP server (stdlib) with a cached JSON API and static assets."""

from __future__ import annotations

import html
import json
import logging
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}
STATIC_FILES = {"app.css", "app.js", "favicon.svg"}
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'self'; form-action 'none'; frame-ancestors 'none'"
)
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
) -> StatusHTTPServer:
    api = CachedJson(build)
    index = (STATIC_DIR / "index.html").read_text().replace("{{base}}", base_path + "/")
    index = index.replace("{{title}}", html.escape(title))
    index_bytes = index.encode()
    assets = {name: (STATIC_DIR / name).read_bytes() for name in STATIC_FILES}

    class Handler(BaseHTTPRequestHandler):
        server_version = "status"
        sys_version = ""
        protocol_version = "HTTP/1.1"
        timeout = REQUEST_TIMEOUT

        def log_message(self, format: str, *args: Any) -> None:
            log.debug("%s - %s", self.address_string(), format % args)

        def _send(
            self, status: int, body: bytes, ctype: str, cache: str = "no-store", head: bool = False
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def _route(self, head: bool) -> None:
            path = self.path.split("?", 1)[0]
            if base_path and (path == base_path or path.startswith(base_path + "/")):
                path = path[len(base_path) :] or "/"
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
                self._send(200, assets[name], ctype, "public, max-age=300", head)
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

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _deny

    return StatusHTTPServer((host, port), Handler, max_connections)
