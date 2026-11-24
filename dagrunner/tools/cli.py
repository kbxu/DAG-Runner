"""Run-history viewer CLI (started as ``python -m dagrunner``)."""
from __future__ import annotations

from ..server.runner import main


if __name__ == "__main__":
    raise SystemExit(main())
