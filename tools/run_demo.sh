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
PYTHONPATH=src PHONE_STATUS_PORT=8190 PHONE_STATUS_DB_PATH=data/demo.sqlite \
  PHONE_STATUS_SERVICES_FILE=tools/demo-services.toml PHONE_STATUS_TITLE="Home server status" \
  "$PY" -m phone_status
