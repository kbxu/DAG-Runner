from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from dagrunner.database import StateDatabase
from dagrunner.maintenance import MaintenanceService, main


DEFINITION = "name: One\ntasks:\n  a:\n    command: echo a\n"


def iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds")


class MaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.database = StateDatabase(root / "scheduler.db")
        self.key = self.database.create_workflow("One", DEFINITION)["workflow_key"]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_run(
        self,
        run_id: str,
        *,
        status: str = "SUCCESS",
        ended: datetime | None = None,
        resumed_from: str | None = None,
    ) -> None:
        self.database.create_run(
            run_id, self.key, ["a"], scope=["a"], resumed_from_run_id=resumed_from
        )
        self.database.set_task_status(run_id, "a", "SUCCESS", exit_code=0)
        self.database.finish_run(run_id, status)
        if ended is not None:
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE workflow_runs SET start_time = ?, end_time = ? "
                    "WHERE run_id = ?",
                    (iso(ended), iso(ended), run_id),
                )

    def _count(self, table: str) -> int:
        with self.database.connect() as connection:
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def test_archive_moves_old_terminal_runs_only(self) -> None:
        old = datetime.now().astimezone() - timedelta(days=70)
        recent = datetime.now().astimezone() - timedelta(days=10)
        self._make_run("run-old-1", ended=old)
        self._make_run("run-old-2", status="FAILED", ended=old)
        self._make_run("run-recent", ended=recent)

        result = MaintenanceService(self.database, retention_days=60).archive()

        self.assertEqual(result["runs"], 2)
        self.assertEqual(result["tasks"], 2)
        self.assertEqual(self._count("workflow_runs"), 1)
        self.assertEqual(self._count("workflow_runs_archive"), 2)
        self.assertEqual(self._count("task_runs"), 1)
        self.assertEqual(self._count("task_runs_archive"), 2)
        self.assertIsNotNone(self.database.get_run("run-recent"))

    def test_archive_skips_running_runs(self) -> None:
        old = datetime.now().astimezone() - timedelta(days=70)
        self.database.create_run("run-running", self.key, ["a"], scope=["a"])
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE workflow_runs SET start_time = ?, end_time = NULL "
                "WHERE run_id = ?",
                (iso(old), "run-running"),
            )

        result = MaintenanceService(self.database, retention_days=60).archive()

        self.assertEqual(result["runs"], 0)
        self.assertIsNotNone(self.database.get_run("run-running"))

    def test_archive_is_idempotent(self) -> None:
        old = datetime.now().astimezone() - timedelta(days=70)
        self._make_run("run-old", ended=old)
        service = MaintenanceService(self.database, retention_days=60)

        self.assertEqual(service.archive()["runs"], 1)
        self.assertEqual(service.archive()["runs"], 0)
        self.assertEqual(self._count("workflow_runs_archive"), 1)

    def test_archive_clears_references_from_live_runs(self) -> None:
        old = datetime.now().astimezone() - timedelta(days=70)
        self._make_run("run-old", ended=old)
        self._make_run(
            "run-live",
            ended=datetime.now().astimezone() - timedelta(days=1),
            resumed_from="run-old",
        )

        MaintenanceService(self.database, retention_days=60).archive()

        live = self.database.get_run("run-live")
        self.assertIsNotNone(live)
        self.assertIsNone(live["resumed_from_run_id"])
        self.assertIsNone(self.database.get_run("run-old"))

    def test_cli_archives(self) -> None:
        old = datetime.now().astimezone() - timedelta(days=70)
        self._make_run("run-old", ended=old)

        code = main(["--db", str(self.database.path), "--archive", "--days", "60"])

        self.assertEqual(code, 0)
        self.assertEqual(self._count("workflow_runs_archive"), 1)


if __name__ == "__main__":
    unittest.main()
