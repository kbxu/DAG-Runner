"""SQLite state database (the default backend)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .database_base import SCHEMA, BaseStateDatabase


class StateDatabase(BaseStateDatabase):
    dialect = "sqlite"
    supports_on_conflict = True
    integrity_errors = (sqlite3.IntegrityError,)

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        super().__init__()

    @property
    def state_dir(self) -> Path:
        return self.path.parent

    def _create_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript("PRAGMA journal_mode = WAL;")
        for table in SCHEMA:
            connection.execute(self._render_create_table(table))
        self._create_indexes(connection)

    def _table_columns(self, connection: sqlite3.Connection, table: str) -> set[str]:
        return {
            row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
        }

    def _after_init(self, connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA optimize")


def reset_database(path: str | Path) -> None:
    """Delete the SQLite database file and its WAL side files."""
    database_path = Path(path).resolve()
    for suffix in ("", "-wal", "-shm"):
        Path(str(database_path) + suffix).unlink(missing_ok=True)
