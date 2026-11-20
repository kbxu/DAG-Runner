#!/usr/bin/env bash
# Copy this file to var/start-satellite.sh and adjust DAGR_PYTHON to the target interpreter.
# Copy var/example_configs/satellite.example.yaml to var/satellite.yaml and edit it.
# Logs are written to var/logs/satellite.out.log and var/logs/satellite.err.log.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${DAGR_PYTHON:-python3}"
mkdir -p "$ROOT/var/logs"
CURRENT=""
if [ -f "$ROOT/var/current" ]; then
  CURRENT="$(head -n 1 "$ROOT/var/current" | tr -d '[:space:]')"
fi
cd "$ROOT"
if [ -n "$CURRENT" ] && [ -f "$ROOT/var/versions/$CURRENT/run_satellite.py" ]; then
  exec "$PY" "$ROOT/var/versions/$CURRENT/run_satellite.py" --config "$ROOT/var/satellite.yaml" >> "$ROOT/var/logs/satellite.out.log" 2>> "$ROOT/var/logs/satellite.err.log"
fi
exec "$PY" -m dagrunner.satellite --config "$ROOT/var/satellite.yaml" >> "$ROOT/var/logs/satellite.out.log" 2>> "$ROOT/var/logs/satellite.err.log"
