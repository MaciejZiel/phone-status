from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_: object) -> None:
        pass

    def do_GET(self) -> None:
        routes = {
            "/ok": (200, b"ok"),
            "/boom": (500, b"no"),
            "/metrics": (200, json.dumps({"a": {"b": 7}, "items": [{"n": 3}], "s": "hi", "o": {}}).encode()),
            "/badjson": (200, b"not json"),
        }
        status, body = routes.get(self.path, (404, b""))
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def fake_upstream() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
