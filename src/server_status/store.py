"""SQLite storage of service checks and uptime aggregation."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

DAY = 86400


@dataclass(frozen=True)
class CheckRow:
    ts: float
    ok: bool
    latency_ms: float | None
    error: str


class Store:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS checks ("
                "service TEXT NOT NULL, ts REAL NOT NULL, ok INTEGER NOT NULL, "
                "latency_ms REAL, error TEXT NOT NULL DEFAULT '')"
            )
            self._db.execute("CREATE INDEX IF NOT EXISTS checks_service_ts ON checks(service, ts)")
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def record(self, service: str, ts: float, ok: bool, latency_ms: float | None, error: str = "") -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO checks(service, ts, ok, latency_ms, error) VALUES (?,?,?,?,?)",
                (service, ts, int(ok), latency_ms, error[:200]),
            )
            self._db.commit()

    def prune(self, now: float, retention_days: int) -> int:
        with self._lock:
            cur = self._db.execute("DELETE FROM checks WHERE ts < ?", (now - retention_days * DAY,))
            self._db.commit()
            return cur.rowcount

    def latest(self, service: str) -> CheckRow | None:
        with self._lock:
            row = self._db.execute(
                # Insertion order, not ts: a wall clock that jumped forward and back must not
                # leave a stale "future" row as the latest result.
                "SELECT ts, ok, latency_ms, error FROM checks WHERE service=? ORDER BY rowid DESC LIMIT 1",
                (service,),
            ).fetchone()
        return CheckRow(row[0], bool(row[1]), row[2], row[3]) if row else None

    def buckets(self, service: str, now: float, window: float, count: int) -> list[float | None]:
        """Share of successful checks per equal time bucket (None = no data)."""
        start = now - window
        size = window / count
        with self._lock:
            rows = self._db.execute(
                # A check at exactly `now` falls into the last bucket, so clamp before grouping;
                # clamping afterwards would let that group overwrite the last bucket.
                "SELECT MIN(CAST((ts - ?) / ? AS INTEGER), ?) AS b, COUNT(*), SUM(ok) "
                "FROM checks WHERE service=? AND ts >= ? AND ts <= ? GROUP BY b",
                (start, size, count - 1, service, start, now),
            ).fetchall()
        result: list[float | None] = [None] * count
        for b, total, ok in rows:
            result[int(b)] = ok / total
        return result

    def uptime_pct(self, service: str, now: float, window: float) -> float | None:
        with self._lock:
            total, ok = self._db.execute(
                "SELECT COUNT(*), COALESCE(SUM(ok), 0) FROM checks WHERE service=? AND ts >= ? AND ts <= ?",
                (service, now - window, now),
            ).fetchone()
        return round(100 * ok / total, 2) if total else None

    def avg_latency(self, service: str, now: float, window: float) -> float | None:
        with self._lock:
            (avg,) = self._db.execute(
                "SELECT AVG(latency_ms) FROM checks WHERE service=? AND ok=1 AND ts >= ? AND ts <= ?",
                (service, now - window, now),
            ).fetchone()
        return round(avg, 1) if avg is not None else None
