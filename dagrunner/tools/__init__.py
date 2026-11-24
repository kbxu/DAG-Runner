"""Command line tools that are not part of the running service.

* :mod:`dagrunner.tools.cli` — run-history viewer (``python -m dagrunner``)
* :mod:`dagrunner.tools.migrate_workflows` — external scheduler import
* :mod:`dagrunner.tools.workflow_editor` — unsaved-workflow editing helpers
"""
from __future__ import annotations

from typing import Any


def __getattr__(name: str) -> Any:
    if name in ("main",):
        from . import cli

        return getattr(cli, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["main"]
