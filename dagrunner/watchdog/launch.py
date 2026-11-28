"""Child process launch, verification and termination helpers."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable


class LaunchError(RuntimeError):
    pass


def normalize_host(host: str) -> str:
    if host in ("", "0.0.0.0", "::", "[::]"):
        return "127.0.0.1"
    return host


def port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((normalize_host(host), int(port))) != 0


def process_alive(pid: int) -> bool:
    """Check process existence without signalling it.

    ``os.kill(pid, 0)`` is not usable on Windows: it is implemented through the
    console control events and interrupts this very process. Use OpenProcess
    instead and treat ERROR_ACCESS_DENIED as "alive".
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def wait_for_exit(host: str, port: int, pid: int, timeout: float) -> bool:
    """Wait until the old process is gone and its port is free."""
    host = normalize_host(host)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_free(host, port) and not process_alive(pid):
            return True
        time.sleep(0.3)
    return port_free(host, port) and not process_alive(pid)


def child_script(code: Path, role: str) -> Path:
    name = "run_server.py" if role == "master" else "run_satellite.py"
    script = code / name
    if not script.is_file():
        raise LaunchError(f"启动脚本不存在: {script}")
    return script


def build_environment(code: Path, root: Path) -> dict:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(code) + (os.pathsep + existing if existing else "")
    env["DAGRUNNER_ROOT"] = str(root)
    return env


def _detached_flags() -> dict:
    if os.name == "nt":
        return {
            "creationflags": (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            )
        }
    return {"start_new_session": True}


def launch_child(
    role: str,
    root: str | Path,
    config: str | Path | None = None,
    *,
    stdout=None,
    stderr=None,
) -> subprocess.Popen:
    """Start the supervised service for ``role`` directly from ``root``."""
    root = Path(root).resolve()
    script = child_script(root, role)
    if config is None:
        config = root / "var" / ("config.yaml" if role == "master" else "satellite.yaml")
    config = Path(config).resolve()
    log_dir = root / "var" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    prefix = "server" if role == "master" else "satellite"
    out = stdout if stdout is not None else (log_dir / f"{prefix}.out.log").open("ab")
    err = stderr if stderr is not None else (log_dir / f"{prefix}.err.log").open("ab")
    try:
        return subprocess.Popen(
            [sys.executable, str(script), "--config", str(config)],
            cwd=str(root),
            env=build_environment(root, root),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            **_detached_flags(),
        )
    finally:
        # The child holds its own duplicated handles; close ours either way.
        if stdout is None:
            out.close()
        if stderr is None:
            err.close()


def verify_process(
    process: subprocess.Popen,
    *,
    probe: Callable[[], object] | None = None,
    timeout: float = 20.0,
    stable: float = 2.0,
    abort: Callable[[], bool] | None = None,
) -> bool:
    """Return True when the child is alive and healthy within ``timeout``.

    ``probe`` may return a bool or an ``(ok, reason)`` tuple. Without a probe,
    staying alive for ``stable`` seconds counts as success. ``abort`` is polled
    each iteration so a shutdown does not have to wait for the full timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if abort is not None and abort():
            return False
        if process.poll() is not None:
            return False
        if probe is not None:
            try:
                result = probe()
            except Exception:  # noqa: BLE001 - a failing probe means unhealthy
                result = False
            ok = result[0] if isinstance(result, tuple) else bool(result)
            if ok:
                return True
        else:
            time.sleep(stable)
            return process.poll() is None
        time.sleep(0.3)
    return False


def _run_quiet(command: list[str], timeout: float = 10.0) -> None:
    try:
        subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def terminate_tree(process: subprocess.Popen, stop_timeout: float = 20.0) -> None:
    """Stop the child and its process tree, escalating only after a grace period."""
    pid = process.pid
    if process.poll() is not None:
        return
    if os.name == "nt":
        # Ask politely first (no /F), then force the whole tree.
        _run_quiet(["taskkill", "/PID", str(pid), "/T"])
        try:
            process.wait(timeout=max(stop_timeout, 1.0))
            return
        except subprocess.TimeoutExpired:
            pass
        _run_quiet(["taskkill", "/PID", str(pid), "/T", "/F"])
        try:
            process.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            pass
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=max(stop_timeout, 1.0))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            pass
