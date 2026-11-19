"""MySQL 8 state database backend.

The business logic lives in ``database_base``; this module only adapts
connection handling, schema DDL and the ``?`` placeholder style to PyMySQL.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pymysql
from pymysql import converters
from pymysql.constants import FIELD_TYPE
from pymysql.cursors import DictCursor

from .database_base import SCHEMA, BaseStateDatabase


def _conversion_table() -> dict:
    """Return PyMySQL conversions with DECIMAL mapped to int.

    Aggregate expressions such as ``SUM`` come back as ``Decimal`` from MySQL,
    which would leak into JSON responses. All values stored here are integers,
    so ``int`` is both simpler and safer.
    """
    conversions_map = converters.conversions.copy()
    conversions_map[FIELD_TYPE.DECIMAL] = int
    conversions_map[FIELD_TYPE.NEWDECIMAL] = int
    return conversions_map


def _format_placeholders(sql: str) -> str:
    """Rewrite ``?`` placeholders as ``%s`` outside of string literals.

    Percent signs inside literals are doubled because PyMySQL formats the
    statement with ``%`` before sending it; ``%%`` is rendered back to ``%``.
    """
    out: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(sql):
        char = sql[index]
        if quote is not None:
            if char == "%":
                out.append("%%")
            else:
                out.append(char)
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    out.append(sql[index + 1])
                    index += 1
                else:
                    quote = None
        elif char in ("'", '"'):
            quote = char
            out.append(char)
        elif char == "?":
            out.append("%s")
        else:
            out.append(char)
        index += 1
    return "".join(out)


class _CursorWrapper:
    """Small adapter so ``connection.execute(...)`` works like sqlite3."""

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    def execute(self, sql: str, params: Any = None) -> "_CursorWrapper":
        self._cursor.execute(sql, params)
        return self

    def executemany(self, sql: str, params: Any = None) -> Any:
        return self._cursor.executemany(sql, params)

    def fetchone(self) -> Any:
        return self._cursor.fetchone()

    def fetchall(self) -> list[Any]:
        return self._cursor.fetchall()

    def fetchmany(self, size: int | None = None) -> list[Any]:
        if size is None:
            return self._cursor.fetchall()
        return self._cursor.fetchmany(size)

    def close(self) -> None:
        self._cursor.close()

    @property
    def lastrowid(self) -> Any:
        return self._cursor.lastrowid

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def __iter__(self) -> Any:
        return iter(self._cursor)

    def __enter__(self) -> "_CursorWrapper":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class _ConnectionWrapper:
    """Wrap a PyMySQL connection behind the sqlite3-like ``execute`` API."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def execute(self, sql: str, params: Any = None) -> _CursorWrapper:
        return _CursorWrapper(self._connection.cursor()).execute(
            _format_placeholders(sql), () if params is None else params
        )

    def executemany(self, sql: str, params: Any = None) -> Any:
        cursor = self._connection.cursor()
        return cursor.executemany(
            _format_placeholders(sql), () if params is None else params
        )

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()

    def cursor(self, *args: Any, **kwargs: Any) -> Any:
        return self._connection.cursor(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


class Mysql8StateDatabase(BaseStateDatabase):
    dialect = "mysql8"
    supports_on_conflict = False
    integrity_errors = (pymysql.err.IntegrityError,)

    def __init__(self, config: dict) -> None:
        self.config = dict(config)
        self.host = str(self.config.get("host", "127.0.0.1"))
        self.port = int(self.config.get("port", 3306))
        self.user = str(self.config.get("user", "root"))
        self.password = str(self.config.get("password", "") or "")
        self.database_name = str(self.config.get("database", "dagrunner"))
        self.charset = str(self.config.get("charset", "utf8mb4"))
        state_dir = self.config.get("state_dir") or "var"
        self._state_dir = Path(state_dir).resolve()
        self._state_dir.mkdir(parents=True, exist_ok=True)
        super().__init__()

    @property
    def state_dir(self) -> Path:
        return self._state_dir

    def _connect_kwargs(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "charset": self.charset,
            "autocommit": False,
            "conv": _conversion_table(),
            "cursorclass": DictCursor,
            "connect_timeout": 10,
        }

    def _ensure_database(self) -> None:
        """Create the target schema if the user has not done so yet."""
        connection = pymysql.connect(
            **{**self._connect_kwargs(), "database": None, "autocommit": True}
        )
        try:
            with connection.cursor() as cursor:
                database = self.database_name.replace("`", "")
                charset = self.charset.replace("`", "")
                cursor.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{database}` "
                    f"CHARACTER SET {charset}"
                )
        finally:
            connection.close()

    def _initialize(self) -> None:
        self._ensure_database()
        super()._initialize()

    def _create_connection(self) -> _ConnectionWrapper:
        return _ConnectionWrapper(
            pymysql.connect(database=self.database_name, **self._connect_kwargs())
        )

    def _create_schema(self, connection: _ConnectionWrapper) -> None:
        for table in SCHEMA:
            connection.execute(self._render_create_table(table))
        self._create_indexes(connection)

    def _table_columns(self, connection: _ConnectionWrapper, table: str) -> set[str]:
        return {
            row["name"]
            for row in connection.execute(
                "SELECT COLUMN_NAME AS name FROM information_schema.columns "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ?",
                (table,),
            )
        }

    def _after_init(self, connection: _ConnectionWrapper) -> None:
        for table in SCHEMA:
            connection.execute(f"ANALYZE TABLE {table.name}")


def reset_database(config: dict) -> None:
    """Drop and recreate the configured MySQL database."""
    settings = dict(config)
    database_name = str(settings.get("database", "dagrunner")).replace("`", "")
    charset = str(settings.get("charset", "utf8mb4")).replace("`", "")
    connection = pymysql.connect(
        host=str(settings.get("host", "127.0.0.1")),
        port=int(settings.get("port", 3306)),
        user=str(settings.get("user", "root")),
        password=str(settings.get("password", "") or ""),
        charset=charset,
        autocommit=True,
        connect_timeout=10,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{database_name}`")
            cursor.execute(
                f"CREATE DATABASE `{database_name}` CHARACTER SET {charset}"
            )
    finally:
        connection.close()
