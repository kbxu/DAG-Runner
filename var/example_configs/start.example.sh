#!/usr/bin/env bash
# Copy this file to var/start.sh and adjust DAGR_PYTHON to the target interpreter.
# Copy var/example_configs/config.example.yaml to var/config.yaml and edit it.
# Program output is mirrored to this console and appended to var/logs/*.log.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${DAGR_PYTHON:-python3}"
mkdir -p "$ROOT/var/logs"
LOG_OUT="$ROOT/var/logs/server.out.log"
LOG_ERR="$ROOT/var/logs/server.err.log"
CURRENT=""
if [ -f "$ROOT/var/current" ]; then
  CURRENT="$(head -n 1 "$ROOT/var/current" | tr -d '[:space:]')"
fi
echo "[dagrunner] root:    $ROOT"
echo "[dagrunner] log out: $LOG_OUT"
echo "[dagrunner] log err: $LOG_ERR"
if [ -n "$CURRENT" ] && [ -f "$ROOT/var/versions/$CURRENT/run_server.py" ]; then
  echo "[dagrunner] version: $CURRENT"
  echo "[dagrunner] starting, this window stays open while the server runs ..."
  exec "$PY" "$ROOT/var/versions/$CURRENT/run_tee.py" "$LOG_OUT" "$LOG_ERR" \
    "$PY" "$ROOT/var/versions/$CURRENT/run_server.py" --config "$ROOT/var/config.yaml"
fi
echo "[dagrunner] starting, this window stays open while the server runs ..."
exec "$PY" "$ROOT/run_tee.py" "$LOG_OUT" "$LOG_ERR" \
  "$PY" -m dagrunner.server --config "$ROOT/var/config.yaml"
