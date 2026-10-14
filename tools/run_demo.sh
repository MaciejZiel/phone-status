#!/bin/sh
# Starts the fake services and the status page against a seeded demo database.
# usage: tools/run_demo.sh [python]   (stop with Ctrl-C)
set -eu
cd "$(dirname "$0")/.."
PY="${1:-python3}"
rm -f data/demo.sqlite data/demo.sqlite-wal data/demo.sqlite-shm
mkdir -p data
"$PY" tools/seed_demo.py data/demo.sqlite
"$PY" tools/fake_services.py 8191 &
FAKE=$!
trap 'kill $FAKE' EXIT INT TERM
# The host figures are invented (tools/demo_app.py); the administrator token is "demo-token-for-screenshots-only-0000".
PYTHONPATH=src "$PY" tools/demo_app.py 8190 data/demo.sqlite tools/demo-services.toml
