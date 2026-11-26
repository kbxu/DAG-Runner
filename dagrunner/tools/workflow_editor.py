"""Validate and extend unsaved workflows without executing or persisting them."""

import os
import secrets

import yaml

from ..workflow import LiteralDumper, Workflow, WorkflowError


def demo_setup() -> str:
    """Two-line example setup: change directory and define a global variable."""
    if os.name == "nt":
        return "cd demo\nset DEMO_ENV=local"
    return "cd demo\nexport DEMO_ENV=local"


def demo_command(description: str, python_command: str) -> str:
    """Three-line example command: platform echo, variable definition, python step."""
    label = description.strip() or "task"
    if os.name == "nt":
        return "\n".join(
            (
                f'echo "windows - {label}"',
                "set DEMO_STEP=1",
                python_command,
            )
        )
    return "\n".join(
        (
            f'echo "linux - {label}"',
            "export DEMO_STEP=1",
            python_command,
        )
    )


class EditorLoader(yaml.SafeLoader):
    """Reject duplicate keys instead of silently hiding nodes in the preview."""


def _mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            if key in result:
                raise WorkflowError(
                    f"YAML 第 {key_node.start_mark.line + 1} 行存在重复字段：{key}"
                )
            result[key] = loader.construct_object(value_node, deep=deep)
        except TypeError as exc:
            raise WorkflowError("YAML 字段名必须是简单值") from exc
    return result


EditorLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def preview_definition(definition, workflow_name=None):
    if not isinstance(definition, str):
        raise WorkflowError("工作流 YAML 必须是文本")
    try:
        yaml.load(definition, Loader=EditorLoader)
        workflow = Workflow.from_yaml(definition, name_override=workflow_name)
    except (yaml.YAMLError, RecursionError) as exc:
        raise WorkflowError(f"无法解析工作流 YAML：{exc}") from exc
    return {
        "runner": workflow.runner or "local",
        "tasks": [
            {
                "name": task.name,
                "description": task.description,
                "depends": list(task.depends),
                "enabled": task.enabled,
                "type": task.task_type,
                "command": task.command,
                "success": list(task.success),
                "failure": list(task.failure),
                "runner": workflow.task_runner(task),
            }
            for task in workflow.tasks.values()
        ],
    }


def build_model(definition):
    """把工作流 YAML 解析成表单模式可用的结构化模型（保持任务顺序）。"""
    data = yaml.safe_load(definition)
    if not isinstance(data, dict):
        raise WorkflowError("工作流 YAML 必须是映射")
    tasks = []
    for name, raw in (data.get("tasks") or {}).items():
        if not isinstance(raw, dict):
            continue
        task_type = raw.get("type", "command")
        command = raw.get("command")
        if isinstance(command, list):
            command = "\n".join(str(item) for item in command)
        relation = "AND"
        condition = raw.get("condition")
        if isinstance(condition, dict):
            relation = str(condition.get("relation", "AND")).upper()
        tasks.append(
            {
                "name": str(name),
                "description": str(raw.get("description", "")),
                "type": task_type,
                "enabled": bool(raw.get("enabled", True)),
                "depends": [str(item) for item in _string_list(raw.get("depends"))],
                "command": command if isinstance(command, str) else "",
                "conditionRelation": relation,
                "success": [str(item) for item in _string_list(raw.get("success"))],
                "failure": [str(item) for item in _string_list(raw.get("failure"))],
            }
        )
    description = data.get("name") if isinstance(data.get("name"), str) else ""
    setup = data.get("setup")
    schedule = None
    raw_schedule = data.get("schedule")
    if isinstance(raw_schedule, dict):
        crons = raw_schedule.get("crons", raw_schedule.get("cron"))
        if isinstance(crons, str):
            crons = [crons]
        if isinstance(crons, list):
            schedule = {
                "crons": [str(item) for item in crons if str(item).strip()],
                "timezone": str(raw_schedule.get("timezone", "Asia/Shanghai")),
                "enabled": bool(raw_schedule.get("enabled", False)),
            }
    return {
        "description": description,
        "runner": str(data.get("runner") or ""),
        "setup": setup if isinstance(setup, str) else "",
        "schedule": schedule,
        "tasks": tasks,
    }


def _string_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, str)]
    return []


def merge_model(definition, model):
    """把表单模型合并回原 YAML，保留未编辑字段与顶层配置。"""
    data = yaml.safe_load(definition)
    if not isinstance(data, dict):
        raise WorkflowError("工作流 YAML 必须是映射")
    originals = data.get("tasks") or {}
    if not isinstance(originals, dict):
        originals = {}
    tasks = {}
    for item in model.get("tasks", []):
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        raw = dict(originals.get(name, {}))
        for key in ("command", "condition", "success", "failure"):
            raw.pop(key, None)
        description = str(item.get("description", ""))
        if description:
            raw["description"] = description
        elif "description" in raw:
            del raw["description"]
        task_type = item.get("type") if item.get("type") in ("command", "condition") else "command"
        raw["type"] = task_type
        if bool(item.get("enabled", True)):
            raw.pop("enabled", None)
        else:
            raw["enabled"] = False
        depends = [str(entry) for entry in (item.get("depends") or []) if str(entry)]
        if depends:
            raw["depends"] = depends
        elif "depends" in raw:
            raw["depends"] = []
        if task_type == "command":
            raw["command"] = item.get("command") or ""
        else:
            relation = "OR" if str(item.get("conditionRelation", "AND")).upper() == "OR" else "AND"
            raw["condition"] = {
                "relation": relation,
                "groups": [
                    {
                        "relation": "AND",
                        "items": [
                            {"task": entry, "status": "SUCCESS"} for entry in depends
                        ],
                    }
                ],
            }
            success = [str(entry) for entry in (item.get("success") or []) if str(entry)]
            failure = [str(entry) for entry in (item.get("failure") or []) if str(entry)]
            if success:
                raw["success"] = success
            if failure:
                raw["failure"] = failure
        tasks[name] = raw
    data["tasks"] = tasks
    if model.get("description") is not None:
        data["name"] = str(model.get("description") or "")
    if "setup" in model:
        setup = model.get("setup") or ""
        if setup:
            data["setup"] = setup
        else:
            data.pop("setup", None)
    if "runner" in model:
        runner = str(model.get("runner") or "")
        if runner:
            data["runner"] = runner
        else:
            data.pop("runner", None)
    if model.get("schedule") is not None:
        schedule = model.get("schedule") or {}
        crons = [
            str(item).strip()
            for item in (schedule.get("crons") or [])
            if str(item).strip()
        ]
        if crons:
            data["schedule"] = {
                "crons": crons,
                "timezone": str(schedule.get("timezone") or "Asia/Shanghai"),
                "enabled": bool(schedule.get("enabled", False)),
            }
        else:
            data.pop("schedule", None)
    dumped = yaml.dump(
        data, Dumper=LiteralDumper, allow_unicode=True, sort_keys=False, width=1000
    )
    preview_definition(dumped)
    return dumped


def extend_definition(definition, parent, kind, branch, workflow_name=None):
    preview_definition(definition, workflow_name)
    data = yaml.safe_load(definition)
    tasks = data["tasks"]
    if not isinstance(parent, str) or parent not in tasks:
        raise WorkflowError("所选节点已不存在，请重新选择")
    if kind not in ("command", "condition"):
        raise WorkflowError("请选择普通模块或条件分支")
    if tasks[parent].get("type") == "condition" and branch not in ("success", "failure"):
        raise WorkflowError("请选择条件成立或不成立分支")

    def new_id():
        while True:
            value = f"task_{secrets.token_hex(6)}"
            if value not in tasks:
                return value

    def command_task(description, dependency):
        return {
            "description": description,
            "command": demo_command(
                description, 'python -c "print(\'TODO implement task\')"'
            ),
            "depends": [dependency],
        }

    added = new_id()
    if kind == "command":
        tasks[added] = command_task("新增普通任务", parent)
    else:
        tasks[added] = {
            "description": "判断上游任务是否成功",
            "type": "condition",
            "depends": [parent],
            "condition": {"relation": "AND", "groups": [{
                "relation": "AND", "items": [{"task": parent, "status": "SUCCESS"}]
            }]},
        }
        success = new_id()
        tasks[success] = command_task("条件成立后执行", added)
        failure = new_id()
        tasks[failure] = command_task("条件不成立后执行", added)
        tasks[added].update(success=[success], failure=[failure])
    if tasks[parent].get("type") == "condition":
        # Copy the list so YAML aliases cannot mutate another branch by accident.
        tasks[parent] = dict(tasks[parent])
        tasks[parent][branch] = [*tasks[parent].get(branch, []), added]

    # Keep the generated YAML readable: put the new module(s) directly under the
    # selected module instead of appending them at the end of the file. When the
    # selected module already has child modules, place them after the last one.
    inserted = [(added, tasks[added])]
    if kind == "condition":
        inserted.extend([(success, tasks[success]), (failure, tasks[failure])])
    inserted_names = {name for name, _ in inserted}

    def references_target(node, target):
        if not isinstance(node, dict):
            return False
        for field in ("depends", "success", "failure"):
            value = node.get(field)
            if value == target:
                return True
            if isinstance(value, (list, tuple)) and target in value:
                return True
        return False

    child_keys = [
        key
        for key, value in tasks.items()
        if key != parent and key not in inserted_names and references_target(value, parent)
    ]
    order = list(tasks.keys())
    anchor = order.index(parent)
    if child_keys:
        anchor = max(anchor, max(order.index(key) for key in child_keys))
    reordered = {}
    for index, key in enumerate(order):
        if key in inserted_names:
            continue
        reordered[key] = tasks[key]
        if index == anchor:
            for name, value in inserted:
                reordered[name] = value
    data["tasks"] = reordered

    definition = yaml.dump(
        data, Dumper=LiteralDumper, allow_unicode=True, sort_keys=False, width=1000
    )
    return {"definition": definition, "added": added, **preview_definition(definition, workflow_name)}
