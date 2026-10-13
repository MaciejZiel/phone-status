"""Host metrics: readers (injectable, each degrades to None) and a sampler."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


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
        return parse_uptime(text) if text else None
    except (ValueError, IndexError):
        return None


def read_loadavg() -> tuple[float, float, float] | None:
    try:
        return os.getloadavg()
    except OSError:
        return None


def read_cpu_times() -> tuple[int, int] | None:
    text = _read("/proc/stat")
    return parse_proc_stat(text) if text else None


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


def read_battery() -> Battery | None:
    exe = shutil.which("termux-battery-status")
    if exe:
        try:
            out = subprocess.run([exe], capture_output=True, text=True, timeout=8, check=True).stdout
            battery = parse_termux_battery(out)
            if battery:
                return battery
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
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


@dataclass
class HostReaders:
    uptime: Callable[[], float | None] = read_uptime
    loadavg: Callable[[], tuple[float, float, float] | None] = read_loadavg
    cpu_times: Callable[[], tuple[int, int] | None] = read_cpu_times
    memory: Callable[[], tuple[int, int] | None] = read_memory
    net_bytes: Callable[[], tuple[int, int] | None] = read_net_bytes
    storage: Callable[[str], tuple[int, int, int] | None] = read_storage
    battery: Callable[[], Battery | None] = read_battery
    cpu_count: Callable[[], int | None] = os.cpu_count
    clock: Callable[[], float] = time.monotonic


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
        cpu = r.cpu_times()
        if cpu and self._prev_cpu and now - self._prev_cpu_at >= 1 and cpu[1] > self._prev_cpu[1]:
            d_total = cpu[1] - self._prev_cpu[1]
            d_idle = cpu[0] - self._prev_cpu[0]
            cpu_percent = round(max(0.0, min(100.0, 100 * (1 - d_idle / d_total))), 1)
        if cpu and (self._prev_cpu is None or now - self._prev_cpu_at >= 1):
            self._prev_cpu, self._prev_cpu_at = cpu, now

        load = r.loadavg()
        cores = r.cpu_count()
        if cpu_percent is None and load and cores:
            cpu_percent = round(min(100.0, 100 * load[0] / cores), 1)

        net: dict[str, float] | None = None
        counters = r.net_bytes()
        if counters:
            if self._prev_net and now - self._prev_net[0] >= 1:
                dt = now - self._prev_net[0]
                net = {
                    "rx_bps": max(0.0, (counters[0] - self._prev_net[1]) / dt),
                    "tx_bps": max(0.0, (counters[1] - self._prev_net[2]) / dt),
                }
            if self._prev_net is None or now - self._prev_net[0] >= 1:
                self._prev_net = (now, counters[0], counters[1])

        mem = r.memory()
        disk = r.storage(self._storage_path)
        battery = r.battery()
        snap: dict[str, object] = {
            "uptime_s": r.uptime(),
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
