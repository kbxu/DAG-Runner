from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from dagrunner import runner
from dagrunner.database import StateDatabase


class CliViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.database = StateDatabase(root / "scheduler.db")
        self.run_id = "20260101_000000_test01"
        self.database.create_run(
            self.run_id,
            "workflow_test",
            ["first", "second"],
            trigger_type="manual",
            scope=["first", "second"],
        )
        self.database.set_task_status(
            self.run_id,
            "first",
            "SUCCESS",
            exit_code=0,
            log_file=str(root / "first.log"),
        )
        self.database.set_task_status(
            self.run_id, "second", "FAILED", exit_code=1, error_message="boom"
        )
        self.database.finish_run(self.run_id, "FAILED", "boom")
        (root / "first.log").write_text("hello log\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run_cli(self, *args: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = runner.main(["--db", str(self.database.path), *args])
        return code, buffer.getvalue()

    def test_list_runs_lists_all_workflows(self) -> None:
        code, output = self._run_cli("--list-runs")
        self.assertEqual(code, 0)
        self.assertIn("run_id", output)
        self.assertIn(self.run_id, output)
        self.assertIn("workflow_test", output)

    def test_status_shows_tasks(self) -> None:
        code, output = self._run_cli("--status", "--run-id", self.run_id)
        self.assertEqual(code, 0)
        self.assertIn("first", output)
        self.assertIn("second", output)

    def test_show_log_prints_log_file(self) -> None:
        code, output = self._run_cli(
            "--show-log", "--run-id", self.run_id, "--task", "first"
        )
        self.assertEqual(code, 0)
        self.assertIn("hello log", output)

    def test_run_flags_are_rejected(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            runner.main(
                ["--db", str(self.database.path), "--workflow", "workflow_test"]
            )
        self.assertEqual(caught.exception.code, 2)

    def test_no_action_prints_help(self) -> None:
        code, output = self._run_cli()
        self.assertEqual(code, 2)
        self.assertIn("--list-runs", output)


if __name__ == "__main__":
    unittest.main()
