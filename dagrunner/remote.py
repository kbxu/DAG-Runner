from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from concurrent.futures import as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Thread

from apscheduler.schedulers.background import BackgroundScheduler

from .executor import (
    ExecutionResult,
    _append_error,
    _wait_for_process,
    write_log_header,
)
from .pool import DaemonPool
from .workflow import Task


HOST_KEY_POLICIES = ("accept-new", "yes", "no", "ask")
DEFAULT_HOST_KEY_POLICY = "accept-new"
DEFAULT_CONNECT_TIMEOUT = 10


@dataclass(frozen=True)
class RemoteSettings:
    """Process-wide SSH defaults shared by every configured remote host."""

    ssh_path: str = ""
    key_path: str = ""
    host_key_policy: str = DEFAULT_HOST_KEY_POLICY
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT


@dataclass(frozen=True)
class RemoteConfig:
    alias: str
    username: str
    host: str
    port: int = 22
    workdir: str = ""
    ssh_path: str = ""
    key_path: str = ""
    host_key_policy: str = DEFAULT_HOST_KEY_POLICY
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT


@dataclass(frozen=True)
class RemoteCheckResult:
    reachable: bool
    error_message: str | None = None
    latency_ms: int | None = None


def detect_ssh_path() -> str:
    found = shutil.which("ssh")
    if found:
        return found
    if os.name == "nt":
        candidate = Path(r"C:\Windows\System32\OpenSSH\ssh.exe")
        if candidate.is_file():
            return str(candidate)
    return ""


def detect_key_path() -> str:
    directory = Path.home() / ".ssh"
    for name in ("id_rsa", "id_ed25519", "id_ecdsa"):
        candidate = directory / name
        if candidate.is_file():
            return str(candidate)
    return ""


class RemoteTaskExecutor:
    """Run a task command on a Linux host through the local ``ssh`` client."""

    def __init__(self, config: RemoteConfig):
        self.config = config

    def execute(
        self,
        task: Task,
        *,
        cwd: Path,
        workflow_setup: str,
        log_file: Path,
        cancel_event=None,
        workflow_timeout_event=None,
        workflow_timeout: int | None = None,
    ) -> ExecutionResult:
        script = self.build_script(task, workflow_setup)
        command = self.build_ssh_command(_remote_shell(task))
        log_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            with log_file.open("wb") as output:
                write_log_header(output, script)
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=os.name != "nt",
                    creationflags=(
                        subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                    ),
                )
                try:
                    process.stdin.write(script.encode("utf-8"))
                except OSError:
                    # ssh may exit early (auth failure, connection refused); the
                    # process wait below still records its exit code.
                    pass
                try:
                    process.stdin.close()
                except OSError:
                    pass
                return _wait_for_process(
                    process,
                    log_file,
                    task.timeout,
                    cancel_event,
                    workflow_timeout_event,
                    workflow_timeout,
                )
        except OSError as exc:
            message = f"could not start ssh: {exc}"
            _append_error(log_file, message)
            return ExecutionResult(127, message)

    def build_script(self, task: Task, workflow_setup: str) -> str:
        if isinstance(task.command, str):
            task_command = task.command
            if task.args:
                task_command += " " + " ".join(shlex.quote(arg) for arg in task.args)
        else:
            task_command = shlex.join([*task.command, *task.args])
        lines = ["set -e"]
        workdir = self.config.workdir.strip()
        if workdir:
            lines.append(f"cd {_remote_workdir(workdir)}")
        if workflow_setup:
            lines.append(workflow_setup)
        lines.append(task_command)
        return "\n".join(lines)

    def build_ssh_command(self, shell: str) -> list[str]:
        ssh = self.config.ssh_path.strip() or "ssh"
        argv = [ssh]
        key_path = self.config.key_path.strip()
        if key_path:
            argv += ["-i", key_path]
        argv += ["-p", str(self.config.port)]
        argv += ["-o", f"ConnectTimeout={self.config.connect_timeout}"]
        argv += ["-o", "ServerAliveInterval=15"]
        argv += ["-o", "ServerAliveCountMax=3"]
        argv += ["-o", f"StrictHostKeyChecking={self.config.host_key_policy}"]
        argv += ["-o", "BatchMode=yes"]
        argv.append(f"{self.config.username}@{self.config.host}")
        # The script arrives on stdin, so the remote shell must read commands from
        # there instead of from a local console.
        argv.append(f"{shell} -s")
        return argv


def _remote_shell(task: Task) -> str:
    return task.shell if task.shell in ("bash", "sh") else "bash"


def _remote_workdir(workdir: str) -> str:
    """Quote a remote path while letting the login shell expand a leading ~."""
    if workdir == "~":
        return '"$HOME"'
    if workdir.startswith("~/"):
        return f'"$HOME"/{shlex.quote(workdir[2:])}'
    return shlex.quote(workdir)


def check_remote(config: RemoteConfig) -> RemoteCheckResult:
    """Verify that ssh can authenticate and execute a trivial command."""
    argv = RemoteTaskExecutor(config).build_ssh_command("bash")
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            input=b"echo ok",
            capture_output=True,
            timeout=config.connect_timeout + 10,
        )
    except FileNotFoundError:
        return RemoteCheckResult(False, f"ssh not found: {config.ssh_path or 'ssh'}")
    except subprocess.TimeoutExpired:
        return RemoteCheckResult(False, "connection timed out")
    if completed.returncode == 0:
        return RemoteCheckResult(True, None, int((time.monotonic() - started) * 1000))
    output = (completed.stderr or completed.stdout or b"").decode(
        "utf-8", "replace"
    ).strip()
    message = output or f"ssh exited with code {completed.returncode}"
    return RemoteCheckResult(False, message[:500])


class RemoteMonitor:
    """Periodically probe every configured remote and persist the result."""

    def __init__(self, database, interval_seconds: int = 300):
        self.database = database
        self.interval_seconds = interval_seconds
        self.scheduler = BackgroundScheduler(daemon=True)

    def start(self) -> None:
        self.scheduler.add_job(
            self.check_all,
            "interval",
            seconds=self.interval_seconds,
            id="remote-monitor",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.start()
        Thread(target=self.check_all, daemon=True).start()

    def check_all(self) -> None:
        configs = self.database.list_remotes()
        if not configs:
            return
        workers = max(1, min(8, len(configs)))
        with DaemonPool(
            max_workers=workers, thread_name_prefix="remote-check"
        ) as pool:
            futures = [pool.submit(self._check_one, config) for config in configs]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    print(f"remote check failed: {exc}", file=sys.stderr)

    def _check_one(self, config: RemoteConfig) -> None:
        result = check_remote(config)
        self.database.set_remote_status(
            config.alias, result.reachable, result.error_message, result.latency_ms
        )

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
