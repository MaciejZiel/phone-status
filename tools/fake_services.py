"""Local fake services for demos and screenshots (not used in production).

Serves on 127.0.0.1:<port>:
  /reroute/health, /flights/health, /planbot/health, /pgqueue/health (the last one fails)
  /reroute/metrics   JSON with a nested counter
"""

from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        if self.path == "/pgqueue/health":
            self.send_error(503)
            return
        if self.path == "/flights/health":
            time.sleep(0.12)
        if self.path.endswith("/health"):
            body = b'{"status":"ok"}'
        elif self.path == "/reroute/metrics":
            body = json.dumps({"observations": {"today": 48213, "total": 1904427}, "lag_s": 4.2}).encode()
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8191
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
