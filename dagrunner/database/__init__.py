"""State database entry point.

``StateDatabase`` remains the SQLite implementation for direct path-based use
(the CLI and the test-suite). Server startup selects a backend from the ``db``
config entry through :func:`create_state_database`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .base import (
    RUN_STATUSES,
    TASK_STATUSES,
    BaseStateDatabase,
    _RUN_PROGRESS_COLUMNS,
    _as_local_time,
    local_now,
)
from .mysql8 import Mysql8StateDatabase, reset_database as reset_mysql_database
from .sqlite import StateDatabase, reset_database as reset_sqlite_database

__all__ = [
    "BaseStateDatabase",
    "Mysql8StateDatabase",
    "StateDatabase",
    "create_state_database",
    "load_database_config",
    "local_now",
    "reset_state_database",
]


def create_state_database(config: Any) -> BaseStateDatabase:
    """Build a state database from a ``db`` config entry.

    ``config`` may be a path (SQLite, default) or a mapping::

        {"type": "mysql8", "host": "127.0.0.1", "port": 3306, ...}

    A mapping without ``type`` defaults to SQLite and uses ``path``.
    """
    if isinstance(config, dict):
        db_type = str(config.get("type", "sqlite")).strip().lower()
        if db_type in ("sqlite", "sqlite3"):
            return StateDatabase(config.get("path") or "var/scheduler.db")
        if db_type in ("mysql", "mysql8", "mysql8.0"):
            return Mysql8StateDatabase(config)
        raise ValueError(f"unsupported database type: {db_type!r}")
    return StateDatabase(config)


def load_database_config(path: str | Path) -> Any:
    """Read the ``db`` entry from a YAML config file.

    Keeps the same file format as the server, so a CLI command can point at the
    existing ``var/config.yaml``. A top-level ``state_dir`` is forwarded into a
    database mapping when present.
    """
    config_path = Path(path)
    if not config_path.is_file():
        raise ValueError(f"配置文件不存在: {config_path}")
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"无法解析配置文件 {config_path}: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"配置文件 {config_path} 必须是 YAML 对象")
    db = data.get("db", "var/scheduler.db")
    state_dir = data.get("state_dir")
    if isinstance(db, dict) and state_dir:
        db = {**db, "state_dir": state_dir}
    return db


def reset_state_database(config: Any) -> None:
    """Delete the SQLite file(s) or drop/recreate the MySQL database."""
    if isinstance(config, dict):
        db_type = str(config.get("type", "sqlite")).strip().lower()
        if db_type in ("mysql", "mysql8", "mysql8.0"):
            reset_mysql_database(config)
            return
        reset_sqlite_database(config.get("path") or "var/scheduler.db")
        return
    reset_sqlite_database(config)
