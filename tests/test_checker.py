from __future__ import annotations

from server_status.checker import (
    Checker,
    check_health,
    describe_failure,
    extract,
    fetch_metrics,
    pick_metrics,
)
from server_status.config import MetricField, ServiceConfig
from server_status.store import Store


def test_check_health_ok_http_error_and_refused(fake_upstream: str) -> None:
    ok = check_health(f"{fake_upstream}/ok", 3)
    assert ok.ok
    assert ok.latency_ms is not None
    bad = check_health(f"{fake_upstream}/boom", 3)
    assert (bad.ok, bad.error) == (False, "HTTP 500")
    down = check_health("http://127.0.0.1:1/x", 1)
    assert not down.ok
    assert down.latency_ms is None


def test_extract_paths() -> None:
    doc = {"a": {"b": 7}, "items": [{"n": 3}, {"n": 4}]}
    assert extract(doc, "a.b") == 7
    assert extract(doc, "items.1.n") == 4
    assert extract(doc, "items.-1.n") == 4
    assert extract(doc, "items.5.n") is None
    assert extract(doc, "a.zzz") is None


def test_pick_metrics_filters_types() -> None:
    doc = {"n": 1, "s": "x" * 200, "o": {}, "t": True}
    fields = (MetricField("n", "N"), MetricField("s", "S"), MetricField("o", "O"), MetricField("t", "T"))
    got = pick_metrics(doc, fields)
    assert [g["label"] for g in got] == ["N", "S", "T"]
    assert len(str(got[1]["value"])) == 80


def test_fetch_metrics(fake_upstream: str) -> None:
    svc = ServiceConfig(
        "a",
        "A",
        f"{fake_upstream}/ok",
        metrics_url=f"{fake_upstream}/metrics",
        metrics=(MetricField("a.b", "B", "u"),),
    )
    assert fetch_metrics(svc) == [{"label": "B", "value": 7, "unit": "u"}]
    broken = ServiceConfig("a", "A", "http://x", metrics_url=f"{fake_upstream}/badjson", metrics=svc.metrics)
    assert fetch_metrics(broken) is None


def test_checker_run_once_records_and_keeps_last_metrics(fake_upstream: str) -> None:
    store = Store(":memory:")
    svc = ServiceConfig(
        "a",
        "A",
        f"{fake_upstream}/ok",
        metrics_url=f"{fake_upstream}/metrics",
        metrics=(MetricField("a.b", "B"),),
    )
    down = ServiceConfig("b", "B", f"{fake_upstream}/boom")
    checker = Checker((svc, down), store, 8)
    checker.run_once()
    latest_a, latest_b = store.latest("a"), store.latest("b")
    assert latest_a is not None
    assert latest_b is not None
    assert latest_a.ok
    assert not latest_b.ok
    assert checker.metric_values("a")[0]["value"] == 7
    assert checker.metric_values("b") == []


def test_prune_runs_at_most_hourly() -> None:
    calls: list[float] = []

    class CountingStore(Store):
        def prune(self, now: float, retention_days: int) -> int:
            calls.append(now)
            return 0

    checker = Checker((), CountingStore(":memory:"), 8)
    checker.run_once()
    checker.run_once()
    assert len(calls) == 1


def test_failed_checks_report_generic_errors_only(fake_upstream: str) -> None:
    port = fake_upstream.rsplit(":", 1)[1]
    secret_path = "/internal/secret-path"
    # a server that answers 404, a port nobody listens on, a name that does not resolve, a bad URL
    cases = {
        f"{fake_upstream}{secret_path}": "HTTP 404",
        f"http://127.0.0.1:1{secret_path}": "connection refused",
        f"http://no-such-host.invalid:8123{secret_path}": "dns error",
        "not-a-url": "unreachable",
    }
    for url, expected in cases.items():
        result = check_health(url, 2)
        assert not result.ok
        assert result.error == expected, url
        for leak in ("127.0.0.1", "invalid", port, "secret", "/", ":"):
            assert leak not in result.error.replace("HTTP ", ""), (url, leak)


def test_describe_failure_classifies_without_leaking() -> None:
    import socket
    import ssl
    import urllib.error

    assert describe_failure(TimeoutError("timed out")) == "timeout"
    assert describe_failure(urllib.error.URLError("timed out")) == "timeout"
    assert (
        describe_failure(urllib.error.URLError(ConnectionRefusedError(111, "refused")))
        == "connection refused"
    )
    assert describe_failure(ConnectionResetError("reset by 10.0.0.5:8080")) == "connection reset"
    assert (
        describe_failure(urllib.error.URLError(socket.gaierror(-2, "Name or service not known")))
        == "dns error"
    )
    assert (
        describe_failure(urllib.error.URLError(ssl.SSLError("bad cert for http://10.0.0.5/x"))) == "tls error"
    )
    assert describe_failure(OSError("cannot open /var/run/x.sock")) == "unreachable"
