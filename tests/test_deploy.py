"""The nginx snippets are plain text, so check the parts that matter without running nginx."""

from __future__ import annotations

import re
from pathlib import Path

from server_status.server import CSP, SECURITY_HEADERS

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
