from __future__ import annotations

import os
import subprocess
from pathlib import Path
from threading import Event

from .scripting import (
    ExecutionResult,
    append_error,
    build_local_plan,
    default_env,
    remove_temp_script,
    wait_for_process,
    write_log_header,
)
from .workflow import Task


class TaskExecutor:
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
        log_file.parent.mkdir(parents=True, exist_ok=True)
        plan = build_local_plan(task, workflow_setup)

        try:
            with log_file.open("wb") as output:
                write_log_header(output, plan.script)
                process = subprocess.Popen(
                    plan.argv,
                    cwd=cwd,
                    env=default_env(),
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=os.name != "nt",
                    creationflags=(
                        subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                    ),
                )
                return wait_for_process(
                    process,
                    log_file,
                    task.timeout,
                    cancel_event,
                    workflow_timeout_event,
                    workflow_timeout,
                )
        except OSError as exc:
            message = f"could not start command: {exc}"
            append_error(log_file, message)
            return ExecutionResult(127, message)
        finally:
            if plan.temp_script is not None:
                remove_temp_script(plan.temp_script)


__all__ = ["ExecutionResult", "TaskExecutor"]
