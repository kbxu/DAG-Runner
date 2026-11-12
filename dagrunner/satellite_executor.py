"""Master-side adapter that runs workflow tasks on a satellite node."""
from __future__ import annotations

from pathlib import Path
from threading import Event

from .satellite_master import SatelliteManager
from .satellite_protocol import job_spec_from_task, new_job_id
from .scripting import ExecutionResult
from .workflow import Task


class SatelliteTaskExecutor:
    """Implement the task executor interface by dispatching to a satellite."""

    def __init__(self, manager: SatelliteManager, alias: str):
        self.manager = manager
        self.alias = alias

    @property
    def platform(self) -> str:
        config = self.manager.database.get_satellite(self.alias)
        return "windows" if config is not None and config.platform == "windows" else "linux"

    def execute(
        self,
        task: Task,
        *,
        cwd: Path,
        workflow_setup: str,
        log_file: Path,
        cancel_event: Event | None = None,
        workflow_timeout_event: Event | None = None,
        workflow_timeout: int | None = None,
        run_id: str = "",
        workflow_name: str = "",
    ) -> ExecutionResult:
        spec = job_spec_from_task(
            task,
            job_id=new_job_id(),
            workflow_name=workflow_name or task.name,
            run_id=run_id or "unknown",
            setup=workflow_setup,
            workdir="",
        )
        return self.manager.execute(
            self.alias,
            spec,
            log_file,
            task_timeout=task.timeout,
            cancel_event=cancel_event,
            workflow_timeout_event=workflow_timeout_event,
            workflow_timeout=workflow_timeout,
        )
