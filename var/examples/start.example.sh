#!/usr/bin/env bash
# Copy this file to var/start.sh and adjust DAGR_PYTHON to the target interpreter.
# The watchdog supervises the service. When watchdog.enabled=false it exits with
# code 5 and this script falls back to running the service directly.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${DAGR_PYTHON:-python3}"
mkdir -p "$ROOT/var/logs"
WD_OUT="$ROOT/var/logs/watchdog.out.log"
WD_ERR="$ROOT/var/logs/watchdog.err.log"
echo "[dagrunner] root:     $ROOT"
echo "[dagrunner] watchdog: $PY -m dagrunner.watchdog --role master"
set +e
"$PY" "$ROOT/run_tee.py" "$WD_OUT" "$WD_ERR" \
    "$PY" -m dagrunner.watchdog --role master --root "$ROOT"
code=$?
set -e
if [ "$code" -eq 0 ]; then
    exit 0
fi
if [ "$code" -ne 5 ]; then
    echo "[dagrunner] watchdog exited with code $code; see $WD_ERR"
    exit "$code"
fi
echo "[dagrunner] watchdog disabled; running the service directly"

LOG_OUT="$ROOT/var/logs/server.out.log"
LOG_ERR="$ROOT/var/logs/server.err.log"
echo "[dagrunner] root:    $ROOT"
echo "[dagrunner] log out: $LOG_OUT"
echo "[dagrunner] log err: $LOG_ERR"
echo "[dagrunner] starting, this window stays open while the server runs ..."
exec "$PY" "$ROOT/run_tee.py" "$LOG_OUT" "$LOG_ERR" \
    "$PY" -m dagrunner.server --config "$ROOT/var/config.yaml"
