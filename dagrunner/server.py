#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from waitress import serve

from .webapp import create_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DAG Workflow Runner Web Service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7119)
    parser.add_argument("--db", type=Path, default=Path("var") / "scheduler.db")
    parser.add_argument("--logs", type=Path, default=Path("var") / "logs")
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
    )
    print(f"dag-runner service listening on http://{args.host}:{args.port}")
    if args.allow_insecure_remote_login:
        print("warning: insecure remote HTTP login is enabled")
    serve(app, host=args.host, port=args.port, threads=args.threads)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
