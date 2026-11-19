"""Database-agnostic state storage.

All business logic and SQL live here and are written once. Dialect-specific
behavior (connection setup, schema rendering, placeholder style, upsert
syntax, integrity errors) is provided by the subclasses in
``database_sqlite`` and ``database_mysql8``.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from .satellite_protocol import SatelliteConfig


TASK_STATUSES = {"PENDING", "RUNNING", "SUCCESS", "FAILED", "SKIPPED"}
RUN_STATUSES = {"RUNNING", "SUCCESS", "FAILED"}

# Out-of-scope tasks are stored as SKIPPED/OUTSIDE_SCOPE placeholders so a run
# keeps its full DAG shape, but they never execute. Run progress must therefore
# count only the tasks in the run's scope: a run started from a single task (or a
# subtree) reports that many tasks, not every task in the workflow. Correlated
# scalar subqueries keep this portable: no GROUP BY, so MySQL's
# ONLY_FULL_GROUP_BY mode cannot reject the run columns.
_RUN_PROGRESS_COLUMNS = """(SELECT COUNT(CASE WHEN COALESCE(tr.skip_kind, '') <> 'OUTSIDE_SCOPE' THEN 1 END) FROM task_runs tr WHERE tr.run_id = wr.run_id) AS task_count,
                           (SELECT COUNT(CASE WHEN tr.status = 'SUCCESS' AND COALESCE(tr.skip_kind, '') <> 'OUTSIDE_SCOPE' THEN 1 END) FROM task_runs tr WHERE tr.run_id = wr.run_id) AS success_count,
                           (SELECT COUNT(CASE WHEN tr.status = 'FAILED' AND tr.handled_by IS NULL AND COALESCE(tr.skip_kind, '') <> 'OUTSIDE_SCOPE' THEN 1 END) FROM task_runs tr WHERE tr.run_id = wr.run_id) AS failed_count,
                           (SELECT COUNT(CASE WHEN tr.status = 'FAILED' AND tr.handled_by IS NOT NULL AND COALESCE(tr.skip_kind, '') <> 'OUTSIDE_SCOPE' THEN 1 END) FROM task_runs tr WHERE tr.run_id = wr.run_id) AS handled_count,
                           (SELECT COUNT(CASE WHEN tr.status = 'RUNNING' AND COALESCE(tr.skip_kind, '') <> 'OUTSIDE_SCOPE' THEN 1 END) FROM task_runs tr WHERE tr.run_id = wr.run_id) AS running_count,
                           (SELECT COUNT(CASE WHEN tr.status = 'SKIPPED' AND COALESCE(tr.skip_kind, '') <> 'OUTSIDE_SCOPE' THEN 1 END) FROM task_runs tr WHERE tr.run_id = wr.run_id) AS skipped_count"""


class Row(Protocol):
    """Minimal row interface shared by sqlite3.Row and the MySQL dict cursor."""

    def __getitem__(self, key: str) -> Any: ...

    def keys(self) -> Iterable[str]: ...


def local_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _as_local_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    local = parsed.astimezone()
    if local.microsecond:
        return local.isoformat(timespec="milliseconds")
    return local.isoformat(timespec="seconds")


def _satellite_config(row: Row) -> SatelliteConfig:
    return SatelliteConfig(
        alias=row["alias"],
        name=row["name"],
        mode=row["mode"],
        url=row["url"],
        token=row["token"],
        host_key_policy=row["host_key_policy"],
        cert_fingerprint=row["cert_fingerprint"],
        status=row["status"],
        last_seen=row["last_seen"],
        last_error=row["last_error"],
        platform=row["platform"],
        hostname=row["hostname"],
        agent_version=row["agent_version"],
    )


# ---------------------------------------------------------------------------
# Declarative schema. One definition, rendered per dialect.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    sqlite_type: str
    mysql_type: str
    not_null: bool = False
    default: str | None = None
    unique: bool = False
    check: str | None = None
    auto_increment: bool = False


@dataclass(frozen=True)
class ForeignKeySpec:
    columns: tuple[str, ...]
    ref_table: str
    ref_columns: tuple[str, ...]
    on_delete: str | None = None


@dataclass(frozen=True)
class IndexSpec:
    name: str
    columns: tuple[str, ...]


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: tuple[ColumnSpec, ...]
    primary_key: tuple[str, ...] = ()
    foreign_keys: tuple[ForeignKeySpec, ...] = ()
    indexes: tuple[IndexSpec, ...] = ()


SCHEMA: tuple[TableSpec, ...] = (
    TableSpec(
        name="workflows",
        columns=(
            ColumnSpec("id", "INTEGER", "INT", auto_increment=True),
            ColumnSpec("workflow_key", "TEXT", "VARCHAR(255)", unique=True),
            ColumnSpec("name", "TEXT", "VARCHAR(255)", not_null=True),
            ColumnSpec("definition", "TEXT", "LONGTEXT", not_null=True),
            ColumnSpec("created_time", "TEXT", "VARCHAR(64)", not_null=True),
            ColumnSpec("updated_time", "TEXT", "VARCHAR(64)", not_null=True),
            ColumnSpec(
                "runner", "TEXT", "VARCHAR(64)", not_null=True, default="'local'"
            ),
        ),
        primary_key=("id",),
    ),
    TableSpec(
        name="workflow_runs",
        columns=(
            ColumnSpec("run_id", "TEXT", "VARCHAR(64)", not_null=True),
            ColumnSpec("workflow_name", "TEXT", "VARCHAR(255)", not_null=True),
            ColumnSpec(
                "status",
                "TEXT",
                "VARCHAR(16)",
                not_null=True,
                check="status IN ('RUNNING','SUCCESS','FAILED')",
            ),
            ColumnSpec("start_time", "TEXT", "VARCHAR(64)", not_null=True),
            ColumnSpec("end_time", "TEXT", "VARCHAR(64)"),
            ColumnSpec("resumed_from_run_id", "TEXT", "VARCHAR(64)"),
            ColumnSpec("from_task", "TEXT", "VARCHAR(255)"),
            ColumnSpec("error_message", "TEXT", "TEXT"),
            ColumnSpec(
                "trigger_type",
                "TEXT",
                "VARCHAR(32)",
                not_null=True,
                default="'manual'",
            ),
            ColumnSpec("scope", "TEXT", "TEXT"),
            ColumnSpec(
                "runner", "TEXT", "VARCHAR(64)", not_null=True, default="'local'"
            ),
            ColumnSpec("runner_locations", "TEXT", "TEXT"),
        ),
        primary_key=("run_id",),
        foreign_keys=(
            ForeignKeySpec(("resumed_from_run_id",), "workflow_runs", ("run_id",)),
        ),
        indexes=(
            IndexSpec("idx_workflow_runs_name_start", ("workflow_name", "start_time DESC")),
        ),
    ),
    TableSpec(
        name="task_runs",
        columns=(
            ColumnSpec("run_id", "TEXT", "VARCHAR(64)", not_null=True),
            ColumnSpec("workflow_name", "TEXT", "VARCHAR(255)", not_null=True),
            ColumnSpec("task_name", "TEXT", "VARCHAR(255)", not_null=True),
            ColumnSpec(
                "status",
                "TEXT",
                "VARCHAR(16)",
                not_null=True,
                check="status IN ('PENDING','RUNNING','SUCCESS','FAILED','SKIPPED')",
            ),
            ColumnSpec("start_time", "TEXT", "VARCHAR(64)"),
            ColumnSpec("end_time", "TEXT", "VARCHAR(64)"),
            ColumnSpec("exit_code", "INTEGER", "INT"),
            ColumnSpec("log_file", "TEXT", "VARCHAR(512)"),
            ColumnSpec("error_message", "TEXT", "TEXT"),
            ColumnSpec("reused_from_run_id", "TEXT", "VARCHAR(64)"),
            ColumnSpec(
                "task_description",
                "TEXT",
                "VARCHAR(512)",
                not_null=True,
                default="''",
            ),
            ColumnSpec(
                "depends_json",
                "TEXT",
                "VARCHAR(4000)",
                not_null=True,
                default="'[]'",
            ),
            ColumnSpec(
                "task_type",
                "TEXT",
                "VARCHAR(32)",
                not_null=True,
                default="'command'",
            ),
            ColumnSpec("condition_result", "TEXT", "VARCHAR(64)"),
            ColumnSpec("handled_by", "TEXT", "VARCHAR(64)"),
            ColumnSpec("skip_kind", "TEXT", "VARCHAR(32)"),
            ColumnSpec(
                "snapshot_version", "INTEGER", "INT", not_null=True, default="0"
            ),
        ),
        primary_key=("run_id", "task_name"),
        foreign_keys=(
            ForeignKeySpec(
                ("run_id",), "workflow_runs", ("run_id",), on_delete="CASCADE"
            ),
            ForeignKeySpec(("reused_from_run_id",), "workflow_runs", ("run_id",)),
        ),
        indexes=(
            IndexSpec("idx_task_runs_lookup", ("workflow_name", "task_name", "run_id")),
        ),
    ),
    TableSpec(
        name="users",
        columns=(
            ColumnSpec("username", "TEXT", "VARCHAR(255)", not_null=True),
            ColumnSpec("password_hash", "TEXT", "VARCHAR(255)", not_null=True),
            ColumnSpec("created_time", "TEXT", "VARCHAR(64)", not_null=True),
            ColumnSpec("updated_time", "TEXT", "VARCHAR(64)", not_null=True),
        ),
        primary_key=("username",),
    ),
    TableSpec(
        name="auth_sessions",
        columns=(
            ColumnSpec("token_hash", "TEXT", "VARCHAR(255)", not_null=True),
            ColumnSpec("username", "TEXT", "VARCHAR(255)", not_null=True),
            ColumnSpec("created_time", "TEXT", "VARCHAR(64)", not_null=True),
            ColumnSpec("expires_at", "TEXT", "VARCHAR(64)", not_null=True),
        ),
        primary_key=("token_hash",),
        foreign_keys=(
            ForeignKeySpec(
                ("username",), "users", ("username",), on_delete="CASCADE"
            ),
        ),
        indexes=(IndexSpec("idx_auth_sessions_expires_at", ("expires_at",)),),
    ),
    TableSpec(
        name="login_attempts",
        columns=(
            ColumnSpec("ip_address", "TEXT", "VARCHAR(64)", not_null=True),
            ColumnSpec(
                "failed_attempts", "INTEGER", "INT", not_null=True, default="0"
            ),
            ColumnSpec("blocked_until", "TEXT", "VARCHAR(64)"),
            ColumnSpec("updated_time", "TEXT", "VARCHAR(64)", not_null=True),
        ),
        primary_key=("ip_address",),
    ),
    TableSpec(
        name="schedules",
        columns=(
            ColumnSpec("workflow_name", "TEXT", "VARCHAR(255)", not_null=True),
            ColumnSpec(
                "description",
                "TEXT",
                "VARCHAR(512)",
                not_null=True,
                default="''",
            ),
            ColumnSpec("cron_expression", "TEXT", "VARCHAR(255)", not_null=True),
            ColumnSpec("timezone", "TEXT", "VARCHAR(64)", not_null=True),
            ColumnSpec(
                "enabled",
                "INTEGER",
                "INT",
                not_null=True,
                default="0",
                check="enabled IN (0, 1)",
            ),
            ColumnSpec("created_time", "TEXT", "VARCHAR(64)", not_null=True),
            ColumnSpec("updated_time", "TEXT", "VARCHAR(64)", not_null=True),
        ),
        primary_key=("workflow_name",),
    ),
    TableSpec(
        name="satellites",
        columns=(
            ColumnSpec("alias", "TEXT", "VARCHAR(255)", not_null=True),
            ColumnSpec("name", "TEXT", "VARCHAR(255)", not_null=True, default="''"),
            ColumnSpec(
                "mode",
                "TEXT",
                "VARCHAR(16)",
                not_null=True,
                default="'auto'",
                check="mode IN ('auto','forward','reverse')",
            ),
            ColumnSpec(
                "url", "TEXT", "VARCHAR(512)", not_null=True, default="''"
            ),
            ColumnSpec("token", "TEXT", "VARCHAR(255)", not_null=True),
            ColumnSpec(
                "host_key_policy",
                "TEXT",
                "VARCHAR(32)",
                not_null=True,
                default="'accept-new'",
            ),
            ColumnSpec(
                "cert_fingerprint",
                "TEXT",
                "VARCHAR(255)",
                not_null=True,
                default="''",
            ),
            ColumnSpec(
                "status",
                "TEXT",
                "VARCHAR(16)",
                not_null=True,
                default="'offline'",
                check="status IN ('offline','online','error')",
            ),
            ColumnSpec("last_seen", "TEXT", "VARCHAR(64)"),
            ColumnSpec("last_error", "TEXT", "TEXT"),
            ColumnSpec(
                "platform", "TEXT", "VARCHAR(64)", not_null=True, default="''"
            ),
            ColumnSpec(
                "hostname", "TEXT", "VARCHAR(255)", not_null=True, default="''"
            ),
            ColumnSpec(
                "agent_version", "TEXT", "VARCHAR(64)", not_null=True, default="''"
            ),
            ColumnSpec("created_time", "TEXT", "VARCHAR(64)", not_null=True),
            ColumnSpec("updated_time", "TEXT", "VARCHAR(64)", not_null=True),
        ),
        primary_key=("alias",),
    ),
    TableSpec(
        name="satellite_jobs",
        columns=(
            ColumnSpec("job_id", "TEXT", "VARCHAR(255)", not_null=True),
            ColumnSpec(
                "satellite_alias", "TEXT", "VARCHAR(255)", not_null=True
            ),
            ColumnSpec(
                "workflow_name",
                "TEXT",
                "VARCHAR(255)",
                not_null=True,
                default="''",
            ),
            ColumnSpec("run_id", "TEXT", "VARCHAR(64)", not_null=True, default="''"),
            ColumnSpec(
                "task_name", "TEXT", "VARCHAR(255)", not_null=True, default="''"
            ),
            ColumnSpec(
                "status",
                "TEXT",
                "VARCHAR(16)",
                not_null=True,
                default="'pending'",
                check="status IN ('pending','running','success','failed','cancelled','error')",
            ),
            ColumnSpec("exit_code", "INTEGER", "INT"),
            ColumnSpec("error_message", "TEXT", "TEXT"),
            ColumnSpec(
                "log_file", "TEXT", "VARCHAR(512)", not_null=True, default="''"
            ),
            ColumnSpec("created_time", "TEXT", "VARCHAR(64)", not_null=True),
            ColumnSpec("started_time", "TEXT", "VARCHAR(64)"),
            ColumnSpec("finished_time", "TEXT", "VARCHAR(64)"),
        ),
        primary_key=("job_id",),
        indexes=(
            IndexSpec(
                "idx_satellite_jobs_alias_time", ("satellite_alias", "created_time")
            ),
        ),
    ),
)

# Columns that older databases may be missing. The fresh schema above already
# contains them; this list only matters when upgrading an existing database.
_ENSURE_COLUMNS: tuple[tuple[str, str, str, str], ...] = (
    ("workflow_runs", "trigger_type", "TEXT NOT NULL DEFAULT 'manual'", "VARCHAR(32) NOT NULL DEFAULT 'manual'"),
    ("schedules", "description", "TEXT NOT NULL DEFAULT ''", "VARCHAR(512) NOT NULL DEFAULT ''"),
    ("task_runs", "task_description", "TEXT NOT NULL DEFAULT ''", "VARCHAR(512) NOT NULL DEFAULT ''"),
    ("task_runs", "depends_json", "TEXT NOT NULL DEFAULT '[]'", "VARCHAR(4000) NOT NULL DEFAULT '[]'"),
    ("task_runs", "task_type", "TEXT NOT NULL DEFAULT 'command'", "VARCHAR(32) NOT NULL DEFAULT 'command'"),
    ("task_runs", "condition_result", "TEXT", "VARCHAR(64)"),
    ("task_runs", "handled_by", "TEXT", "VARCHAR(64)"),
    ("task_runs", "skip_kind", "TEXT", "VARCHAR(32)"),
    ("workflow_runs", "scope", "TEXT", "TEXT"),
    ("workflow_runs", "runner", "TEXT NOT NULL DEFAULT 'local'", "VARCHAR(64) NOT NULL DEFAULT 'local'"),
    ("workflow_runs", "runner_locations", "TEXT", "TEXT"),
    ("workflows", "runner", "TEXT NOT NULL DEFAULT 'local'", "VARCHAR(64) NOT NULL DEFAULT 'local'"),
    ("task_runs", "snapshot_version", "INTEGER NOT NULL DEFAULT 0", "INT NOT NULL DEFAULT 0"),
)

# (table, timestamp columns, primary-key columns) for timestamp normalization.
_TIMESTAMP_TABLES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("workflows", ("created_time", "updated_time"), ("id",)),
    ("workflow_runs", ("start_time", "end_time"), ("run_id",)),
    ("task_runs", ("start_time", "end_time"), ("run_id", "task_name")),
    ("schedules", ("created_time", "updated_time"), ("workflow_name",)),
)


class BaseStateDatabase:
    """Shared state-database implementation.

    Subclasses provide the dialect-specific pieces: connection creation,
    schema DDL, column introspection and post-initialization hints.
    """

    dialect = ""
    supports_on_conflict = False
    integrity_errors: tuple[type[BaseException], ...] = ()

    def __init__(self) -> None:
        self._initialize()

    @property
    def state_dir(self) -> Path:
        """Directory for locks, notifier state and other local files."""
        raise NotImplementedError

    def _create_connection(self) -> Any:
        raise NotImplementedError

    def _create_schema(self, connection: Any) -> None:
        raise NotImplementedError

    def _table_columns(self, connection: Any, table: str) -> set[str]:
        raise NotImplementedError

    def _after_init(self, connection: Any) -> None:
        """Optional dialect hook run at the end of initialization."""

    @contextmanager
    def connect(self) -> Iterator[Any]:
        connection = self._create_connection()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as connection:
            self._create_schema(connection)
            self._ensure_columns(connection)
            self._drop_legacy_tables(connection)
            self._normalize_timestamps(connection)
            self._after_init(connection)

    # ------------------------------------------------------------------
    # Schema rendering / migration
    # ------------------------------------------------------------------

    def _render_create_table(self, table: TableSpec) -> str:
        lines: list[str] = []
        for column in table.columns:
            definition = (
                column.sqlite_type if self.dialect == "sqlite" else column.mysql_type
            )
            parts = [column.name, definition]
            if column.auto_increment:
                parts.append(
                    "PRIMARY KEY AUTOINCREMENT"
                    if self.dialect == "sqlite"
                    else "AUTO_INCREMENT"
                )
            if column.not_null and not column.auto_increment:
                parts.append("NOT NULL")
            if column.default is not None:
                parts.append(f"DEFAULT {column.default}")
            if column.unique:
                parts.append("UNIQUE")
            if column.check:
                parts.append(f"CHECK ({column.check})")
            lines.append("    " + " ".join(parts))
        primary_key = table.primary_key
        if self.dialect == "sqlite" and any(
            column.auto_increment for column in table.columns
        ):
            # SQLite renders the auto-increment primary key inline.
            primary_key = ()
        if primary_key:
            lines.append("    PRIMARY KEY (" + ", ".join(primary_key) + ")")
        for foreign_key in table.foreign_keys:
            line = (
                f"    FOREIGN KEY ({', '.join(foreign_key.columns)}) "
                f"REFERENCES {foreign_key.ref_table} "
                f"({', '.join(foreign_key.ref_columns)})"
            )
            if foreign_key.on_delete:
                line += f" ON DELETE {foreign_key.on_delete}"
            lines.append(line)
        return (
            f"CREATE TABLE IF NOT EXISTS {table.name} (\n"
            + ",\n".join(lines)
            + "\n)"
        )

    def _create_indexes(self, connection: Any) -> None:
        for table in SCHEMA:
            for index in table.indexes:
                columns = ", ".join(index.columns)
                if self.dialect == "sqlite":
                    connection.execute(
                        f"CREATE INDEX IF NOT EXISTS {index.name} "
                        f"ON {table.name} ({columns})"
                    )
                    continue
                exists = connection.execute(
                    "SELECT 1 FROM information_schema.statistics "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ? "
                    "AND INDEX_NAME = ? LIMIT 1",
                    (table.name, index.name),
                ).fetchone()
                if exists is None:
                    connection.execute(
                        f"CREATE INDEX {index.name} ON {table.name} ({columns})"
                    )

    def _ensure_columns(self, connection: Any) -> None:
        for table, column, sqlite_definition, mysql_definition in _ENSURE_COLUMNS:
            definition = (
                sqlite_definition if self.dialect == "sqlite" else mysql_definition
            )
            if column not in self._table_columns(connection, table):
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                )

    @staticmethod
    def _drop_legacy_tables(connection: Any) -> None:
        """Remove tables from the retired SSH remote feature."""
        for table in ("remote_status", "remote_settings", "remotes"):
            connection.execute(f"DROP TABLE IF EXISTS {table}")

    def _normalize_timestamps(self, connection: Any) -> None:
        for table, columns, keys in _TIMESTAMP_TABLES:
            selected = ", ".join((*keys, *columns))
            for row in connection.execute(f"SELECT {selected} FROM {table}").fetchall():
                values = tuple(
                    _as_local_time(row[column]) if row[column] else None
                    for column in columns
                )
                if values == tuple(row[column] for column in columns):
                    continue
                assignments = ", ".join(f"{column} = ?" for column in columns)
                where = " AND ".join(f"{key} = ?" for key in keys)
                connection.execute(
                    f"UPDATE {table} SET {assignments} WHERE {where}",
                    (*values, *(row[key] for key in keys)),
                )

    def _upsert(
        self,
        connection: Any,
        table: str,
        columns: Sequence[str],
        values: Sequence[Any],
        conflict_columns: Sequence[str],
        update_columns: Sequence[str],
    ) -> None:
        placeholders = ", ".join("?" for _ in columns)
        column_list = ", ".join(columns)
        if self.supports_on_conflict:
            assignments = ", ".join(f"{c} = excluded.{c}" for c in update_columns)
            sql = (
                f"INSERT INTO {table} ({column_list}) VALUES ({placeholders}) "
                f"ON CONFLICT ({', '.join(conflict_columns)}) "
                f"DO UPDATE SET {assignments}"
            )
        else:
            assignments = ", ".join(f"{c} = VALUES({c})" for c in update_columns)
            sql = (
                f"INSERT INTO {table} ({column_list}) VALUES ({placeholders}) "
                f"ON DUPLICATE KEY UPDATE {assignments}"
            )
        connection.execute(sql, values)

    # ------------------------------------------------------------------
    # Users / authentication
    # ------------------------------------------------------------------

    def upsert_user(self, username: str, password_hash: str) -> None:
        now = local_now()
        with self.connect() as connection:
            self._upsert(
                connection,
                "users",
                ("username", "password_hash", "created_time", "updated_time"),
                (username, password_hash, now, now),
                ("username",),
                ("password_hash", "updated_time"),
            )
            connection.execute(
                "DELETE FROM auth_sessions WHERE username = ?", (username,)
            )

    def get_user(self, username: str) -> Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()

    def create_auth_session(
        self, token_hash: str, username: str, expires_at: str
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM auth_sessions WHERE expires_at <= ?", (local_now(),)
            )
            connection.execute(
                """INSERT INTO auth_sessions
                       (token_hash, username, created_time, expires_at)
                   VALUES (?, ?, ?, ?)""",
                (token_hash, username, local_now(), expires_at),
            )

    def get_auth_session(self, token_hash: str) -> Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM auth_sessions WHERE token_hash = ?", (token_hash,)
            ).fetchone()

    def delete_auth_session(self, token_hash: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,)
            )

    def get_login_attempt(self, ip_address: str) -> Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM login_attempts WHERE ip_address = ?", (ip_address,)
            ).fetchone()

    def record_login_failure(
        self,
        ip_address: str,
        *,
        now: str,
        blocked_until: str,
        block_after: int,
    ) -> tuple[int, str | None]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM login_attempts WHERE ip_address = ?", (ip_address,)
            ).fetchone()
            attempts = 0
            if row and (not row["blocked_until"] or row["blocked_until"] > now):
                attempts = int(row["failed_attempts"])
            attempts += 1
            active_block = blocked_until if attempts >= block_after else None
            self._upsert(
                connection,
                "login_attempts",
                ("ip_address", "failed_attempts", "blocked_until", "updated_time"),
                (ip_address, attempts, active_block, now),
                ("ip_address",),
                ("failed_attempts", "blocked_until", "updated_time"),
            )
        return attempts, active_block

    def clear_login_failures(self, ip_address: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM login_attempts WHERE ip_address = ?", (ip_address,)
            )

    # ------------------------------------------------------------------
    # Workflows
    # ------------------------------------------------------------------

    def create_workflow(
        self, name: str, definition: str, workflow_key: str | None = None
    ) -> Row:
        now = local_now()
        with self.connect() as connection:
            key = workflow_key or self._unused_workflow_key(connection)
            try:
                cursor = connection.execute(
                    """INSERT INTO workflows
                       (workflow_key, name, definition, created_time, updated_time)
                       VALUES (?, ?, ?, ?, ?)""",
                    (key, name, definition, now, now),
                )
            except self.integrity_errors as exc:
                if workflow_key:
                    raise ValueError(
                        f"workflow ID is already in use: {workflow_key}"
                    ) from exc
                raise
            return connection.execute(
                "SELECT * FROM workflows WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()

    def next_workflow_key(self) -> str:
        with self.connect() as connection:
            return self._unused_workflow_key(connection)

    @staticmethod
    def _unused_workflow_key(connection: Any) -> str:
        while True:
            candidate = f"workflow_{secrets.token_hex(6)}"
            exists = connection.execute(
                "SELECT 1 FROM workflows WHERE workflow_key = ?", (candidate,)
            ).fetchone()
            if exists is None:
                return candidate

    def list_workflows(self) -> list[Row]:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM workflows ORDER BY id"
            ).fetchall()

    def get_workflow(self, workflow_key: str) -> Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM workflows WHERE workflow_key = ?", (workflow_key,)
            ).fetchone()

    def update_workflow(self, workflow_key: str, name: str, definition: str) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE workflows SET name = ?, definition = ?, updated_time = ?
                   WHERE workflow_key = ?""",
                (name, definition, local_now(), workflow_key),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"workflow not found: {workflow_key}")

    def delete_workflow(self, workflow_key: str) -> None:
        with self.connect() as connection:
            # Runs may reference earlier runs through resume/reuse metadata. Clear
            # those references first so deleting one workflow cannot violate the
            # self-referencing foreign keys of runs belonging to another workflow.
            # The derived-table wrapper keeps the statement valid on MySQL, which
            # rejects a subquery reading the table being updated.
            connection.execute(
                "UPDATE workflow_runs SET resumed_from_run_id = NULL "
                "WHERE resumed_from_run_id IN ("
                "SELECT run_id FROM ("
                "SELECT run_id FROM workflow_runs WHERE workflow_name = ?"
                ") AS _runs)",
                (workflow_key,),
            )
            connection.execute(
                "UPDATE task_runs SET reused_from_run_id = NULL "
                "WHERE reused_from_run_id IN ("
                "SELECT run_id FROM ("
                "SELECT run_id FROM workflow_runs WHERE workflow_name = ?"
                ") AS _runs)",
                (workflow_key,),
            )
            # task_runs are removed by the ON DELETE CASCADE on their run_id.
            connection.execute(
                "DELETE FROM workflow_runs WHERE workflow_name = ?", (workflow_key,)
            )
            connection.execute(
                "DELETE FROM schedules WHERE workflow_name = ?", (workflow_key,)
            )
            cursor = connection.execute(
                "DELETE FROM workflows WHERE workflow_key = ?", (workflow_key,)
            )
            if cursor.rowcount != 1:
                raise KeyError(f"workflow not found: {workflow_key}")

    # ------------------------------------------------------------------
    # Runs / tasks
    # ------------------------------------------------------------------

    def create_run(
        self,
        run_id: str,
        workflow_name: str,
        task_names: Sequence[str],
        resumed_from_run_id: str | None = None,
        from_task: str | None = None,
        trigger_type: str = "manual",
        task_metadata: dict[str, dict[str, object]] | None = None,
        scope: Sequence[str] | None = None,
        runner: str = "local",
        runner_locations: Sequence[dict[str, object]] | None = None,
    ) -> None:
        now = local_now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO workflow_runs
                   (run_id, workflow_name, status, start_time, resumed_from_run_id, from_task,
                    trigger_type, scope, runner, runner_locations)
                   VALUES (?, ?, 'RUNNING', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    workflow_name,
                    now,
                    resumed_from_run_id,
                    from_task,
                    trigger_type,
                    json.dumps(list(scope), ensure_ascii=False) if scope is not None else None,
                    runner or "local",
                    json.dumps(list(runner_locations), ensure_ascii=False)
                    if runner_locations is not None
                    else None,
                ),
            )
            metadata = task_metadata or {}
            connection.executemany(
                """INSERT INTO task_runs
                   (run_id, workflow_name, task_name, status, task_description, depends_json,
                    task_type, snapshot_version)
                   VALUES (?, ?, ?, 'PENDING', ?, ?, ?, 2)""",
                [
                    (
                        run_id,
                        workflow_name,
                        name,
                        str(metadata.get(name, {}).get("description", "")),
                        json.dumps(
                            metadata.get(name, {}).get("depends", ()),
                            ensure_ascii=False,
                        ),
                        str(metadata.get(name, {}).get("task_type", "command")),
                    )
                    for name in task_names
                ],
            )

    def set_task_status(
        self,
        run_id: str,
        task_name: str,
        status: str,
        *,
        exit_code: int | None = None,
        log_file: str | None = None,
        error_message: str | None = None,
        reused_from_run_id: str | None = None,
        condition_result: str | None = None,
        handled_by: str | None = None,
        skip_kind: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> None:
        if status not in TASK_STATUSES:
            raise ValueError(f"invalid task status: {status}")
        now = local_now()
        start_time = start_time if start_time is not None else (now if status == "RUNNING" else None)
        end_time = end_time if end_time is not None else (now if status in {"SUCCESS", "FAILED", "SKIPPED"} else None)
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE task_runs
                   SET status = ?,
                       start_time = COALESCE(?, start_time),
                       end_time = ?, exit_code = ?,
                       log_file = COALESCE(?, log_file),
                       error_message = ?, reused_from_run_id = ?,
                       condition_result = ?, handled_by = ?, skip_kind = ?
                   WHERE run_id = ? AND task_name = ?""",
                (
                    status,
                    start_time,
                    end_time,
                    exit_code,
                    log_file,
                    error_message,
                    reused_from_run_id,
                    condition_result,
                    handled_by,
                    skip_kind,
                    run_id,
                    task_name,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"task run not found: {run_id}/{task_name}")

    def mark_task_handled(self, run_id: str, task_name: str, handled_by: str) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE task_runs SET handled_by = ? WHERE run_id = ? AND task_name = ?",
                (handled_by, run_id, task_name),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"task run not found: {run_id}/{task_name}")

    def finish_run(self, run_id: str, status: str, error_message: str | None = None) -> None:
        if status not in RUN_STATUSES - {"RUNNING"}:
            raise ValueError(f"invalid final run status: {status}")
        with self.connect() as connection:
            connection.execute(
                """UPDATE workflow_runs SET status = ?, end_time = ?, error_message = ?
                   WHERE run_id = ?""",
                (status, local_now(), error_message, run_id),
            )

    def latest_run(self, workflow_name: str) -> Row | None:
        with self.connect() as connection:
            return connection.execute(
                """SELECT * FROM workflow_runs WHERE workflow_name = ?
                   ORDER BY start_time DESC, run_id DESC LIMIT 1""",
                (workflow_name,),
            ).fetchone()

    def latest_run_times(self) -> dict[str, str]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT workflow_name, MAX(start_time) AS last_run_time
                   FROM workflow_runs GROUP BY workflow_name"""
            ).fetchall()
        return {row["workflow_name"]: row["last_run_time"] for row in rows}

    def task_states(self, run_id: str) -> dict[str, Row]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ? ORDER BY task_name", (run_id,)
            ).fetchall()
        return {row["task_name"]: row for row in rows}

    def task_status_counts(self, since: str) -> dict[str, int]:
        """Count task records per status for runs that started since an ISO timestamp."""
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT status, COUNT(*) AS count FROM task_runs
                   WHERE start_time IS NOT NULL AND start_time >= ?
                   GROUP BY status""",
                (since,),
            ).fetchall()
        return {row["status"]: int(row["count"]) for row in rows}

    def list_runs(self, workflow_name: str, limit: int = 20) -> list[Row]:
        with self.connect() as connection:
            return connection.execute(
                """SELECT * FROM workflow_runs WHERE workflow_name = ?
                   ORDER BY start_time DESC, run_id DESC LIMIT ?""",
                (workflow_name, limit),
            ).fetchall()

    def list_all_runs(self, limit: int = 100) -> list[Row]:
        with self.connect() as connection:
            return connection.execute(
                f"""SELECT wr.*,
                           {_RUN_PROGRESS_COLUMNS}
                    FROM workflow_runs wr
                    ORDER BY wr.start_time DESC, wr.run_id DESC LIMIT ?""",
                (limit,),
            ).fetchall()

    def search_runs(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        workflow_query: str = "",
        status: str = "",
        trigger_type: str = "",
        tag: str = "",
    ) -> tuple[list[Row], int, int]:
        conditions: list[str] = []
        parameters: list[object] = []
        if workflow_query:
            conditions.append(
                "(instr(lower(wr.workflow_name), lower(?)) > 0 OR "
                "instr(lower(COALESCE(w.name, '')), lower(?)) > 0)"
            )
            parameters.extend((workflow_query, workflow_query))
        if status:
            conditions.append("wr.status = ?")
            parameters.append(status)
        if trigger_type:
            conditions.append("wr.trigger_type = ?")
            parameters.append(trigger_type)
        if tag.startswith("tag:"):
            label = tag[4:]
            conditions.append("w.name LIKE ?")
            parameters.append(f"【{label}】%")
        elif tag == "other":
            conditions.append("w.name NOT LIKE '【%】%'")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        from_clause = (
            "FROM workflow_runs wr "
            "LEFT JOIN workflows w ON w.workflow_key = wr.workflow_name"
        )
        with self.connect() as connection:
            total_row = connection.execute(
                f"SELECT COUNT(*) AS total {from_clause} {where}", parameters
            ).fetchone()
            total = int(total_row["total"])
            total_pages = max(1, (total + page_size - 1) // page_size)
            page = min(max(page, 1), total_pages)
            rows = connection.execute(
                f"""SELECT wr.*, w.name AS workflow_description,
                            {_RUN_PROGRESS_COLUMNS}
                     {from_clause}
                     {where}
                     ORDER BY wr.start_time DESC, wr.run_id DESC
                     LIMIT ? OFFSET ?""",
                (*parameters, page_size, (page - 1) * page_size),
            ).fetchall()
        return rows, total, page

    def get_run(self, run_id: str) -> Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM workflow_runs WHERE run_id = ?", (run_id,)
            ).fetchone()

    def get_task(self, run_id: str, task_name: str) -> Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ? AND task_name = ?",
                (run_id, task_name),
            ).fetchone()

    def delete_run(self, run_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE workflow_runs SET resumed_from_run_id = NULL "
                "WHERE resumed_from_run_id = ?",
                (run_id,),
            )
            connection.execute(
                "UPDATE task_runs SET reused_from_run_id = NULL "
                "WHERE reused_from_run_id = ?",
                (run_id,),
            )
            cursor = connection.execute(
                "DELETE FROM workflow_runs WHERE run_id = ?", (run_id,)
            )
            if cursor.rowcount != 1:
                raise KeyError(f"run not found: {run_id}")

    # ------------------------------------------------------------------
    # Schedules
    # ------------------------------------------------------------------

    def upsert_schedule(
        self,
        workflow_name: str,
        description: str,
        cron_expression: str,
        timezone_name: str,
        enabled: bool,
    ) -> None:
        now = local_now()
        with self.connect() as connection:
            self._upsert(
                connection,
                "schedules",
                (
                    "workflow_name",
                    "description",
                    "cron_expression",
                    "timezone",
                    "enabled",
                    "created_time",
                    "updated_time",
                ),
                (
                    workflow_name,
                    description,
                    cron_expression,
                    timezone_name,
                    int(enabled),
                    now,
                    now,
                ),
                ("workflow_name",),
                (
                    "description",
                    "cron_expression",
                    "timezone",
                    "enabled",
                    "updated_time",
                ),
            )

    def get_schedule(self, workflow_name: str) -> Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM schedules WHERE workflow_name = ?", (workflow_name,)
            ).fetchone()

    def update_schedule_description(
        self, workflow_name: str, description: str
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE schedules SET description = ? WHERE workflow_name = ?",
                (description, workflow_name),
            )

    def list_schedules(self) -> list[Row]:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM schedules ORDER BY workflow_name"
            ).fetchall()

    def delete_schedule(self, workflow_name: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM schedules WHERE workflow_name = ?", (workflow_name,)
            )

    def workflows_using_runner(self, alias: str) -> list[str]:
        with self.connect() as connection:
            return [
                row["workflow_key"]
                for row in connection.execute(
                    "SELECT workflow_key FROM workflows WHERE runner = ? ORDER BY workflow_key",
                    (alias,),
                ).fetchall()
            ]

    def set_workflow_runner(self, workflow_key: str, runner: str) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE workflows SET runner = ?, updated_time = ? WHERE workflow_key = ?",
                (runner, local_now(), workflow_key),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"workflow not found: {workflow_key}")

    # ------------------------------------------------------------------
    # Satellites
    # ------------------------------------------------------------------

    def upsert_satellite(self, config: SatelliteConfig) -> None:
        now = local_now()
        with self.connect() as connection:
            self._upsert(
                connection,
                "satellites",
                (
                    "alias",
                    "name",
                    "mode",
                    "url",
                    "token",
                    "host_key_policy",
                    "cert_fingerprint",
                    "status",
                    "last_seen",
                    "last_error",
                    "platform",
                    "hostname",
                    "agent_version",
                    "created_time",
                    "updated_time",
                ),
                (
                    config.alias,
                    config.name,
                    config.mode,
                    config.url,
                    config.token,
                    config.host_key_policy,
                    config.cert_fingerprint,
                    "offline",
                    None,
                    None,
                    "",
                    "",
                    "",
                    now,
                    now,
                ),
                ("alias",),
                (
                    "name",
                    "mode",
                    "url",
                    "token",
                    "host_key_policy",
                    "cert_fingerprint",
                    "status",
                    "last_seen",
                    "last_error",
                    "platform",
                    "hostname",
                    "agent_version",
                    "updated_time",
                ),
            )

    def list_satellites(self) -> list[SatelliteConfig]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM satellites ORDER BY alias"
            ).fetchall()
        return [_satellite_config(row) for row in rows]

    def get_satellite(self, alias: str) -> SatelliteConfig | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM satellites WHERE alias = ?", (alias,)
            ).fetchone()
        return _satellite_config(row) if row is not None else None

    def delete_satellite(self, alias: str) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM satellites WHERE alias = ?", (alias,)
            )
            if cursor.rowcount != 1:
                raise KeyError(f"satellite not found: {alias}")

    def is_satellite(self, alias: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM satellites WHERE alias = ?", (alias,)
            ).fetchone()
        return row is not None

    def set_satellite_status(
        self,
        alias: str,
        status: str,
        *,
        last_error: str | None = None,
        platform: str | None = None,
        hostname: str | None = None,
        agent_version: str | None = None,
        last_seen: str | None = None,
    ) -> None:
        now = local_now()
        with self.connect() as connection:
            connection.execute(
                """UPDATE satellites
                    SET status = ?,
                        last_seen = COALESCE(?, last_seen),
                        last_error = ?,
                        platform = COALESCE(?, platform),
                        hostname = COALESCE(?, hostname),
                        agent_version = COALESCE(?, agent_version),
                        updated_time = ?
                    WHERE alias = ?""",
                (
                    status,
                    last_seen or (now if status == "online" else None),
                    last_error,
                    platform,
                    hostname,
                    agent_version,
                    now,
                    alias,
                ),
            )

    def remember_satellite_fingerprint(self, alias: str, fingerprint: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE satellites SET cert_fingerprint = ?, updated_time = ? "
                "WHERE alias = ?",
                (fingerprint, local_now(), alias),
            )

    def create_satellite_job(
        self,
        job_id: str,
        satellite_alias: str,
        workflow_name: str,
        run_id: str,
        task_name: str,
        log_file: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO satellite_jobs
                        (job_id, satellite_alias, workflow_name, run_id, task_name,
                         status, log_file, created_time)
                    VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    job_id,
                    satellite_alias,
                    workflow_name,
                    run_id,
                    task_name,
                    log_file,
                    local_now(),
                ),
            )

    def get_satellite_job(self, job_id: str) -> Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM satellite_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()

    def update_satellite_job(self, job_id: str, **fields: object) -> None:
        if not fields:
            return
        allowed = {
            "status",
            "exit_code",
            "error_message",
            "started_time",
            "finished_time",
            "log_file",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown satellite job field(s): {', '.join(sorted(unknown))}")
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values: list[object] = list(fields.values())
        if "status" in fields and fields["status"] in {"success", "failed", "cancelled", "error"}:
            assignments += ", finished_time = ?"
            values.append(local_now())
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE satellite_jobs SET {assignments} WHERE job_id = ?",
                (*values, job_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"satellite job not found: {job_id}")

    def list_satellite_jobs(
        self, satellite_alias: str = "", limit: int = 50
    ) -> list[Row]:
        with self.connect() as connection:
            if satellite_alias:
                return connection.execute(
                    "SELECT * FROM satellite_jobs WHERE satellite_alias = ? "
                    "ORDER BY created_time DESC LIMIT ?",
                    (satellite_alias, limit),
                ).fetchall()
            return connection.execute(
                "SELECT * FROM satellite_jobs ORDER BY created_time DESC LIMIT ?",
                (limit,),
            ).fetchall()

    def mark_orphaned(self, workflow_name: str) -> int:
        """Close runs left RUNNING after a process or host crash (call while holding lock)."""
        now = local_now()
        message = "runner stopped before completion; recovered on next startup"
        with self.connect() as connection:
            run_ids = [
                row["run_id"]
                for row in connection.execute(
                    "SELECT run_id FROM workflow_runs WHERE workflow_name = ? AND status = 'RUNNING'",
                    (workflow_name,),
                ).fetchall()
            ]
            for run_id in run_ids:
                connection.execute(
                    """UPDATE task_runs SET status = 'FAILED', end_time = ?, error_message = ?
                       WHERE run_id = ? AND status = 'RUNNING'""",
                    (now, message, run_id),
                )
                connection.execute(
                    """UPDATE task_runs SET status = 'SKIPPED', end_time = ?, error_message = ?
                       WHERE run_id = ? AND status = 'PENDING'""",
                    (now, "runner stopped before task started", run_id),
                )
                connection.execute(
                    """UPDATE workflow_runs SET status = 'FAILED', end_time = ?, error_message = ?
                       WHERE run_id = ?""",
                    (now, message, run_id),
                )
        return len(run_ids)

    def running_workflow_names(self) -> list[str]:
        with self.connect() as connection:
            return [
                row["workflow_name"]
                for row in connection.execute(
                    "SELECT DISTINCT workflow_name FROM workflow_runs WHERE status = 'RUNNING'"
                ).fetchall()
            ]
