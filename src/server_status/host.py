"""Host metrics: readers (injectable, each degrades to None) and a sampler."""

from __future__ import annotations

import ctypes
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Battery:
    percent: float | None
    status: str  # charging | discharging | full | not_charging | unknown
    temperature_c: float | None


# ---- pure parsers (unit tested without touching the machine) -------------------------------


def parse_uptime(text: str) -> float:
    return float(text.split()[0])


def parse_meminfo(text: str) -> tuple[int, int] | None:
    """Return (total, available) in bytes."""
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            values[key] = int(parts[0]) * 1024
    if "MemTotal" not in values:
        return None
    available = values.get("MemAvailable", values.get("MemFree", 0))
    return values["MemTotal"], available


def parse_proc_stat(text: str) -> tuple[int, int] | None:
    """Return (idle, total) jiffies from the aggregate cpu line."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            nums = [int(x) for x in line.split()[1:]]
            if len(nums) < 4:
                return None
            idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
            return idle, sum(nums[:8])
    return None


def parse_net_dev(text: str) -> tuple[int, int] | None:
    """Return summed (rx_bytes, tx_bytes) of all non-loopback interfaces."""
    rx = tx = 0
    found = False
    for line in text.splitlines():
        name, sep, rest = line.partition(":")
        if not sep or name.strip() == "lo":
            continue
        cols = rest.split()
        if len(cols) >= 9 and cols[0].isdigit():
            rx += int(cols[0])
            tx += int(cols[8])
            found = True
    return (rx, tx) if found else None


def parse_termux_battery(text: str) -> Battery | None:
    data = json.loads(text)
    if not isinstance(data, dict) or "percentage" not in data:
        return None
    status = str(data.get("status", "unknown")).lower().replace(" ", "_")
    temp = data.get("temperature")
    return Battery(
        percent=float(data["percentage"]),
        status=status,
        temperature_c=round(float(temp), 1) if isinstance(temp, int | float) else None,
    )


def parse_sysfs_battery(files: dict[str, str]) -> Battery | None:
    cap = files.get("capacity", "").strip()
    if not cap.isdigit():
        return None
    status = files.get("status", "unknown").strip().lower().replace(" ", "_").replace("-", "_")
    temp = files.get("temp", "").strip()
    try:
        temp_c = round(int(temp) / 10, 1) if temp else None
    except ValueError:
        temp_c = None
    return Battery(float(cap), status or "unknown", temp_c)


# ---- readers (touch the machine; return None when unavailable) ------------------------------


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text()
    except OSError:
        return None


def read_uptime() -> float | None:
    text = _read("/proc/uptime")
    try:
        if text:
            return parse_uptime(text)
    except (ValueError, IndexError):
        pass
    # Android 10+ denies /proc/uptime to apps; CLOCK_BOOTTIME is the same clock and always allowed.
    boottime = getattr(time, "CLOCK_BOOTTIME", None)
    if boottime is None:
        return None
    try:
        return round(time.clock_gettime(boottime), 2)
    except OSError:
        return None


def _libc_getloadavg() -> tuple[float, float, float] | None:
    """Call libc getloadavg(3) directly (Python builds without os.getloadavg, e.g. Termux)."""
    libc = ctypes.CDLL(None)
    fn = getattr(libc, "getloadavg", None)
    if fn is None:
        return None
    buf = (ctypes.c_double * 3)()
    if fn(buf, 3) != 3:
        return None
    return buf[0], buf[1], buf[2]


def read_loadavg() -> tuple[float, float, float] | None:
    """Load average via os.getloadavg, else libc getloadavg through ctypes, else None."""
    getter = getattr(os, "getloadavg", None)
    if getter is not None:
        try:
            return getter()
        except OSError:
            return None
    try:
        return _libc_getloadavg()
    except (OSError, AttributeError, TypeError, ValueError):
        return None


def parse_cpu_list(text: str) -> set[int] | None:
    """Parse a kernel CPU list such as '0-3,6' (as in /sys/devices/system/cpu/online)."""
    cpus: set[int] = set()
    try:
        for part in text.strip().split(","):
            if not part:
                continue
            lo, _, hi = part.partition("-")
            cpus.update(range(int(lo), int(hi or lo) + 1))
    except ValueError:
        return None
    return cpus or None


CPU_SYSFS = "/sys/devices/system/cpu"


def read_cpuidle_times(
    base: str = CPU_SYSFS, clock_ns: Callable[[], int] = time.monotonic_ns
) -> tuple[int, int] | None:
    """Return (idle, total) microseconds summed over online CPUs from cpuidle residency counters.

    Fallback for Android, where /proc/stat is not readable by apps but
    /sys/devices/system/cpu/cpu*/cpuidle/state*/time is. "total" is wall time times the
    number of CPUs, so deltas give the same busy share as /proc/stat jiffies.
    """
    root = Path(base)
    online = parse_cpu_list(_read(f"{base}/online") or "")
    idle = 0
    cpus = 0
    for cpu_dir in root.glob("cpu[0-9]*"):
        index = cpu_dir.name[3:]
        if not index.isdigit() or (online is not None and int(index) not in online):
            continue
        files = list(cpu_dir.glob("cpuidle/state[0-9]*/time"))
        if not files:
            continue
        try:
            idle += sum(int(f.read_text()) for f in files)
        except (OSError, ValueError):
            return None
        cpus += 1
    if not cpus:
        return None
    return idle, clock_ns() // 1000 * cpus


def read_cpu_times() -> tuple[int, int] | None:
    text = _read("/proc/stat")
    parsed = parse_proc_stat(text) if text else None
    return parsed if parsed is not None else read_cpuidle_times()


def read_memory() -> tuple[int, int] | None:
    text = _read("/proc/meminfo")
    return parse_meminfo(text) if text else None


def read_net_bytes() -> tuple[int, int] | None:
    text = _read("/proc/net/dev")
    return parse_net_dev(text) if text else None


def read_storage(path: str) -> tuple[int, int, int] | None:
    try:
        usage = shutil.disk_usage(os.path.expanduser(path))
    except OSError:
        return None
    return usage.total, usage.used, usage.free


def read_termux_battery(timeout: float = 8.0) -> Battery | None:
    exe = shutil.which("termux-battery-status")
    if not exe:
        return None
    try:
        out = subprocess.run([exe], capture_output=True, text=True, timeout=timeout, check=True).stdout
        return parse_termux_battery(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def read_sysfs_battery() -> Battery | None:
    for supply in ("battery", "Battery", "bms"):
        base = f"/sys/class/power_supply/{supply}/"
        cap = _read(base + "capacity")
        if cap is not None:
            files = {
                "capacity": cap,
                "status": _read(base + "status") or "",
                "temp": _read(base + "temp") or "",
            }
            return parse_sysfs_battery(files)
    return None


class BatteryReader:
    """Reads the battery at most every `min_interval` seconds.

    `termux-battery-status` talks to the Termux:API app; without the app it blocks until the
    subprocess timeout. Calling it on every host sample would stall the sampler and keep
    waking the app, so results are cached and failures back off for `retry_after` seconds.
    """

    def __init__(
        self,
        termux: Callable[[], Battery | None] = read_termux_battery,
        sysfs: Callable[[], Battery | None] = read_sysfs_battery,
        min_interval: float = 60.0,
        retry_after: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._termux = termux
        self._sysfs = sysfs
        self._min_interval = min_interval
        self._retry_after = retry_after
        self._clock = clock
        self._cached: Battery | None = None
        self._next_read = 0.0
        self._termux_after = 0.0

    def __call__(self) -> Battery | None:
        now = self._clock()
        if now < self._next_read:
            return self._cached
        battery = None
        if now >= self._termux_after:
            battery = self._termux()
            if battery is None:
                self._termux_after = now + self._retry_after
        if battery is None:
            battery = self._sysfs()
        self._cached = battery
        self._next_read = now + (self._min_interval if battery else self._retry_after)
        return battery


@dataclass
class HostReaders:
    uptime: Callable[[], float | None] = read_uptime
    loadavg: Callable[[], tuple[float, float, float] | None] = read_loadavg
    cpu_times: Callable[[], tuple[int, int] | None] = read_cpu_times
    memory: Callable[[], tuple[int, int] | None] = read_memory
    net_bytes: Callable[[], tuple[int, int] | None] = read_net_bytes
    storage: Callable[[str], tuple[int, int, int] | None] = read_storage
    battery: Callable[[], Battery | None] = field(default_factory=BatteryReader)
    cpu_count: Callable[[], int | None] = os.cpu_count
    clock: Callable[[], float] = time.monotonic


def _guard[T](name: str, fn: Callable[..., T | None], *args: object) -> T | None:
    """Run one reader; any failure is logged and turns into None so it only blanks its own field."""
    try:
        return fn(*args)
    except Exception as exc:
        log.warning("host reader %s failed: %s: %s", name, type(exc).__name__, exc)
        return None


class HostSampler:
    """Samples the host periodically; `snapshot()` returns the last JSON-ready result."""

    def __init__(self, readers: HostReaders | None = None, storage_path: str = "~") -> None:
        self._r = readers or HostReaders()
        self._storage_path = storage_path
        self._lock = threading.Lock()
        self._prev_cpu: tuple[int, int] | None = None
        self._prev_cpu_at = 0.0
        self._prev_net: tuple[float, int, int] | None = None
        self._snapshot: dict[str, object] = {}

    def sample(self) -> dict[str, object]:
        r = self._r
        now = r.clock()

        cpu_percent: float | None = None
        cpu = _guard("cpu_times", r.cpu_times)
        if cpu and self._prev_cpu and now - self._prev_cpu_at >= 1 and cpu[1] > self._prev_cpu[1]:
            d_total = cpu[1] - self._prev_cpu[1]
            d_idle = cpu[0] - self._prev_cpu[0]
            cpu_percent = round(max(0.0, min(100.0, 100 * (1 - d_idle / d_total))), 1)
        if cpu and (self._prev_cpu is None or now - self._prev_cpu_at >= 1):
            self._prev_cpu, self._prev_cpu_at = cpu, now

        load = _guard("loadavg", r.loadavg)
        cores = _guard("cpu_count", r.cpu_count)
        if cpu_percent is None and load and cores:
            cpu_percent = round(min(100.0, 100 * load[0] / cores), 1)

        # None = counters unreadable (e.g. Android denies /proc/net/dev); None rates = first sample.
        net: dict[str, float | None] | None = None
        counters = _guard("net_bytes", r.net_bytes)
        if counters:
            net = {"rx_bps": None, "tx_bps": None}
            if self._prev_net and now - self._prev_net[0] >= 1:
                dt = now - self._prev_net[0]
                net = {
                    "rx_bps": max(0.0, (counters[0] - self._prev_net[1]) / dt),
                    "tx_bps": max(0.0, (counters[1] - self._prev_net[2]) / dt),
                }
            if self._prev_net is None or now - self._prev_net[0] >= 1:
                self._prev_net = (now, counters[0], counters[1])

        mem = _guard("memory", r.memory)
        disk = _guard("storage", r.storage, self._storage_path)
        battery = _guard("battery", r.battery)
        uptime = _guard("uptime", r.uptime)
        snap: dict[str, object] = {
            "uptime_s": uptime,
            "cpu": {
                "percent": cpu_percent,
                "load": list(load) if load else None,
                "cores": cores,
            },
            "memory": {"total": mem[0], "available": mem[1], "used": mem[0] - mem[1]} if mem else None,
            "storage": {"total": disk[0], "used": disk[1], "free": disk[2]} if disk else None,
            "battery": (
                {
                    "percent": battery.percent,
                    "status": battery.status,
                    "charging": battery.status == "charging",
                    "temperature_c": battery.temperature_c,
                }
                if battery
                else None
            ),
            "network": net,
            "sampled_at": time.time(),
        }
        with self._lock:
            self._snapshot = snap
        return snap

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return dict(self._snapshot)
