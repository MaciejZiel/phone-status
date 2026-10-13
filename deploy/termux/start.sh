#!/data/data/com.termux/files/usr/bin/sh
# Starts server-status. Run from Termux:Boot or by hand; configuration comes from
# deploy/termux/env (copy of env.example) or the environment.
set -eu
APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
# PHONE_STATUS_* names are deprecated (still read for one release); the app logs a warning for them.
ENV_FILE="${SERVER_STATUS_ENV_FILE:-${PHONE_STATUS_ENV_FILE:-$APP_DIR/deploy/termux/env}}"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi
export PYTHONPATH="$APP_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
if [ -z "${SERVER_STATUS_DB_PATH:-}" ] && [ -z "${PHONE_STATUS_DB_PATH:-}" ]; then
  export SERVER_STATUS_DB_PATH="$APP_DIR/data/status.sqlite"
fi
if [ -z "${SERVER_STATUS_SERVICES_FILE:-}" ] && [ -z "${PHONE_STATUS_SERVICES_FILE:-}" ]; then
  export SERVER_STATUS_SERVICES_FILE="$APP_DIR/services.toml"
fi
command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock
exec python3 -m server_status
