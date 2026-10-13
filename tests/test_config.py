from __future__ import annotations

import pytest

from phone_status.config import (
    ConfigError,
    Settings,
    load_services,
    normalize_base_path,
    parse_services,
)


def test_defaults() -> None:
    s = Settings.from_env({})
    assert (s.host, s.port, s.base_path, s.check_interval) == ("127.0.0.1", 8090, "", 45.0)


def test_env_overrides() -> None:
    s = Settings.from_env({"PHONE_STATUS_PORT": "9000", "PHONE_STATUS_BASE_PATH": "status/"})
    assert s.port == 9000
    assert s.base_path == "/status"


@pytest.mark.parametrize(
    "env",
    [
        {"PHONE_STATUS_PORT": "abc"},
        {"PHONE_STATUS_PORT": "70000"},
        {"PHONE_STATUS_CHECK_INTERVAL": "1"},
        {"PHONE_STATUS_RETENTION_DAYS": "3"},
    ],
)
def test_invalid_env(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        Settings.from_env(env)


def test_normalize_base_path() -> None:
    assert normalize_base_path("") == ""
    assert normalize_base_path("/") == ""
    assert normalize_base_path("/a/b/") == "/a/b"
    for bad in ('/x"><script>', "/a/../b", "/a b", "/a?b"):
        with pytest.raises(ConfigError):
            normalize_base_path(bad)


@pytest.mark.parametrize("timeout", ["5", 0, 60, True])
def test_service_timeout_validated(timeout: object) -> None:
    with pytest.raises(ConfigError, match="timeout"):
        parse_services({"services": [{"name": "A", "health_url": "http://a/", "timeout": timeout}]})


def test_parse_services_full() -> None:
    (svc,) = parse_services(
        {
            "services": [
                {
                    "name": "Live Flights",
                    "health_url": "http://127.0.0.1:1/h",
                    "metrics_url": "http://127.0.0.1:1/m",
                    "metrics": [{"path": "a.b", "label": "A", "unit": "x"}, {"path": "c"}],
                }
            ]
        }
    )
    assert svc.id == "live-flights"
    assert [m.label for m in svc.metrics] == ["A", "c"]


@pytest.mark.parametrize(
    "item",
    [
        {"health_url": "http://x"},
        {"name": "a"},
        {"name": "a", "health_url": "ftp://x"},
        {"name": "a", "health_url": "http://x", "link": "javascript:alert(1)"},
        {"name": "a", "health_url": "http://x", "metrics": [{"path": "p"}]},
    ],
)
def test_parse_services_rejects(item: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        parse_services({"services": [item]})


def test_duplicate_ids() -> None:
    svc = {"name": "a", "health_url": "http://x"}
    with pytest.raises(ConfigError, match="duplicate"):
        parse_services({"services": [svc, svc]})


def test_load_services_missing_and_bad(tmp_path) -> None:
    assert load_services(tmp_path / "none.toml") == ()
    bad = tmp_path / "bad.toml"
    bad.write_text("[[services")
    with pytest.raises(ConfigError):
        load_services(bad)


def test_example_file_is_valid() -> None:
    from pathlib import Path

    root = Path(__file__).parent.parent
    assert len(load_services(root / "services.example.toml")) == 4
    assert len(load_services(root / "tools" / "demo-services.toml")) == 4
