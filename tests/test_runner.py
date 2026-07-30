from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from threading import Event
import unittest
from unittest.mock import patch

from dagrunner.database import StateDatabase
from dagrunner.executor import ExecutionResult, TaskExecutor
from dagrunner.logger import TaskLogManager
from dagrunner.runner import WorkflowRunner
from dagrunner.workflow import Task, Workflow, WorkflowError


class FakeExecutor:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    def execute(
        self, task, *, cwd, workflow_env, workflow_setup, log_file, cancel_event=None
    ):
        self.calls.append(task.name)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text(f"fake {task.name}\n", encoding="utf-8")
        code = self.outcomes.get(task.name, 0)
        return ExecutionResult(code, None if code == 0 else "fake failure")


def sample_workflow(workdir: Path) -> Workflow:
    return Workflow(
        name="test_flow",
        workdir=workdir,
        tasks={
            "task_a": Task("task_a", "unused"),
            "task_b": Task("task_b", "unused", depends=("task_a",)),
            "task_c": Task("task_c", "unused", depends=("task_b",)),
        },
    )


class RunnerTests(unittest.TestCase):
    @patch("dagrunner.executor._terminate_process")
    @patch("dagrunner.executor.subprocess.Popen")
    def test_executor_honors_cancel_event(self, popen_mock, terminate_mock):
        popen_mock.return_value = SimpleNamespace(poll=lambda: None, returncode=None)
        cancelled = Event()
        cancelled.set()
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = TaskExecutor().execute(
                Task("task_a", ("python", "never_executed.py")),
                cwd=root,
                workflow_env={},
                workflow_setup="",
                log_file=root / "task.log",
                cancel_event=cancelled,
            )
        self.assertEqual(result.exit_code, 130)
        self.assertEqual(result.error_message, "task stopped by user")
        terminate_mock.assert_called_once_with(popen_mock.return_value)

    @patch("dagrunner.executor.subprocess.Popen")
    def test_setup_and_task_share_one_shell(self, popen_mock):
        popen_mock.return_value = SimpleNamespace(poll=lambda: 0, returncode=0)
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = TaskExecutor().execute(
                Task("task_a", ("python", "job.py"), args=("--label", "hello world")),
                cwd=root,
                workflow_env={"DIRECT_ENV": "yes"},
                workflow_setup="source /opt/conda/etc/profile.d/conda.sh\nconda activate ta",
                log_file=root / "task.log",
            )

        command = popen_mock.call_args.args[0]
        self.assertEqual(result.exit_code, 0)
        self.assertIn("conda activate ta\npython job.py --label 'hello world'", command[-1])
        self.assertEqual(popen_mock.call_args.kwargs["env"]["DIRECT_ENV"], "yes")

    def test_failure_skips_descendants(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = StateDatabase(root / "state.db")
            executor = FakeExecutor({"task_b": 9})
            run_id, status = WorkflowRunner(
                database, TaskLogManager(root / "logs"), executor=executor
            ).run(sample_workflow(root))

            states = database.task_states(run_id)
            self.assertEqual(status, "FAILED")
            self.assertEqual(executor.calls, ["task_a", "task_b"])
            self.assertEqual(states["task_a"]["status"], "SUCCESS")
            self.assertEqual(states["task_b"]["status"], "FAILED")
            self.assertEqual(states["task_c"]["status"], "SKIPPED")

    def test_cancel_before_start_persists_failed_run_without_execution(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = StateDatabase(root / "state.db")
            executor = FakeExecutor({})
            cancelled = Event()
            cancelled.set()
            run_id, status = WorkflowRunner(
                database, TaskLogManager(root / "logs"), executor=executor
            ).run(sample_workflow(root), cancel_event=cancelled)

            self.assertEqual(status, "FAILED")
            self.assertEqual(executor.calls, [])
            self.assertEqual(database.get_run(run_id)["error_message"], "stopped by user")
            self.assertTrue(
                all(row["status"] == "SKIPPED" for row in database.task_states(run_id).values())
            )

    def test_resume_reuses_ancestor_and_runs_descendants(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = StateDatabase(root / "state.db")
            workflow = sample_workflow(root)
            first_executor = FakeExecutor({"task_b": 1})
            first_id, _ = WorkflowRunner(
                database, TaskLogManager(root / "logs"), executor=first_executor
            ).run(workflow)

            second_executor = FakeExecutor({})
            second_id, status = WorkflowRunner(
                database, TaskLogManager(root / "logs"), executor=second_executor
            ).run(workflow, from_task="task_b")
            states = database.task_states(second_id)

            self.assertEqual(status, "SUCCESS")
            self.assertEqual(second_executor.calls, ["task_b", "task_c"])
            self.assertEqual(states["task_a"]["status"], "SUCCESS")
            self.assertEqual(states["task_a"]["reused_from_run_id"], first_id)
            self.assertEqual(states["task_b"]["status"], "SUCCESS")
            self.assertEqual(states["task_c"]["status"], "SUCCESS")

    def test_cycle_is_rejected(self):
        workflow = Workflow(
            name="cycle",
            workdir=Path("."),
            tasks={
                "a": Task("a", "unused", depends=("b",)),
                "b": Task("b", "unused", depends=("a",)),
            },
        )
        with self.assertRaises(WorkflowError):
            workflow.topological_order()


if __name__ == "__main__":
    unittest.main()
