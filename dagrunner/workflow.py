from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import Any

import yaml


_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]+$")


class WorkflowError(ValueError):
    """Raised when a workflow definition is invalid."""


def migrate_legacy_env(content: str) -> tuple[str, bool]:
    """Promote legacy workflow/task env mappings into setup."""
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError:
        return content, False
    if not isinstance(data, dict):
        return content, False
    had_legacy_env = "env" in data
    values = data.pop("env", {}) or {}
    if not isinstance(values, dict):
        return content, False
    values = {str(name): str(value) for name, value in values.items()}
    tasks = data.get("tasks") or {}
    if isinstance(tasks, dict):
        for task_name, task in tasks.items():
            if not isinstance(task, dict) or "env" not in task:
                continue
            had_legacy_env = True
            task_values = task.pop("env") or {}
            if not isinstance(task_values, dict):
                raise WorkflowError(f"task {task_name!r} env must be a mapping")
            for name, value in task_values.items():
                name, value = str(name), str(value)
                if name in values and values[name] != value:
                    raise WorkflowError(
                        f"legacy env {name!r} has conflicting values; migrate it manually"
                    )
                values[name] = value
    if not values and not had_legacy_env:
        return content, False
    if not values:
        return yaml.safe_dump(
            data, allow_unicode=True, sort_keys=False, width=1000
        ), True

    def assignments(shell: str) -> str:
        lines = []
        for name, value in values.items():
            escaped = value.replace("'", "''" if shell == "powershell" else "'\"'\"'")
            lines.append(
                f"$env:{name} = '{escaped}'"
                if shell == "powershell"
                else f"export {name}='{escaped}'"
            )
        return "\n".join(lines)

    setup = data.get("setup")
    if isinstance(setup, dict):
        if setup:
            updated = dict(setup)
            for platform, script in setup.items():
                shell = "powershell" if platform == "windows" else "bash"
                updated[platform] = "\n".join((assignments(shell), str(script).strip())).strip()
        else:
            updated = {
                "linux": assignments("bash"),
                "windows": assignments("powershell"),
            }
        data["setup"] = updated
    elif isinstance(setup, str) and setup.strip():
        shell = (
            "powershell"
            if "$env:" in setup or "Set-Location" in setup
            else "bash"
        )
        data["setup"] = "\n".join((assignments(shell), setup.strip())).strip()
    else:
        data["setup"] = {
            "linux": assignments("bash"),
            "windows": assignments("powershell"),
        }
    return yaml.safe_dump(
        data, allow_unicode=True, sort_keys=False, width=1000
    ), True


@dataclass(frozen=True)
class ConditionItem:
    task: str
    status: str


@dataclass(frozen=True)
class ConditionGroup:
    relation: str
    items: tuple[ConditionItem, ...]

    def evaluate(self, states: dict[str, str]) -> bool:
        matches = (states[item.task] == item.status for item in self.items)
        return all(matches) if self.relation == "AND" else any(matches)


@dataclass(frozen=True)
class ConditionSpec:
    relation: str
    groups: tuple[ConditionGroup, ...]

    def evaluate(self, states: dict[str, str]) -> bool:
        matches = (group.evaluate(states) for group in self.groups)
        return all(matches) if self.relation == "AND" else any(matches)

    @property
    def referenced_tasks(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(item.task for group in self.groups for item in group.items)
        )


@dataclass(frozen=True)
class Task:
    name: str
    command: str | tuple[str, ...] | None = None
    depends: tuple[str, ...] = ()
    args: tuple[str, ...] = ()
    cwd: str | None = None
    timeout: int | None = None
    enabled: bool = True
    description: str = ""
    task_type: str = "command"
    condition: ConditionSpec | None = None
    success: tuple[str, ...] = ()
    failure: tuple[str, ...] = ()

    @property
    def is_condition(self) -> bool:
        return self.task_type == "condition"


@dataclass(frozen=True)
class ScheduleDefinition:
    cron: str
    timezone: str = "Asia/Shanghai"
    enabled: bool = False


@dataclass(frozen=True)
class Workflow:
    name: str
    tasks: dict[str, Task]
    config_dir: Path = field(default_factory=Path.cwd, repr=False, compare=False)
    setup: str | dict[str, str] = ""
    schedule: ScheduleDefinition | None = None
    description: str = ""

    @classmethod
    def load(cls, path: str | Path) -> "Workflow":
        config_path = Path(path).resolve()
        try:
            content = config_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise WorkflowError(f"cannot read workflow file {config_path}: {exc}") from exc
        return cls.from_yaml(content, config_dir=config_path.parent, fallback_name=config_path.stem)

    @classmethod
    def from_yaml(
        cls,
        content: str,
        *,
        config_dir: str | Path | None = None,
        fallback_name: str = "imported_workflow",
        name_override: str | None = None,
        description_override: str | None = None,
    ) -> "Workflow":
        try:
            data = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            raise WorkflowError(f"invalid workflow YAML: {exc}") from exc

        if not isinstance(data, dict):
            raise WorkflowError("workflow YAML must be a mapping")
        if "workdir" in data:
            raise WorkflowError(
                "workflow 'workdir' is no longer supported; change directories in 'setup'"
            )
        if "env" in data:
            raise WorkflowError("workflow 'env' is no longer supported; define variables in 'setup'")
        name = name_override or data.get("name") or fallback_name
        if not isinstance(name, str) or not name.strip():
            raise WorkflowError("workflow 'name' must be a non-empty string")
        if not _IDENTIFIER.fullmatch(name):
            raise WorkflowError("workflow 'name' may contain only letters, digits, _, . and -")
        raw_tasks = data.get("tasks")
        if not isinstance(raw_tasks, dict) or not raw_tasks:
            raise WorkflowError("workflow 'tasks' must be a non-empty mapping")

        tasks: dict[str, Task] = {}
        for task_name, raw in raw_tasks.items():
            if not isinstance(task_name, str) or not task_name:
                raise WorkflowError("task names must be non-empty strings")
            if not _IDENTIFIER.fullmatch(task_name):
                raise WorkflowError(
                    f"task name {task_name!r} may contain only letters, digits, _, . and -"
                )
            if not isinstance(raw, dict):
                raise WorkflowError(f"task {task_name!r} must be a mapping")
            if "env" in raw:
                raise WorkflowError(
                    f"task {task_name!r} env is no longer supported; define variables in setup or command"
                )
            task_type = raw.get("type", "command")
            if task_type not in {"command", "condition"}:
                raise WorkflowError(
                    f"task {task_name!r} type must be 'command' or 'condition'"
                )
            command = raw.get("command")
            condition = None
            success: tuple[str, ...] = ()
            failure: tuple[str, ...] = ()
            if task_type == "command":
                if isinstance(command, list) and all(isinstance(x, str) for x in command):
                    command = tuple(command)
                elif not isinstance(command, str) or not command.strip():
                    raise WorkflowError(
                        f"task {task_name!r} needs a string or string-list command"
                    )
            else:
                if command is not None:
                    raise WorkflowError(
                        f"condition task {task_name!r} must not define command"
                    )
                condition = _condition_value(raw.get("condition"), task_name)
                success = _string_tuple(raw.get("success", []), task_name, "success")
                failure = _string_tuple(raw.get("failure", []), task_name, "failure")
                if not success and not failure:
                    raise WorkflowError(
                        f"condition task {task_name!r} needs a success or failure branch"
                    )
            depends = _string_tuple(raw.get("depends", []), task_name, "depends")
            args = _string_tuple(raw.get("args", []), task_name, "args")
            timeout = raw.get("timeout")
            if timeout is not None and (not isinstance(timeout, int) or timeout <= 0):
                raise WorkflowError(f"task {task_name!r} timeout must be a positive integer")
            tasks[task_name] = Task(
                name=task_name,
                command=command,
                depends=depends,
                args=args,
                cwd=_optional_string(raw.get("cwd"), task_name, "cwd"),
                timeout=timeout,
                enabled=bool(raw.get("enabled", True)),
                description=str(raw.get("description", "")),
                task_type=task_type,
                condition=condition,
                success=success,
                failure=failure,
            )

        workflow = cls(
            name=name,
            tasks=tasks,
            config_dir=Path(config_dir or Path.cwd()).resolve(),
            setup=_setup_value(data.get("setup", "")),
            schedule=_schedule_value(data.get("schedule")),
            description=(
                description_override
                if description_override is not None
                else str(data.get("description", ""))
            ),
        )
        workflow.topological_order()  # Validate references and cycles now.
        return workflow

    def topological_order(self) -> list[str]:
        indegree = {name: 0 for name in self.tasks}
        children: dict[str, list[str]] = {name: [] for name in self.tasks}
        for name, task in self.tasks.items():
            for dependency in task.depends:
                if dependency not in self.tasks:
                    raise WorkflowError(f"task {name!r} depends on unknown task {dependency!r}")
                if dependency == name:
                    raise WorkflowError(f"task {name!r} cannot depend on itself")
                indegree[name] += 1
                children[dependency].append(name)
            if task.is_condition:
                assert task.condition is not None
                missing_inputs = set(task.condition.referenced_tasks) - set(task.depends)
                if missing_inputs:
                    raise WorkflowError(
                        f"condition task {name!r} must depend on referenced task(s): "
                        f"{', '.join(sorted(missing_inputs))}"
                    )
                overlap = set(task.success) & set(task.failure)
                if overlap:
                    raise WorkflowError(
                        f"condition task {name!r} has targets in both branches: "
                        f"{', '.join(sorted(overlap))}"
                    )
                for target in (*task.success, *task.failure):
                    if target not in self.tasks:
                        raise WorkflowError(
                            f"condition task {name!r} targets unknown task {target!r}"
                        )
                    if name not in self.tasks[target].depends:
                        raise WorkflowError(
                            f"condition branch target {target!r} must depend on {name!r}"
                        )

        ready = [name for name in self.tasks if indegree[name] == 0]
        order: list[str] = []
        while ready:
            name = ready.pop(0)
            order.append(name)
            for child in children[name]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
        if len(order) != len(self.tasks):
            cyclic = sorted(name for name, degree in indegree.items() if degree > 0)
            raise WorkflowError(f"workflow contains a dependency cycle involving: {', '.join(cyclic)}")
        return order

    def descendants(self, task_name: str) -> set[str]:
        if task_name not in self.tasks:
            raise WorkflowError(f"unknown --from task {task_name!r}")
        children: dict[str, list[str]] = {name: [] for name in self.tasks}
        for name, task in self.tasks.items():
            for dependency in task.depends:
                children[dependency].append(name)
        result = {task_name}
        pending = [task_name]
        while pending:
            current = pending.pop()
            for child in children[current]:
                if child not in result:
                    result.add(child)
                    pending.append(child)
        return result

    def task_cwd(self, task: Task) -> Path:
        if not task.cwd:
            return self.config_dir
        value = Path(task.cwd)
        return value if value.is_absolute() else (self.config_dir / value).resolve()

    def setup_for_current_platform(self) -> str:
        if isinstance(self.setup, str):
            return self.setup
        platform_name = "windows" if os.name == "nt" else "linux"
        return self.setup.get(platform_name, self.setup.get("default", ""))


def _string_tuple(value: Any, task_name: str, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise WorkflowError(f"task {task_name!r} {field_name} must be a list of strings")
    return tuple(value)


def _optional_string(value: Any, task_name: str, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise WorkflowError(f"task {task_name!r} {field_name} must be a string")
    return value


def _condition_value(value: Any, task_name: str) -> ConditionSpec:
    if not isinstance(value, dict):
        raise WorkflowError(f"condition task {task_name!r} condition must be a mapping")
    relation = _condition_relation(value.get("relation", "AND"), task_name)
    raw_groups = value.get("groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise WorkflowError(
            f"condition task {task_name!r} condition.groups must be a non-empty list"
        )
    groups: list[ConditionGroup] = []
    for group_index, raw_group in enumerate(raw_groups, start=1):
        if not isinstance(raw_group, dict):
            raise WorkflowError(
                f"condition task {task_name!r} group {group_index} must be a mapping"
            )
        group_relation = _condition_relation(
            raw_group.get("relation", "AND"), task_name
        )
        raw_items = raw_group.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise WorkflowError(
                f"condition task {task_name!r} group {group_index} items must be a non-empty list"
            )
        items: list[ConditionItem] = []
        for item_index, raw_item in enumerate(raw_items, start=1):
            if not isinstance(raw_item, dict):
                raise WorkflowError(
                    f"condition task {task_name!r} group {group_index} item "
                    f"{item_index} must be a mapping"
                )
            referenced_task = raw_item.get("task")
            if not isinstance(referenced_task, str) or not referenced_task:
                raise WorkflowError(
                    f"condition task {task_name!r} group {group_index} item "
                    f"{item_index} needs a task"
                )
            status = str(raw_item.get("status", "")).upper()
            if status == "FAILURE":
                status = "FAILED"
            if status not in {"SUCCESS", "FAILED", "SKIPPED"}:
                raise WorkflowError(
                    f"condition task {task_name!r} has unsupported status {status!r}"
                )
            items.append(ConditionItem(referenced_task, status))
        groups.append(ConditionGroup(group_relation, tuple(items)))
    return ConditionSpec(relation, tuple(groups))


def _condition_relation(value: Any, task_name: str) -> str:
    relation = str(value).upper()
    if relation not in {"AND", "OR"}:
        raise WorkflowError(
            f"condition task {task_name!r} relation must be AND or OR"
        )
    return relation


def _setup_value(value: Any) -> str | dict[str, str]:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        allowed = {"linux", "windows", "default"}
        unknown = set(value) - allowed
        if unknown or not all(isinstance(item, str) for item in value.values()):
            raise WorkflowError(
                "workflow 'setup' mapping accepts only string linux/windows/default values"
            )
        return {str(key): item.strip() for key, item in value.items()}
    raise WorkflowError("workflow 'setup' must be a string or platform mapping")


def _schedule_value(value: Any) -> ScheduleDefinition | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise WorkflowError("workflow 'schedule' must be a mapping")
    cron = value.get("cron")
    if not isinstance(cron, str) or len(cron.split()) != 5:
        raise WorkflowError("workflow schedule.cron must be a five-field cron string")
    timezone_name = value.get("timezone", "Asia/Shanghai")
    if not isinstance(timezone_name, str) or not timezone_name:
        raise WorkflowError("workflow schedule.timezone must be a non-empty string")
    return ScheduleDefinition(
        cron=cron,
        timezone=timezone_name,
        enabled=bool(value.get("enabled", False)),
    )
