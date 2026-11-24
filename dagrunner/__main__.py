"""Thin package entry so ``python -m dagrunner`` keeps working."""
from __future__ import annotations

from .tools.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
