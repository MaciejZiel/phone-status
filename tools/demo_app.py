"""The status page with a made-up host, for demos and screenshots (not used in production).

Every host figure below is invented, so screenshots never show the machine they were taken on.
The administrator token is the obviously fake DEMO_TOKEN below.
usage: PYTHONPATH=src python tools/demo_app.py <port> <db> <services.toml>
"""

from __future__ import annotations

import sys
from pathlib import Path

from server_status.admin import AdminAuth
from server_status.checker import Checker
from server_status.config import load_services
from server_status.host import Battery, HostReaders, HostSampler
from server_status.server import make_server
from server_status.status import StatusBuilder
from server_status.store import Store

DEMO_TOKEN = "demo-token-for-screenshots-only-0000"


def main(port: int, db: str, services_file: str) -> None:
    ticks = iter(range(10**9))
    readers = HostReaders(
        uptime=lambda: 9 * 86400 + 5 * 3600 + 1234.0,
        loadavg=lambda: (0.62, 0.48, 0.41),
        cpu_times=lambda: (next(ticks) * 7, next(ticks) * 10 + 100),
        memory=lambda: (8_000_000_000, 2_650_000_000),
        net_bytes=lambda: (next(ticks) * 90_000, next(ticks) * 30_000),
        storage=lambda _path: (128_000_000_000, 61_400_000_000, 66_600_000_000),
        battery=lambda: Battery(76.0, "charging", 31.5),
        cpu_count=lambda: 8,
        clock=lambda: next(ticks) * 5.0,
    )
    store = Store(db)
    sampler = HostSampler(readers)
    checker = Checker(load_services(Path(services_file)), store, 8)
    builder = StatusBuilder("Home server status", sampler, checker, store, 45.0)
    for _ in range(3):
        sampler.sample()
    checker.run_once()
    server = make_server(
        "127.0.0.1",
        port,
        "",
        builder.build,
        "Home server status",
        admin=AdminAuth(DEMO_TOKEN),
        build_admin=builder.build_admin,
    )
    server.serve_forever()


if __name__ == "__main__":
    main(int(sys.argv[1]), sys.argv[2], sys.argv[3])
