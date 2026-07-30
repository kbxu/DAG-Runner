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


@dataclass(frozen=True)
class Task:
    name: str
    command: str | tuple[str, ...]
    depends: tuple[str, ...] = ()
    args: tuple[str, ...] = ()
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    timeout: int | None = None
    enabled: bool = True
    description: str = ""


@dataclass(frozen=True)
class ScheduleDefinition:
    cron: str
    timezone: str = "Asia/Shanghai"
    enabled: bool = False


@dataclass(frozen=True)
class Workflow:
    name: str
    tasks: dict[str, Task]
    workdir: Path
    env: dict[str, str] = field(default_factory=dict)
    setup: str | dict[str, str] = ""
    schedule: ScheduleDefinition | None = None
    description: str = ""

    @classmethod
    def load(cls, path: str | Path) -> "Workflow":
        config_path = Path(path).resolve()
        try:
            data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise WorkflowError(f"cannot read workflow file {config_path}: {exc}") from exc
        except yaml.YAMLError as exc:
            raise WorkflowError(f"invalid YAML in {config_path}: {exc}") from exc

        if not isinstance(data, dict):
            raise WorkflowError("workflow YAML must be a mapping")
        name = data.get("name") or config_path.stem
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
            command = raw.get("command")
            if isinstance(command, list) and all(isinstance(x, str) for x in command):
                command = tuple(command)
            elif not isinstance(command, str) or not command.strip():
                raise WorkflowError(f"task {task_name!r} needs a string or string-list command")
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
                env=_string_dict(raw.get("env", {}), f"task {task_name!r} env"),
                timeout=timeout,
                enabled=bool(raw.get("enabled", True)),
                description=str(raw.get("description", "")),
            )

        workdir_value = data.get("workdir", ".")
        if not isinstance(workdir_value, str):
            raise WorkflowError("workflow 'workdir' must be a string")
        workdir = Path(workdir_value)
        if not workdir.is_absolute():
            workdir = (config_path.parent / workdir).resolve()
        workflow = cls(
            name=name,
            tasks=tasks,
            workdir=workdir,
            env=_string_dict(data.get("env", {}), "workflow env"),
            setup=_setup_value(data.get("setup", "")),
            schedule=_schedule_value(data.get("schedule")),
            description=str(data.get("description", "")),
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
            return self.workdir
        value = Path(task.cwd)
        return value if value.is_absolute() else (self.workdir / value).resolve()

    def setup_for_current_platform(self) -> str:
        if isinstance(self.setup, str):
            return self.setup
        platform_name = "windows" if os.name == "nt" else "linux"
        return self.setup.get(platform_name, self.setup.get("default", ""))


def _string_tuple(value: Any, task_name: str, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise WorkflowError(f"task {task_name!r} {field_name} must be a list of strings")
    return tuple(value)


def _string_dict(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise WorkflowError(f"{label} must be a mapping")
    return {str(key): str(item) for key, item in value.items()}


def _optional_string(value: Any, task_name: str, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise WorkflowError(f"task {task_name!r} {field_name} must be a string")
    return value


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
