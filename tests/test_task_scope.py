from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from dagrunner.database import StateDatabase
from dagrunner.executor import ExecutionResult
from dagrunner.logger import TaskLogManager
from dagrunner.runner import WorkflowRunner
from dagrunner.workflow import Task, Workflow, WorkflowError


class FixedExecutor:
    def __init__(self, results: dict[str, ExecutionResult] | None = None) -> None:
        self.ran: list[str] = []
        self.results = results or {}

    def execute(self, task, **kwargs) -> ExecutionResult:
        self.ran.append(task.name)
        return self.results.get(task.name, ExecutionResult(0))


class TaskScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.root = root
        self.database = StateDatabase(root / "scheduler.db")
        self.workflow = Workflow(
            name="pipeline",
            tasks={
                "first": Task(name="first", command="first"),
                "second": Task(name="second", command="second", depends=("first",)),
                "third": Task(name="third", command="third", depends=("second",)),
            },
            config_dir=root,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def runner(self, executor) -> WorkflowRunner:
        return WorkflowRunner(
            self.database, TaskLogManager(self.root / "logs"), executor=executor
        )

    def test_only_task_runs_single_task_without_history(self) -> None:
        executor = FixedExecutor()
        run_id, status = self.runner(executor).run(
            self.workflow, "second", only_task=True
        )
        self.assertEqual(status, "SUCCESS")
        self.assertEqual(executor.ran, ["second"])
        states = self.database.task_states(run_id)
        self.assertEqual(states["second"]["status"], "SUCCESS")
        self.assertEqual(states["first"]["status"], "SKIPPED")
        self.assertEqual(states["third"]["status"], "SKIPPED")

    def test_only_task_ignores_upstream_state(self) -> None:
        executor = FixedExecutor()
        run_id, status = self.runner(executor).run(
            self.workflow, "second", only_task=True
        )
        self.assertEqual(status, "SUCCESS")
        self.assertEqual(executor.ran, ["second"])

    def test_only_task_needs_from_task(self) -> None:
        with self.assertRaises(WorkflowError):
            self.runner(FixedExecutor()).run(self.workflow, only_task=True)

    def test_from_task_runs_descendants(self) -> None:
        self.runner(FixedExecutor()).run(self.workflow)
        executor = FixedExecutor()
        run_id, status = self.runner(executor).run(self.workflow, "second")
        self.assertEqual(status, "SUCCESS")
        self.assertEqual(executor.ran, ["second", "third"])
        states = self.database.task_states(run_id)
        self.assertEqual(states["first"]["status"], "SKIPPED")
        self.assertEqual(states["first"]["skip_kind"], "OUTSIDE_SCOPE")
        self.assertIsNone(states["first"]["reused_from_run_id"])
        self.assertEqual(states["second"]["status"], "SUCCESS")
        self.assertEqual(states["third"]["status"], "SUCCESS")

    def test_scoped_runs_are_isolated_from_each_other(self) -> None:
        first_id, first_status = self.runner(FixedExecutor()).run(self.workflow, "third")
        second_id, second_status = self.runner(FixedExecutor()).run(self.workflow, "second")
        self.assertEqual((first_status, second_status), ("SUCCESS", "SUCCESS"))
        first = self.database.task_states(first_id)
        second = self.database.task_states(second_id)
        # Run 1 only executed "third"; "second" stayed out of scope.
        self.assertEqual(first["third"]["status"], "SUCCESS")
        self.assertEqual(first["second"]["status"], "SKIPPED")
        # Run 2 executed "second" and "third" on its own, borrowing nothing.
        self.assertEqual(second["second"]["status"], "SUCCESS")
        self.assertEqual(second["third"]["status"], "SUCCESS")
        self.assertEqual(second["first"]["status"], "SKIPPED")
        for states in (first, second):
            self.assertTrue(
                all(row["reused_from_run_id"] is None for row in states.values())
            )

    def test_from_task_without_history_runs_the_subtree(self) -> None:
        executor = FixedExecutor()
        run_id, status = self.runner(executor).run(self.workflow, "second")
        self.assertEqual(status, "SUCCESS")
        self.assertEqual(executor.ran, ["second", "third"])
        states = self.database.task_states(run_id)
        self.assertEqual(states["first"]["status"], "SKIPPED")
        self.assertIsNone(states["first"]["reused_from_run_id"])
        self.assertEqual(states["second"]["status"], "SUCCESS")
        self.assertEqual(states["third"]["status"], "SUCCESS")

    def test_repeated_scoped_runs_from_different_tasks(self) -> None:
        executor = FixedExecutor()
        runner = self.runner(executor)
        for task in ("third", "second", "first"):
            run_id, status = runner.run(self.workflow, task)
            self.assertEqual(status, "SUCCESS", task)
            states = self.database.task_states(run_id)
            self.assertNotEqual(states[task]["status"], "SKIPPED", task)
            self.assertIsNone(states[task]["error_message"], task)

    def test_resume_repeats_single_node_scope(self) -> None:
        first_id, _ = self.runner(
            FixedExecutor({"second": ExecutionResult(1, "boom")})
        ).run(self.workflow, "second", only_task=True)
        second_id, status = self.runner(FixedExecutor()).run(
            self.workflow, resume_run_id=first_id
        )
        self.assertEqual(status, "SUCCESS")
        states = self.database.task_states(second_id)
        self.assertEqual(states["second"]["status"], "SUCCESS")
        for name in ("first", "third"):
            self.assertEqual(states[name]["status"], "SKIPPED")
            self.assertEqual(states[name]["skip_kind"], "OUTSIDE_SCOPE")

    def test_resume_repeats_subtree_scope(self) -> None:
        first_id, _ = self.runner(
            FixedExecutor({"second": ExecutionResult(1, "boom")})
        ).run(self.workflow, "second")
        second_id, status = self.runner(FixedExecutor()).run(
            self.workflow, resume_run_id=first_id
        )
        self.assertEqual(status, "SUCCESS")
        states = self.database.task_states(second_id)
        self.assertEqual(states["second"]["status"], "SUCCESS")
        self.assertEqual(states["third"]["status"], "SUCCESS")
        self.assertEqual(states["first"]["status"], "SKIPPED")

    def test_resume_marks_previous_success_as_reused(self) -> None:
        first_id, _ = self.runner(
            FixedExecutor({"third": ExecutionResult(1, "boom")})
        ).run(self.workflow, "second")
        second_id, status = self.runner(FixedExecutor()).run(
            self.workflow, resume_run_id=first_id
        )
        self.assertEqual(status, "SUCCESS")
        states = self.database.task_states(second_id)
        self.assertEqual(states["second"]["status"], "SKIPPED")
        self.assertEqual(states["second"]["skip_kind"], "REUSED")
        self.assertIsNone(states["second"]["reused_from_run_id"])
        self.assertEqual(states["third"]["status"], "SUCCESS")

    def test_multiple_resumes_of_the_same_run(self) -> None:
        first_id, _ = self.runner(
            FixedExecutor({"second": ExecutionResult(1, "boom")})
        ).run(self.workflow, "second")
        for _ in range(2):
            run_id, status = self.runner(FixedExecutor()).run(
                self.workflow, resume_run_id=first_id
            )
            self.assertEqual(status, "SUCCESS")

    def test_resume_record_is_self_contained(self) -> None:
        failing = FixedExecutor(
            {"first": ExecutionResult(0), "second": ExecutionResult(1, "boom")}
        )
        first_id, status = self.runner(failing).run(self.workflow)
        self.assertEqual(status, "FAILED")
        second_id, status = self.runner(FixedExecutor()).run(
            self.workflow, resume_run_id=first_id
        )
        self.assertEqual(status, "SUCCESS")
        states = self.database.task_states(second_id)
        self.assertEqual(states["first"]["status"], "SKIPPED")
        self.assertEqual(states["first"]["skip_kind"], "REUSED")
        self.assertIsNone(states["first"]["reused_from_run_id"])
        self.assertIsNone(states["first"]["log_file"])
        self.assertEqual(states["second"]["status"], "SUCCESS")
        self.assertEqual(states["third"]["status"], "SUCCESS")

    def test_full_run_is_unchanged(self) -> None:
        executor = FixedExecutor()
        run_id, status = self.runner(executor).run(self.workflow)
        self.assertEqual(status, "SUCCESS")
        self.assertEqual(executor.ran, ["first", "second", "third"])
        states = self.database.task_states(run_id)
        self.assertTrue(all(row["status"] == "SUCCESS" for row in states.values()))


if __name__ == "__main__":
    unittest.main()
