"""Main service package: web app, scheduling, execution and account APIs.

Run it with::

    python -m dagrunner.server --config var/config.yaml
"""
from __future__ import annotations

from typing import Any


def __getattr__(name: str) -> Any:
    # Lazily expose the entry point so importing lightweight modules such as
    # ``dagrunner.server.service`` does not pull in Flask/APScheduler.
    if name in ("main", "load_config_file", "build_satellite_manager"):
        from . import app

        return getattr(app, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["main", "load_config_file", "build_satellite_manager"]
