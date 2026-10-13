from __future__ import annotations

from phone_status.store import DAY, Store

NOW = 1_000_000.0


def test_latest_and_uptime() -> None:
    s = Store(":memory:")
    assert s.latest("a") is None
    assert s.uptime_pct("a", NOW, DAY) is None
    s.record("a", NOW - 30, True, 10.0)
    s.record("a", NOW - 20, False, None, "timeout")
    s.record("a", NOW - 10, True, 30.0)
    last = s.latest("a")
    assert last is not None
    assert last.ok
    assert last.latency_ms == 30.0
    assert s.uptime_pct("a", NOW, DAY) == 66.67
    assert s.avg_latency("a", NOW, DAY) == 20.0
    assert s.uptime_pct("other", NOW, DAY) is None


def test_buckets_place_checks_in_time_order() -> None:
    s = Store(":memory:")
    s.record("a", NOW - DAY + 10, True, 1.0)  # first bucket
    s.record("a", NOW - 10, False, None)  # last bucket
    s.record("a", NOW - 20, True, 1.0)  # last bucket
    bars = s.buckets("a", NOW, DAY, 24)
    assert len(bars) == 24
    assert bars[0] == 1.0
    assert bars[-1] == 0.5
    assert bars[10] is None


def test_prune_and_window_bounds() -> None:
    s = Store(":memory:")
    s.record("a", NOW - 9 * DAY, True, 1.0)
    s.record("a", NOW - 1, True, 1.0)
    assert s.prune(NOW, 8) == 1
    assert s.uptime_pct("a", NOW, 7 * DAY) == 100.0


def test_persists_on_disk(tmp_path) -> None:
    path = tmp_path / "sub" / "db.sqlite"
    s = Store(path)
    s.record("a", NOW, True, 1.0)
    s.close()
    assert Store(path).latest("a") is not None
