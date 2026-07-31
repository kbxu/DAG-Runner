from __future__ import annotations

import os
import shlex
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from .workflow import Task


@dataclass(frozen=True)
class ExecutionResult:
    exit_code: int
    error_message: str | None = None


class TaskExecutor:
    def execute(
        self,
        task: Task,
        *,
        cwd: Path,
        workflow_setup: str,
        log_file: Path,
        cancel_event: Event | None = None,
    ) -> ExecutionResult:
        environment = os.environ.copy()
        if isinstance(task.command, str):
            task_command = task.command
            if task.args:
                task_command += " " + " ".join(shlex.quote(arg) for arg in task.args)
        else:
            argv = [*task.command, *task.args]
            task_command = _shell_join(argv)

        if workflow_setup:
            # Shell activation/export must happen in the same process that starts the task.
            command = _shell_command(workflow_setup, task_command)
        elif isinstance(task.command, str):
            command = _shell_command("", task_command)
        else:
            command = argv

        log_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            with log_file.open("wb") as output:
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=environment,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=os.name != "nt",
                    creationflags=(
                        subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                    ),
                )
                started = time.monotonic()
                while process.poll() is None:
                    cancelled = cancel_event.wait(0.2) if cancel_event else False
                    if cancel_event is None:
                        time.sleep(0.2)
                    if process.poll() is not None:
                        break
                    if cancelled:
                        _terminate_process(process)
                        message = "task stopped by user"
                        _append_error(log_file, message)
                        return ExecutionResult(130, message)
                    if task.timeout and time.monotonic() - started >= task.timeout:
                        _terminate_process(process)
                        message = f"task timed out after {task.timeout} seconds"
                        _append_error(log_file, message)
                        return ExecutionResult(124, message)
            if process.returncode == 0:
                return ExecutionResult(0)
            return ExecutionResult(
                process.returncode, f"command exited with code {process.returncode}"
            )
        except OSError as exc:
            message = f"could not start command: {exc}"
            _append_error(log_file, message)
            return ExecutionResult(127, message)


def _append_error(log_file: Path, message: str) -> None:
    with log_file.open("ab") as output:
        output.write((f"\n[mini-scheduler] {message}\n").encode("utf-8", errors="replace"))


def _shell_command(setup: str, task_command: str) -> list[str]:
    if os.name == "nt":
        script = "$ErrorActionPreference = 'Stop'\n"
        if setup:
            script += setup + "\n"
        script += task_command
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script]
    script = "set -e\n"
    if setup:
        script += setup + "\n"
    script += task_command
    return ["/bin/bash", "-lc", script]


def _shell_join(argv: list[str]) -> str:
    if os.name == "nt":
        return "& " + " ".join(
            "'" + argument.replace("'", "''") + "'" for argument in argv
        )
    return shlex.join(argv)


def _terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            except OSError:
                process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
