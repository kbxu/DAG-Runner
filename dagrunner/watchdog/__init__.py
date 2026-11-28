"""Process watchdog for the DAG Runner master service and satellite agent.

The package supervises one child process (``run_server.py`` or
``run_satellite.py``) and restarts it when it dies or stops responding.

Run it with::

    python -m dagrunner.watchdog --role master
    python -m dagrunner.watchdog --role satellite

Exports are resolved lazily so submodule imports do not pay for the whole
supervisor stack.
"""
from __future__ import annotations

import importlib
from typing import Any

# name -> submodule that defines it
_EXPORTS = {
    "HealthTracker": "health",
    "judge_master_payload": "health",
    "probe_master": "health",
    "probe_satellite": "health",
    "LaunchError": "launch",
    "launch_child": "launch",
    "normalize_host": "launch",
    "port_free": "launch",
    "process_alive": "launch",
    "terminate_tree": "launch",
    "verify_process": "launch",
    "wait_for_exit": "launch",
    "INTENT_RESTART": "state",
    "INTENT_RUN": "state",
    "INTENT_STOP": "state",
    "WatchdogLocked": "state",
    "get_intent": "state",
    "is_active": "state",
    "read_state": "state",
    "request_restart": "state",
    "request_stop": "state",
    "set_intent": "state",
    "update_state": "state",
    "watchdog_lock": "state",
    "Watchdog": "supervisor",
    "WatchdogConfig": "supervisor",
    "load_settings": "supervisor",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is not None:
        module = importlib.import_module(f".{module_name}", __name__)
        return getattr(module, name)
    # Submodule attribute access, e.g. ``from dagrunner.watchdog import state``.
    try:
        return importlib.import_module(f".{name}", __name__)
    except ImportError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
