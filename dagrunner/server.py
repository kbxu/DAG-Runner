#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from waitress import serve

from .webapp import create_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DAG Workflow Runner Web Service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7111)
    parser.add_argument("--config-dir", type=Path, default=Path("workflows"))
    parser.add_argument("--db", type=Path, default=Path("var") / "scheduler.db")
    parser.add_argument("--logs", type=Path, default=Path("var") / "logs")
    parser.add_argument("--threads", type=int, default=8)
    args = parser.parse_args(argv)
    app = create_app(
        config_dir=args.config_dir,
        database_path=args.db,
        logs_path=args.logs,
    )
    print(f"dag-runner service listening on http://{args.host}:{args.port}")
    serve(app, host=args.host, port=args.port, threads=args.threads)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
