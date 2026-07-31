#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
import uuid
from collections.abc import Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from threading import Event

from .database import StateDatabase
from .executor import TaskExecutor
from .logger import TaskLogManager
from .notifier import Notifier, NullNotifier, TaskEvent
from .workflow import Workflow, WorkflowError


TERMINAL_TASK_STATES = {"SUCCESS", "FAILED", "SKIPPED"}
CONDITION_SKIP_KINDS = {"CONDITION_NOT_SELECTED", "CONDITION_PATH_NOT_SELECTED"}


class AlreadyRunningError(RuntimeError):
    pass


class WorkflowRunner:
    def __init__(
        self,
        database: StateDatabase,
        logs: TaskLogManager,
        executor: TaskExecutor | None = None,
        notifier: Notifier | None = None,
        max_parallel_tasks: int = 4,
    ):
        if max_parallel_tasks <= 0:
            raise ValueError("max_parallel_tasks must be positive")
        self.database = database
        self.logs = logs
        self.executor = executor or TaskExecutor()
        self.notifier = notifier or NullNotifier()
        self.max_parallel_tasks = max_parallel_tasks

    def run(
        self,
        workflow: Workflow,
        from_task: str | None = None,
        *,
        run_id: str | None = None,
        trigger_type: str = "manual",
        cancel_event: Event | None = None,
        resume_run_id: str | None = None,
    ) -> tuple[str, str]:
        if from_task and resume_run_id:
            raise WorkflowError("from_task and resume_run_id cannot be used together")
        order = workflow.topological_order()
        previous = (
            self.database.get_run(resume_run_id)
            if resume_run_id
            else self.database.latest_run(workflow.name) if from_task else None
        )
        if (from_task or resume_run_id) and previous is None:
            raise WorkflowError(f"cannot resume {workflow.name!r}: no previous run exists")
        if previous and previous["workflow_name"] != workflow.name:
            raise WorkflowError(
                f"run {previous['run_id']!r} belongs to workflow "
                f"{previous['workflow_name']!r}, not {workflow.name!r}"
            )
        previous_states = self.database.task_states(previous["run_id"]) if previous else {}
        if resume_run_id:
            selected = {
                name
                for name in order
                if name not in previous_states
                or not _is_reusable_previous_state(previous_states[name])
            }
        else:
            selected = workflow.descendants(from_task) if from_task else set(order)
        run_id = run_id or new_run_id()
        self.database.create_run(
            run_id,
            workflow.name,
            order,
            resumed_from_run_id=previous["run_id"] if previous else None,
            from_task=from_task,
            trigger_type=trigger_type,
            task_metadata={
                name: {
                    "description": workflow.tasks[name].description,
                    "depends": workflow.tasks[name].depends,
                    "task_type": workflow.tasks[name].task_type,
                }
                for name in order
            },
        )
        states = {name: "PENDING" for name in order}
        handled_failures: set[str] = set()
        benign_skips: set[str] = set()
        task_cancel_event = cancel_event or Event()

        try:
            if previous:
                self._seed_resume(
                    run_id,
                    workflow.name,
                    order,
                    selected,
                    previous,
                    previous_states,
                    states,
                    handled_failures,
                    benign_skips,
                )
            self._run_tasks(
                workflow,
                run_id,
                order,
                states,
                task_cancel_event,
                handled_failures,
                benign_skips,
            )

            selected_incomplete = any(
                states[name] != "SUCCESS"
                and name not in handled_failures
                and name not in benign_skips
                and workflow.tasks[name].enabled
                for name in selected
            )
            unhandled_failure = any(
                state == "FAILED" and name not in handled_failures
                for name, state in states.items()
            )
            final_status = "FAILED" if unhandled_failure or selected_incomplete else "SUCCESS"
            run_error = "stopped by user" if task_cancel_event.is_set() else None
            self.database.finish_run(run_id, final_status, run_error)
            return run_id, final_status
        except BaseException as exc:
            task_cancel_event.set()
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

    def _run_tasks(
        self,
        workflow: Workflow,
        run_id: str,
        order: list[str],
        states: dict[str, str],
        cancel_event: Event,
        handled_failures: set[str],
        benign_skips: set[str],
    ) -> None:
        order_index = {name: index for index, name in enumerate(order)}
        running: dict[Future, tuple[str, Path]] = {}
        pool = ThreadPoolExecutor(
            max_workers=self.max_parallel_tasks,
            thread_name_prefix="dag-task",
        )
        try:
            while any(state in {"PENDING", "RUNNING"} for state in states.values()):
                self._resolve_unrunnable_tasks(
                    workflow, run_id, order, states, cancel_event, benign_skips
                )
                condition_evaluated = False
                if not cancel_event.is_set():
                    for task_name in order:
                        if len(running) >= self.max_parallel_tasks:
                            break
                        if states[task_name] != "PENDING":
                            continue
                        task = workflow.tasks[task_name]
                        if not all(
                            states[dependency] in TERMINAL_TASK_STATES
                            for dependency in task.depends
                        ):
                            continue
                        if task.is_condition:
                            assert task.condition is not None
                            self.database.set_task_status(run_id, task_name, "RUNNING")
                            states[task_name] = "RUNNING"
                            matched = task.condition.evaluate(states)
                            decision = "success" if matched else "failure"
                            self.database.set_task_status(
                                run_id,
                                task_name,
                                "SUCCESS",
                                condition_result=decision,
                            )
                            states[task_name] = "SUCCESS"
                            for referenced in task.condition.referenced_tasks:
                                if states[referenced] == "FAILED":
                                    handled_failures.add(referenced)
                                    self.database.mark_task_handled(
                                        run_id, referenced, task_name
                                    )
                            unselected = task.failure if matched else task.success
                            for target in unselected:
                                if states[target] == "PENDING":
                                    self._skip(
                                        run_id,
                                        target,
                                        f"condition {task_name} selected {decision} branch",
                                        states,
                                        skip_kind="CONDITION_NOT_SELECTED",
                                        benign_skips=benign_skips,
                                    )
                            condition_evaluated = True
                            continue
                        if task.depends and not any(
                            states[dependency] == "SUCCESS"
                            for dependency in task.depends
                        ):
                            continue
                        if task.command is None:
                            raise RuntimeError(
                                f"command task {task_name!r} has no command"
                            )
                        log_file = self.logs.path_for(
                            workflow.name, run_id, task_name
                        )
                        self.database.set_task_status(
                            run_id,
                            task_name,
                            "RUNNING",
                            log_file=str(log_file),
                        )
                        states[task_name] = "RUNNING"
                        future = pool.submit(
                            self.executor.execute,
                            task,
                            cwd=workflow.task_cwd(task),
                            workflow_setup=workflow.setup_for_current_platform(),
                            log_file=log_file,
                            cancel_event=cancel_event,
                        )
                        running[future] = (task_name, log_file)

                if not running:
                    if condition_evaluated:
                        continue
                    pending = [name for name in order if states[name] == "PENDING"]
                    if pending:
                        raise RuntimeError(
                            f"no runnable tasks remain: {', '.join(pending)}"
                        )
                    break

                completed, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in sorted(
                    completed, key=lambda item: order_index[running[item][0]]
                ):
                    task_name, log_file = running.pop(future)
                    try:
                        result = future.result()
                        status = "SUCCESS" if result.exit_code == 0 else "FAILED"
                        exit_code = result.exit_code
                        error_message = result.error_message
                    except Exception as exc:
                        status = "FAILED"
                        exit_code = None
                        error_message = f"task executor crashed: {exc}"
                    self.database.set_task_status(
                        run_id,
                        task_name,
                        status,
                        exit_code=exit_code,
                        log_file=str(log_file),
                        error_message=error_message,
                    )
                    states[task_name] = status
                    self._notify(
                        TaskEvent(
                            workflow_name=workflow.name,
                            run_id=run_id,
                            task_name=task_name,
                            status=status,
                            log_file=str(log_file),
                            exit_code=exit_code,
                            error_message=error_message,
                        )
                    )
        except BaseException:
            cancel_event.set()
            for future in running:
                future.cancel()
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    def _resolve_unrunnable_tasks(
        self,
        workflow: Workflow,
        run_id: str,
        order: list[str],
        states: dict[str, str],
        cancel_event: Event,
        benign_skips: set[str],
    ) -> None:
        changed = True
        while changed:
            changed = False
            for task_name in order:
                if states[task_name] != "PENDING":
                    continue
                if cancel_event.is_set():
                    self._skip(run_id, task_name, "run stopped by user", states)
                    changed = True
                    continue
                task = workflow.tasks[task_name]
                if not task.enabled:
                    self._skip(run_id, task_name, "task is disabled", states)
                    changed = True
                    continue
                if task.is_condition:
                    if not all(
                        states[dependency] in TERMINAL_TASK_STATES
                        for dependency in task.depends
                    ):
                        continue
                    if task.depends and all(
                        dependency in benign_skips for dependency in task.depends
                    ):
                        self._skip(
                            run_id,
                            task_name,
                            "condition path was not selected",
                            states,
                            skip_kind="CONDITION_PATH_NOT_SELECTED",
                            benign_skips=benign_skips,
                        )
                        changed = True
                    continue
                bad_dependencies = [
                    dependency
                    for dependency in task.depends
                    if states[dependency] == "FAILED"
                    or (
                        states[dependency] == "SKIPPED"
                        and dependency not in benign_skips
                    )
                ]
                if bad_dependencies:
                    detail = ", ".join(
                        f"{name}={states[name]}" for name in bad_dependencies
                    )
                    self._skip(
                        run_id,
                        task_name,
                        f"dependency not successful: {detail}",
                        states,
                    )
                    changed = True
                    continue
                if (
                    task.depends
                    and all(
                        states[dependency] in TERMINAL_TASK_STATES
                        for dependency in task.depends
                    )
                    and all(dependency in benign_skips for dependency in task.depends)
                ):
                    self._skip(
                        run_id,
                        task_name,
                        "all dependency paths were not selected",
                        states,
                        skip_kind="CONDITION_PATH_NOT_SELECTED",
                        benign_skips=benign_skips,
                    )
                    changed = True

    def _seed_resume(
        self,
        run_id,
        workflow_name,
        order,
        selected,
        previous,
        previous_states,
        states,
        handled_failures,
        benign_skips,
    ) -> None:
        for task_name in order:
            if task_name in selected:
                continue
            old = previous_states.get(task_name)
            if old is not None and _is_reusable_previous_state(old):
                log_file = old["log_file"]
                if log_file:
                    try:
                        log_file = str(
                            self.logs.copy_for_run(
                                log_file,
                                workflow_name,
                                run_id,
                                task_name,
                            )
                        )
                    except (OSError, ValueError):
                        # Reusing terminal state must not fail only because an old log is missing.
                        pass
                self.database.set_task_status(
                    run_id,
                    task_name,
                    old["status"],
                    exit_code=old["exit_code"],
                    log_file=log_file,
                    error_message=old["error_message"],
                    reused_from_run_id=previous["run_id"],
                    condition_result=old["condition_result"],
                    handled_by=old["handled_by"],
                    skip_kind=old["skip_kind"],
                )
                states[task_name] = old["status"]
                if old["handled_by"]:
                    handled_failures.add(task_name)
                if old["skip_kind"] in CONDITION_SKIP_KINDS:
                    benign_skips.add(task_name)
            else:
                self._skip(
                    run_id,
                    task_name,
                    f"outside --from scope; previous status was {old['status'] if old else 'MISSING'}",
                    states,
                )

    def _skip(
        self,
        run_id: str,
        task_name: str,
        reason: str,
        states: dict[str, str],
        *,
        skip_kind: str | None = None,
        benign_skips: set[str] | None = None,
    ) -> None:
        self.database.set_task_status(
            run_id,
            task_name,
            "SKIPPED",
            error_message=reason,
            skip_kind=skip_kind,
        )
        states[task_name] = "SKIPPED"
        if skip_kind in CONDITION_SKIP_KINDS and benign_skips is not None:
            benign_skips.add(task_name)

    def _notify(self, event: TaskEvent) -> None:
        try:
            if event.status == "SUCCESS":
                self.notifier.on_task_success(event)
            else:
                self.notifier.on_task_failed(event)
        except Exception as exc:
            print(f"warning: notifier failed for {event.task_name}: {exc}", file=sys.stderr)


def _is_reusable_previous_state(row) -> bool:
    return (
        row["status"] == "SUCCESS"
        or (row["status"] == "FAILED" and bool(row["handled_by"]))
        or (
            row["status"] == "SKIPPED"
            and row["skip_kind"] in CONDITION_SKIP_KINDS
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a database-backed DAG workflow")
    parser.add_argument("--workflow", help="imported workflow ID, for example workflow_000001")
    parser.add_argument("--from", dest="from_task", help="rerun this task and its descendants")
    parser.add_argument("--db", type=Path, default=Path("var") / "scheduler.db")
    parser.add_argument("--logs", type=Path, default=Path("var") / "logs")
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

        if not args.workflow:
            raise WorkflowError("provide --workflow with an imported workflow ID")
        stored = database.get_workflow(args.workflow)
        if stored is None:
            raise WorkflowError(f"workflow not found: {args.workflow}")
        workflow = Workflow.from_yaml(
            stored["definition"],
            name_override=stored["workflow_key"],
            description_override=stored["name"],
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
