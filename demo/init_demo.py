#!/usr/bin/env python3
"""Build a clean demo database (workflows, satellite node, sample runs).

Used for documentation screenshots and for a quick hands-on evaluation:

    python -m demo.init_demo --db var/scheduler.db --reset \
        --satellite moon:<shared-token>

The imported workflows live in ``demo/examples``; the task scripts they call
live in ``demo/tasks``. Nothing here touches production data as long as you
point ``--db`` at a throwaway path.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dagrunner.auth import store_password
from dagrunner.database import StateDatabase
from dagrunner.logger import TaskLogManager
from dagrunner.runner import WorkflowRunner
from dagrunner.satellite_protocol import SatelliteConfig
from dagrunner.service import encode_cron_expressions
from dagrunner.workflow import Workflow


ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = Path(__file__).resolve().parent / "examples"

# workflow file -> how many local runs to create
RUNS = {
    "demo_pipeline": 2,
    "demo_condition": 1,
    "demo_deploy_gate": 1,
}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Initialize a demo DAG Runner database")
    parser.add_argument("--db", type=Path, default=Path("var") / "scheduler.db")
    parser.add_argument("--logs", type=Path, default=Path("var") / "logs")
    parser.add_argument("--reset", action="store_true", help="delete the database first")
    parser.add_argument(
        "--satellite",
        action="append",
        default=[],
        metavar="ALIAS:TOKEN",
        help="add a satellite node (repeatable)",
    )
    parser.add_argument("--password", default="demo-admin-password-2026")
    return parser.parse_args(argv)


def import_workflows(database: StateDatabase) -> dict[str, Workflow]:
    imported: dict[str, Workflow] = {}
    for path in sorted(EXAMPLES.glob("demo_*.yaml")):
        definition = path.read_text(encoding="utf-8")
        parsed = Workflow.from_yaml(definition, fallback_name=path.stem)
        row = database.create_workflow(parsed.description, definition)
        key = row["workflow_key"]
        runner = parsed.runner or "local"
        if runner != "local":
            database.set_workflow_runner(key, runner)
        if parsed.schedule:
            database.upsert_schedule(
                key,
                parsed.description,
                encode_cron_expressions(parsed.schedule.crons),
                parsed.schedule.timezone,
                parsed.schedule.enabled,
            )
        imported[path.stem] = Workflow.from_yaml(
            definition, name_override=key, description_override=parsed.description
        )
        print(f"workflow {path.stem} -> {key} (runner={runner})")
    return imported


def add_satellites(database: StateDatabase, specs: list[str]) -> None:
    for spec in specs:
        alias, _, token = spec.partition(":")
        if not token:
            raise SystemExit(f"--satellite needs ALIAS:TOKEN, got {spec!r}")
        database.upsert_satellite(
            SatelliteConfig(alias=alias, name=alias, mode="reverse", token=token)
        )
        print(f"satellite {alias}")


def create_runs(database: StateDatabase, workflows: dict[str, Workflow], logs: Path) -> None:
    for name, count in RUNS.items():
        workflow = workflows.get(name)
        if workflow is None or (workflow.runner or "local") != "local":
            continue
        for _ in range(count):
            run_id, status = WorkflowRunner(
                database, TaskLogManager(logs)
            ).run(workflow, trigger_type="manual", runner_alias="local")
            print(f"  {name} run {run_id} -> {status}")


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.reset:
        for suffix in ("", "-wal", "-shm"):
            Path(str(args.db) + suffix).unlink(missing_ok=True)
        print(f"reset {args.db}")
    args.db.parent.mkdir(parents=True, exist_ok=True)
    args.logs.mkdir(parents=True, exist_ok=True)

    database = StateDatabase(args.db)
    store_password(database, "admin", args.password)
    print(f"login: admin / {args.password}")

    workflows = import_workflows(database)
    add_satellites(database, args.satellite)
    create_runs(database, workflows, args.logs)
    print(f"done: {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
