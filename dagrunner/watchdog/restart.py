"""Watchdog that restarts the master or satellite after an update.

The running service spawns this module as a detached process just before it
exits. It waits for the old process to release its port, points ``var/current``
at the new version, launches the new process and verifies that it stays up.
If the new process dies, it automatically rolls back to the previous version.

Usage::

    python -m dagrunner.watchdog.restart --role master --root <repo> \
        --config <repo>/var/config.yaml --apply 0.2.0 --previous 0.1.0 \
        --pid <old pid> --host 127.0.0.1 --port 7119
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..install import activate, read_state, write_state


class RestartError(RuntimeError):
    pass


def _normalize_host(host: str) -> str:
    if host in ("", "0.0.0.0", "::", "[::]"):
        return "127.0.0.1"
    return host


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, int(port))) != 0


def _process_alive(pid: int) -> bool:
    # Windows-safe check; os.kill(pid, 0) signals the console process group.
    from .launch import process_alive

    return process_alive(pid)


def wait_for_exit(host: str, port: int, pid: int, timeout: float) -> bool:
    """Wait until the old process is gone and its port is free."""
    host = _normalize_host(host)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_free(host, port) and not _process_alive(pid):
            return True
        time.sleep(0.3)
    return _port_free(host, port) and not _process_alive(pid)


def _launch(role: str, root: Path, version: str, config: Path) -> subprocess.Popen:
    code = root / "var" / "versions" / str(version)
    script = code / ("run_server.py" if role == "master" else "run_satellite.py")
    if not script.is_file():
        raise RestartError(f"启动脚本不存在: {script}")
    log_dir = root / "var" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    prefix = "server" if role == "master" else "satellite"
    out = (log_dir / f"{prefix}.out.log").open("ab")
    err = (log_dir / f"{prefix}.err.log").open("ab")
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(code) + (os.pathsep + existing if existing else "")
    env["DAGRUNNER_ROOT"] = str(root)
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(
        [sys.executable, str(script), "--config", str(config)],
        cwd=str(root),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=out,
        stderr=err,
        **kwargs,
    )


def _health_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 500
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _verify(
    process: subprocess.Popen,
    *,
    health_url: str = "",
    timeout: float = 20.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        if health_url and _health_ok(health_url):
            return True
        if not health_url:
            # No HTTP probe available: a process that stays alive for a couple
            # of seconds is good enough for the satellite role.
            time.sleep(2.0)
            return process.poll() is None
        time.sleep(0.5)
    return False


def _record(state: dict, **fields: object) -> None:
    state.update(fields)
    try:
        write_state(state)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m dagrunner.watchdog.restart",
        description="Restart dagrunner after an update",
    )
    parser.add_argument("--role", choices=("master", "satellite"), required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--apply", default="")
    parser.add_argument("--previous", default="")
    parser.add_argument("--pid", type=int, default=0)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--wait-timeout", type=float, default=60.0)
    parser.add_argument("--verify-timeout", type=float, default=20.0)
    args = parser.parse_args(argv)

    root = args.root.resolve()
    config = args.config.resolve()
    state = read_state(root)
    _record(
        state,
        restart={
            "role": args.role,
            "apply": args.apply,
            "previous": args.previous,
            "pid": args.pid,
            "status": "waiting",
        },
    )

    try:
        if not args.apply:
            raise RestartError("missing --apply <version>")
        if not wait_for_exit(args.host, args.port, args.pid, args.wait_timeout):
            raise RestartError("旧进程未在超时内退出")
        activate(args.apply, root)
        _record(state, restart={**state.get("restart", {}), "status": "launching"})
        process = _launch(args.role, root, args.apply, config)
        health_url = (
            f"http://{_normalize_host(args.host)}:{args.port}/health"
            if args.role == "master" and args.port
            else ""
        )
        if not _verify(process, health_url=health_url, timeout=args.verify_timeout):
            raise RestartError(f"新版本 {args.apply} 启动失败")
        _record(
            state,
            restart={
                **state.get("restart", {}),
                "status": "running",
                "pid": process.pid,
                "version": args.apply,
            },
        )
        print(f"restart ok: {args.role} {args.apply} pid={process.pid}")
        return 0
    except BaseException as exc:
        message = str(exc)
        # Roll back when a previous version is known and different.
        previous = str(args.previous or "")
        if previous and previous != args.apply:
            try:
                activate(previous, root)
                process = _launch(args.role, root, previous, config)
                status = "rolled_back" if _verify(process, timeout=10.0) else "rollback_failed"
                _record(
                    state,
                    restart={
                        **state.get("restart", {}),
                        "status": status,
                        "pid": process.pid,
                        "version": previous,
                        "error": message,
                    },
                )
                print(f"restart failed ({message}); rolled back to {previous}", file=sys.stderr)
                return 1
            except BaseException as rollback_exc:
                _record(
                    state,
                    restart={
                        **state.get("restart", {}),
                        "status": "rollback_failed",
                        "error": f"{message}; rollback: {rollback_exc}",
                    },
                )
                print(f"restart failed and rollback failed: {rollback_exc}", file=sys.stderr)
                return 2
        _record(
            state,
            restart={**state.get("restart", {}), "status": "failed", "error": message},
        )
        print(f"restart failed: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
