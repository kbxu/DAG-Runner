#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

from waitress import serve

from .webapp import create_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DAG Workflow Runner Web Service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7119)
    parser.add_argument("--db", type=Path, default=Path("var") / "scheduler.db")
    parser.add_argument("--logs", type=Path, default=Path("var") / "logs")
    parser.add_argument(
        "--notifier-config",
        type=Path,
        help="email notifier YAML/JSON (default: discover beside --db)",
    )
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument(
        "--language",
        choices=("zh-CN", "en"),
        default="zh-CN",
        help="default web interface language (default: zh-CN)",
    )
    parser.add_argument(
        "--allow-insecure-remote-login",
        action="store_true",
        help=(
            "allow non-local clients to keep login sessions over HTTP; "
            "use only on a trusted network"
        ),
    )
    args = parser.parse_args(argv)
    app = create_app(
        database_path=args.db,
        logs_path=args.logs,
        allow_insecure_remote_login=args.allow_insecure_remote_login,
        language=args.language,
        notifier_config_path=args.notifier_config,
    )
    _install_signal_handlers(app)
    print(f"dag-runner service listening on http://{args.host}:{args.port}")
    if args.allow_insecure_remote_login:
        print("warning: insecure remote HTTP login is enabled")
    serve(app, host=args.host, port=args.port, threads=args.threads)
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
