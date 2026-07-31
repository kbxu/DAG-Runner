from __future__ import annotations

import sys
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Event, Lock

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .database import StateDatabase
from .logger import TaskLogManager
from .runner import AlreadyRunningError, WorkflowRunner, new_run_id, workflow_lock
from .workflow import Workflow, WorkflowError


DEFAULT_SCHEDULE_CRON = "0 18 * * mon-fri"
DEFAULT_SCHEDULE_TIMEZONE = "Asia/Shanghai"


class ServiceError(RuntimeError):
    pass


class WorkflowRegistry:
    def __init__(self, database: StateDatabase):
        self.database = database
        self.workflows: dict[str, Workflow] = {}
        self.rows: dict[str, object] = {}
        self.errors: dict[str, str] = {}
        self.refresh()

    def refresh(self) -> None:
        workflows: dict[str, Workflow] = {}
        rows: dict[str, object] = {}
        errors: dict[str, str] = {}
        for row in self.database.list_workflows():
            key = row["workflow_key"]
            try:
                workflow = Workflow.from_yaml(
                    row["definition"],
                    name_override=key,
                    description_override=row["name"],
                )
                workflows[key] = workflow
                rows[key] = row
            except WorkflowError as exc:
                errors[key] = str(exc)
        self.workflows, self.rows, self.errors = workflows, rows, errors

    def get(self, workflow_name: str) -> Workflow:
        try:
            return self.workflows[workflow_name]
        except KeyError as exc:
            raise ServiceError(f"workflow not found: {workflow_name}") from exc

    def definition(self, workflow_name: str) -> str:
        self.refresh()
        self.get(workflow_name)
        return self.rows[workflow_name]["definition"]

    def delete(self, workflow_name: str) -> None:
        self.database.delete_workflow(workflow_name)
        self.refresh()


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
        self,
        workflow_name: str,
        *,
        from_task: str | None = None,
        trigger_type: str = "manual",
        resume_run_id: str | None = None,
    ) -> str:
        self.registry.refresh()
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
                    resume_run_id,
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
        resume_run_id: str | None,
    ) -> tuple[str, str]:
        lock_path = self.database.path.parent / "locks" / f"{workflow.name}.lock"
        with workflow_lock(lock_path):
            return WorkflowRunner(self.database, self.logs).run(
                workflow,
                from_task,
                run_id=run_id,
                trigger_type=trigger_type,
                cancel_event=cancel_event,
                resume_run_id=resume_run_id,
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
        if run["status"] != "FAILED":
            raise ServiceError("only FAILED runs can be resumed")
        latest = self.database.latest_run(run["workflow_name"])
        if latest is None or latest["run_id"] != run_id:
            raise ServiceError("resume is only allowed from the latest run of this workflow")
        return self.start_run(
            run["workflow_name"],
            trigger_type="resume",
            resume_run_id=run_id,
        )

    def active_run_ids(self) -> set[str]:
        with self._lock:
            return set(self._active)

    def is_workflow_active(self, workflow_name: str) -> bool:
        with self._lock:
            return any(
                item.workflow_name == workflow_name for item in self._active.values()
            )

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

    def update(
        self, workflow_name: str, cron_expression: str, timezone_name: str, enabled: bool
    ) -> None:
        workflow = self.registry.get(workflow_name)
        try:
            CronTrigger.from_crontab(cron_expression, timezone=timezone_name)
        except (ValueError, TypeError, KeyError) as exc:
            raise ServiceError(f"invalid cron/timezone: {exc}") from exc
        self.database.upsert_schedule(
            workflow_name,
            workflow.description,
            cron_expression,
            timezone_name,
            enabled,
        )
        if self.scheduler.get_job(self._job_id(workflow_name)):
            self.scheduler.remove_job(self._job_id(workflow_name))
        if enabled:
            self._install_job(workflow_name, cron_expression, timezone_name)

    def delete(self, workflow_name: str) -> None:
        job_id = self._job_id(workflow_name)
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)
        self.database.delete_schedule(workflow_name)

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
