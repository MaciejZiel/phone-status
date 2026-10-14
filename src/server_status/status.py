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


# Shown blurred on the public page so the layout matches the administrator view. Fixed made-up
# numbers: nothing here is derived from the host, so removing the blur reveals nothing.
PLACEHOLDER_CARDS: tuple[dict[str, Any], ...] = (
    {"label": "Power", "value": "76", "unit": "%", "sub": "details hidden", "pct": 76, "low": True},
    {"label": "Temperature", "value": "31.0", "unit": "\u00b0C", "sub": "details hidden"},
    {"label": "Load", "value": "0.42", "unit": "", "sub": "details hidden"},
    {"label": "Network", "value": "1.2 MB/s", "unit": "down", "sub": "details hidden"},
)


def public_host(snapshot: dict[str, Any]) -> dict[str, Any]:
    """The part of the host snapshot that is served: coarse figures only.

    Uptime is floored to the hour, load and usage are rounded to 5 %. Everything else the sampler
    collects (exact bytes, cores, load, network, power, temperature) is never published; the extra
    cards are fixed placeholders.
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
        "extra": [{**card, "placeholder": True} for card in PLACEHOLDER_CARDS],
    }


def _bytes(n: float) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def _rate(bps: float) -> str:
    return f"{_bytes(bps)}/s"


def _num(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _exact_percent(used: object, total: object) -> float | None:
    u, t = _num(used), _num(total)
    return round(100 * u / t, 1) if u is not None and t else None


def _usage_sub(block: dict[str, Any]) -> str:
    used, total = _num(block.get("used")), _num(block.get("total"))
    if used is None or total is None:
        return "unreadable"
    return f"{_bytes(used)} of {_bytes(total)} ({int(used):,} of {int(total):,} B)"


def admin_host(snapshot: dict[str, Any]) -> dict[str, Any]:
    """The full host snapshot for the administrator view, plus the cards the page draws from it."""
    cpu = snapshot.get("cpu") or {}
    mem = snapshot.get("memory") or {}
    disk = snapshot.get("storage") or {}
    power = snapshot.get("battery") or {}
    net = snapshot.get("network") or {}
    load = cpu.get("load")
    cores = cpu.get("cores")
    uptime = _num(snapshot.get("uptime_s"))

    if power:
        pct = _num(power.get("percent"))
        status = str(power.get("status", "unknown")).replace("_", " ")
        power_card: dict[str, Any] = {
            "label": "Power",
            "value": f"{pct:g}" if pct is not None else "n/a",
            "unit": "%" if pct is not None else "",
            "sub": f"battery, {status}",
            "pct": pct,
            "low": True,
        }
        temp = _num(power.get("temperature_c"))
        temp_card = {
            "label": "Temperature",
            "value": f"{temp:.1f}" if temp is not None else "n/a",
            "unit": "\u00b0C" if temp is not None else "",
            "sub": "battery sensor" if temp is not None else "unreadable",
        }
    else:
        power_card = {"label": "Power", "value": "n/a", "unit": "", "sub": "no reader available"}
        temp_card = {"label": "Temperature", "value": "n/a", "unit": "", "sub": "no reader available"}

    if isinstance(load, list) and load:
        load_card = {
            "label": "Load",
            "value": f"{load[0]:.2f}",
            "unit": "",
            "sub": " / ".join(f"{x:.2f}" for x in load) + (f" \u00b7 {cores} cores" if cores else ""),
        }
    else:
        load_card = {"label": "Load", "value": "n/a", "unit": "", "sub": "unreadable"}

    if not net:
        net_card = {"label": "Network", "value": "n/a", "unit": "", "sub": "not readable on this host"}
    elif net.get("rx_bps") is None:
        net_card = {"label": "Network", "value": "\u2026", "unit": "", "sub": "measuring"}
    else:
        net_card = {
            "label": "Network",
            "value": _rate(net["rx_bps"]),
            "unit": "down",
            "sub": f"{_rate(net['tx_bps'] or 0)} up",
        }

    return {
        "exact": True,
        "uptime_s": int(uptime) if uptime is not None else None,
        "cpu_percent": _num(cpu.get("percent")),
        "memory_percent": _exact_percent(mem.get("used"), mem.get("total")),
        "storage_percent": _exact_percent(disk.get("used"), disk.get("total")),
        "sub": {
            "cpu": f"{cores} cores" if cores else "core count unknown",
            "memory": _usage_sub(mem),
            "storage": _usage_sub(disk),
        },
        "extra": [power_card, temp_card, load_card, net_card],
        "raw": snapshot,
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
        """The public document."""
        return self._build(public_host, admin=False)

    def build_admin(self) -> dict[str, Any]:
        """The administrator document: full host data and the full text of failed checks."""
        return self._build(admin_host, admin=True)

    def _build(self, host: Callable[[dict[str, Any]], dict[str, Any]], admin: bool) -> dict[str, Any]:
        now = self._clock()
        services = [self._service(svc, now, admin) for svc in self._checker.services]
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
            "host": host(self._sampler.snapshot()),
            "services": services,
        }

    def _service(self, svc: Any, now: float, admin: bool = False) -> dict[str, Any]:
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
            "error": (last.error if admin else public_error(last.error)) if last and not last.ok else "",
            "checked_at": last.ts if last else None,
            "avg_latency_ms_24h": self._store.avg_latency(svc.id, now, DAY),
            "uptime_24h": self._store.uptime_pct(svc.id, now, DAY),
            "uptime_7d": self._store.uptime_pct(svc.id, now, 7 * DAY),
            "bars_24h": self._store.buckets(svc.id, now, DAY, HOURLY_BUCKETS),
            "bars_7d": self._store.buckets(svc.id, now, 7 * DAY, WEEK_BUCKETS),
            "metrics": self._checker.metric_values(svc.id),
        }
