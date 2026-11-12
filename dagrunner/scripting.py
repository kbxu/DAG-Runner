"""Shared command/script construction and process helpers.

The local executor and the satellite agent build commands from the same task
model. Keeping the quoting, script wrapping, and process-tree termination here
guarantees every execution path behaves the same way on Linux and Windows.
"""
from __future__ import annotations

import locale
import os
import re
import shlex
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import BinaryIO

from .workflow import Task


@dataclass(frozen=True)
class ExecutionResult:
    exit_code: int
    error_message: str | None = None


@dataclass(frozen=True)
class ScriptPlan:
    """A ready-to-run command: argv, display text, and an optional temp script."""

    argv: list[str] | str
    script: str
    temp_script: Path | None = None
    shell: str = ""


def quote_argument(argument: str, shell: str) -> str:
    """Quote one argument for the given shell dialect."""
    if os.name != "nt":
        return shlex.quote(argument)
    if shell == "powershell":
        return ps_quote(argument)
    return cmd_quote(argument)


def join_args(argv: list[str], shell: str) -> str:
    return " ".join(quote_argument(argument, shell) for argument in argv)


def task_command(task: Task, shell: str) -> str:
    """Render a task command (with args) as a single shell string."""
    if isinstance(task.command, str):
        text = task.command
        if task.args:
            text += " " + " ".join(quote_argument(arg, shell) for arg in task.args)
        return text
    return join_args([*task.command, *task.args], shell)


def posix_script(setup: str, command: str) -> str:
    script = "set -e\n"
    if setup:
        script += setup + "\n"
    script += command
    return script


def windows_script(setup: str, command: str, shell: str) -> tuple[str, str, str]:
    """Return (script, suffix, encoding) for a Windows cmd/powershell script."""
    if shell == "powershell":
        text = "$ErrorActionPreference = 'Stop'\n"
        if setup:
            text += setup + "\n"
        text += command
        return text, ".ps1", "utf-8-sig"
    parts: list[str] = []
    if setup:
        parts.extend(setup.strip().splitlines())
    parts.append(command)
    text = "\n".join(part.strip() for part in parts if part.strip())
    return text, ".bat", cmd_encoding()


def build_local_plan(task: Task, workflow_setup: str) -> ScriptPlan:
    """Build the command for running a task on this machine."""
    if os.name == "nt":
        return _build_windows_plan(task, workflow_setup)

    # Linux: no quoting pitfalls for a plain argv, so a script file is
    # unnecessary unless a shell or setup is involved.
    if (
        not workflow_setup
        and task.shell is None
        and not isinstance(task.command, str)
    ):
        argv = [*task.command, *task.args]
        return ScriptPlan(shlex.join(argv), shlex.join(argv), None, "")
    shell = task.shell if task.shell in ("bash", "sh") else "bash"
    script = posix_script(workflow_setup, task_command(task, shell))
    return ScriptPlan([shell, "-lc", script], script, None, shell)


def _build_windows_plan(task: Task, workflow_setup: str) -> ScriptPlan:
    shell = task.shell if task.shell in ("cmd", "powershell") else "cmd"
    command = task_command(task, shell)
    if shell == "powershell":
        # A quoted argv is a string literal in PowerShell; & invokes it.
        if not isinstance(task.command, str):
            command = "& " + command
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
        suffix, encoding = ".bat", cmd_encoding()
        argv = ["cmd.exe", "/c"]
    script = windows_script(workflow_setup, command, shell)[0]
    temp_script = write_temp_script(script, suffix, encoding)
    return ScriptPlan([*argv, str(temp_script)], script, temp_script, shell)


def shell_workdir(workdir: str) -> str:
    """Quote a work directory while letting the login shell expand a leading ~."""
    if workdir == "~":
        return '"$HOME"'
    if workdir.startswith("~/"):
        return f'"$HOME"/{shlex.quote(workdir[2:])}'
    return shlex.quote(workdir)


def write_log_header(output: BinaryIO, script: str) -> None:
    """Write the raw executed script at the top of a task log."""
    header = (
        f"Executing command:\n{script}\n\n"
        "##################################################################\n"
        "Command output:\n"
    ).encode("utf-8", "replace")
    output.write(header)
    output.flush()


def append_error(log_file: Path, message: str) -> None:
    with log_file.open("ab") as output:
        output.write((f"\n[mini-scheduler] {message}\n").encode("utf-8", errors="replace"))


def write_temp_script(content: str, suffix: str, encoding: str) -> Path:
    handle, name = tempfile.mkstemp(prefix="dagrunner_", suffix=suffix)
    try:
        with os.fdopen(handle, "w", encoding=encoding, errors="replace") as output:
            output.write(content)
    except BaseException:
        remove_temp_script(Path(name))
        raise
    return Path(name)


def remove_temp_script(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def cmd_encoding() -> str:
    """Return the encoding cmd.exe uses to read a .bat file.

    cmd reads batch files with the console output code page, which may differ
    from the ANSI code page Python reports (for example UTF-8 mode). Match it
    so non-ASCII setup/command text is not mangled.
    """
    if os.name != "nt":
        return "utf-8"
    try:
        import ctypes

        codepage = ctypes.windll.kernel32.GetConsoleOutputCP()
    except (OSError, AttributeError):
        codepage = 0
    if codepage == 65001:
        return "utf-8"
    if codepage:
        return f"cp{codepage}"
    return locale.getpreferredencoding(False) or "mbcs"


def cmd_quote(argument: str) -> str:
    if not argument or any(char in argument for char in ' \t"&|<>^%'):
        return '"' + argument.replace('"', '""') + '"'
    return argument


def ps_quote(argument: str) -> str:
    return "'" + argument.replace("'", "''") + "'"


def default_env() -> dict[str, str]:
    """Return the environment a fresh shell would inherit, ignoring this process.

    The backend may run inside an activated conda env or with other tweaks; a task
    should start from the default environment a user gets by opening cmd.exe or
    bash directly, so it only includes the persistent system/user variables.
    """
    if os.name != "nt":
        return os.environ.copy()
    import winreg

    system_env: dict[str, str] = {}
    user_env: dict[str, str] = {}

    def read(root, subkey, target):
        try:
            with winreg.OpenKey(root, subkey) as key:
                index = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    index += 1
                    if isinstance(value, str):
                        target[name] = value
        except OSError:
            pass

    read(
        winreg.HKEY_LOCAL_MACHINE,
        r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        system_env,
    )
    read(winreg.HKEY_CURRENT_USER, r"Environment", user_env)

    merged = {**system_env, **user_env}
    sys_path = system_env.get("Path") or system_env.get("PATH", "")
    user_path = user_env.get("Path") or user_env.get("PATH", "")
    if sys_path or user_path:
        merged["PATH"] = ";".join(part for part in (sys_path, user_path) if part)
    merged.pop("Path", None)

    # Runtime-only variables (not persisted in the registry), e.g. SystemRoot,
    # USERNAME. Skip conda's tweaks and PROMPT, which conda rewrites.
    skip = {"PROMPT", "_CE_CONDA", "_CE_M"}
    for name, value in os.environ.items():
        if name in merged or name in skip or name.startswith("CONDA"):
            continue
        merged[name] = value

    def expand(value: str) -> str:
        for _ in range(10):
            new = re.sub(
                r"%([^%]+)%", lambda m: merged.get(m.group(1), m.group(0)), value
            )
            if new == value:
                break
            value = new
        return value

    return {name: expand(value) for name, value in merged.items()}


def wait_for_process(
    process: subprocess.Popen,
    log_file: Path,
    task_timeout: int | None,
    cancel_event: Event | None,
    workflow_timeout_event: Event | None,
    workflow_timeout: int | None,
) -> ExecutionResult:
    started = time.monotonic()
    while process.poll() is None:
        cancelled = cancel_event.wait(0.2) if cancel_event else False
        if cancel_event is None:
            time.sleep(0.2)
        if process.poll() is not None:
            break
        if cancelled:
            terminate_process(process)
            workflow_timed_out = bool(
                workflow_timeout_event and workflow_timeout_event.is_set()
            )
            message = (
                f"workflow timed out after {workflow_timeout} seconds"
                if workflow_timed_out
                else "task stopped by user"
            )
            append_error(log_file, message)
            return ExecutionResult(124 if workflow_timed_out else 130, message)
        if task_timeout and time.monotonic() - started >= task_timeout:
            terminate_process(process)
            message = f"task timed out after {task_timeout} seconds"
            append_error(log_file, message)
            return ExecutionResult(124, message)
    if process.returncode == 0:
        return ExecutionResult(0)
    return ExecutionResult(
        process.returncode, f"command exited with code {process.returncode}"
    )


def terminate_process(process: subprocess.Popen) -> None:
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
