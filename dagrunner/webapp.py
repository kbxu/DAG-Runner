from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree

from flask import Flask, Response, g, jsonify, redirect, render_template, request, url_for

from .auth import AuthService, SESSION_COOKIE, SESSION_HOURS
from .database import StateDatabase
from .logger import TaskLogManager
from .migrate_workflows import (
    SOURCE_DOLPHINSCHEDULER,
    SOURCE_WINDOWS_TASK_SCHEDULER,
    WINDOWS_TASK_NAMESPACE,
    convert_dolphinscheduler_definition,
    convert_windows_task_scheduler_definition,
    dump_workflow_yaml,
)
from .service import (
    DEFAULT_SCHEDULE_CRON,
    DEFAULT_SCHEDULE_TIMEZONE,
    ExecutionService,
    ScheduleService,
    ServiceError,
    WorkflowRegistry,
    decode_cron_expressions,
)
from .workflow import Workflow, WorkflowError, migrate_legacy_env


def create_app(
    *,
    database_path="var/scheduler.db",
    logs_path="var/logs",
    start_scheduler: bool = True,
    allow_insecure_remote_login: bool = False,
    language: str = "zh-CN",
) -> Flask:
    if language not in {"zh-CN", "en"}:
        raise ValueError("language must be 'zh-CN' or 'en'")
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["DEFAULT_LANGUAGE"] = language
    database = StateDatabase(database_path)
    for stored in database.list_workflows():
        migrated, changed = migrate_legacy_env(stored["definition"])
        if changed:
            database.update_workflow(
                stored["workflow_key"], stored["name"], migrated
            )
    registry = WorkflowRegistry(database)
    auth = AuthService(database)
    logs = TaskLogManager(logs_path)
    executions = ExecutionService(database, registry, logs)
    schedules = ScheduleService(database, registry, executions)
    if start_scheduler:
        executions.recover_orphaned_runs()
        schedules.start()

    app.extensions["state_database"] = database
    app.extensions["workflow_registry"] = registry
    app.extensions["auth_service"] = auth
    app.extensions["execution_service"] = executions
    app.extensions["schedule_service"] = schedules

    public_endpoints = {"static", "login_page", "api_login"}

    @app.before_request
    def require_login():
        requested_language = request.headers.get("X-DAGRunner-Language", "")
        g.language = (
            requested_language
            if requested_language in {"zh-CN", "en"}
            else language
        )
        if request.endpoint in public_endpoints:
            return None
        token = request.cookies.get(SESSION_COOKIE)
        username = auth.authenticate(token)
        if username:
            g.current_user = username
            return None
        return_to = (
            "/"
            if request.path.startswith("/api/") or request.method != "GET"
            else request.full_path.rstrip("?")
        )
        login_url = url_for(
            "login_page",
            next=return_to,
        )
        if request.path.startswith("/api/") and request.headers.get(
            "Sec-Fetch-Mode"
        ) != "navigate":
            return jsonify(
                {
                    "ok": False,
                    "error": _localized_error("登录已失效，请重新登录", g.language),
                    "login_url": login_url,
                }
            ), 401
        return redirect(login_url)

    @app.after_request
    def prevent_private_response_caching(response):
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.endpoint == "login_page":
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
                "form-action 'self'"
            )
        return response

    @app.get("/login")
    def login_page():
        if auth.authenticate(request.cookies.get(SESSION_COOKIE)):
            return redirect(_safe_next(request.args.get("next")))
        return render_template(
            "login.html",
            next_url=_safe_next(request.args.get("next")),
            allow_insecure_remote_login=allow_insecure_remote_login,
            default_language=language,
        )

    @app.post("/api/auth/login")
    def api_login():
        payload = request.get_json(silent=True) or {}
        username = str(payload.get("username", "")).strip()
        password_hash = str(payload.get("password_hash", ""))
        ip_address = request.remote_addr or "unknown"
        result = auth.login(username, password_hash, ip_address)
        if result.status == "blocked":
            response = jsonify(
                {
                    "ok": False,
                    "error": _localized_error(
                        "登录失败次数过多，请在 10 分钟后重试", g.language
                    ),
                    "retry_after": result.retry_after,
                }
            )
            response.status_code = 429
            response.headers["Retry-After"] = str(result.retry_after)
            return response
        if result.status != "success":
            return jsonify(
                {
                    "ok": False,
                    "error": _localized_error("账号或密码错误", g.language),
                    "remaining_attempts": result.remaining_attempts,
                }
            ), 401
        assert result.token is not None and result.expires_at is not None
        response = jsonify(
            {
                "ok": True,
                "username": username,
                "redirect": _safe_next(payload.get("next")),
                "expires_in": SESSION_HOURS * 3600,
            }
        )
        response.set_cookie(
            SESSION_COOKIE,
            result.token,
            max_age=SESSION_HOURS * 3600,
            expires=result.expires_at,
            httponly=True,
            secure=_secure_cookie_enabled(allow_insecure_remote_login),
            samesite="Strict",
            path="/",
        )
        return response

    @app.post("/api/auth/logout")
    def api_logout():
        auth.logout(request.cookies.get(SESSION_COOKIE))
        response = jsonify({"ok": True, "redirect": url_for("login_page")})
        response.delete_cookie(
            SESSION_COOKIE,
            path="/",
            httponly=True,
            secure=_secure_cookie_enabled(allow_insecure_remote_login),
            samesite="Strict",
        )
        return response

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            username=g.current_user,
            default_language=language,
        )

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
            stored_crons = (
                decode_cron_expressions(schedule["cron_expression"])
                if schedule
                else ()
            )
            schedule_entries = schedules.schedule_entries(name, stored_crons)
            serialized_entries = [
                {
                    "cron": entry["cron"],
                    "next_run_time": (
                        entry["next_run_time"].isoformat()
                        if entry["next_run_time"]
                        else None
                    ),
                }
                for entry in schedule_entries
            ]
            next_entry = min(
                (entry for entry in serialized_entries if entry["next_run_time"]),
                key=lambda entry: entry["next_run_time"],
                default=None,
            )
            displayed_entry = next_entry or (
                serialized_entries[0] if serialized_entries else None
            )
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
                            "type": task.task_type,
                            "success": list(task.success),
                            "failure": list(task.failure),
                        }
                        for task in workflow.tasks.values()
                    ],
                    "schedule": {
                        "cron": displayed_entry["cron"] if displayed_entry else "",
                        "crons": list(stored_crons),
                        "entries": serialized_entries,
                        "timezone": schedule["timezone"] if schedule else "Asia/Shanghai",
                        "enabled": bool(schedule["enabled"]) if schedule else False,
                        "next_run_time": (
                            next_entry["next_run_time"] if next_entry else None
                        ),
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
                schedule.crons if schedule else [DEFAULT_SCHEDULE_CRON],
                schedule.timezone if schedule else DEFAULT_SCHEDULE_TIMEZONE,
                False,
            )
        except Exception:
            database.delete_workflow(workflow_name)
            registry.refresh()
            raise
        return jsonify({"ok": True, "id": workflow_name, "name": display_name}), 201

    @app.post("/api/workflows/import-preview")
    def import_workflow_preview():
        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            raise ServiceError("请选择工作流文件")
        try:
            content = uploaded.read().decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ServiceError("工作流文件必须使用 UTF-8 编码") from exc
        return jsonify(_prepare_import_preview(uploaded.filename, content))

    @app.get("/api/workflows/next-id")
    def next_workflow_id():
        return jsonify({"id": database.next_workflow_key()})

    @app.get("/api/workflows/example-definition")
    def example_workflow_definition():
        example_path = (
            Path(__file__).resolve().parent.parent
            / "demo"
            / "examples"
            / "dagr_example_pipeline.yaml"
        )
        try:
            definition = example_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ServiceError("示例工作流文件不可用") from exc
        return Response(definition, mimetype="application/yaml")

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
        cron_expressions = payload.get("crons", payload.get("cron", []))
        if isinstance(cron_expressions, str):
            cron_expressions = [cron_expressions]
        if not isinstance(cron_expressions, list):
            raise ServiceError("crons must be a list")
        timezone_name = str(payload.get("timezone", "Asia/Shanghai")).strip()
        enabled = bool(payload.get("enabled", False))
        schedules.update(workflow_name, cron_expressions, timezone_name, enabled)
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
                    "type": row["task_type"] if snapshot else (
                        definition.task_type if definition else "command"
                    ),
                    "condition_result": row["condition_result"],
                    "handled_by": row["handled_by"],
                    "skip_kind": row["skip_kind"],
                    "success": list(definition.success) if definition else [],
                    "failure": list(definition.failure) if definition else [],
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
        return jsonify(
            {"ok": False, "error": _localized_error(str(exc), g.language)}
        ), 400

    if start_scheduler:
        atexit.register(_shutdown, schedules, executions)
    return app


def _shutdown(schedules: ScheduleService, executions: ExecutionService) -> None:
    schedules.shutdown()
    executions.shutdown()


def _prepare_import_preview(filename: str, content: str) -> dict[str, object]:
    """Detect an import source and convert external exports entirely in memory."""
    stripped = content.strip()
    if not stripped:
        raise ServiceError("工作流文件不能为空")

    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        root = None
    if root is not None:
        if root.tag != f"{{{WINDOWS_TASK_NAMESPACE}}}Task":
            raise ServiceError("无法识别的 XML 工作流格式")
        try:
            configs, warnings = convert_windows_task_scheduler_definition(
                root,
                Path(filename).stem,
            )
        except ValueError as exc:
            raise ServiceError(f"Windows Task Scheduler 转换失败：{exc}") from exc
        return {
            "source": SOURCE_WINDOWS_TASK_SCHEDULER,
            "source_label": "Windows Task Scheduler",
            "definition": dump_workflow_yaml(configs[0]),
            "warnings": warnings,
            "filename": f"dagr_{Path(filename).stem}.yaml",
        }

    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        payload = None
    definitions = payload if isinstance(payload, list) else [payload]
    is_dolphinscheduler = bool(definitions) and all(
        isinstance(item, dict)
        and "processDefinition" in item
        and "taskDefinitionList" in item
        for item in definitions
    )
    if is_dolphinscheduler:
        if len(definitions) != 1:
            raise ServiceError(
                "DolphinScheduler 导出文件包含多个工作流，请拆分后逐个导入"
            )
        try:
            config, warnings = convert_dolphinscheduler_definition(definitions[0])
        except ValueError as exc:
            raise ServiceError(f"DolphinScheduler 转换失败：{exc}") from exc
        return {
            "source": SOURCE_DOLPHINSCHEDULER,
            "source_label": "DolphinScheduler",
            "definition": dump_workflow_yaml(config),
            "warnings": warnings,
            "filename": f"dagr_{Path(filename).stem}.yaml",
        }

    try:
        Workflow.from_yaml(content, fallback_name=Path(filename).stem)
    except WorkflowError as exc:
        raise ServiceError(f"无法识别工作流来源：{exc}") from exc
    return {
        "source": "dag-runner",
        "source_label": "DAG Runner YAML",
        "definition": content,
        "warnings": [],
        "filename": filename,
    }


def _row_dict(row) -> dict:
    return {key: row[key] for key in row.keys()}


def _command_text(command, args) -> str:
    if command is None:
        return "条件分支"
    base = command if isinstance(command, str) else " ".join(command)
    return " ".join([base, *args]).strip()


def _safe_next(value) -> str:
    candidate = str(value or "/")
    parsed = urlsplit(candidate)
    if (
        parsed.scheme
        or parsed.netloc
        or not candidate.startswith("/")
        or candidate.startswith("//")
        or "\\" in candidate
        or any(ord(character) < 32 for character in candidate)
        or candidate.startswith("/login")
        or candidate.startswith("/api/auth/")
    ):
        return "/"
    return candidate


_ENGLISH_ERRORS = {
    "登录已失效，请重新登录": "Your session has expired. Please sign in again",
    "登录失败次数过多，请在 10 分钟后重试": "Too many failed sign-in attempts. Try again in 10 minutes",
    "账号或密码错误": "Incorrect username or password",
    "请选择 YAML 文件": "Select a YAML file",
    "YAML 文件必须使用 UTF-8 编码": "The YAML file must use UTF-8 encoding",
    "请选择工作流文件": "Select a workflow file",
    "工作流文件必须使用 UTF-8 编码": "The workflow file must use UTF-8 encoding",
    "示例工作流文件不可用": "The example workflow is unavailable",
    "工作流运行中，不能编辑": "A running workflow cannot be edited",
    "请先下线定时，再编辑工作流": "Disable the schedule before editing the workflow",
    "工作流文件不能为空": "The workflow file cannot be empty",
    "无法识别的 XML 工作流格式": "Unrecognized XML workflow format",
    "DolphinScheduler 导出文件包含多个工作流，请拆分后逐个导入": "The DolphinScheduler export contains multiple workflows; split it and import each workflow separately",
}

_ENGLISH_ERROR_PREFIXES = {
    "Windows Task Scheduler 转换失败：": "Windows Task Scheduler conversion failed: ",
    "DolphinScheduler 转换失败：": "DolphinScheduler conversion failed: ",
    "无法识别工作流来源：": "Could not detect the workflow source: ",
}


def _localized_error(message: str, language: str) -> str:
    if language != "en":
        return message
    if message in _ENGLISH_ERRORS:
        return _ENGLISH_ERRORS[message]
    for prefix, translation in _ENGLISH_ERROR_PREFIXES.items():
        if message.startswith(prefix):
            return translation + message[len(prefix):]
    return message


def _secure_cookie_enabled(allow_insecure_remote_login: bool = False) -> bool:
    configured = os.getenv("DAGRUNNER_COOKIE_SECURE", "").strip().lower()
    return request.is_secure or (
        not allow_insecure_remote_login
        and configured in {"1", "true", "yes", "on"}
    )
