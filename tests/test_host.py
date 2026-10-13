from __future__ import annotations

from phone_status.host import (
    Battery,
    HostReaders,
    HostSampler,
    parse_meminfo,
    parse_net_dev,
    parse_proc_stat,
    parse_sysfs_battery,
    parse_termux_battery,
    parse_uptime,
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
        self.cpu = (50, 100)
        self.net = (1000, 1000)
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
    assert snap["network"] is None
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
