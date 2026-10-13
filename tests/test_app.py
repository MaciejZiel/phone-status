from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from http.client import HTTPConnection

import pytest

from phone_status import server as server_module
from phone_status.checker import Checker
from phone_status.config import ServiceConfig
from phone_status.host import Battery, HostReaders, HostSampler
from phone_status.server import make_server
from phone_status.status import StatusBuilder
from phone_status.store import Store

NOW = 5_000_000.0


def _fake_health(url: str, timeout: float):  # type: ignore[no-untyped-def]
    from phone_status.checker import CheckResult

    return CheckResult(True, 12.0) if "good" in url else CheckResult(False, None, "Timeout")


@pytest.fixture
def builder() -> StatusBuilder:
    store = Store(":memory:")
    services = (
        ServiceConfig("good", "Good", "http://good/h", link="https://example.com"),
        ServiceConfig("bad", "Bad", "http://bad/h"),
    )
    checker = Checker(services, store, 8, health=_fake_health, clock=lambda: NOW)
    checker.run_once()
    readers = HostReaders(battery=lambda: Battery(50.0, "discharging", 31.0))
    sampler = HostSampler(readers)
    sampler.sample()
    return StatusBuilder("Test page", sampler, checker, store, 45.0, clock=lambda: NOW + 1)


def _serve(builder: StatusBuilder, base: str = "") -> Iterator[tuple[str, int]]:
    server = make_server("127.0.0.1", 0, base, builder.build, "Test <page>")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield "127.0.0.1", server.server_address[1]
    server.shutdown()
    server.server_close()


@pytest.fixture
def addr(builder: StatusBuilder) -> Iterator[tuple[str, int]]:
    yield from _serve(builder)


@pytest.fixture
def addr_based(builder: StatusBuilder) -> Iterator[tuple[str, int]]:
    yield from _serve(builder, "/status")


def _get(addr: tuple[str, int], path: str, method: str = "GET") -> tuple[int, dict[str, str], bytes]:
    conn = HTTPConnection(*addr, timeout=5)
    conn.request(method, path)
    resp = conn.getresponse()
    body = resp.read()
    headers = {k.lower(): v for k, v in resp.getheaders()}
    conn.close()
    return resp.status, headers, body


def test_status_document(addr: tuple[str, int]) -> None:
    status, headers, body = _get(addr, "/api/status")
    assert status == 200
    assert headers["content-type"] == "application/json"
    assert "max-age" in headers["cache-control"]
    doc = json.loads(body)
    assert doc["summary"] == {"state": "degraded", "up": 1, "down": 1, "total": 2}
    good, bad = doc["services"]
    assert good["state"] == "up"
    assert good["latency_ms"] == 12.0
    assert good["uptime_24h"] == 100.0
    assert len(good["bars_24h"]) == 24
    assert len(good["bars_7d"]) == 28
    assert bad["state"] == "down"
    assert bad["error"] == "Timeout"
    assert doc["host"]["battery"]["percent"] == 50.0


def test_no_secrets_in_document(addr: tuple[str, int]) -> None:
    _, _, body = _get(addr, "/api/status")
    text = body.decode()
    assert "http://good/h" not in text  # health URLs stay private


def test_index_and_static(addr: tuple[str, int]) -> None:
    status, headers, body = _get(addr, "/")
    assert status == 200
    assert b"Test &lt;page&gt;" in body
    assert b'<base href="/">' in body
    assert "script-src 'self'" in headers["content-security-policy"]
    assert b"cdn" not in body.lower()
    for name, ctype in (("app.js", "javascript"), ("app.css", "css"), ("favicon.svg", "svg")):
        s, h, _ = _get(addr, f"/static/{name}")
        assert s == 200
        assert ctype in h["content-type"]


def test_base_path_and_prefix_stripped(addr_based: tuple[str, int]) -> None:
    _, _, body = _get(addr_based, "/status/")
    assert b'<base href="/status/">' in body
    assert _get(addr_based, "/status/api/status")[0] == 200
    assert _get(addr_based, "/api/status")[0] == 200  # proxy that strips the prefix


def test_head_405_404_and_traversal(addr: tuple[str, int]) -> None:
    s, h, body = _get(addr, "/", "HEAD")
    assert (s, body) == (200, b"")
    assert int(h["content-length"]) > 0
    assert _get(addr, "/api/status", "POST")[0] == 405
    assert _get(addr, "/nope")[0] == 404
    assert _get(addr, "/static/../server.py")[0] == 404
    assert _get(addr, "/static/%2e%2e/server.py")[0] == 404
    assert _get(addr, "/healthz")[1]["cache-control"] == "no-store"


def test_unreachable_after_shutdown_raises() -> None:
    def hit(fn: Callable[[], object]) -> None:
        with pytest.raises(urllib.error.URLError):
            fn()

    hit(lambda: urllib.request.urlopen("http://127.0.0.1:1/", timeout=1))


def _raw_server(
    build: Callable[[], dict[str, object]], max_connections: int = 64
) -> tuple[str, int, Callable[[], None]]:
    server = make_server("127.0.0.1", 0, "", build, "t", max_connections=max_connections)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def stop() -> None:
        server.shutdown()
        server.server_close()

    return "127.0.0.1", server.server_address[1], stop


def test_idle_connection_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_module, "REQUEST_TIMEOUT", 0.3)
    host, port, stop = _raw_server(dict)
    try:
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.sendall(b"GET / HTTP/1.1\r\n")  # never finishes the headers
            assert sock.recv(1024) == b""  # server hangs up instead of waiting forever
    finally:
        stop()


def test_connection_cap_drops_extra_clients() -> None:
    host, port, stop = _raw_server(dict, max_connections=2)
    held = [socket.create_connection((host, port), timeout=5) for _ in range(2)]
    try:
        time.sleep(0.2)
        with socket.create_connection((host, port), timeout=5) as extra:
            extra.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
            assert extra.recv(1024) == b""
        held.pop().close()
        time.sleep(0.2)
        assert _get((host, port), "/healthz")[0] == 200
    finally:
        for sock in held:
            sock.close()
        stop()


def test_405_closes_connection_without_reading_body(addr: tuple[str, int]) -> None:
    with socket.create_connection(addr, timeout=5) as sock:
        sock.sendall(
            b"POST /api/status HTTP/1.1\r\nHost: x\r\nContent-Length: 30\r\n\r\n"
            b"GET /smuggled HTTP/1.1\r\nX: y\r\n"
        )
        data = b""
        while chunk := sock.recv(4096):
            data += chunk
    assert data.startswith(b"HTTP/1.1 405")
    assert b"Connection: close" in data
    assert data.count(b"HTTP/1.1") == 1


def test_build_failure_returns_503() -> None:
    def broken() -> dict[str, object]:
        raise RuntimeError("db gone")

    host, port, stop = _raw_server(broken)
    try:
        assert _get((host, port), "/api/status")[0] == 503
        assert _get((host, port), "/healthz")[0] == 200
    finally:
        stop()
