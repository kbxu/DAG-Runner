#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

import yaml
from waitress import serve

from .webapp import create_app

DEFAULT_CONFIG_PATH = Path("var") / "config.example.yaml"


def load_config_file(path: Path) -> dict:
    """Read startup settings from a YAML config file."""
    if not path.is_file():
        raise SystemExit(f"配置文件不存在: {path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise SystemExit(f"无法解析配置文件 {path}: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise SystemExit(f"配置文件 {path} 必须是 YAML 对象")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DAG Workflow Runner Web Service")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="startup YAML config (default: var/config.example.yaml)",
    )
    args = parser.parse_args(argv)
    config = load_config_file(args.config)
    host = str(config.get("host", "127.0.0.1"))
    port = int(config.get("port", 7119))
    threads = int(config.get("threads", 8))
    language = config.get("language", "zh-CN")
    db = Path(config.get("db", "var/scheduler.db"))
    logs = Path(config.get("logs", "var/logs"))
    notifier_config = config.get("notifier_config")
    allow_insecure_remote_login = bool(config.get("allow_insecure_remote_login", False))
    app = create_app(
        database_path=db,
        logs_path=logs,
        allow_insecure_remote_login=allow_insecure_remote_login,
        language=language,
        notifier_config_path=Path(notifier_config) if notifier_config else None,
    )
    _install_signal_handlers(app)
    print(f"dag-runner service listening on http://{host}:{port}")
    if allow_insecure_remote_login:
        print("warning: insecure remote HTTP login is enabled")
    serve(app, host=host, port=port, threads=threads)
    return 0


def _install_signal_handlers(app) -> None:
    """Stop runs and exit at once on Ctrl+C / SIGTERM.

    Waitress swallows KeyboardInterrupt, and interpreter shutdown joins
    non-daemon executor threads before atexit handlers run, so a running task
    could keep the process alive. Force the exit here instead.
    """
    executions = app.extensions["execution_service"]
    schedules = app.extensions["schedule_service"]
    remote_monitor = app.extensions["remote_monitor"]

    def _handle(signum, frame):
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            executions.cancel_all()
        except Exception:
            pass
        # Give running tasks a moment to terminate their child processes so a
        # lingering ssh/command does not keep the terminal attached.
        time.sleep(0.5)
        for shutdown in (schedules.shutdown, remote_monitor.shutdown):
            try:
                shutdown()
            except Exception:
                pass
        os._exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError):
            pass


if __name__ == "__main__":
    raise SystemExit(main())
