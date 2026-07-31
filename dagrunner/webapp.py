from __future__ import annotations

import atexit
import json

from flask import Flask, Response, jsonify, render_template, request

from .database import StateDatabase
from .logger import TaskLogManager
from .service import (
    DEFAULT_SCHEDULE_CRON,
    DEFAULT_SCHEDULE_TIMEZONE,
    ExecutionService,
    ScheduleService,
    ServiceError,
    WorkflowRegistry,
)
from .workflow import Workflow, WorkflowError, migrate_legacy_env


def create_app(
    *,
    database_path="var/scheduler.db",
    logs_path="var/logs",
    start_scheduler: bool = True,
) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    database = StateDatabase(database_path)
    for stored in database.list_workflows():
        migrated, changed = migrate_legacy_env(stored["definition"])
        if changed:
            database.update_workflow(
                stored["workflow_key"], stored["name"], migrated
            )
    registry = WorkflowRegistry(database)
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
        last_run_times = database.latest_run_times()
        schedule_rows = {
            row["workflow_name"]: row for row in database.list_schedules()
        }
        result = []
        for name, workflow in sorted(registry.workflows.items()):
            schedule = schedule_rows.get(name)
            result.append(
                {
                    "name": name,
                    "db_id": registry.rows[name]["id"],
                    "description": workflow.description,
                    "last_run_time": last_run_times.get(name),
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

    @app.post("/api/workflows/import")
    def import_workflow():
        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            raise ServiceError("请选择 YAML 文件")
        try:
            definition = uploaded.read().decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ServiceError("YAML 文件必须使用 UTF-8 编码") from exc
        parsed = Workflow.from_yaml(
            definition,
            fallback_name=uploaded.filename.rsplit(".", 1)[0],
        )
        display_name = parsed.description.strip() or parsed.name
        row = database.create_workflow(display_name, definition)
        workflow_name = row["workflow_key"]
        registry.refresh()
        schedule = parsed.schedule
        try:
            schedules.update(
                workflow_name,
                schedule.cron if schedule else DEFAULT_SCHEDULE_CRON,
                schedule.timezone if schedule else DEFAULT_SCHEDULE_TIMEZONE,
                False,
            )
        except Exception:
            database.delete_workflow(workflow_name)
            registry.refresh()
            raise
        return jsonify({"ok": True, "id": workflow_name, "name": display_name}), 201

    @app.get("/api/workflows/next-id")
    def next_workflow_id():
        return jsonify({"id": database.next_workflow_key()})

    @app.get("/api/workflows/<workflow_name>/yaml")
    def export_workflow_yaml(workflow_name: str):
        definition = registry.definition(workflow_name)
        return Response(
            definition,
            mimetype="application/yaml",
            headers={
                "Content-Disposition": f'attachment; filename="{workflow_name}.yaml"'
            },
        )

    @app.put("/api/workflows/<workflow_name>")
    def edit_workflow(workflow_name: str):
        if executions.is_workflow_active(workflow_name):
            raise ServiceError("工作流运行中，不能编辑")
        schedule = database.get_schedule(workflow_name)
        if schedule and schedule["enabled"]:
            raise ServiceError("请先下线定时，再编辑工作流")
        payload = request.get_json(silent=True) or {}
        definition = str(payload.get("definition", ""))
        current = database.get_workflow(workflow_name)
        if current is None:
            raise ServiceError(f"workflow not found: {workflow_name}")
        parsed = Workflow.from_yaml(
            definition,
            name_override=workflow_name,
        )
        display_name = parsed.description.strip() or current["name"]
        database.update_workflow(workflow_name, display_name, definition)
        database.update_schedule_description(workflow_name, display_name)
        registry.refresh()
        return jsonify({"ok": True})

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

    @app.delete("/api/workflows/<workflow_name>")
    def delete_workflow(workflow_name: str):
        if executions.is_workflow_active(workflow_name):
            raise ServiceError("cannot delete a workflow while it is running")
        schedules.delete(workflow_name)
        registry.delete(workflow_name)
        try:
            logs.delete_workflow(workflow_name)
        except (OSError, ValueError) as exc:
            raise ServiceError(
                f"workflow and run records deleted, but logs could not be removed: {exc}"
            ) from exc
        return jsonify({"ok": True})

    @app.get("/api/runs")
    def runs():
        registry.refresh()
        page = max(request.args.get("page", 1, type=int), 1)
        requested_page_size = request.args.get(
            "page_size", request.args.get("limit", 20, type=int), type=int
        )
        page_size = min(max(requested_page_size or 20, 1), 20)
        workflow_query = request.args.get("workflow", "").strip()
        status = request.args.get("status", "").strip().upper()
        trigger_type = request.args.get("trigger", "").strip().lower()
        if status and status not in {"RUNNING", "SUCCESS", "FAILED"}:
            raise ServiceError(f"invalid run status filter: {status}")
        if trigger_type and trigger_type not in {"manual", "schedule", "rerun", "resume"}:
            raise ServiceError(f"invalid trigger filter: {trigger_type}")
        rows, total, page = database.search_runs(
            page=page,
            page_size=page_size,
            workflow_query=workflow_query,
            status=status,
            trigger_type=trigger_type,
        )
        active_ids = executions.active_run_ids()
        run_rows = []
        for row in rows:
            item = {**_row_dict(row), "active": row["run_id"] in active_ids}
            item["workflow_description"] = (
                row["workflow_description"] or row["workflow_name"]
            )
            run_rows.append(item)
        return jsonify(
            {
                "runs": run_rows,
                "pagination": {
                    "page": page,
                    "page_size": page_size,
                    "total": total,
                    "total_pages": max(1, (total + page_size - 1) // page_size),
                },
            }
        )

    @app.get("/api/runs/<run_id>")
    def run_detail(run_id: str):
        run = database.get_run(run_id)
        if run is None:
            raise ServiceError(f"run not found: {run_id}")
        registry.refresh()
        workflow = registry.workflows.get(run["workflow_name"])
        run_data = _row_dict(run)
        run_data["workflow_description"] = (
            workflow.description
            if workflow and workflow.description
            else run["workflow_name"]
        )
        tasks = []
        graph_tasks = []
        for row in database.task_states(run_id).values():
            task = _row_dict(row)
            definition = workflow.tasks.get(row["task_name"]) if workflow else None
            snapshot = bool(row["snapshot_version"])
            description = (
                row["task_description"]
                if snapshot
                else definition.description if definition else ""
            )
            depends = (
                json.loads(row["depends_json"])
                if snapshot
                else list(definition.depends) if definition else []
            )
            task["task_description"] = description or row["task_name"]
            tasks.append(task)
            graph_tasks.append(
                {
                    "name": row["task_name"],
                    "description": description,
                    "depends": depends,
                    "enabled": definition.enabled if definition else True,
                    "status": row["status"],
                }
            )
        return jsonify({"run": run_data, "tasks": tasks, "graph_tasks": graph_tasks})

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

    @app.delete("/api/runs/<run_id>")
    def delete_run(run_id: str):
        run = database.get_run(run_id)
        if run is None:
            raise ServiceError(f"run not found: {run_id}")
        if run_id in executions.active_run_ids():
            raise ServiceError("cannot delete a run while it is active")
        database.delete_run(run_id)
        try:
            logs.delete_run(run["workflow_name"], run_id)
        except (OSError, ValueError) as exc:
            raise ServiceError(f"run record deleted, but logs could not be removed: {exc}") from exc
        return jsonify({"ok": True})

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
