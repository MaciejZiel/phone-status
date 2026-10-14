"""The nginx snippets are plain text, so check the parts that matter without running nginx."""

from __future__ import annotations

import re
from pathlib import Path

from server_status.server import ADMIN_CSP, CSP, SECURITY_HEADERS

DEPLOY = Path(__file__).parent.parent / "deploy" / "termux"


def _active(name: str) -> str:
    """The file without comment lines."""
    lines = (DEPLOY / name).read_text().splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


def test_rate_limit_is_enabled_and_keyed_on_the_visitor_address() -> None:
    zones = _active("nginx-zones.conf")
    assert "$http_cf_connecting_ip" in zones
    assert re.search(r"limit_req_zone\s+\$status_client\s+zone=status:", zones)
    assert "$binary_remote_addr zone" not in zones  # the key is the map variable, not the tunnel address
    location = _active("nginx-server-status.conf")
    assert re.search(r"^\s*limit_req\s+zone=status\s+burst=\d+", location, re.MULTILINE)
    assert "limit_except GET HEAD" in location


def test_headers_snippet_matches_the_app_and_is_included_in_every_location() -> None:
    headers = _active("nginx-headers.conf")
    for name, value in SECURITY_HEADERS.items():
        assert f'add_header {name} "{value}" always;' in headers
        assert f"proxy_hide_header {name};" in headers
    assert "frame-ancestors 'none'" in CSP
    location = _active("nginx-server-status.conf")
    assert location.count("include snippets/server-status-headers.conf;") == 2


def test_admin_headers_snippet_matches_the_app_and_is_uncacheable() -> None:
    headers = _active("nginx-headers-admin.conf")
    admin_headers = {**SECURITY_HEADERS, "Content-Security-Policy": ADMIN_CSP}
    for name, value in admin_headers.items():
        assert f'add_header {name} "{value}" always;' in headers
    assert 'add_header Cache-Control "no-store" always;' in headers
    assert "form-action 'self'" in headers


def test_admin_locations_are_limited_uncached_and_use_the_admin_headers() -> None:
    zones = _active("nginx-zones.conf")
    assert re.search(r"limit_req_zone\s+\$status_client\s+zone=statuslogin:\S+\s+rate=\d+r/m", zones)
    config = _active("nginx-server-status-admin.conf")
    blocks = re.findall(r"location [^{]+\{.*?^\}", config, re.DOTALL | re.MULTILINE)
    assert len(blocks) == 3
    for block in blocks:
        assert "include snippets/server-status-headers-admin.conf;" in block
        assert re.search(r"limit_req\s+zone=status(login)?\s+burst=\d+", block)
        assert "proxy_no_cache 1;" in block
        assert "proxy_cache_bypass 1;" in block
    login = next(b for b in blocks if "admin/login" in b.splitlines()[0])
    assert "zone=statuslogin" in login


def test_env_example_never_contains_a_token() -> None:
    text = (DEPLOY / "env.example").read_text()
    assert re.search(r"^# SERVER_STATUS_ADMIN_TOKEN=\"\$\(cat ", text, re.MULTILINE)
    assert not re.search(r"^SERVER_STATUS_ADMIN_TOKEN=", text, re.MULTILINE)
