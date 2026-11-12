from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .satellite_protocol import SatelliteConfig


TASK_STATUSES = {"PENDING", "RUNNING", "SUCCESS", "FAILED", "SKIPPED"}
RUN_STATUSES = {"RUNNING", "SUCCESS", "FAILED"}


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


def _satellite_config(row: sqlite3.Row) -> SatelliteConfig:
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


class StateDatabase:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
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
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS workflows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow_key TEXT UNIQUE,
                    name TEXT NOT NULL,
                    definition TEXT NOT NULL,
                    created_time TEXT NOT NULL,
                    updated_time TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    run_id TEXT PRIMARY KEY,
                    workflow_name TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('RUNNING','SUCCESS','FAILED')),
                    start_time TEXT NOT NULL,
                    end_time TEXT,
                    resumed_from_run_id TEXT,
                    from_task TEXT,
                    error_message TEXT,
                    trigger_type TEXT NOT NULL DEFAULT 'manual',
                    FOREIGN KEY (resumed_from_run_id) REFERENCES workflow_runs(run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_workflow_runs_name_start
                    ON workflow_runs(workflow_name, start_time DESC);

                CREATE TABLE IF NOT EXISTS task_runs (
                    run_id TEXT NOT NULL,
                    workflow_name TEXT NOT NULL,
                    task_name TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('PENDING','RUNNING','SUCCESS','FAILED','SKIPPED')),
                    start_time TEXT,
                    end_time TEXT,
                    exit_code INTEGER,
                    log_file TEXT,
                    error_message TEXT,
                    reused_from_run_id TEXT,
                    task_description TEXT NOT NULL DEFAULT '',
                    depends_json TEXT NOT NULL DEFAULT '[]',
                    task_type TEXT NOT NULL DEFAULT 'command',
                    condition_result TEXT,
                    handled_by TEXT,
                    skip_kind TEXT,
                    snapshot_version INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (run_id, task_name),
                    FOREIGN KEY (run_id) REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
                    FOREIGN KEY (reused_from_run_id) REFERENCES workflow_runs(run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_task_runs_lookup
                    ON task_runs(workflow_name, task_name, run_id);

                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    created_time TEXT NOT NULL,
                    updated_time TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    created_time TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires_at
                    ON auth_sessions(expires_at);

                CREATE TABLE IF NOT EXISTS login_attempts (
                    ip_address TEXT PRIMARY KEY,
                    failed_attempts INTEGER NOT NULL DEFAULT 0,
                    blocked_until TEXT,
                    updated_time TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS schedules (
                    workflow_name TEXT PRIMARY KEY,
                    description TEXT NOT NULL DEFAULT '',
                    cron_expression TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
                    created_time TEXT NOT NULL,
                    updated_time TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS satellites (
                    alias TEXT PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '',
                    mode TEXT NOT NULL DEFAULT 'auto'
                        CHECK (mode IN ('auto','forward','reverse')),
                    url TEXT NOT NULL DEFAULT '',
                    token TEXT NOT NULL,
                    host_key_policy TEXT NOT NULL DEFAULT 'accept-new',
                    cert_fingerprint TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'offline'
                        CHECK (status IN ('offline','online','error')),
                    last_seen TEXT,
                    last_error TEXT,
                    platform TEXT NOT NULL DEFAULT '',
                    hostname TEXT NOT NULL DEFAULT '',
                    agent_version TEXT NOT NULL DEFAULT '',
                    created_time TEXT NOT NULL,
                    updated_time TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS satellite_jobs (
                    job_id TEXT PRIMARY KEY,
                    satellite_alias TEXT NOT NULL,
                    workflow_name TEXT NOT NULL DEFAULT '',
                    run_id TEXT NOT NULL DEFAULT '',
                    task_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','running','success','failed','cancelled','error')),
                    exit_code INTEGER,
                    error_message TEXT,
                    log_file TEXT NOT NULL DEFAULT '',
                    created_time TEXT NOT NULL,
                    started_time TEXT,
                    finished_time TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_satellite_jobs_alias_time
                    ON satellite_jobs(satellite_alias, created_time);
                """
            )
            self._ensure_column(
                connection,
                "workflow_runs",
                "trigger_type",
                "TEXT NOT NULL DEFAULT 'manual'",
            )
            self._ensure_column(
                connection,
                "schedules",
                "description",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection, "task_runs", "task_description", "TEXT NOT NULL DEFAULT ''"
            )
            self._ensure_column(
                connection, "task_runs", "depends_json", "TEXT NOT NULL DEFAULT '[]'"
            )
            self._ensure_column(
                connection, "task_runs", "task_type", "TEXT NOT NULL DEFAULT 'command'"
            )
            self._ensure_column(connection, "task_runs", "condition_result", "TEXT")
            self._ensure_column(connection, "task_runs", "handled_by", "TEXT")
            self._ensure_column(connection, "task_runs", "skip_kind", "TEXT")
            self._ensure_column(connection, "workflow_runs", "scope", "TEXT")
            self._ensure_column(
                connection, "workflow_runs", "runner", "TEXT NOT NULL DEFAULT 'local'"
            )
            self._ensure_column(
                connection, "workflows", "runner", "TEXT NOT NULL DEFAULT 'local'"
            )
            self._ensure_column(
                connection, "task_runs", "snapshot_version", "INTEGER NOT NULL DEFAULT 0"
            )
            self._drop_legacy_tables(connection)
            self._normalize_timestamps(connection)
            connection.execute("PRAGMA optimize")

    @staticmethod
    def _drop_legacy_tables(connection: sqlite3.Connection) -> None:
        """Remove tables from the retired SSH remote feature."""
        for table in ("remote_status", "remote_settings", "remotes"):
            connection.execute(f"DROP TABLE IF EXISTS {table}")

    def upsert_user(self, username: str, password_hash: str) -> None:
        now = local_now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO users (username, password_hash, created_time, updated_time)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(username) DO UPDATE SET
                       password_hash = excluded.password_hash,
                       updated_time = excluded.updated_time""",
                (username, password_hash, now, now),
            )
            connection.execute(
                "DELETE FROM auth_sessions WHERE username = ?", (username,)
            )

    def get_user(self, username: str) -> sqlite3.Row | None:
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

    def get_auth_session(self, token_hash: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM auth_sessions WHERE token_hash = ?", (token_hash,)
            ).fetchone()

    def delete_auth_session(self, token_hash: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,)
            )

    def get_login_attempt(self, ip_address: str) -> sqlite3.Row | None:
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
            connection.execute(
                """INSERT INTO login_attempts
                       (ip_address, failed_attempts, blocked_until, updated_time)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(ip_address) DO UPDATE SET
                       failed_attempts = excluded.failed_attempts,
                       blocked_until = excluded.blocked_until,
                       updated_time = excluded.updated_time""",
                (ip_address, attempts, active_block, now),
            )
        return attempts, active_block

    def clear_login_failures(self, ip_address: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM login_attempts WHERE ip_address = ?", (ip_address,)
            )

    def create_workflow(
        self, name: str, definition: str, workflow_key: str | None = None
    ) -> sqlite3.Row:
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
            except sqlite3.IntegrityError as exc:
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
    def _unused_workflow_key(connection: sqlite3.Connection) -> str:
        while True:
            candidate = f"workflow_{secrets.token_hex(6)}"
            exists = connection.execute(
                "SELECT 1 FROM workflows WHERE workflow_key = ?", (candidate,)
            ).fetchone()
            if exists is None:
                return candidate

    def list_workflows(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute("SELECT * FROM workflows ORDER BY id").fetchall()

    def get_workflow(self, workflow_key: str) -> sqlite3.Row | None:
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
            connection.execute(
                "UPDATE workflow_runs SET resumed_from_run_id = NULL "
                "WHERE resumed_from_run_id IN ("
                "SELECT run_id FROM workflow_runs WHERE workflow_name = ?)",
                (workflow_key,),
            )
            connection.execute(
                "UPDATE task_runs SET reused_from_run_id = NULL "
                "WHERE reused_from_run_id IN ("
                "SELECT run_id FROM workflow_runs WHERE workflow_name = ?)",
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

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _normalize_timestamps(connection: sqlite3.Connection) -> None:
        for table, columns in (
            ("workflows", ("created_time", "updated_time")),
            ("workflow_runs", ("start_time", "end_time")),
            ("task_runs", ("start_time", "end_time")),
            ("schedules", ("created_time", "updated_time")),
        ):
            selected = ", ".join(columns)
            for row in connection.execute(
                f"SELECT rowid, {selected} FROM {table}"
            ).fetchall():
                values = tuple(
                    _as_local_time(row[column]) if row[column] else None
                    for column in columns
                )
                original = tuple(row[column] for column in columns)
                if values == original:
                    continue
                assignments = ", ".join(f"{column} = ?" for column in columns)
                connection.execute(
                    f"UPDATE {table} SET {assignments} WHERE rowid = ?",
                    (*values, row["rowid"]),
                )

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
    ) -> None:
        now = local_now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO workflow_runs
                   (run_id, workflow_name, status, start_time, resumed_from_run_id, from_task,
                    trigger_type, scope, runner)
                   VALUES (?, ?, 'RUNNING', ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    workflow_name,
                    now,
                    resumed_from_run_id,
                    from_task,
                    trigger_type,
                    json.dumps(list(scope), ensure_ascii=False) if scope is not None else None,
                    runner or "local",
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

    def latest_run(self, workflow_name: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                """SELECT * FROM workflow_runs WHERE workflow_name = ?
                   ORDER BY start_time DESC, rowid DESC LIMIT 1""",
                (workflow_name,),
            ).fetchone()

    def latest_run_times(self) -> dict[str, str]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT workflow_name, MAX(start_time) AS last_run_time
                   FROM workflow_runs GROUP BY workflow_name"""
            ).fetchall()
        return {row["workflow_name"]: row["last_run_time"] for row in rows}

    def task_states(self, run_id: str) -> dict[str, sqlite3.Row]:
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
                   WHERE start_time IS NOT NULL AND datetime(start_time) >= datetime(?)
                   GROUP BY status""",
                (since,),
            ).fetchall()
        return {row["status"]: row["count"] for row in rows}

    def list_runs(self, workflow_name: str, limit: int = 20) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """SELECT * FROM workflow_runs WHERE workflow_name = ?
                   ORDER BY start_time DESC, rowid DESC LIMIT ?""",
                (workflow_name, limit),
            ).fetchall()

    def list_all_runs(self, limit: int = 100) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """SELECT wr.*,
                          COUNT(tr.task_name) AS task_count,
                          SUM(CASE WHEN tr.status = 'SUCCESS' THEN 1 ELSE 0 END) AS success_count,
                          SUM(CASE WHEN tr.status = 'FAILED' AND tr.handled_by IS NULL THEN 1 ELSE 0 END) AS failed_count,
                          SUM(CASE WHEN tr.status = 'FAILED' AND tr.handled_by IS NOT NULL THEN 1 ELSE 0 END) AS handled_count,
                          SUM(CASE WHEN tr.status = 'RUNNING' THEN 1 ELSE 0 END) AS running_count,
                          SUM(CASE WHEN tr.status = 'SKIPPED' THEN 1 ELSE 0 END) AS skipped_count
                   FROM workflow_runs wr
                   LEFT JOIN task_runs tr ON tr.run_id = wr.run_id
                   GROUP BY wr.run_id
                   ORDER BY wr.start_time DESC, wr.rowid DESC LIMIT ?""",
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
    ) -> tuple[list[sqlite3.Row], int, int]:
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
            total = connection.execute(
                f"SELECT COUNT(*) {from_clause} {where}", parameters
            ).fetchone()[0]
            total_pages = max(1, (total + page_size - 1) // page_size)
            page = min(max(page, 1), total_pages)
            rows = connection.execute(
                f"""SELECT wr.*, w.name AS workflow_description,
                            COUNT(tr.task_name) AS task_count,
                            SUM(CASE WHEN tr.status = 'SUCCESS' THEN 1 ELSE 0 END) AS success_count,
                            SUM(CASE WHEN tr.status = 'FAILED' AND tr.handled_by IS NULL THEN 1 ELSE 0 END) AS failed_count,
                            SUM(CASE WHEN tr.status = 'FAILED' AND tr.handled_by IS NOT NULL THEN 1 ELSE 0 END) AS handled_count,
                            SUM(CASE WHEN tr.status = 'RUNNING' THEN 1 ELSE 0 END) AS running_count,
                            SUM(CASE WHEN tr.status = 'SKIPPED' THEN 1 ELSE 0 END) AS skipped_count
                     {from_clause}
                     LEFT JOIN task_runs tr ON tr.run_id = wr.run_id
                     {where}
                     GROUP BY wr.run_id
                     ORDER BY wr.start_time DESC, wr.rowid DESC
                     LIMIT ? OFFSET ?""",
                (*parameters, page_size, (page - 1) * page_size),
            ).fetchall()
        return rows, total, page

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM workflow_runs WHERE run_id = ?", (run_id,)
            ).fetchone()

    def get_task(self, run_id: str, task_name: str) -> sqlite3.Row | None:
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
            connection.execute(
                """INSERT INTO schedules
                       (workflow_name, description, cron_expression, timezone, enabled,
                        created_time, updated_time)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(workflow_name) DO UPDATE SET
                       description = excluded.description,
                       cron_expression = excluded.cron_expression,
                       timezone = excluded.timezone,
                       enabled = excluded.enabled,
                       updated_time = excluded.updated_time""",
                (
                    workflow_name,
                    description,
                    cron_expression,
                    timezone_name,
                    int(enabled),
                    now,
                    now,
                ),
            )

    def get_schedule(self, workflow_name: str) -> sqlite3.Row | None:
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

    def list_schedules(self) -> list[sqlite3.Row]:
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
                row[0]
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
            connection.execute(
                """INSERT INTO satellites
                        (alias, name, mode, url, token, host_key_policy, cert_fingerprint,
                         status, last_seen, last_error, platform, hostname, agent_version,
                         created_time, updated_time)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'offline', NULL, NULL, '', '', '', ?, ?)
                    ON CONFLICT(alias) DO UPDATE SET
                        name = excluded.name,
                        mode = excluded.mode,
                        url = excluded.url,
                        token = excluded.token,
                        host_key_policy = excluded.host_key_policy,
                        cert_fingerprint = excluded.cert_fingerprint,
                        status = 'offline',
                        last_seen = NULL,
                        last_error = NULL,
                        platform = '',
                        hostname = '',
                        agent_version = '',
                        updated_time = excluded.updated_time""",
                (
                    config.alias,
                    config.name,
                    config.mode,
                    config.url,
                    config.token,
                    config.host_key_policy,
                    config.cert_fingerprint,
                    now,
                    now,
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

    def get_satellite_job(self, job_id: str) -> sqlite3.Row | None:
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
    ) -> list[sqlite3.Row]:
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
                row[0]
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
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT workflow_name FROM workflow_runs WHERE status = 'RUNNING'"
                ).fetchall()
            ]
