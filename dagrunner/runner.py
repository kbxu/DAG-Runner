#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from threading import Event

from .database import StateDatabase
from .executor import TaskExecutor
from .logger import TaskLogManager
from .notifier import Notifier, NullNotifier, TaskEvent
from .workflow import Workflow, WorkflowError


class AlreadyRunningError(RuntimeError):
    pass


class WorkflowRunner:
    def __init__(
        self,
        database: StateDatabase,
        logs: TaskLogManager,
        executor: TaskExecutor | None = None,
        notifier: Notifier | None = None,
    ):
        self.database = database
        self.logs = logs
        self.executor = executor or TaskExecutor()
        self.notifier = notifier or NullNotifier()

    def run(
        self,
        workflow: Workflow,
        from_task: str | None = None,
        *,
        run_id: str | None = None,
        trigger_type: str = "manual",
        cancel_event: Event | None = None,
    ) -> tuple[str, str]:
        order = workflow.topological_order()
        previous = self.database.latest_run(workflow.name) if from_task else None
        if from_task and previous is None:
            raise WorkflowError(f"cannot resume {workflow.name!r}: no previous run exists")
        selected = workflow.descendants(from_task) if from_task else set(order)
        previous_states = self.database.task_states(previous["run_id"]) if previous else {}
        run_id = run_id or new_run_id()
        self.database.create_run(
            run_id,
            workflow.name,
            order,
            resumed_from_run_id=previous["run_id"] if previous else None,
            from_task=from_task,
            trigger_type=trigger_type,
        )
        states = {name: "PENDING" for name in order}

        try:
            if from_task:
                self._seed_resume(run_id, order, selected, previous, previous_states, states)
            for task_name in order:
                if states[task_name] != "PENDING":
                    continue
                if cancel_event and cancel_event.is_set():
                    self._skip(run_id, task_name, "run stopped by user", states)
                    continue
                task = workflow.tasks[task_name]
                if not task.enabled:
                    self._skip(run_id, task_name, "task is disabled", states)
                    continue
                bad_dependencies = [
                    dependency for dependency in task.depends if states[dependency] != "SUCCESS"
                ]
                if bad_dependencies:
                    detail = ", ".join(
                        f"{name}={states[name]}" for name in bad_dependencies
                    )
                    self._skip(run_id, task_name, f"dependency not successful: {detail}", states)
                    continue
                log_file = self.logs.path_for(workflow.name, run_id, task_name)
                self.database.set_task_status(
                    run_id, task_name, "RUNNING", log_file=str(log_file)
                )
                states[task_name] = "RUNNING"
                result = self.executor.execute(
                    task,
                    cwd=workflow.task_cwd(task),
                    workflow_env=workflow.env,
                    workflow_setup=workflow.setup_for_current_platform(),
                    log_file=log_file,
                    cancel_event=cancel_event,
                )
                status = "SUCCESS" if result.exit_code == 0 else "FAILED"
                self.database.set_task_status(
                    run_id,
                    task_name,
                    status,
                    exit_code=result.exit_code,
                    log_file=str(log_file),
                    error_message=result.error_message,
                )
                states[task_name] = status
                event = TaskEvent(
                    workflow_name=workflow.name,
                    run_id=run_id,
                    task_name=task_name,
                    status=status,
                    log_file=str(log_file),
                    exit_code=result.exit_code,
                    error_message=result.error_message,
                )
                self._notify(event)

            selected_incomplete = any(
                states[name] != "SUCCESS" and workflow.tasks[name].enabled for name in selected
            )
            final_status = "FAILED" if "FAILED" in states.values() or selected_incomplete else "SUCCESS"
            run_error = "stopped by user" if cancel_event and cancel_event.is_set() else None
            self.database.finish_run(run_id, final_status, run_error)
            return run_id, final_status
        except BaseException as exc:
            # Persist a truthful terminal state even for Ctrl-C or an unexpected notifier/DB error.
            for name, state in states.items():
                if state == "RUNNING":
                    self.database.set_task_status(
                        run_id, name, "FAILED", error_message=f"runner interrupted: {exc}"
                    )
                elif state == "PENDING":
                    self.database.set_task_status(
                        run_id, name, "SKIPPED", error_message="runner stopped before task started"
                    )
            self.database.finish_run(run_id, "FAILED", str(exc))
            raise

    def _seed_resume(self, run_id, order, selected, previous, previous_states, states) -> None:
        for task_name in order:
            if task_name in selected:
                continue
            old = previous_states.get(task_name)
            if old is not None and old["status"] == "SUCCESS":
                self.database.set_task_status(
                    run_id,
                    task_name,
                    "SUCCESS",
                    exit_code=old["exit_code"],
                    log_file=old["log_file"],
                    reused_from_run_id=previous["run_id"],
                )
                states[task_name] = "SUCCESS"
            else:
                self._skip(
                    run_id,
                    task_name,
                    f"outside --from scope; previous status was {old['status'] if old else 'MISSING'}",
                    states,
                )

    def _skip(self, run_id: str, task_name: str, reason: str, states: dict[str, str]) -> None:
        self.database.set_task_status(run_id, task_name, "SKIPPED", error_message=reason)
        states[task_name] = "SKIPPED"

    def _notify(self, event: TaskEvent) -> None:
        try:
            if event.status == "SUCCESS":
                self.notifier.on_task_success(event)
            else:
                self.notifier.on_task_failed(event)
        except Exception as exc:
            print(f"warning: notifier failed for {event.task_name}: {exc}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a single-host YAML DAG workflow")
    parser.add_argument("--workflow", help="workflow name (loads workflows/<name>.yaml)")
    parser.add_argument("--config", type=Path, help="explicit workflow YAML path")
    parser.add_argument("--from", dest="from_task", help="rerun this task and its descendants")
    parser.add_argument("--db", type=Path, default=Path("var") / "scheduler.db")
    parser.add_argument("--logs", type=Path, default=Path("var") / "logs")
    parser.add_argument("--config-dir", type=Path, default=Path("workflows"))
    parser.add_argument("--list-runs", action="store_true", help="show persisted workflow runs")
    parser.add_argument("--status", action="store_true", help="show tasks for --run-id")
    parser.add_argument("--show-log", action="store_true", help="print log for --run-id and --task")
    parser.add_argument("--run-id")
    parser.add_argument("--task")
    parser.add_argument("--limit", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        database = StateDatabase(args.db)
        if args.show_log:
            return _show_log(database, args)
        if args.status:
            return _show_status(database, args)
        if args.list_runs:
            if not args.workflow:
                raise WorkflowError("--list-runs requires --workflow")
            _print_rows(database.list_runs(args.workflow, args.limit), RUN_COLUMNS)
            return 0

        workflow_path = args.config or (
            args.config_dir / f"{args.workflow}.yaml" if args.workflow else None
        )
        if workflow_path is None:
            raise WorkflowError("provide --workflow or --config")
        workflow = Workflow.load(workflow_path)
        if args.workflow and workflow.name != args.workflow:
            raise WorkflowError(
                f"requested workflow {args.workflow!r}, but config declares {workflow.name!r}"
            )
        lock_path = args.db.resolve().parent / "locks" / f"{workflow.name}.lock"
        with workflow_lock(lock_path):
            recovered = database.mark_orphaned(workflow.name)
            if recovered:
                print(f"recovered {recovered} interrupted run(s)", file=sys.stderr)
            run_id, status = WorkflowRunner(database, TaskLogManager(args.logs)).run(
                workflow, args.from_task
            )
        print(f"workflow={workflow.name} run_id={run_id} status={status}")
        return 0 if status == "SUCCESS" else 1
    except (WorkflowError, AlreadyRunningError, OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"unexpected runner error: {exc}", file=sys.stderr)
        return 3


RUN_COLUMNS = ("run_id", "workflow_name", "status", "start_time", "end_time", "from_task")
TASK_COLUMNS = (
    "task_name",
    "status",
    "start_time",
    "end_time",
    "exit_code",
    "log_file",
    "error_message",
)


def _show_status(database: StateDatabase, args) -> int:
    if not args.run_id:
        raise WorkflowError("--status requires --run-id")
    run = database.get_run(args.run_id)
    if run is None:
        raise WorkflowError(f"run not found: {args.run_id}")
    _print_rows([run], RUN_COLUMNS)
    _print_rows(list(database.task_states(args.run_id).values()), TASK_COLUMNS)
    return 0


def _show_log(database: StateDatabase, args) -> int:
    if not args.run_id or not args.task:
        raise WorkflowError("--show-log requires --run-id and --task")
    task = database.task_states(args.run_id).get(args.task)
    if task is None:
        raise WorkflowError(f"task run not found: {args.run_id}/{args.task}")
    if not task["log_file"]:
        raise WorkflowError("task has no log file (it may have been skipped)")
    print(TaskLogManager.read(task["log_file"]), end="")
    return 0


def _print_rows(rows, columns) -> None:
    print("\t".join(columns))
    for row in rows:
        print("\t".join("" if row[column] is None else str(row[column]) for column in columns))


def new_run_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


@contextmanager
def workflow_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            raise AlreadyRunningError(f"workflow is already running (lock: {path})") from exc
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
