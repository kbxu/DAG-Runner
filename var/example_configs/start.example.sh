#!/usr/bin/env bash
# Copy this file to var/start.sh and adjust DAGR_PYTHON to the target interpreter.
# Copy var/example_configs/config.example.yaml to var/config.yaml and edit it.
# Logs are written to var/logs/server.out.log and var/logs/server.err.log.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${DAGR_PYTHON:-python3}"
mkdir -p "$ROOT/var/logs"
CURRENT=""
if [ -f "$ROOT/var/current" ]; then
  CURRENT="$(head -n 1 "$ROOT/var/current" | tr -d '[:space:]')"
fi
cd "$ROOT"
if [ -n "$CURRENT" ] && [ -f "$ROOT/var/versions/$CURRENT/run_server.py" ]; then
  exec "$PY" "$ROOT/var/versions/$CURRENT/run_server.py" --config "$ROOT/var/config.yaml" >> "$ROOT/var/logs/server.out.log" 2>> "$ROOT/var/logs/server.err.log"
fi
exec "$PY" -m dagrunner.server --config "$ROOT/var/config.yaml" >> "$ROOT/var/logs/server.out.log" 2>> "$ROOT/var/logs/server.err.log"
