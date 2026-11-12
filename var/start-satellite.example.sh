#!/usr/bin/env bash
set -e
DAGR_PYTHON=python
cd "$(dirname "$0")/.."
"${DAGR_PYTHON}" -m dagrunner.satellite --config var/satellite.yaml
