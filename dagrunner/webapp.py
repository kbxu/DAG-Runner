from __future__ import annotations

import atexit
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, send_file

from .database import StateDatabase
from .logger import TaskLogManager
from .service import ExecutionService, ScheduleService, ServiceError, WorkflowRegistry
from .workflow import WorkflowError


def create_app(
    *,
    config_dir: str | Path = Path("workflows"),
    database_path: str | Path = Path("var") / "scheduler.db",
    logs_path: str | Path = Path("var") / "logs",
    start_scheduler: bool = True,
) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    database = StateDatabase(database_path)
    registry = WorkflowRegistry(config_dir)
    logs = TaskLogManager(logs_path)
    executions = ExecutionService(database, registry, logs)
    schedules = ScheduleService(database, registry, executions)
    if start_scheduler:
        executions.recover_orphaned_runs()
        schedules.start()

    app.extensions["state_database"] = database
    app.extensions["workflow_registry"] = registry
    app.extensions["execution_service"] = executions
    app.extensions["schedule_service"] = schedules

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "scheduler_running": schedules.scheduler.running})

    @app.get("/api/workflows")
    def workflows():
        registry.refresh()
        schedule_rows = {
            row["workflow_name"]: row for row in database.list_schedules()
        }
        result = []
        for name, workflow in sorted(registry.workflows.items()):
            schedule = schedule_rows.get(name)
            result.append(
                {
                    "name": name,
                    "description": workflow.description,
                    "config_file": str(registry.paths[name]),
                    "workdir": str(workflow.workdir),
                    "task_count": len(workflow.tasks),
                    "tasks": [
                        {
                            "name": task.name,
                            "description": task.description,
                            "depends": list(task.depends),
                            "enabled": task.enabled,
                            "command": _command_text(task.command, task.args),
                        }
                        for task in workflow.tasks.values()
                    ],
                    "schedule": {
                        "cron": schedule["cron_expression"] if schedule else "",
                        "timezone": schedule["timezone"] if schedule else "Asia/Shanghai",
                        "enabled": bool(schedule["enabled"]) if schedule else False,
                        "next_run_time": schedules.next_run_time(name),
                    },
                }
            )
        return jsonify({"workflows": result, "config_errors": registry.errors})

    @app.get("/api/workflows/<workflow_name>/yaml")
    def export_workflow_yaml(workflow_name: str):
        registry.refresh()
        registry.get(workflow_name)
        path = registry.paths[workflow_name]
        return send_file(
            path,
            mimetype="application/yaml",
            as_attachment=True,
            download_name=f"{workflow_name}.yaml",
        )

    @app.put("/api/workflows/<workflow_name>/schedule")
    def update_schedule(workflow_name: str):
        payload = request.get_json(silent=True) or {}
        cron_expression = str(payload.get("cron", "")).strip()
        timezone_name = str(payload.get("timezone", "Asia/Shanghai")).strip()
        enabled = bool(payload.get("enabled", False))
        if not cron_expression:
            raise ServiceError("cron is required")
        schedules.update(workflow_name, cron_expression, timezone_name, enabled)
        return jsonify({"ok": True, "next_run_time": schedules.next_run_time(workflow_name)})

    @app.post("/api/workflows/<workflow_name>/run")
    def run_workflow(workflow_name: str):
        run_id = executions.start_run(workflow_name, trigger_type="manual")
        return jsonify({"ok": True, "run_id": run_id}), 202

    @app.get("/api/runs")
    def runs():
        limit = min(max(request.args.get("limit", 100, type=int), 1), 500)
        active_ids = executions.active_run_ids()
        return jsonify(
            {
                "runs": [
                    {**_row_dict(row), "active": row["run_id"] in active_ids}
                    for row in database.list_all_runs(limit)
                ]
            }
        )

    @app.get("/api/runs/<run_id>")
    def run_detail(run_id: str):
        run = database.get_run(run_id)
        if run is None:
            raise ServiceError(f"run not found: {run_id}")
        tasks = [_row_dict(row) for row in database.task_states(run_id).values()]
        return jsonify({"run": _row_dict(run), "tasks": tasks})

    @app.post("/api/runs/<run_id>/stop")
    def stop_run(run_id: str):
        executions.stop_run(run_id)
        return jsonify({"ok": True})

    @app.post("/api/runs/<run_id>/rerun")
    def rerun(run_id: str):
        new_id = executions.rerun(run_id)
        return jsonify({"ok": True, "run_id": new_id}), 202

    @app.post("/api/runs/<run_id>/resume")
    def resume(run_id: str):
        new_id = executions.resume_failed(run_id)
        return jsonify({"ok": True, "run_id": new_id}), 202

    @app.get("/api/runs/<run_id>/tasks/<task_name>/log")
    def task_log(run_id: str, task_name: str):
        task = database.get_task(run_id, task_name)
        if task is None:
            raise ServiceError(f"task run not found: {run_id}/{task_name}")
        if not task["log_file"]:
            raise ServiceError("task has no log file")
        return Response(TaskLogManager.read(task["log_file"]), mimetype="text/plain")

    @app.errorhandler(ServiceError)
    @app.errorhandler(WorkflowError)
    def expected_error(exc):
        return jsonify({"ok": False, "error": str(exc)}), 400

    if start_scheduler:
        atexit.register(_shutdown, schedules, executions)
    return app


def _shutdown(schedules: ScheduleService, executions: ExecutionService) -> None:
    schedules.shutdown()
    executions.shutdown()


def _row_dict(row) -> dict:
    return {key: row[key] for key in row.keys()}


def _command_text(command, args) -> str:
    base = command if isinstance(command, str) else " ".join(command)
    return " ".join([base, *args]).strip()
