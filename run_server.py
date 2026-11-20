#!/usr/bin/env python3
"""Run the dagrunner master server from a versioned install directory.

The launcher executes this file with the repository root as the working
directory, so relative paths in ``var/config.yaml`` keep working while the
``dagrunner`` package is imported from the directory that contains this script.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dagrunner.server import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
