from __future__ import annotations

import sys
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .database import StateDatabase
from .logger import TaskLogManager
from .runner import AlreadyRunningError, WorkflowRunner, new_run_id, workflow_lock
from .workflow import Workflow, WorkflowError


class ServiceError(RuntimeError):
    pass


class WorkflowRegistry:
    def __init__(self, config_dir: str | Path):
        self.config_dir = Path(config_dir).resolve()
        self.workflows: dict[str, Workflow] = {}
        self.paths: dict[str, Path] = {}
        self.errors: dict[str, str] = {}
        self.refresh()

    def refresh(self) -> None:
        workflows: dict[str, Workflow] = {}
        paths: dict[str, Path] = {}
        errors: dict[str, str] = {}
        for path in sorted(self.config_dir.rglob("*.yaml")):
            try:
                workflow = Workflow.load(path)
                if workflow.name in workflows:
                    raise WorkflowError(
                        f"duplicate workflow name; also defined in {paths[workflow.name]}"
                    )
                workflows[workflow.name] = workflow
                paths[workflow.name] = path
            except (OSError, WorkflowError) as exc:
                errors[str(path)] = str(exc)
        self.workflows, self.paths, self.errors = workflows, paths, errors

    def get(self, workflow_name: str) -> Workflow:
        try:
            return self.workflows[workflow_name]
        except KeyError as exc:
            raise ServiceError(f"workflow not found: {workflow_name}") from exc


@dataclass
class ActiveRun:
    workflow_name: str
    cancel_event: Event
    future: Future | None = None


class ExecutionService:
    def __init__(
        self,
        database: StateDatabase,
        registry: WorkflowRegistry,
        logs: TaskLogManager,
        max_workers: int = 4,
    ):
        self.database = database
        self.registry = registry
        self.logs = logs
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="dag-run")
        self._active: dict[str, ActiveRun] = {}
        self._lock = Lock()

    def start_run(
        self, workflow_name: str, *, from_task: str | None = None, trigger_type: str = "manual"
    ) -> str:
        workflow = self.registry.get(workflow_name)
        if from_task:
            workflow.descendants(from_task)  # Fail before scheduling the background job.
        run_id = new_run_id()
        active = ActiveRun(workflow_name=workflow_name, cancel_event=Event())
        with self._lock:
            if any(item.workflow_name == workflow_name for item in self._active.values()):
                raise ServiceError(f"workflow {workflow_name!r} is already running")
            self._active[run_id] = active
            try:
                active.future = self.pool.submit(
                    self._run,
                    workflow,
                    run_id,
                    from_task,
                    trigger_type,
                    active.cancel_event,
                )
            except Exception:
                self._active.pop(run_id, None)
                raise
            future = active.future
        future.add_done_callback(lambda completed: self._completed(run_id, completed))
        return run_id

    def _run(
        self,
        workflow: Workflow,
        run_id: str,
        from_task: str | None,
        trigger_type: str,
        cancel_event: Event,
    ) -> tuple[str, str]:
        lock_path = self.database.path.parent / "locks" / f"{workflow.name}.lock"
        with workflow_lock(lock_path):
            return WorkflowRunner(self.database, self.logs).run(
                workflow,
                from_task,
                run_id=run_id,
                trigger_type=trigger_type,
                cancel_event=cancel_event,
            )

    def _completed(self, run_id: str, future: Future) -> None:
        with self._lock:
            self._active.pop(run_id, None)
        try:
            future.result()
        except (AlreadyRunningError, WorkflowError, OSError, ValueError, KeyError) as exc:
            print(f"workflow run {run_id} failed to start: {exc}", file=sys.stderr)
        except Exception as exc:
            print(f"workflow run {run_id} crashed: {exc}", file=sys.stderr)

    def stop_run(self, run_id: str) -> None:
        with self._lock:
            active = self._active.get(run_id)
            if active is None:
                raise ServiceError(f"run is not active: {run_id}")
            active.cancel_event.set()

    def rerun(self, run_id: str) -> str:
        run = self.database.get_run(run_id)
        if run is None:
            raise ServiceError(f"run not found: {run_id}")
        return self.start_run(run["workflow_name"], trigger_type="rerun")

    def resume_failed(self, run_id: str) -> str:
        run = self.database.get_run(run_id)
        if run is None:
            raise ServiceError(f"run not found: {run_id}")
        workflow = self.registry.get(run["workflow_name"])
        states = self.database.task_states(run_id)
        failed_task = next(
            (
                name
                for name in workflow.topological_order()
                if name in states and states[name]["status"] == "FAILED"
            ),
            None,
        )
        if failed_task is None:
            raise ServiceError("this run has no FAILED task to resume from")
        # WorkflowRunner resumes from the latest run. Prevent surprising behavior if an older row was clicked.
        latest = self.database.latest_run(workflow.name)
        if latest is None or latest["run_id"] != run_id:
            raise ServiceError("resume is only allowed from the latest run of this workflow")
        return self.start_run(
            workflow.name, from_task=failed_task, trigger_type="resume"
        )

    def active_run_ids(self) -> set[str]:
        with self._lock:
            return set(self._active)

    def recover_orphaned_runs(self) -> int:
        recovered = 0
        for workflow_name in self.database.running_workflow_names():
            lock_path = self.database.path.parent / "locks" / f"{workflow_name}.lock"
            try:
                with workflow_lock(lock_path):
                    recovered += self.database.mark_orphaned(workflow_name)
            except AlreadyRunningError:
                # A CLI or another service instance still owns this run; it is not orphaned.
                continue
        return recovered

    def shutdown(self) -> None:
        with self._lock:
            for active in self._active.values():
                active.cancel_event.set()
        self.pool.shutdown(wait=True, cancel_futures=False)


class ScheduleService:
    def __init__(
        self,
        database: StateDatabase,
        registry: WorkflowRegistry,
        executions: ExecutionService,
    ):
        self.database = database
        self.registry = registry
        self.executions = executions
        self.scheduler = BackgroundScheduler()

    def start(self) -> None:
        self._import_yaml_defaults()
        for schedule in self.database.list_schedules():
            if schedule["enabled"] and schedule["workflow_name"] in self.registry.workflows:
                try:
                    self._install_job(
                        schedule["workflow_name"],
                        schedule["cron_expression"],
                        schedule["timezone"],
                    )
                except (ValueError, TypeError, KeyError) as exc:
                    print(
                        f"invalid schedule for {schedule['workflow_name']}: {exc}",
                        file=sys.stderr,
                    )
        self.scheduler.start()

    def _import_yaml_defaults(self) -> None:
        for workflow in self.registry.workflows.values():
            if workflow.schedule and self.database.get_schedule(workflow.name) is None:
                self.database.upsert_schedule(
                    workflow.name,
                    workflow.schedule.cron,
                    workflow.schedule.timezone,
                    workflow.schedule.enabled,
                )

    def update(
        self, workflow_name: str, cron_expression: str, timezone_name: str, enabled: bool
    ) -> None:
        self.registry.get(workflow_name)
        try:
            CronTrigger.from_crontab(cron_expression, timezone=timezone_name)
        except (ValueError, TypeError, KeyError) as exc:
            raise ServiceError(f"invalid cron/timezone: {exc}") from exc
        self.database.upsert_schedule(
            workflow_name, cron_expression, timezone_name, enabled
        )
        if self.scheduler.get_job(self._job_id(workflow_name)):
            self.scheduler.remove_job(self._job_id(workflow_name))
        if enabled:
            self._install_job(workflow_name, cron_expression, timezone_name)

    def _install_job(self, workflow_name: str, cron_expression: str, timezone_name: str) -> None:
        trigger = CronTrigger.from_crontab(cron_expression, timezone=timezone_name)
        self.scheduler.add_job(
            self._scheduled_run,
            trigger=trigger,
            args=[workflow_name],
            id=self._job_id(workflow_name),
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=300,
        )

    def _scheduled_run(self, workflow_name: str) -> None:
        try:
            self.executions.start_run(workflow_name, trigger_type="schedule")
        except ServiceError as exc:
            print(f"scheduled workflow {workflow_name} skipped: {exc}", file=sys.stderr)

    def next_run_time(self, workflow_name: str) -> str | None:
        job = self.scheduler.get_job(self._job_id(workflow_name))
        next_time = getattr(job, "next_run_time", None) if job else None
        return next_time.isoformat() if next_time else None

    @staticmethod
    def _job_id(workflow_name: str) -> str:
        return f"workflow:{workflow_name}"

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
