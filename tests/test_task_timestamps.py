from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from dagrunner.database import StateDatabase, _as_local_time, local_now
from dagrunner.executor import ExecutionResult
from dagrunner.logger import TaskLogManager
from dagrunner.runner import WorkflowRunner
from dagrunner.workflow import Task, Workflow


class FixedExecutor:
    def __init__(self, results: dict[str, ExecutionResult]):
        self.results = results

    def execute(self, task, **kwargs) -> ExecutionResult:
        return self.results[task.name]


class TaskTimestampTests(unittest.TestCase):
    def test_local_now_keeps_milliseconds(self) -> None:
        self.assertRegex(local_now(), r"\.\d{3}[+-]\d{2}:\d{2}$")

    def test_as_local_time_preserves_second_and_millisecond_precision(self) -> None:
        self.assertNotIn(".", _as_local_time("2026-10-23T02:33:19+00:00"))
        self.assertIn(".", _as_local_time("2026-10-23T02:33:19.123+00:00"))

    def test_resume_keeps_previous_task_times(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = StateDatabase(root / "scheduler.db")
            workflow = Workflow(
                name="pipeline",
                tasks={
                    "first": Task(name="first", command="first"),
                    "second": Task(name="second", command="second", depends=("first",)),
                    "third": Task(name="third", command="third", depends=("second",)),
                },
                config_dir=root,
            )
            first_run, status = WorkflowRunner(
                database,
                TaskLogManager(root / "logs"),
                executor=FixedExecutor(
                    {"first": ExecutionResult(0), "second": ExecutionResult(1, "boom")}
                ),
            ).run(workflow)
            self.assertEqual(status, "FAILED")
            first_start = database.task_states(first_run)["first"]["start_time"]
            self.assertIn(".", first_start)

            second_run, status = WorkflowRunner(
                database,
                TaskLogManager(root / "logs"),
                executor=FixedExecutor(
                    {"second": ExecutionResult(0), "third": ExecutionResult(0)}
                ),
            ).run(workflow, resume_run_id=first_run)
            self.assertEqual(status, "SUCCESS")
            states = database.task_states(second_run)
            self.assertEqual(states["first"]["start_time"], first_start)
            ordered = [states[name]["start_time"] for name in ("first", "second", "third")]
            self.assertEqual(ordered, sorted(ordered))
            self.assertLess(states["first"]["start_time"], states["second"]["start_time"])
