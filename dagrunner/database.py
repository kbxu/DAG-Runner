from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


TASK_STATUSES = {"PENDING", "RUNNING", "SUCCESS", "FAILED", "SKIPPED"}
RUN_STATUSES = {"RUNNING", "SUCCESS", "FAILED"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
                    PRIMARY KEY (run_id, task_name),
                    FOREIGN KEY (run_id) REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
                    FOREIGN KEY (reused_from_run_id) REFERENCES workflow_runs(run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_task_runs_lookup
                    ON task_runs(workflow_name, task_name, run_id);

                CREATE TABLE IF NOT EXISTS schedules (
                    workflow_name TEXT PRIMARY KEY,
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

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def create_run(
        self,
        run_id: str,
        workflow_name: str,
        task_names: Sequence[str],
        resumed_from_run_id: str | None = None,
        from_task: str | None = None,
        trigger_type: str = "manual",
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO workflow_runs
                   (run_id, workflow_name, status, start_time, resumed_from_run_id, from_task,
                    trigger_type)
                   VALUES (?, ?, 'RUNNING', ?, ?, ?, ?)""",
                (run_id, workflow_name, now, resumed_from_run_id, from_task, trigger_type),
            )
            connection.executemany(
                """INSERT INTO task_runs (run_id, workflow_name, task_name, status)
                   VALUES (?, ?, ?, 'PENDING')""",
                [(run_id, workflow_name, name) for name in task_names],
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
        now = utc_now()
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
                (status, utc_now(), error_message, run_id),
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

    def upsert_schedule(
        self, workflow_name: str, cron_expression: str, timezone_name: str, enabled: bool
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO schedules
                       (workflow_name, cron_expression, timezone, enabled, created_time, updated_time)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(workflow_name) DO UPDATE SET
                       cron_expression = excluded.cron_expression,
                       timezone = excluded.timezone,
                       enabled = excluded.enabled,
                       updated_time = excluded.updated_time""",
                (workflow_name, cron_expression, timezone_name, int(enabled), now, now),
            )

    def get_schedule(self, workflow_name: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM schedules WHERE workflow_name = ?", (workflow_name,)
            ).fetchone()

    def list_schedules(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM schedules ORDER BY workflow_name"
            ).fetchall()

    def mark_orphaned(self, workflow_name: str) -> int:
        """Close runs left RUNNING after a process or host crash (call while holding lock)."""
        now = utc_now()
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
