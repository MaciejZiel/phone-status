from __future__ import annotations

import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from http.client import HTTPConnection

import pytest

from server_status import server as server_module
from server_status.checker import Checker
from server_status.config import ServiceConfig
from server_status.host import Battery, HostReaders, HostSampler
from server_status.server import make_server
from server_status.status import StatusBuilder, public_host
from server_status.store import Store

NOW = 5_000_000.0


def _fake_health(url: str, timeout: float):  # type: ignore[no-untyped-def]
    from server_status.checker import CheckResult

    return CheckResult(True, 12.0) if "good" in url else CheckResult(False, None, "timeout")


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
    assert bad["error"] == "timeout"
    # coarse figures plus fixed placeholder cards; nothing else about the host
    assert set(doc["host"]) == {"uptime_s", "cpu_percent", "memory_percent", "storage_percent", "extra"}
    assert doc["host"]["extra"]
    assert all(card["placeholder"] for card in doc["host"]["extra"])


def test_no_secrets_in_document(addr: tuple[str, int]) -> None:
    _, _, body = _get(addr, "/api/status")
    text = body.decode()
    assert "http://good/h" not in text  # health URLs stay private
    for word in ("battery", "phone", "android", "termux", "charging", "cores", "50.0", "31.0 "):
        assert word not in text.lower()


def test_public_host_rounds_and_drops() -> None:
    snap = {
        "uptime_s": 190_000.7,
        "cpu": {"percent": 37.4, "load": [1.0, 0.5, 0.2], "cores": 8},
        "memory": {"total": 8_000_000_000, "available": 1, "used": 5_100_000_000},
        "storage": {"total": 100, "used": 99, "free": 1},
        "battery": {"percent": 80.0, "status": "charging", "charging": True, "temperature_c": 31.0},
        "network": {"rx_bps": 1234.0, "tx_bps": 5.0},
    }
    host = public_host(snap)
    extra = host.pop("extra")
    assert host == {
        "uptime_s": 187_200,  # 52 h
        "cpu_percent": 35,
        "memory_percent": 65,
        "storage_percent": 100,
    }
    # placeholders are constants: they do not change with the sampled values
    assert extra == public_host({})["extra"]
    assert "80.0" not in str(extra)
    empty = public_host({})
    empty.pop("extra")
    assert empty == {
        "uptime_s": None,
        "cpu_percent": None,
        "memory_percent": None,
        "storage_percent": None,
    }


def test_down_service_error_is_generic_even_for_old_rows() -> None:
    from server_status.checker import CheckResult

    leaky = "URLError: <urlopen error [Errno 111] Connection refused> http://10.0.0.5:8081/internal/health"

    def health(url: str, timeout: float) -> CheckResult:
        return CheckResult(False, None, leaky) if "old" in url else CheckResult(False, None, "HTTP 502")

    store = Store(":memory:")
    services = (ServiceConfig("old", "Old", "http://old/h"), ServiceConfig("new", "New", "http://new/h"))
    checker = Checker(services, store, 8, health=health, clock=lambda: NOW)
    checker.run_once()
    doc = StatusBuilder("t", HostSampler(HostReaders()), checker, store, 45.0, clock=lambda: NOW + 1).build()
    errors = {s["id"]: s["error"] for s in doc["services"]}
    assert errors == {"old": "unreachable", "new": "HTTP 502"}
    assert "10.0.0.5" not in json.dumps(doc)


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


def test_icons_are_images_and_cached(addr: tuple[str, int]) -> None:
    for path, ctype in (
        ("/favicon.ico", "image/x-icon"),
        ("/apple-touch-icon.png", "image/png"),
        ("/static/favicon.svg", "image/svg+xml"),
        ("/static/favicon.ico", "image/x-icon"),
        ("/static/favicon-32x32.png", "image/png"),
        ("/static/apple-touch-icon.png", "image/png"),
    ):
        s, h, body = _get(addr, path)
        assert s == 200, path
        assert h["content-type"] == ctype
        assert h["cache-control"] == "public, max-age=86400"
        assert body
    assert _get(addr, "/favicon.ico", "HEAD")[0] == 200


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


def test_page_assets_do_not_mention_battery_or_phone(addr: tuple[str, int]) -> None:
    for path in ("/", "/static/app.js", "/static/app.css"):
        _, _, body = _get(addr, path)
        text = body.decode().lower()
        assert "battery" not in text, path
        assert "phone" not in text, path


def test_asset_urls_carry_a_content_version(addr: tuple[str, int]) -> None:
    _, _, body = _get(addr, "/")
    page = body.decode()
    match = re.search(r'static/app\.js\?v=([0-9a-f]{10})"', page)
    assert match, page
    assert f'static/app.css?v={match.group(1)}"' in page
    # The versioned address still serves the file.
    status, _, asset = _get(addr, f"/static/app.js?v={match.group(1)}")
    assert status == 200
    assert asset
