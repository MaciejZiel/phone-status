"""Backfill a demo SQLite database with 7 days of plausible check history.

usage: python tools/seed_demo.py data/demo.sqlite
"""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from phone_status.store import Store

SERVICES = {"reroute": 0.999, "live-flights": 0.995, "plan-bot": 0.985, "pgqueue": 0.93}


def main(path: str) -> None:
    rng = random.Random(7)
    store = Store(path)
    now = time.time()
    t = now - 7 * 86400
    while t < now - 60:
        for sid, reliability in SERVICES.items():
            ok = rng.random() < reliability
            store.record(sid, t, ok, rng.uniform(18, 60) if ok else None, "" if ok else "timeout")
        t += 300  # one check every 5 minutes keeps the demo database small
    store.close()


if __name__ == "__main__":
    main(sys.argv[1])
