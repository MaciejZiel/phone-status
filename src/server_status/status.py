"""Builds the public JSON document from the sampler, the store and the checker."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from .checker import Checker
from .host import HostSampler
from .store import DAY, Store

HOURLY_BUCKETS = 24
WEEK_BUCKETS = 28  # 6 h each

# What the public document may say about a failed check. Anything else becomes GENERIC_ERROR,
# so a message that carries a URL, host:port or path (e.g. from an older database row) never leaks.
GENERIC_ERROR = "unreachable"
KNOWN_ERRORS = frozenset(
    {"timeout", "connection refused", "connection reset", "dns error", "tls error", GENERIC_ERROR}
)
_HTTP_ERROR = re.compile(r"HTTP [1-5]\d\d")


def public_error(error: str) -> str:
    if error in KNOWN_ERRORS or _HTTP_ERROR.fullmatch(error):
        return error
    return GENERIC_ERROR


def _round_to(value: object, step: int) -> int | None:
    if not isinstance(value, int | float):
        return None
    return int(max(0, min(100, step * round(value / step))))


def _percent(used: object, total: object) -> int | None:
    if not isinstance(used, int | float) or not isinstance(total, int | float) or total <= 0:
        return None
    return _round_to(100 * used / total, 5)


def public_host(snapshot: dict[str, Any]) -> dict[str, Any]:
    """The part of the host snapshot that is served: coarse figures only.

    Uptime is floored to the hour, load and usage are rounded to 5 %. Exact byte counts, core count,
    load averages, network rates and the battery are collected but never published.
    """
    uptime = snapshot.get("uptime_s")
    cpu = snapshot.get("cpu") or {}
    mem = snapshot.get("memory") or {}
    disk = snapshot.get("storage") or {}
    return {
        "uptime_s": int(uptime // 3600 * 3600) if isinstance(uptime, int | float) else None,
        "cpu_percent": _round_to(cpu.get("percent"), 5),
        "memory_percent": _percent(mem.get("used"), mem.get("total")),
        "storage_percent": _percent(disk.get("used"), disk.get("total")),
    }


class StatusBuilder:
    def __init__(
        self,
        title: str,
        sampler: HostSampler,
        checker: Checker,
        store: Store,
        check_interval: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._title = title
        self._sampler = sampler
        self._checker = checker
        self._store = store
        self._interval = check_interval
        self._clock = clock

    def build(self) -> dict[str, Any]:
        now = self._clock()
        services = [self._service(svc, now) for svc in self._checker.services]
        up = sum(1 for s in services if s["state"] == "up")
        down = sum(1 for s in services if s["state"] == "down")
        if down:
            state = "down" if down == len(services) else "degraded"
        elif services and up == len(services):
            state = "up"
        else:
            state = "unknown"
        return {
            "title": self._title,
            "generated_at": now,
            "check_interval_s": self._interval,
            "summary": {"state": state, "up": up, "down": down, "total": len(services)},
            "host": public_host(self._sampler.snapshot()),
            "services": services,
        }

    def _service(self, svc: Any, now: float) -> dict[str, Any]:
        last = self._store.latest(svc.id)
        if last is None or now - last.ts > max(3 * self._interval, 120):
            state = "unknown"
        else:
            state = "up" if last.ok else "down"
        return {
            "id": svc.id,
            "name": svc.name,
            "description": svc.description,
            "link": svc.link,
            "state": state,
            "latency_ms": round(last.latency_ms, 1) if last and last.latency_ms is not None else None,
            "error": public_error(last.error) if last and not last.ok else "",
            "checked_at": last.ts if last else None,
            "avg_latency_ms_24h": self._store.avg_latency(svc.id, now, DAY),
            "uptime_24h": self._store.uptime_pct(svc.id, now, DAY),
            "uptime_7d": self._store.uptime_pct(svc.id, now, 7 * DAY),
            "bars_24h": self._store.buckets(svc.id, now, DAY, HOURLY_BUCKETS),
            "bars_7d": self._store.buckets(svc.id, now, 7 * DAY, WEEK_BUCKETS),
            "metrics": self._checker.metric_values(svc.id),
        }
