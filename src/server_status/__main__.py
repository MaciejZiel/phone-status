"""Entry point: `python -m server_status`."""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from collections.abc import Callable

from .checker import Checker
from .config import ConfigError, env_setting, load_settings
from .host import HostSampler
from .server import make_server
from .status import StatusBuilder
from .store import Store

log = logging.getLogger("server_status")


def periodic(stop: threading.Event, interval: float, fn: Callable[[], object], name: str) -> threading.Thread:
    def loop() -> None:
        while not stop.is_set():
            try:
                fn()
            except Exception:
                log.exception("%s failed", name)
            stop.wait(interval)

    thread = threading.Thread(target=loop, name=name, daemon=True)
    thread.start()
    return thread


def main() -> int:
    logging.basicConfig(
        level=env_setting(os.environ, "LOG", "INFO"), format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        settings = load_settings(os.environ)
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return 2
    store = Store(settings.db_path)
    sampler = HostSampler(storage_path=settings.storage_path)
    checker = Checker(settings.services, store, settings.retention_days)
    builder = StatusBuilder(settings.title, sampler, checker, store, settings.check_interval)
    server = make_server(settings.host, settings.port, settings.base_path, builder.build, settings.title)

    stop = threading.Event()
    periodic(stop, settings.host_interval, sampler.sample, "host-sampler")
    periodic(stop, settings.check_interval, checker.run_once, "checker")

    def shutdown(*_: object) -> None:
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    log.info(
        "serving on http://%s:%s%s with %d service(s)",
        settings.host,
        server.server_address[1],
        settings.base_path or "/",
        len(settings.services),
    )
    server.serve_forever()
    server.server_close()
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
