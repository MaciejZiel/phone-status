"""Settings (environment variables) and the service list (TOML file)."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlparse


class ConfigError(ValueError):
    """Raised when the configuration is invalid."""


@dataclass(frozen=True)
class MetricField:
    """One value to display from a service's JSON metrics document."""

    path: str
    label: str
    unit: str = ""


@dataclass(frozen=True)
class ServiceConfig:
    id: str
    name: str
    health_url: str
    description: str = ""
    link: str = ""
    timeout: float = 5.0
    metrics_url: str = ""
    metrics: tuple[MetricField, ...] = ()


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8090
    base_path: str = ""
    db_path: Path = Path("data/status.sqlite")
    services_file: Path = Path("services.toml")
    check_interval: float = 45.0
    host_interval: float = 15.0
    retention_days: int = 8
    storage_path: str = "~"
    title: str = "Phone server status"
    services: tuple[ServiceConfig, ...] = field(default=(), compare=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        def get(name: str, default: str) -> str:
            return env.get(f"PHONE_STATUS_{name}", default)

        try:
            port = int(get("PORT", "8090"))
            check_interval = float(get("CHECK_INTERVAL", "45"))
            host_interval = float(get("HOST_INTERVAL", "15"))
            retention = int(get("RETENTION_DAYS", "8"))
        except ValueError as exc:
            raise ConfigError(f"invalid numeric setting: {exc}") from exc
        if not 0 <= port <= 65535:
            raise ConfigError("PHONE_STATUS_PORT out of range")
        if check_interval < 5 or host_interval < 2:
            raise ConfigError("intervals too small (check >= 5 s, host >= 2 s)")
        if retention < 8:
            raise ConfigError("PHONE_STATUS_RETENTION_DAYS must be at least 8 (7 d uptime bars)")
        return cls(
            host=get("HOST", "127.0.0.1"),
            port=port,
            base_path=normalize_base_path(get("BASE_PATH", "")),
            db_path=Path(get("DB_PATH", "data/status.sqlite")),
            services_file=Path(get("SERVICES_FILE", "services.toml")),
            check_interval=check_interval,
            host_interval=host_interval,
            retention_days=retention,
            storage_path=get("STORAGE_PATH", "~"),
            title=get("TITLE", "Phone server status"),
        )


_BASE_PATH = re.compile(r"[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*")


def normalize_base_path(value: str) -> str:
    """Return '' or '/prefix' (leading slash, no trailing slash).

    The prefix is written into the page's <base href>, so only plain URL path characters are allowed.
    """
    value = value.strip().strip("/")
    if not value:
        return ""
    if not _BASE_PATH.fullmatch(value) or any(part in (".", "..") for part in value.split("/")):
        raise ConfigError(f"PHONE_STATUS_BASE_PATH must be a plain URL path, got {value!r}")
    return f"/{value}"


def _timeout(value: object, sid: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"service {sid!r}: timeout must be a number")
    timeout = float(value)
    if not 0.5 <= timeout <= 30:
        raise ConfigError(f"service {sid!r}: timeout must be between 0.5 and 30 seconds")
    return timeout


def _http_url(value: object, what: str, *, required: bool) -> str:
    if value in (None, ""):
        if required:
            raise ConfigError(f"{what} is required")
        return ""
    if not isinstance(value, str):
        raise ConfigError(f"{what} must be a string")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(f"{what} must be an http(s) URL, got {value!r}")
    return value


def parse_services(data: Mapping[str, object]) -> tuple[ServiceConfig, ...]:
    raw = data.get("services", [])
    if not isinstance(raw, list):
        raise ConfigError("'services' must be an array of tables")
    result: list[ServiceConfig] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConfigError(f"services[{index}] must be a table")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigError(f"services[{index}].name is required")
        sid = str(item.get("id") or name.lower().replace(" ", "-"))
        if sid in seen:
            raise ConfigError(f"duplicate service id {sid!r}")
        seen.add(sid)
        fields = []
        for m in item.get("metrics", []):
            if not isinstance(m, dict) or "path" not in m:
                raise ConfigError(f"service {sid!r}: each metric needs a 'path'")
            fields.append(MetricField(str(m["path"]), str(m.get("label", m["path"])), str(m.get("unit", ""))))
        metrics_url = _http_url(item.get("metrics_url"), f"service {sid!r} metrics_url", required=False)
        if fields and not metrics_url:
            raise ConfigError(f"service {sid!r}: metrics given without metrics_url")
        result.append(
            ServiceConfig(
                id=sid,
                name=name,
                health_url=_http_url(item.get("health_url"), f"service {sid!r} health_url", required=True),
                description=str(item.get("description", "")),
                link=_http_url(item.get("link"), f"service {sid!r} link", required=False),
                timeout=_timeout(item.get("timeout", 5.0), sid),
                metrics_url=metrics_url,
                metrics=tuple(fields),
            )
        )
    return tuple(result)


def load_services(path: Path) -> tuple[ServiceConfig, ...]:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return ()
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return parse_services(data)


def load_settings(env: Mapping[str, str]) -> Settings:
    base = Settings.from_env(env)
    services = load_services(base.services_file)
    return replace(base, services=services)
