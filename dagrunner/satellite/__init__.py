"""Satellite node package: agent, protocol, crypto, master channel and updates.

Run the agent with::

    python -m dagrunner.satellite --config var/satellite.yaml
"""
from __future__ import annotations

from typing import Any


def __getattr__(name: str) -> Any:
    # Lazily expose the CLI so importing lightweight modules such as
    # ``dagrunner.satellite.protocol`` does not pull in Flask/cryptography.
    if name in ("main", "load_config", "build_agent_config"):
        from . import cli

        return getattr(cli, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["main", "load_config", "build_agent_config"]
