#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections.abc import Iterator
from concurrent.futures import FIRST_COMPLETED, Future, wait
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from threading import Event, Timer

from .database import StateDatabase
from .executor import TaskExecutor
from .logger import TaskLogManager
from .pool import DaemonPool
from .notifier import Notifier, NullNotifier, TaskFailure, WorkflowEvent, load_notifier
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
        remote_executor: TaskExecutor | None = None,
    ):
        if max_parallel_tasks <= 0:
            raise ValueError("max_parallel_tasks must be positive")
        self.database = database
        self.logs = logs
        self.executor = executor or TaskExecutor()
        self.notifier = notifier or NullNotifier()
        self.max_parallel_tasks = max_parallel_tasks
        self.remote_executor = remote_executor
        self._runner_alias = "local"

    def run(
        self,
        workflow: Workflow,
        from_task: str | None = None,
        *,
        run_id: str | None = None,
        trigger_type: str = "manual",
        cancel_event: Event | None = None,
        resume_run_id: str | None = None,
        only_task: bool = False,
        runner_alias: str = "local",
    ) -> tuple[str, str]:
        self._runner_alias = runner_alias or "local"
        if from_task and resume_run_id:
            raise WorkflowError("from_task and resume_run_id cannot be used together")
        if only_task and not from_task:
            raise WorkflowError("only_task needs a from_task")
        order = workflow.topological_order()
        previous = self.database.get_run(resume_run_id) if resume_run_id else None
        if resume_run_id and previous is None:
            raise WorkflowError(f"cannot resume {workflow.name!r}: no previous run exists")
        if previous and previous["workflow_name"] != workflow.name:
            raise WorkflowError(
                f"run {previous['run_id']!r} belongs to workflow "
                f"{previous['workflow_name']!r}, not {workflow.name!r}"
            )
        if resume_run_id:
            # A resume repeats the scope recorded by the run it resumes, so resuming a
            # single-node run runs that node again, and resuming a subtree runs it again.
            scope = _decode_scope(previous["scope"], order)
        elif from_task and only_task:
            scope = [from_task]
        elif from_task:
            scope = [name for name in order if name in workflow.descendants(from_task)]
        else:
            scope = list(order)
        selected = {name for name in scope if name in workflow.tasks}
        previous_states = self.database.task_states(previous["run_id"]) if previous else {}
        run_id = run_id or new_run_id()
        self.database.create_run(
            run_id,
            workflow.name,
            order,
            resumed_from_run_id=previous["run_id"] if previous else None,
            from_task=from_task,
            trigger_type=trigger_type,
            scope=scope,
            runner=self._runner_alias,
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
        reused: set[str] = set()
        task_cancel_event = cancel_event or Event()
        workflow_timeout_event = Event()
        workflow_timeout_timer: Timer | None = None
        if workflow.timeout is not None:
            def timeout_workflow() -> None:
                workflow_timeout_event.set()
                task_cancel_event.set()

            workflow_timeout_timer = Timer(workflow.timeout, timeout_workflow)
            workflow_timeout_timer.daemon = True
            workflow_timeout_timer.start()

        try:
            if previous:
                reused = self._seed_resume(
                    run_id, order, selected, previous_states, states
                )
            for name in order:
                if name not in selected and states[name] == "PENDING":
                    self._skip(
                        run_id,
                        name,
                        "不在本次运行范围内",
                        states,
                        skip_kind="OUTSIDE_SCOPE",
                    )
            self._run_tasks(
                workflow,
                run_id,
                order,
                states,
                task_cancel_event,
                workflow_timeout_event,
                handled_failures,
                benign_skips,
                selected,
                reused,
            )

            selected_incomplete = any(
                states[name] != "SUCCESS"
                and name not in reused
                and name not in handled_failures
                and name not in benign_skips
                and workflow.tasks[name].enabled
                for name in selected
            )
            unhandled_failure = any(
                state == "FAILED" and name not in handled_failures
                for name, state in states.items()
            )
            final_status = (
                "FAILED"
                if workflow_timeout_event.is_set()
                or unhandled_failure
                or selected_incomplete
                else "SUCCESS"
            )
            if workflow_timeout_event.is_set():
                run_error = f"workflow timed out after {workflow.timeout} seconds"
            else:
                run_error = "stopped by user" if task_cancel_event.is_set() else None
            self.database.finish_run(run_id, final_status, run_error)
            self._notify_workflow(workflow, run_id, final_status, run_error)
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
            self._notify_workflow(workflow, run_id, "FAILED", str(exc))
            raise
        finally:
            if workflow_timeout_timer is not None:
                workflow_timeout_timer.cancel()

    def _run_tasks(
        self,
        workflow: Workflow,
        run_id: str,
        order: list[str],
        states: dict[str, str],
        cancel_event: Event,
        workflow_timeout_event: Event,
        handled_failures: set[str],
        benign_skips: set[str],
        selected: set[str],
        reused: set[str] | None = None,
    ) -> None:
        reused = reused or set()
        use_remote = (
            self._runner_alias not in ("", "local") and self.remote_executor is not None
        )
        executor = self.remote_executor if use_remote else self.executor
        workflow_setup = (
            workflow.setup_for_platform("linux")
            if use_remote
            else workflow.setup_for_current_platform()
        )
        order_index = {name: index for index, name in enumerate(order)}
        running: dict[Future, tuple[str, Path]] = {}
        pool = DaemonPool(
            max_workers=self.max_parallel_tasks,
            thread_name_prefix="dag-task",
        )
        try:
            while any(state in {"PENDING", "RUNNING"} for state in states.values()):
                self._resolve_unrunnable_tasks(
                    workflow,
                    run_id,
                    order,
                    states,
                    cancel_event,
                    workflow_timeout_event,
                    benign_skips,
                    selected,
                    reused,
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
                            for dependency in self._scope_dependencies(task, selected)
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
                        dependencies = self._scope_dependencies(task, selected)
                        if dependencies and not any(
                            self._is_satisfied(states, reused, dependency)
                            for dependency in dependencies
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
                            executor.execute,
                            task,
                            cwd=workflow.task_cwd(task),
                            workflow_setup=workflow_setup,
                            log_file=log_file,
                            cancel_event=cancel_event,
                            workflow_timeout_event=workflow_timeout_event,
                            workflow_timeout=workflow.timeout,
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
        workflow_timeout_event: Event,
        benign_skips: set[str],
        selected: set[str],
        reused: set[str] | None = None,
    ) -> None:
        reused = reused or set()
        changed = True
        while changed:
            changed = False
            for task_name in order:
                if states[task_name] != "PENDING":
                    continue
                if cancel_event.is_set():
                    reason = (
                        f"workflow timed out after {workflow.timeout} seconds"
                        if workflow_timeout_event.is_set()
                        else "run stopped by user"
                    )
                    self._skip(run_id, task_name, reason, states)
                    changed = True
                    continue
                task = workflow.tasks[task_name]
                if not task.enabled:
                    self._skip(run_id, task_name, "task is disabled", states)
                    changed = True
                    continue
                dependencies = self._scope_dependencies(task, selected)
                if task.is_condition:
                    if not all(
                        states[dependency] in TERMINAL_TASK_STATES
                        for dependency in dependencies
                    ):
                        continue
                    if dependencies and all(
                        dependency in benign_skips for dependency in dependencies
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
                    for dependency in dependencies
                    if not self._is_satisfied(states, reused, dependency)
                    and (
                        states[dependency] == "FAILED"
                        or (
                            states[dependency] == "SKIPPED"
                            and dependency not in benign_skips
                        )
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
                    dependencies
                    and all(
                        states[dependency] in TERMINAL_TASK_STATES
                        for dependency in dependencies
                    )
                    and all(dependency in benign_skips for dependency in dependencies)
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

    @staticmethod
    def _scope_dependencies(task: Task, selected: set[str]) -> tuple[str, ...]:
        """Dependencies that gate this run; tasks outside the scope are ignored."""
        return tuple(dependency for dependency in task.depends if dependency in selected)

    @staticmethod
    def _is_satisfied(states: dict[str, str], reused: set[str], task_name: str) -> bool:
        """A dependency counts when it succeeded now or succeeded in an earlier run."""
        return states[task_name] == "SUCCESS" or task_name in reused

    def _seed_resume(
        self, run_id, order, selected, previous_states, states
    ) -> set[str]:
        """Skip the in-scope tasks that already succeeded; they still satisfy dependents."""
        reused: set[str] = set()
        for task_name in order:
            if task_name not in selected:
                continue
            old = previous_states.get(task_name)
            if old is None or old["status"] != "SUCCESS":
                continue
            self._skip(
                run_id,
                task_name,
                "上次运行已成功，本次未重跑",
                states,
                skip_kind="REUSED",
            )
            reused.add(task_name)
        return reused

    def _skip(
        self,
        run_id,
        task_name,
        reason,
        states: dict[str, str],
        *,
        skip_kind: str | None = None,
        benign_skips: set[str] | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> None:
        self.database.set_task_status(
            run_id,
            task_name,
            "SKIPPED",
            error_message=reason,
            skip_kind=skip_kind,
            start_time=start_time,
            end_time=end_time,
        )
        states[task_name] = "SKIPPED"
        if skip_kind in CONDITION_SKIP_KINDS and benign_skips is not None:
            benign_skips.add(task_name)

    def _notify_workflow(
        self,
        workflow: Workflow,
        run_id: str,
        status: str,
        error_message: str | None,
    ) -> None:
        notification = workflow.email_notification
        if not notification.should_send(status):
            return
        try:
            failures = tuple(
                TaskFailure(
                    task_name=row["task_name"],
                    exit_code=row["exit_code"],
                    error_message=row["error_message"],
                    log_file=row["log_file"],
                )
                for row in self.database.task_states(run_id).values()
                if row["status"] == "FAILED"
            )
            event = WorkflowEvent(
                workflow_name=workflow.name,
                run_id=run_id,
                status=status,
                recipients=notification.recipients,
                error_message=error_message,
                failed_tasks=failures,
            )
            if status == "SUCCESS":
                self.notifier.on_workflow_success(event)
            else:
                self.notifier.on_workflow_failed(event)
        except Exception as exc:
            print(
                f"warning: notifier failed for workflow {workflow.name}: {exc}",
                file=sys.stderr,
            )


def _decode_scope(raw: str | None, order: list[str]) -> list[str]:
    """Read the recorded scope of a run, keeping only tasks the workflow still has."""
    if not raw:
        return list(order)
    try:
        names = json.loads(raw)
    except (TypeError, ValueError):
        return list(order)
    if not isinstance(names, list):
        return list(order)
    recorded = {str(name) for name in names}
    return [name for name in order if name in recorded]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a database-backed DAG workflow")
    parser.add_argument(
        "--workflow", help="imported workflow ID, for example workflow_a1b2c3d4e5f6"
    )
    parser.add_argument(
        "--from",
        dest="from_task",
        help="run this task and its descendants (independent of earlier runs)",
    )
    parser.add_argument("--db", type=Path, default=Path("var") / "scheduler.db")
    parser.add_argument("--logs", type=Path, default=Path("var") / "logs")
    parser.add_argument(
        "--notifier-config",
        type=Path,
        help="email notifier YAML/JSON (default: discover beside --db)",
    )
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
        runner_alias = stored["runner"] or "local"
        remote_executor = None
        if runner_alias != "local":
            from .remote import RemoteTaskExecutor

            config = database.get_remote(runner_alias)
            if config is None:
                raise WorkflowError(f"remote {runner_alias!r} not found")
            remote_executor = RemoteTaskExecutor(config)
        lock_path = args.db.resolve().parent / "locks" / f"{workflow.name}.lock"
        with workflow_lock(lock_path):
            recovered = database.mark_orphaned(workflow.name)
            if recovered:
                print(f"recovered {recovered} interrupted run(s)", file=sys.stderr)
            notifier = load_notifier(
                args.notifier_config, var_dir=args.db.resolve().parent
            )
            run_id, status = WorkflowRunner(
                database,
                TaskLogManager(args.logs),
                notifier=notifier,
                remote_executor=remote_executor,
            ).run(workflow, args.from_task, runner_alias=runner_alias)
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
