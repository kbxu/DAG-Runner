#!/usr/bin/env bash
# Copy this file to var/start-satellite.sh and adjust DAGR_PYTHON.
# The watchdog supervises the satellite agent. When watchdog.enabled=false it
# exits with code 5 and this script falls back to running the agent directly.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${DAGR_PYTHON:-python3}"
mkdir -p "$ROOT/var/logs"
WD_OUT="$ROOT/var/logs/watchdog-satellite.out.log"
WD_ERR="$ROOT/var/logs/watchdog-satellite.err.log"
echo "[dagrunner] root:     $ROOT"
echo "[dagrunner] watchdog: $PY -m dagrunner.watchdog --role satellite"
set +e
"$PY" "$ROOT/run_tee.py" "$WD_OUT" "$WD_ERR" \
    "$PY" -m dagrunner.watchdog --role satellite --root "$ROOT"
code=$?
set -e
if [ "$code" -eq 0 ]; then
    exit 0
fi
if [ "$code" -ne 5 ]; then
    echo "[dagrunner] watchdog exited with code $code; see $WD_ERR"
    exit "$code"
fi
echo "[dagrunner] watchdog disabled; running the satellite directly"

LOG_OUT="$ROOT/var/logs/satellite.out.log"
LOG_ERR="$ROOT/var/logs/satellite.err.log"
echo "[dagrunner] root:    $ROOT"
echo "[dagrunner] log out: $LOG_OUT"
echo "[dagrunner] log err: $LOG_ERR"
echo "[dagrunner] starting, this window stays open while the satellite runs ..."
exec "$PY" "$ROOT/run_tee.py" "$LOG_OUT" "$LOG_ERR" \
    "$PY" -m dagrunner.satellite --config "$ROOT/var/satellite.yaml"
