from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator
from http.client import HTTPConnection
from urllib.parse import urlencode

import pytest

from server_status.admin import AdminAuth, RateLimiter, client_key
from server_status.checker import Checker, CheckResult
from server_status.config import ServiceConfig
from server_status.host import Battery, HostReaders, HostSampler
from server_status.server import COOKIE_NAME, make_server
from server_status.status import StatusBuilder
from server_status.store import Store

NOW = 5_000_000.0
TOKEN = "t0k3n-" + "x" * 40
LEAKY = "URLError: <urlopen error [Errno 111] Connection refused> http://10.0.0.5:8081/internal/health"
# Values that exist only in the sampler: they must never appear in anything public.
SECRET_NUMBERS = ("7654321", "9876543210", "87.0", "43.5", "8.8 ", "6543210")


def _health(url: str, timeout: float) -> CheckResult:
    return CheckResult(True, 12.0) if "good" in url else CheckResult(False, None, LEAKY)


def _builder() -> StatusBuilder:
    store = Store(":memory:")
    services = (ServiceConfig("good", "Good", "http://good/h"), ServiceConfig("bad", "Bad", "http://bad/h"))
    checker = Checker(services, store, 8, health=_health, clock=lambda: NOW)
    checker.run_once()
    readers = HostReaders(
        battery=lambda: Battery(87.0, "charging", 43.5),
        memory=lambda: (9_876_543_210, 3_333_333_333),
        storage=lambda p: (500_000_000_000, 7_654_321, 6_543_210),
        loadavg=lambda: (1.11, 0.5, 0.25),
        cpu_count=lambda: 6,
        cpu_times=lambda: (50, 100),
        net_bytes=lambda: (1000, 2000),
        uptime=lambda: 190_000.0,
    )
    sampler = HostSampler(readers)
    sampler.sample()
    return StatusBuilder("Test page", sampler, checker, store, 45.0, clock=lambda: NOW + 1)


def _start(admin: AdminAuth | None, base: str = "") -> Iterator[tuple[str, int]]:
    builder = _builder()
    server = make_server(
        "127.0.0.1", 0, base, builder.build, "Test", admin=admin, build_admin=builder.build_admin
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield "127.0.0.1", server.server_address[1]
    server.shutdown()
    server.server_close()


@pytest.fixture
def addr() -> Iterator[tuple[str, int]]:
    yield from _start(AdminAuth(TOKEN))


@pytest.fixture
def addr_disabled() -> Iterator[tuple[str, int]]:
    yield from _start(None)


@pytest.fixture
def addr_based() -> Iterator[tuple[str, int]]:
    yield from _start(AdminAuth(TOKEN), "/status")


def _req(
    addr: tuple[str, int],
    path: str,
    method: str = "GET",
    form: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    conn = HTTPConnection(*addr, timeout=5)
    body = urlencode(form) if form is not None else None
    hdrs = dict(headers or {})
    if body is not None:
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    conn.request(method, path, body=body, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read()
    out = {k.lower(): v for k, v in resp.getheaders()}
    conn.close()
    return resp.status, out, data


def _login(
    addr: tuple[str, int], token: str = TOKEN, ip: str = "203.0.113.7"
) -> tuple[int, dict[str, str], bytes]:
    return _req(addr, "/admin/login", "POST", {"token": token}, {"CF-Connecting-IP": ip})


def _cookie(addr: tuple[str, int]) -> dict[str, str]:
    status, headers, _ = _login(addr)
    assert status == 303
    return {"Cookie": headers["set-cookie"].split(";", 1)[0]}


# ---- public side ---------------------------------------------------------------------------


def test_public_json_and_pages_never_contain_real_values(addr: tuple[str, int]) -> None:
    for path in ("/api/status", "/", "/static/app.js", "/static/app.css"):
        _, _, body = _req(addr, path)
        text = body.decode().lower()
        for secret in SECRET_NUMBERS:
            assert secret not in text, (path, secret)
        for word in ("battery", "phone", "android", "termux", "charging"):
            assert word not in text, (path, word)
        assert LEAKY.lower() not in text


def test_public_json_has_placeholder_cards_only(addr: tuple[str, int]) -> None:
    doc = json.loads(_req(addr, "/api/status")[2])
    labels = [c["label"] for c in doc["host"]["extra"]]
    assert labels == ["Power", "Temperature", "Load", "Network"]
    assert all(c["placeholder"] for c in doc["host"]["extra"])
    assert doc["services"][1]["error"] == "unreachable"
    assert b"administrator" in _req(addr, "/")[2]  # the note on the public page
    assert b"blurred" in _req(addr, "/static/app.css")[2]


def test_public_response_sets_no_cookie(addr: tuple[str, int]) -> None:
    _cookie(addr)
    for path in ("/", "/api/status"):
        assert "set-cookie" not in _req(addr, path)[1]


# ---- admin disabled ------------------------------------------------------------------------


def test_admin_is_404_when_no_token(addr_disabled: tuple[str, int]) -> None:
    for path in ("/admin", "/admin/login", "/api/admin/status", "/admin/anything"):
        assert _req(addr_disabled, path)[0] == 404, path
    assert _req(addr_disabled, "/admin/login", "POST", {"token": TOKEN})[0] == 404
    assert _req(addr_disabled, "/admin/logout", "POST", {})[0] == 404
    assert _req(addr_disabled, "/api/status")[0] == 200


@pytest.mark.parametrize("value", [None, "", "   ", "short-token"])
def test_unusable_tokens_disable_admin(value: str | None) -> None:
    assert AdminAuth.from_token(value) is None


def test_long_token_enables_admin() -> None:
    assert AdminAuth.from_token(f"  {TOKEN}\n") is not None


# ---- login ---------------------------------------------------------------------------------


def test_admin_without_cookie_is_401_or_redirect(addr: tuple[str, int]) -> None:
    status, headers, body = _req(addr, "/api/admin/status")
    assert status == 401
    assert b"battery" not in body
    status, headers, _ = _req(addr, "/admin")
    assert status == 303
    assert headers["location"] == "/admin/login"
    assert _req(addr, "/admin", headers={"Cookie": f"{COOKIE_NAME}=forged"})[0] == 303
    assert _req(addr, "/api/admin/status", headers={"Cookie": f"{COOKIE_NAME}=forged"})[0] == 401


def test_login_page_allows_its_own_form_only(addr: tuple[str, int]) -> None:
    status, headers, body = _req(addr, "/admin/login")
    assert status == 200
    assert b'method="post"' in body
    assert "form-action 'self'" in headers["content-security-policy"]
    assert headers["cache-control"] == "no-store"
    assert "form-action 'none'" in _req(addr, "/")[1]["content-security-policy"]


def test_wrong_token_is_rejected(addr: tuple[str, int]) -> None:
    status, headers, body = _login(addr, "wrong-" + "y" * 40)
    assert status == 401
    assert "set-cookie" not in headers
    assert b"not accepted" in body
    assert b"wrong-" not in body


def test_correct_token_sets_a_hardened_cookie(addr: tuple[str, int]) -> None:
    status, headers, _ = _login(addr)
    assert status == 303
    assert headers["location"] == "/admin"
    cookie = headers["set-cookie"]
    name, _, rest = cookie.partition("=")
    assert name == COOKIE_NAME
    value = rest.split(";", 1)[0]
    assert len(value) >= 40
    assert TOKEN not in cookie
    flags = {part.strip().lower() for part in cookie.split(";")[1:]}
    assert {"httponly", "secure", "samesite=strict", "path=/", "max-age=43200"} <= flags


def test_cookie_path_follows_base_path(addr_based: tuple[str, int]) -> None:
    status, headers, _ = _req(addr_based, "/status/admin/login", "POST", {"token": TOKEN})
    assert status == 303
    assert headers["location"] == "/status/admin"
    assert "Path=/status;" in headers["set-cookie"]
    cookie = {"Cookie": headers["set-cookie"].split(";", 1)[0]}
    assert _req(addr_based, "/status/api/admin/status", headers=cookie)[0] == 200


def test_correct_token_gives_the_full_data(addr: tuple[str, int]) -> None:
    cookie = _cookie(addr)
    status, headers, body = _req(addr, "/api/admin/status", headers=cookie)
    assert status == 200
    assert headers["cache-control"] == "no-store"
    assert headers["content-type"] == "application/json"
    host = json.loads(body)["host"]
    assert host["exact"] is True
    raw = host["raw"]
    assert raw["battery"] == {"percent": 87.0, "status": "charging", "charging": True, "temperature_c": 43.5}
    assert raw["memory"]["total"] == 9_876_543_210
    assert raw["storage"]["used"] == 7_654_321
    assert raw["cpu"]["cores"] == 6
    assert raw["cpu"]["load"] == [1.11, 0.5, 0.25]
    assert raw["network"] is not None
    assert raw["uptime_s"] == 190_000.0
    assert host["uptime_s"] == 190_000
    assert [c["label"] for c in host["extra"]] == ["Power", "Temperature", "Load", "Network"]
    assert not any(c.get("placeholder") for c in host["extra"])
    assert host["extra"][0]["value"] == "87"
    assert host["extra"][1]["value"] == "43.5"
    assert "9,876,543,210" in host["sub"]["memory"]
    assert TOKEN not in body.decode()


def test_admin_services_carry_the_full_error_text(addr: tuple[str, int]) -> None:
    cookie = _cookie(addr)
    doc = json.loads(_req(addr, "/api/admin/status", headers=cookie)[2])
    assert doc["services"][1]["error"] == LEAKY
    assert doc["services"][0]["latency_ms"] == 12.0


def test_admin_page_and_logout(addr: tuple[str, int]) -> None:
    cookie = _cookie(addr)
    status, headers, body = _req(addr, "/admin", headers=cookie)
    assert status == 200
    assert headers["cache-control"] == "no-store"
    assert b"Administrator view" in body
    assert b'data-api="api/admin/status"' in body
    assert b"phone" not in body.lower()
    assert _req(addr, "/admin/login", headers=cookie)[0] == 303  # already signed in
    status, headers, _ = _req(addr, "/admin/logout", "POST", {}, cookie)
    assert status == 303
    assert "max-age=0" in headers["set-cookie"].lower()
    assert _req(addr, "/api/admin/status", headers=cookie)[0] == 401  # the session is gone server side


def test_cross_site_post_is_refused(addr: tuple[str, int]) -> None:
    status, _, _ = _req(addr, "/admin/login", "POST", {"token": TOKEN}, {"Sec-Fetch-Site": "cross-site"})
    assert status == 403
    assert _req(addr, "/admin/login", "POST", {"token": TOKEN}, {"Sec-Fetch-Site": "same-origin"})[0] == 303


def test_oversized_login_body_is_refused(addr: tuple[str, int]) -> None:
    assert _req(addr, "/admin/login", "POST", {"token": "a" * 5000})[0] == 413


def test_other_posts_stay_405(addr: tuple[str, int]) -> None:
    assert _req(addr, "/api/status", "POST", {})[0] == 405
    assert _req(addr, "/api/admin/status", "POST", {})[0] == 405


# ---- rate limit ----------------------------------------------------------------------------


def test_login_is_rate_limited_per_visitor(addr: tuple[str, int]) -> None:
    for _ in range(5):
        assert _login(addr, "bad-" + "z" * 40, "198.51.100.1")[0] == 401
    status, headers, _ = _login(addr, "bad-" + "z" * 40, "198.51.100.1")
    assert status == 429
    assert int(headers["retry-after"]) > 0
    # even the right token is refused while locked out, another visitor is unaffected
    assert _login(addr, TOKEN, "198.51.100.1")[0] == 429
    assert _login(addr, TOKEN, "198.51.100.2")[0] == 303


def test_rate_limiter_window_and_bounded_table() -> None:
    now = [0.0]
    limiter = RateLimiter(2, 10.0, lambda: now[0], max_keys=3)
    assert limiter.hit("a") == 0
    assert limiter.hit("a") == 0
    assert limiter.hit("a") > 0
    now[0] = 10.0
    assert limiter.hit("a") == 0
    for key in "bcdef":
        limiter.hit(key)
    assert len(limiter._hits) <= 3


def test_client_key_ignores_garbage_headers() -> None:
    assert client_key("203.0.113.9", "127.0.0.1") == "203.0.113.9"
    assert client_key("not an ip" * 50, "127.0.0.1") == "127.0.0.1"
    assert client_key(None, "127.0.0.1") == "127.0.0.1"


# ---- sessions ------------------------------------------------------------------------------


def test_sessions_expire_and_table_is_bounded() -> None:
    now = [0.0]
    auth = AdminAuth(TOKEN, clock=lambda: now[0], session_ttl=100, max_sessions=2, attempts=100)
    sids = [str(auth.login("c", TOKEN)[1]) for _ in range(3)]
    assert len(set(sids)) == 3
    assert not auth.valid(sids[0])  # evicted: only two are kept
    assert auth.valid(sids[1])
    assert auth.valid(sids[2])
    now[0] = 101.0
    assert not auth.valid(sids[2])
    assert not auth.valid(None)


# ---- the token never leaks -----------------------------------------------------------------


def test_token_never_appears_in_logs_or_responses(
    addr: tuple[str, int], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    wrong = "guess-" + "q" * 40
    responses = [_login(addr, wrong, "192.0.2.1"), _login(addr, TOKEN, "192.0.2.2")]
    cookie = {"Cookie": responses[1][1]["set-cookie"].split(";", 1)[0]}
    responses += [_req(addr, p, headers=cookie) for p in ("/admin", "/api/admin/status", "/admin/login")]
    responses.append(_req(addr, "/admin/logout", "POST", {}, cookie))
    for _status, headers, body in responses:
        blob = (json.dumps(headers) + body.decode()).replace(cookie["Cookie"], "")
        assert TOKEN not in blob
        assert wrong not in blob
    assert TOKEN not in caplog.text
    assert wrong not in caplog.text
    assert "login failed" in caplog.text  # failures are logged, without the secret
