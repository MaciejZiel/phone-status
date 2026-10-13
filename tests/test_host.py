from __future__ import annotations

from pathlib import Path

import pytest

from phone_status import host
from phone_status.host import (
    Battery,
    BatteryReader,
    HostReaders,
    HostSampler,
    parse_cpu_list,
    parse_meminfo,
    parse_net_dev,
    parse_proc_stat,
    parse_sysfs_battery,
    parse_termux_battery,
    parse_uptime,
    read_cpuidle_times,
    read_uptime,
)

MEMINFO = "MemTotal:        8000000 kB\nMemFree:          100000 kB\nMemAvailable:    3000000 kB\n"
NETDEV = (
    "Inter-|   Receive                            |  Transmit\n"
    " face |bytes packets errs drop fifo frame compressed multicast|bytes packets ...\n"
    "    lo: 5000 10 0 0 0 0 0 0 5000 10 0 0 0 0 0 0\n"
    " wlan0: 1000 10 0 0 0 0 0 0 400 10 0 0 0 0 0 0\n"
    " rmnet0: 200 1 0 0 0 0 0 0 100 1 0 0 0 0 0 0\n"
)


def test_parsers() -> None:
    assert parse_uptime("1234.56 999.0\n") == 1234.56
    assert parse_meminfo(MEMINFO) == (8000000 * 1024, 3000000 * 1024)
    assert parse_meminfo("garbage") is None
    assert parse_proc_stat("cpu  10 0 10 70 10 0 0 0 0 0\ncpu0 1 1 1 1") == (80, 100)
    assert parse_proc_stat("intr 1") is None
    assert parse_net_dev(NETDEV) == (1200, 500)
    assert parse_net_dev("") is None


def test_battery_parsers() -> None:
    b = parse_termux_battery('{"percentage": 81, "status": "CHARGING", "temperature": 29.5}')
    assert b == Battery(81.0, "charging", 29.5)
    assert parse_termux_battery('{"error": "x"}') is None
    assert parse_sysfs_battery({"capacity": "55\n", "status": "Not charging\n", "temp": "312\n"}) == Battery(
        55.0, "not_charging", 31.2
    )
    assert parse_sysfs_battery({"capacity": ""}) is None


class Fake:
    """Controllable fake readers with a manual clock."""

    def __init__(self) -> None:
        self.t = 100.0
        self.cpu: tuple[int, int] = (50, 100)
        self.net: tuple[int, int] | None = (1000, 1000)
        self.battery: Battery | None = Battery(80.0, "charging", 30.0)
        self.memory: tuple[int, int] | None = (1000, 400)

    def readers(self) -> HostReaders:
        return HostReaders(
            uptime=lambda: 3600.0,
            loadavg=lambda: (1.0, 0.5, 0.25),
            cpu_times=lambda: self.cpu,
            memory=lambda: self.memory,
            net_bytes=lambda: self.net,
            storage=lambda _p: (1000, 250, 750),
            battery=lambda: self.battery,
            cpu_count=lambda: 4,
            clock=lambda: self.t,
        )


def test_sampler_first_sample_uses_load_fallback_and_no_network_rate() -> None:
    f = Fake()
    snap = HostSampler(f.readers()).sample()
    assert snap["cpu"] == {"percent": 25.0, "load": [1.0, 0.5, 0.25], "cores": 4}
    assert snap["network"] == {"rx_bps": None, "tx_bps": None}  # readable, rate needs two samples
    assert snap["memory"] == {"total": 1000, "available": 400, "used": 600}
    assert snap["storage"] == {"total": 1000, "used": 250, "free": 750}
    assert snap["battery"] == {
        "percent": 80.0,
        "status": "charging",
        "charging": True,
        "temperature_c": 30.0,
    }
    assert snap["uptime_s"] == 3600.0


def test_sampler_rates_between_samples() -> None:
    f = Fake()
    sampler = HostSampler(f.readers())
    sampler.sample()
    f.t += 10
    f.cpu = (60, 200)  # 10 idle of 100 total -> 90 % busy
    f.net = (6000, 3000)
    snap = sampler.sample()
    assert snap["cpu"]["percent"] == 90.0  # type: ignore[index]
    assert snap["network"] == {"rx_bps": 500.0, "tx_bps": 200.0}
    assert sampler.snapshot() == snap


def test_sampler_degrades_when_everything_unreadable() -> None:
    r = HostReaders(
        uptime=lambda: None,
        loadavg=lambda: None,
        cpu_times=lambda: None,
        memory=lambda: None,
        net_bytes=lambda: None,
        storage=lambda _p: None,
        battery=lambda: None,
        cpu_count=lambda: None,
    )
    snap = HostSampler(r).sample()
    assert snap["cpu"] == {"percent": None, "load": None, "cores": None}
    assert snap["memory"] is None
    assert snap["storage"] is None
    assert snap["battery"] is None
    assert snap["network"] is None
    assert snap["uptime_s"] is None


def test_sampler_ignores_resample_within_one_second() -> None:
    f = Fake()
    sampler = HostSampler(f.readers())
    sampler.sample()
    f.t += 0.01
    f.cpu = (50, 100_000)
    f.net = (999_999, 999_999)
    snap = sampler.sample()
    assert snap["cpu"]["percent"] == 25.0  # type: ignore[index]  # load fallback, no tiny-window delta
    assert snap["network"] == {"rx_bps": None, "tx_bps": None}


def test_sampler_reports_unreadable_network_as_none() -> None:
    f = Fake()
    f.net = None
    sampler = HostSampler(f.readers())
    sampler.sample()
    f.t += 10
    assert sampler.sample()["network"] is None


def test_parse_cpu_list() -> None:
    assert parse_cpu_list("0-3,6\n") == {0, 1, 2, 3, 6}
    assert parse_cpu_list("5") == {5}
    assert parse_cpu_list("") is None
    assert parse_cpu_list("x-y") is None


def _cpu_tree(base: Path, idle: dict[int, list[int]], online: str | None) -> None:
    for cpu, states in idle.items():
        for i, value in enumerate(states):
            state = base / f"cpu{cpu}" / "cpuidle" / f"state{i}"
            state.mkdir(parents=True, exist_ok=True)
            (state / "time").write_text(f"{value}\n")
    (base / "cpufreq").mkdir(exist_ok=True)  # not a CPU directory
    if online is not None:
        (base / "online").write_text(online)


def test_cpuidle_fallback_sums_online_cpus(tmp_path: Path) -> None:
    _cpu_tree(tmp_path, {0: [100, 200], 1: [50, 50], 2: [999, 999]}, online="0-1\n")
    assert read_cpuidle_times(str(tmp_path), clock_ns=lambda: 10_000_000) == (400, 20_000)


def test_cpuidle_fallback_gives_busy_share_through_sampler(tmp_path: Path) -> None:
    _cpu_tree(tmp_path, {0: [0], 1: [0]}, online=None)
    clock = {"ns": 0}
    f = Fake()
    readers = f.readers()
    readers.cpu_times = lambda: read_cpuidle_times(str(tmp_path), clock_ns=lambda: clock["ns"])
    sampler = HostSampler(readers)
    sampler.sample()
    # 10 s pass on 2 CPUs; together they idled 15 of 20 CPU-seconds -> 25 % busy
    _cpu_tree(tmp_path, {0: [10_000_000], 1: [5_000_000]}, online=None)
    clock["ns"] = 10_000_000_000
    f.t += 10
    assert sampler.sample()["cpu"]["percent"] == 25.0  # type: ignore[index]


def test_cpuidle_fallback_none_without_sysfs(tmp_path: Path) -> None:
    assert read_cpuidle_times(str(tmp_path / "missing")) is None


def test_uptime_falls_back_to_boottime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host, "_read", lambda _path: None)
    if not hasattr(host.time, "CLOCK_BOOTTIME"):
        pytest.skip("CLOCK_BOOTTIME not available on this platform")
    uptime = read_uptime()
    assert uptime is not None
    assert uptime > 0


def test_battery_reader_caches_and_backs_off() -> None:
    calls = {"termux": 0, "sysfs": 0}
    clock = {"t": 0.0}

    def termux() -> Battery | None:
        calls["termux"] += 1
        return None  # e.g. Termux:API app missing, subprocess timed out

    def sysfs() -> Battery | None:
        calls["sysfs"] += 1
        return Battery(50.0, "discharging", None)

    reader = BatteryReader(termux, sysfs, min_interval=60, retry_after=600, clock=lambda: clock["t"])
    assert reader() == Battery(50.0, "discharging", None)
    clock["t"] = 30
    assert reader() == Battery(50.0, "discharging", None)  # cached
    assert calls == {"termux": 1, "sysfs": 1}
    clock["t"] = 61
    reader()
    assert calls == {"termux": 1, "sysfs": 2}  # termux backed off, sysfs re-read
    clock["t"] = 601
    reader()
    assert calls == {"termux": 2, "sysfs": 3}


def test_battery_reader_without_any_source_retries_slowly() -> None:
    calls = {"n": 0}
    clock = {"t": 0.0}

    def nothing() -> Battery | None:
        calls["n"] += 1
        return None

    reader = BatteryReader(nothing, nothing, clock=lambda: clock["t"])
    assert reader() is None
    clock["t"] = 300
    assert reader() is None
    assert calls["n"] == 2  # one termux and one sysfs attempt, nothing until the retry window
