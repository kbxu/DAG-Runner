from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from dagrunner.database import StateDatabase
from dagrunner.server.executor import ExecutionResult
from dagrunner.server.logger import TaskLogManager
from dagrunner.server.runner import WorkflowRunner
from dagrunner.workflow import (
    ConditionGroup,
    ConditionItem,
    ConditionSpec,
    Task,
    Workflow,
    WorkflowError,
)


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

    def test_chained_resume_keeps_reused_tasks_and_from_task(self) -> None:
        workflow = Workflow(
            name="chain",
            tasks={
                "a": Task(name="a", command="a"),
                "b": Task(name="b", command="b", depends=("a",)),
                "c": Task(name="c", command="c", depends=("b",)),
                "d": Task(name="d", command="d", depends=("c",)),
            },
            config_dir=self.root,
        )
        r1, s1 = self.runner(FixedExecutor({"c": ExecutionResult(1, "c boom")})).run(
            workflow, "b"
        )
        self.assertEqual(s1, "FAILED")

        exec2 = FixedExecutor({"d": ExecutionResult(1, "d boom")})
        r2, s2 = self.runner(exec2).run(workflow, resume_run_id=r1)
        self.assertEqual(s2, "FAILED")
        self.assertEqual(exec2.ran, ["c", "d"])
        self.assertEqual(self.database.get_run(r2)["from_task"], "b")

        exec3 = FixedExecutor()
        r3, s3 = self.runner(exec3).run(workflow, resume_run_id=r2)
        self.assertEqual(s3, "SUCCESS")
        self.assertEqual(exec3.ran, ["d"])
        self.assertEqual(self.database.get_run(r3)["from_task"], "b")
        states3 = self.database.task_states(r3)
        self.assertEqual(states3["a"]["skip_kind"], "OUTSIDE_SCOPE")
        self.assertEqual(states3["b"]["skip_kind"], "REUSED")
        self.assertEqual(states3["c"]["skip_kind"], "REUSED")
        self.assertEqual(states3["d"]["status"], "SUCCESS")

    def test_resume_preserves_condition_branch_and_skips_unselected(self) -> None:
        workflow = Workflow(
            name="cond_resume",
            tasks={
                "a": Task(name="a", command="a"),
                "cond": Task(
                    name="cond",
                    task_type="condition",
                    depends=("a",),
                    condition=ConditionSpec(
                        "AND", (ConditionGroup("AND", (ConditionItem("a", "SUCCESS"),)),)
                    ),
                    success=("ok_task",),
                    failure=("fail_task",),
                ),
                "ok_task": Task(name="ok_task", command="ok", depends=("cond",)),
                "fail_task": Task(name="fail_task", command="fail", depends=("cond",)),
            },
            config_dir=self.root,
        )
        r1, s1 = self.runner(
            FixedExecutor({"ok_task": ExecutionResult(1, "boom")})
        ).run(workflow)
        self.assertEqual(s1, "FAILED")

        exec2 = FixedExecutor()
        r2, s2 = self.runner(exec2).run(workflow, resume_run_id=r1)
        self.assertEqual(s2, "SUCCESS")
        self.assertEqual(exec2.ran, ["ok_task"])
        states2 = self.database.task_states(r2)
        self.assertEqual(states2["a"]["skip_kind"], "REUSED")
        self.assertEqual(states2["cond"]["skip_kind"], "REUSED")
        self.assertEqual(states2["cond"]["condition_result"], "success")
        self.assertEqual(states2["fail_task"]["status"], "SKIPPED")
        self.assertEqual(states2["fail_task"]["skip_kind"], "CONDITION_NOT_SELECTED")
        self.assertEqual(states2["ok_task"]["status"], "SUCCESS")


if __name__ == "__main__":
    unittest.main()
