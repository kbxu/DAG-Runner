from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


TASK_STATUSES = {"PENDING", "RUNNING", "SUCCESS", "FAILED", "SKIPPED"}
RUN_STATUSES = {"RUNNING", "SUCCESS", "FAILED"}


def local_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _as_local_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed.astimezone().isoformat(timespec="seconds")


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
                    snapshot_version INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (run_id, task_name),
                    FOREIGN KEY (run_id) REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
                    FOREIGN KEY (reused_from_run_id) REFERENCES workflow_runs(run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_task_runs_lookup
                    ON task_runs(workflow_name, task_name, run_id);

                CREATE TABLE IF NOT EXISTS schedules (
                    workflow_name TEXT PRIMARY KEY,
                    description TEXT NOT NULL DEFAULT '',
                    cron_expression TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
                    created_time TEXT NOT NULL,
                    updated_time TEXT NOT NULL
                );
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
                connection, "task_runs", "snapshot_version", "INTEGER NOT NULL DEFAULT 0"
            )
            self._normalize_timestamps(connection)

    def create_workflow(self, name: str, definition: str) -> sqlite3.Row:
        now = local_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO workflows
                   (workflow_key, name, definition, created_time, updated_time)
                   VALUES (NULL, ?, ?, ?, ?)""",
                (name, definition, now, now),
            )
            workflow_key = f"workflow_{cursor.lastrowid:06d}"
            connection.execute(
                "UPDATE workflows SET workflow_key = ? WHERE id = ?",
                (workflow_key, cursor.lastrowid),
            )
            return connection.execute(
                "SELECT * FROM workflows WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()

    def next_workflow_key(self) -> str:
        with self.connect() as connection:
            sequence = connection.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = 'workflows'"
            ).fetchone()
            next_id = (
                sequence[0] + 1
                if sequence
                else connection.execute(
                    "SELECT COALESCE(MAX(id), 0) + 1 FROM workflows"
                ).fetchone()[0]
            )
        return f"workflow_{next_id:06d}"

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
        task_metadata: dict[str, tuple[str, Sequence[str]]] | None = None,
    ) -> None:
        now = local_now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO workflow_runs
                   (run_id, workflow_name, status, start_time, resumed_from_run_id, from_task,
                    trigger_type)
                   VALUES (?, ?, 'RUNNING', ?, ?, ?, ?)""",
                (run_id, workflow_name, now, resumed_from_run_id, from_task, trigger_type),
            )
            metadata = task_metadata or {}
            connection.executemany(
                """INSERT INTO task_runs
                   (run_id, workflow_name, task_name, status, task_description, depends_json,
                    snapshot_version)
                   VALUES (?, ?, ?, 'PENDING', ?, ?, 1)""",
                [
                    (
                        run_id,
                        workflow_name,
                        name,
                        metadata.get(name, ("", ()))[0],
                        json.dumps(metadata.get(name, ("", ()))[1], ensure_ascii=False),
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
    ) -> None:
        if status not in TASK_STATUSES:
            raise ValueError(f"invalid task status: {status}")
        now = local_now()
        start_time = now if status == "RUNNING" else None
        end_time = now if status in {"SUCCESS", "FAILED", "SKIPPED"} else None
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE task_runs
                   SET status = ?,
                       start_time = COALESCE(?, start_time),
                       end_time = ?, exit_code = ?,
                       log_file = COALESCE(?, log_file),
                       error_message = ?, reused_from_run_id = ?
                   WHERE run_id = ? AND task_name = ?""",
                (
                    status,
                    start_time,
                    end_time,
                    exit_code,
                    log_file,
                    error_message,
                    reused_from_run_id,
                    run_id,
                    task_name,
                ),
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

    def task_states(self, run_id: str) -> dict[str, sqlite3.Row]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ? ORDER BY task_name", (run_id,)
            ).fetchall()
        return {row["task_name"]: row for row in rows}

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
                          SUM(CASE WHEN tr.status = 'FAILED' THEN 1 ELSE 0 END) AS failed_count,
                          SUM(CASE WHEN tr.status = 'RUNNING' THEN 1 ELSE 0 END) AS running_count,
                          SUM(CASE WHEN tr.status = 'SKIPPED' THEN 1 ELSE 0 END) AS skipped_count
                   FROM workflow_runs wr
                   LEFT JOIN task_runs tr ON tr.run_id = wr.run_id
                   GROUP BY wr.run_id
                   ORDER BY wr.start_time DESC, wr.rowid DESC LIMIT ?""",
                (limit,),
            ).fetchall()

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
