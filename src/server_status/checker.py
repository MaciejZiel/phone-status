"""Health checks and optional per-service JSON metrics."""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import __version__
from .config import MetricField, ServiceConfig
from .store import Store

log = logging.getLogger(__name__)
MAX_BODY = 256 * 1024
PRUNE_EVERY = 3600.0
USER_AGENT = f"server-status/{__version__} (+https://github.com/MaciejZiel/server-status)"


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    latency_ms: float | None
    error: str = ""


def check_health(url: str, timeout: float) -> CheckResult:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(1024)
            ok = 200 <= resp.status < 400
            return CheckResult(ok, (time.perf_counter() - start) * 1000, "" if ok else f"HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        exc.close()  # releases the underlying connection
        return CheckResult(False, (time.perf_counter() - start) * 1000, f"HTTP {exc.code}")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        return CheckResult(False, None, type(reason).__name__)


def extract(document: object, path: str) -> object:
    """Resolve a dotted path (dict keys and list indexes) inside parsed JSON."""
    cur = document
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.lstrip("-").isdigit() and -len(cur) <= int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def pick_metrics(document: object, fields: tuple[MetricField, ...]) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for f in fields:
        value = extract(document, f.path)
        if isinstance(value, bool | int | float):
            shown: object = value
        elif isinstance(value, str):
            shown = value[:80]
        else:
            continue
        out.append({"label": f.label, "value": shown, "unit": f.unit})
    return out


def fetch_metrics(svc: ServiceConfig) -> list[dict[str, object]] | None:
    req = urllib.request.Request(
        svc.metrics_url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=svc.timeout) as resp:
            document = json.loads(resp.read(MAX_BODY))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, RecursionError) as exc:
        log.warning("metrics for %s unavailable: %s", svc.id, type(exc).__name__)
        return None
    return pick_metrics(document, svc.metrics)


class Checker:
    def __init__(
        self,
        services: tuple[ServiceConfig, ...],
        store: Store,
        retention_days: int,
        health: Callable[[str, float], CheckResult] = check_health,
        metrics: Callable[[ServiceConfig], list[dict[str, object]] | None] = fetch_metrics,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.services = services
        self._store = store
        self._retention = retention_days
        self._health = health
        self._metrics = metrics
        self._clock = clock
        self._lock = threading.Lock()
        self._metric_values: dict[str, list[dict[str, object]]] = {}
        self._next_prune = 0.0

    def _check_one(self, svc: ServiceConfig) -> None:
        result = self._health(svc.health_url, svc.timeout)
        self._store.record(svc.id, self._clock(), result.ok, result.latency_ms, result.error)
        if svc.metrics_url and svc.metrics:
            values = self._metrics(svc)
            if values is not None:
                with self._lock:
                    self._metric_values[svc.id] = values

    def run_once(self) -> None:
        if self.services:
            with ThreadPoolExecutor(max_workers=min(4, len(self.services))) as pool:
                list(pool.map(self._check_one, self.services))
        # Pruning scans the whole table, so once an hour is plenty (monotonic: immune to clock jumps).
        if time.monotonic() >= self._next_prune:
            self._store.prune(self._clock(), self._retention)
            self._next_prune = time.monotonic() + PRUNE_EVERY

    def metric_values(self, service_id: str) -> list[dict[str, object]]:
        with self._lock:
            return list(self._metric_values.get(service_id, []))
