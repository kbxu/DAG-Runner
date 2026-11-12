"""Wire types and job script construction for the satellite protocol."""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .scripting import (
    ScriptPlan,
    cmd_encoding,
    cmd_quote,
    join_args,
    ps_quote,
    quote_argument,
    shell_workdir,
    write_temp_script,
)
from .workflow import Task


SATELLITE_API_PREFIX = "/api/satellite/v1"
AGENT_VERSION = "1.0.0"
MASTER_ALIAS = "master"

PATH_HEALTH = f"{SATELLITE_API_PREFIX}/health"
PATH_PING = f"{SATELLITE_API_PREFIX}/ping"
PATH_NONCE = f"{SATELLITE_API_PREFIX}/nonce"
PATH_REGISTER = f"{SATELLITE_API_PREFIX}/register"
PATH_STREAM = f"{SATELLITE_API_PREFIX}/stream"
PATH_HEARTBEAT = f"{SATELLITE_API_PREFIX}/heartbeat"
PATH_JOBS = f"{SATELLITE_API_PREFIX}/jobs"
PATH_JOB = f"{SATELLITE_API_PREFIX}/jobs/{{job_id}}"
PATH_JOB_CHUNKS = f"{SATELLITE_API_PREFIX}/jobs/{{job_id}}/chunks"
PATH_JOB_RESULT = f"{SATELLITE_API_PREFIX}/jobs/{{job_id}}/result"
PATH_JOB_CANCEL = f"{SATELLITE_API_PREFIX}/jobs/{{job_id}}/cancel"
ROUTE_JOB = f"{SATELLITE_API_PREFIX}/jobs/<job_id>"
ROUTE_JOB_CANCEL = f"{SATELLITE_API_PREFIX}/jobs/<job_id>/cancel"

JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_SUCCESS = "success"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"
JOB_ERROR = "error"
JOB_STATUSES = (JOB_PENDING, JOB_RUNNING, JOB_SUCCESS, JOB_FAILED, JOB_CANCELLED, JOB_ERROR)

TASK_SHELLS = ("bash", "sh", "cmd", "powershell")
DEFAULT_PLATFORM_SHELLS = {"linux": "bash", "windows": "cmd"}

SSE_EVENT_JOB = "job"
SSE_EVENT_CANCEL = "cancel"
SSE_EVENT_PING = "ping"

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


class ProtocolError(ValueError):
    """Raised when an incoming job specification is invalid."""


@dataclass(frozen=True)
class SatelliteConfig:
    """A satellite node as configured on the master."""

    alias: str
    name: str = ""
    mode: str = "auto"
    url: str = ""
    token: str = ""
    host_key_policy: str = "accept-new"
    cert_fingerprint: str = ""
    status: str = "offline"
    last_seen: str | None = None
    last_error: str | None = None
    platform: str = ""
    hostname: str = ""
    agent_version: str = ""


@dataclass
class JobSpec:
    """A unit of work dispatched from the master to a satellite."""

    job_id: str
    workflow_name: str
    run_id: str
    task_name: str
    command: str | tuple[str, ...]
    args: tuple[str, ...] = ()
    shell: str | None = None
    setup: str = ""
    workdir: str = ""
    timeout: int | None = None
    env: dict[str, str] = field(default_factory=dict)

    @property
    def platform_shell(self) -> str:
        platform = "windows" if os.name == "nt" else "linux"
        return self.shell or DEFAULT_PLATFORM_SHELLS[platform]

    def to_json(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "workflow_name": self.workflow_name,
            "run_id": self.run_id,
            "task_name": self.task_name,
            "command": self.command,
            "args": list(self.args),
            "shell": self.shell,
            "setup": self.setup,
            "workdir": self.workdir,
            "timeout": self.timeout,
            "env": dict(self.env),
        }

    @classmethod
    def from_json(cls, data: Any) -> "JobSpec":
        if not isinstance(data, dict):
            raise ProtocolError("job spec must be an object")
        job_id = _require_id(data.get("job_id"), "job_id")
        workflow_name = _require_id(data.get("workflow_name"), "workflow_name")
        run_id = _require_id(data.get("run_id"), "run_id")
        task_name = _require_id(data.get("task_name"), "task_name")
        command = data.get("command")
        if isinstance(command, list) and command and all(
            isinstance(item, str) for item in command
        ):
            command = tuple(command)
        elif not isinstance(command, str) or not command.strip():
            raise ProtocolError("job command must be a non-empty string or string list")
        args = data.get("args", [])
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise ProtocolError("job args must be a list of strings")
        shell = data.get("shell")
        if shell is not None and shell not in TASK_SHELLS:
            raise ProtocolError(f"job shell must be one of: {', '.join(TASK_SHELLS)}")
        setup = data.get("setup", "")
        if not isinstance(setup, str):
            raise ProtocolError("job setup must be a string")
        workdir = data.get("workdir", "")
        if not isinstance(workdir, str):
            raise ProtocolError("job workdir must be a string")
        timeout = data.get("timeout")
        if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0
        ):
            raise ProtocolError("job timeout must be a positive integer")
        env = data.get("env", {})
        if not isinstance(env, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in env.items()
        ):
            raise ProtocolError("job env must map strings to strings")
        return cls(
            job_id=job_id,
            workflow_name=workflow_name,
            run_id=run_id,
            task_name=task_name,
            command=command,
            args=tuple(args),
            shell=shell,
            setup=setup,
            workdir=workdir,
            timeout=timeout,
            env=dict(env),
        )


def _require_id(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ProtocolError(f"{field_name} may contain only letters, digits, _, . and -")
    return value


def job_spec_from_task(
    task: Task,
    *,
    job_id: str,
    workflow_name: str,
    run_id: str,
    setup: str,
    workdir: str,
    timeout: int | None = None,
) -> JobSpec:
    """Build a dispatchable job from a workflow task."""
    return JobSpec(
        job_id=job_id,
        workflow_name=workflow_name,
        run_id=run_id,
        task_name=task.name,
        command=task.command if isinstance(task.command, str) else tuple(task.command),
        args=tuple(task.args),
        shell=task.shell,
        setup=setup,
        workdir=workdir,
        timeout=timeout if timeout is not None else task.timeout,
    )


def render_command(spec: JobSpec, shell: str) -> str:
    if isinstance(spec.command, str):
        text = spec.command
        if spec.args:
            text += " " + " ".join(quote_argument(arg, shell) for arg in spec.args)
        return text
    return join_args([*spec.command, *spec.args], shell)


def new_job_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def build_job_plan(spec: JobSpec, default_workdir: str = "") -> ScriptPlan:
    """Build the platform script and argv for a job on this machine."""
    shell = spec.platform_shell
    workdir = spec.workdir or default_workdir
    command = render_command(spec, shell)
    if os.name == "nt":
        return _build_windows_job_plan(spec, command, shell, workdir)
    script_lines = ["set -e"]
    if workdir:
        script_lines.append(f"cd {shell_workdir(workdir)}")
    if spec.setup:
        script_lines.append(spec.setup)
    script_lines.append(command)
    script = "\n".join(script_lines)
    return ScriptPlan([shell, "-lc", script], script, None, shell)


def _build_windows_job_plan(
    spec: JobSpec, command: str, shell: str, workdir: str = ""
) -> ScriptPlan:
    if shell == "powershell":
        if not isinstance(spec.command, str):
            command = "& " + command
        lines = ["$ErrorActionPreference = 'Stop'"]
        if workdir:
            lines.append(f"Set-Location -LiteralPath {ps_quote(workdir)}")
        if spec.setup:
            lines.extend(spec.setup.splitlines())
        lines.append(command)
        script = "\n".join(lines)
        suffix, encoding = ".ps1", "utf-8-sig"
        argv = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
        ]
    else:
        lines = []
        if workdir:
            lines.append(f"cd /d {cmd_quote(workdir)}")
        if spec.setup:
            lines.extend(spec.setup.splitlines())
        lines.append(command)
        script = "\n".join(part.strip() for part in lines if part.strip())
        suffix, encoding = ".bat", cmd_encoding()
        argv = ["cmd.exe", "/c"]
    temp_script = write_temp_script(script, suffix, encoding)
    return ScriptPlan([*argv, str(temp_script)], script, temp_script, shell)


def sse_event(event: str, data: dict[str, Any]) -> str:
    """Format one Server-Sent Events frame."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
