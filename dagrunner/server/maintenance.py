"""Maintenance jobs: archive old run records into debug-only tables.

The archive tables live in the same database but the backend never reads them;
they exist so old runs can still be inspected by hand. Log files are left
untouched.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path
import sys

from ..database import (
    BaseStateDatabase,
    StateDatabase,
    create_state_database,
    load_database_config,
)


DEFAULT_RETENTION_DAYS = 60
DEFAULT_BATCH_SIZE = 50
MAX_BATCHES = 1000


class MaintenanceService:
    def __init__(
        self,
        database: BaseStateDatabase,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ):
        self.database = database
        self.retention_days = int(retention_days)
        self.batch_size = max(1, int(batch_size))

    def archive(
        self, *, retention_days: int | None = None, limit: int = 0
    ) -> dict:
        """Move terminal runs older than the retention window into the archive."""
        days = self.retention_days if retention_days is None else int(retention_days)
        cutoff = (
            datetime.now().astimezone() - timedelta(days=days)
        ).isoformat(timespec="milliseconds")
        archived_runs = 0
        archived_tasks = 0
        batches = 0
        while True:
            rows = self.database.list_runs_for_archive(cutoff, self.batch_size)
            if not rows:
                break
            run_ids = [str(row["run_id"]) for row in rows]
            archived_time = datetime.now().astimezone().isoformat(
                timespec="milliseconds"
            )
            runs, tasks = self.database.archive_run_batch(run_ids, archived_time)
            archived_runs += runs
            archived_tasks += tasks
            batches += 1
            if limit and archived_runs >= limit:
                break
            if batches >= MAX_BATCHES:
                break
        return {
            "cutoff": cutoff,
            "runs": archived_runs,
            "tasks": archived_tasks,
            "batches": batches,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dagrunner.server.maintenance",
        description="DAG Runner maintenance (archive old run records)",
    )
    parser.add_argument("--db", type=Path, default=Path("var") / "scheduler.db")
    parser.add_argument(
        "--db-config",
        type=Path,
        default=None,
        help="startup YAML config; reads its db entry (SQLite path or MySQL 8 mapping)",
    )
    parser.add_argument(
        "--archive",
        action="store_true",
        help="archive terminal runs older than the retention window",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_RETENTION_DAYS,
        help="retention window in days (default: 60)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="runs per transaction (default: 50)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="stop after this many runs, 0 for no limit",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.db_config is not None:
            database = create_state_database(load_database_config(args.db_config))
        else:
            database = StateDatabase(args.db)
        service = MaintenanceService(
            database, retention_days=args.days, batch_size=args.batch_size
        )
        if args.archive:
            result = service.archive(limit=args.limit)
            print(
                f"archived runs={result['runs']} tasks={result['tasks']} "
                f"cutoff={result['cutoff']}"
            )
            return 0
        parser.print_help()
        return 2
    except (ValueError, OSError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"unexpected maintenance error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
