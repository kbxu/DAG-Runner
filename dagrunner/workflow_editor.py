"""Validate and extend unsaved workflows without executing or persisting them."""

import secrets

import yaml

from .workflow import Workflow, WorkflowError
from .migrate_workflows import LiteralDumper


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
            }
            for task in workflow.tasks.values()
        ]
    }


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
            "command": ["python", "-c", "print('TODO: implement task')"],
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
    definition = yaml.dump(
        data, Dumper=LiteralDumper, allow_unicode=True, sort_keys=False, width=1000
    )
    return {"definition": definition, "added": added, **preview_definition(definition, workflow_name)}
