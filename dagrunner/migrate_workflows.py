#!/usr/bin/env python3
"""Convert external scheduler exports into reviewable DAG Runner YAML files.

This tool only reads JSON/XML and writes YAML. It never executes a task command.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import shlex
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Any
from xml.etree import ElementTree

import yaml


class LiteralDumper(yaml.SafeDumper):
    pass


def _str_presenter(dumper, value):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


LiteralDumper.add_representer(str, _str_presenter)


SOURCE_DOLPHINSCHEDULER = "dolphinscheduler"
SOURCE_WINDOWS_TASK_SCHEDULER = "windows-task-scheduler"
SOURCE_TYPES = (SOURCE_DOLPHINSCHEDULER, SOURCE_WINDOWS_TASK_SCHEDULER)
WINDOWS_TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
WINDOWS_WEEKDAYS = {
    "Monday": "mon",
    "Tuesday": "tue",
    "Wednesday": "wed",
    "Thursday": "thu",
    "Friday": "fri",
    "Saturday": "sat",
    "Sunday": "sun",
}


def _converted_output_path(
    source: Path, output_dir: Path, index: int, total: int
) -> Path:
    """Build a stable output name from the source export filename."""
    suffix = "" if total == 1 else f"_{index}"
    return output_dir / f"dagr_{source.stem}{suffix}.yaml"


def convert_dolphinscheduler_crontab(crontab: str) -> str:
    """Convert a DolphinScheduler Quartz cron into APScheduler's five fields."""
    fields = crontab.split()
    if len(fields) not in {6, 7}:
        if len(fields) == 5:
            return crontab
        raise ValueError(
            f"unsupported DolphinScheduler cron {crontab!r}: expected 5, 6, or 7 fields"
        )

    second, minute, hour, day, month, weekday = fields[:6]
    year = fields[6] if len(fields) == 7 else "*"
    if second != "0":
        raise ValueError(
            f"cannot convert cron {crontab!r}: this runner does not schedule by seconds"
        )
    if year not in {"*", "?"}:
        raise ValueError(
            f"cannot convert cron {crontab!r}: this runner does not support a year field"
        )
    if any(char.isdigit() for char in weekday) and weekday not in {"*", "?"}:
        raise ValueError(
            f"cannot safely convert cron {crontab!r}: numeric weekdays differ between "
            "Quartz and APScheduler; use MON-SUN names"
        )

    day = "*" if day == "?" else day
    weekday = "*" if weekday == "?" else weekday.lower()
    return " ".join((minute, hour, day, month.lower(), weekday))


def convert_dolphinscheduler_export(
    source: Path,
    output_dir: Path,
    setup: str = "",
    *,
    exclude_disabled: bool = False,
    setup_shell: str = "bash",
) -> list[Path]:
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot parse {source}: {exc}") from exc
    definitions = payload if isinstance(payload, list) else [payload]
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for index, item in enumerate(definitions, start=1):
        config, warnings = convert_dolphinscheduler_definition(
            item,
            setup,
            exclude_disabled=exclude_disabled,
            setup_shell=setup_shell,
        )
        destination = _converted_output_path(
            source, output_dir, index, len(definitions)
        )
        destination.write_text(
            yaml.dump(
                config,
                Dumper=LiteralDumper,
                allow_unicode=True,
                sort_keys=False,
                width=1000,
            ),
            encoding="utf-8",
        )
        written.append(destination)
        print(f"wrote {destination}")
        for warning in warnings:
            print(f"  REVIEW: {warning}", file=sys.stderr)
    return written


def convert_windows_task_scheduler_export(
    source: Path,
    output_dir: Path,
    setup: str = "",
    *,
    timezone_name: str = "Asia/Shanghai",
) -> list[Path]:
    try:
        root = ElementTree.fromstring(source.read_bytes())
    except (OSError, ElementTree.ParseError) as exc:
        raise ValueError(f"cannot parse {source}: {exc}") from exc

    configs, warnings = convert_windows_task_scheduler_definition(
        root,
        source.stem,
        setup,
        timezone_name=timezone_name,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for index, config in enumerate(configs, start=1):
        destination = _converted_output_path(source, output_dir, index, len(configs))
        destination.write_text(
            yaml.dump(
                config,
                Dumper=LiteralDumper,
                allow_unicode=True,
                sort_keys=False,
                width=1000,
            ),
            encoding="utf-8",
        )
        written.append(destination)
        print(f"wrote {destination}")
    for warning in warnings:
        print(f"  REVIEW: {warning}", file=sys.stderr)
    return written


def convert_windows_task_scheduler_definition(
    root: ElementTree.Element,
    source_stem: str,
    setup: str = "",
    *,
    timezone_name: str = "Asia/Shanghai",
) -> tuple[list[dict[str, Any]], list[str]]:
    ns = {"task": WINDOWS_TASK_NAMESPACE}
    if root.tag != f"{{{WINDOWS_TASK_NAMESPACE}}}Task":
        raise ValueError("XML root is not a Windows Task Scheduler Task")

    uri = _xml_text(root, "task:RegistrationInfo/task:URI", ns)
    source_name = (uri or source_stem).strip("\\/").split("\\")[-1] or source_stem
    base_name = f"wts_{_identifier(source_name)}"
    description = (
        _xml_text(root, "task:RegistrationInfo/task:Description", ns)
        or source_name
    )
    author = _xml_text(root, "task:RegistrationInfo/task:Author", ns)
    source_enabled = _xml_bool(
        _xml_text(root, "task:Settings/task:Enabled", ns), default=True
    )
    warnings: list[str] = []

    actions: list[tuple[str, dict[str, Any]]] = []
    previous_task: str | None = None
    for index, action in enumerate(
        root.findall("task:Actions/task:Exec", ns), start=1
    ):
        executable = _xml_text(action, "task:Command", ns)
        if not executable:
            warnings.append(f"omitted Exec action {index} without Command")
            continue
        task_name = f"action_{index}"
        task: dict[str, Any] = {
            "description": f"执行 {PureWindowsPath(executable).name}",
            "command": [executable, *_split_windows_arguments(
                _xml_text(action, "task:Arguments", ns) or ""
            )],
            "depends": [previous_task] if previous_task else [],
        }
        working_directory = _xml_text(action, "task:WorkingDirectory", ns)
        if working_directory:
            task["cwd"] = working_directory
        actions.append((task_name, task))
        previous_task = task_name

    unsupported_actions = [
        child.tag.rsplit("}", 1)[-1]
        for actions_node in root.findall("task:Actions", ns)
        for child in actions_node
        if child.tag != f"{{{WINDOWS_TASK_NAMESPACE}}}Exec"
    ]
    if unsupported_actions:
        warnings.append(
            "omitted unsupported Windows actions: "
            + ", ".join(sorted(set(unsupported_actions)))
        )
    if not actions:
        raise ValueError("Windows task contains no convertible Exec actions")

    timeout = _iso_duration_seconds(
        _xml_text(root, "task:Settings/task:ExecutionTimeLimit", ns)
    )
    if timeout:
        for _, task in actions:
            task["timeout"] = timeout

    logon_type = _xml_text(root, "task:Principals/task:Principal/task:LogonType", ns)
    user_id = _xml_text(root, "task:Principals/task:Principal/task:UserId", ns)
    if logon_type or user_id:
        warnings.append(
            "source task account/logon settings are metadata only; the imported workflow "
            "runs as the DAG Runner service account"
        )

    trigger_crons: list[tuple[str, str]] = []
    for index, trigger in enumerate(
        root.findall("task:Triggers/*", ns), start=1
    ):
        if not _xml_bool(
            _xml_text(trigger, "task:Enabled", ns), default=True
        ):
            continue
        try:
            cron = _windows_trigger_cron(trigger, ns)
        except ValueError as exc:
            warnings.append(f"trigger {index} was not converted: {exc}")
            continue
        start_text = _xml_text(trigger, "task:StartBoundary", ns)
        if start_text:
            try:
                start = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
                if start.second or start.microsecond:
                    warnings.append(
                        f"trigger {index} seconds were omitted because five-field cron "
                        "has minute precision"
                    )
            except ValueError:
                pass
        if cron and cron not in {item[0] for item in trigger_crons}:
            trigger_crons.append((cron, trigger.tag.rsplit("}", 1)[-1]))

    if not trigger_crons:
        warnings.append(
            "no recurring trigger could be converted; the server will show its disabled "
            "generic schedule default"
        )
        trigger_crons = [(None, "unconverted")]
    elif len(trigger_crons) > 1:
        warnings.append(
            "multiple triggers were split into separate workflows; unlike the source "
            "task's MultipleInstancesPolicy, these workflows can overlap"
        )

    configs: list[dict[str, Any]] = []
    total = len(trigger_crons)
    for index, (cron, trigger_type) in enumerate(trigger_crons, start=1):
        workflow_name = base_name if total == 1 else f"{base_name}_{index}"
        workflow_description = (
            description if total == 1 else f"{description}（触发器 {index}/{total}）"
        )
        migration: dict[str, Any] = {
            "source": SOURCE_WINDOWS_TASK_SCHEDULER,
            "source_uri": uri,
            "source_author": author,
            "source_enabled": source_enabled,
            "source_trigger_type": trigger_type,
            "requires_manual_review": bool(warnings),
        }
        config: dict[str, Any] = {
            "name": workflow_name,
            "description": workflow_description,
        }
        if setup.strip():
            config["setup"] = setup.strip()
        config["tasks"] = dict(actions)
        if cron:
            config["schedule"] = {
                "cron": cron,
                "timezone": timezone_name,
                "enabled": False,
            }
        config["migration"] = migration
        configs.append(config)
    return configs, warnings


def _windows_trigger_cron(
    trigger: ElementTree.Element, ns: dict[str, str]
) -> str | None:
    trigger_type = trigger.tag.rsplit("}", 1)[-1]
    if trigger_type != "CalendarTrigger":
        raise ValueError(f"unsupported trigger type {trigger_type}")
    if trigger.find("task:Repetition", ns) is not None:
        raise ValueError("CalendarTrigger repetition intervals are not supported")
    start_text = _xml_text(trigger, "task:StartBoundary", ns)
    if not start_text:
        raise ValueError("CalendarTrigger has no StartBoundary")
    try:
        start = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid StartBoundary {start_text!r}") from exc

    minute, hour = start.minute, start.hour
    weekly = trigger.find("task:ScheduleByWeek", ns)
    if weekly is not None:
        interval = int(_xml_text(weekly, "task:WeeksInterval", ns) or "1")
        if interval != 1:
            raise ValueError("weekly intervals greater than 1 cannot be represented exactly")
        days_node = weekly.find("task:DaysOfWeek", ns)
        days = [
            WINDOWS_WEEKDAYS[child.tag.rsplit("}", 1)[-1]]
            for child in list(days_node or [])
            if child.tag.rsplit("}", 1)[-1] in WINDOWS_WEEKDAYS
        ]
        if not days:
            raise ValueError("ScheduleByWeek has no supported weekdays")
        return f"{minute} {hour} * * {','.join(days)}"

    daily = trigger.find("task:ScheduleByDay", ns)
    if daily is not None:
        interval = int(_xml_text(daily, "task:DaysInterval", ns) or "1")
        if interval != 1:
            raise ValueError("daily intervals greater than 1 cannot be represented exactly")
        return f"{minute} {hour} * * *"

    monthly = trigger.find("task:ScheduleByMonth", ns)
    if monthly is not None:
        days_node = monthly.find("task:DaysOfMonth", ns)
        months_node = monthly.find("task:Months", ns)
        days = [
            child.text.strip()
            for child in list(days_node or [])
            if child.tag.rsplit("}", 1)[-1] == "Day"
            and child.text
            and child.text.strip().isdigit()
        ]
        month_names = {
            name: str(index)
            for index, name in enumerate(
                (
                    "January", "February", "March", "April", "May", "June",
                    "July", "August", "September", "October", "November", "December",
                ),
                start=1,
            )
        }
        months = [
            month_names[child.tag.rsplit("}", 1)[-1]]
            for child in list(months_node or [])
            if child.tag.rsplit("}", 1)[-1] in month_names
        ]
        if not days:
            raise ValueError("ScheduleByMonth has no supported day")
        return f"{minute} {hour} {','.join(days)} {','.join(months) or '*'} *"

    raise ValueError("unsupported CalendarTrigger schedule type")


def _split_windows_arguments(arguments: str) -> list[str]:
    if not arguments.strip():
        return []
    if os.name == "nt":
        count = ctypes.c_int()
        command_line_to_argv = ctypes.windll.shell32.CommandLineToArgvW
        command_line_to_argv.argtypes = [
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_int),
        ]
        command_line_to_argv.restype = ctypes.POINTER(ctypes.c_wchar_p)
        argv = command_line_to_argv(f"placeholder.exe {arguments}", ctypes.byref(count))
        if not argv:
            raise ValueError("could not parse Windows task arguments")
        try:
            return [argv[index] for index in range(1, count.value)]
        finally:
            local_free = ctypes.windll.kernel32.LocalFree
            local_free.argtypes = [ctypes.c_void_p]
            local_free.restype = ctypes.c_void_p
            local_free(argv)
    return [
        token[1:-1] if len(token) >= 2 and token[0] == token[-1] == '"' else token
        for token in shlex.split(arguments, posix=False)
    ]


def _identifier(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_.-")
    return normalized or "imported_task"


def _xml_text(
    element: ElementTree.Element, path: str, ns: dict[str, str]
) -> str | None:
    found = element.find(path, ns)
    return found.text.strip() if found is not None and found.text else None


def _xml_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() == "true"


def _iso_duration_seconds(value: str | None) -> int | None:
    if not value:
        return None
    match = re.fullmatch(
        r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?",
        value,
    )
    if not match:
        return None
    days, hours, minutes, seconds = match.groups()
    total = (
        int(days or 0) * 86400
        + int(hours or 0) * 3600
        + int(minutes or 0) * 60
        + float(seconds or 0)
    )
    return int(total) or None


def _environment_lines(values: dict[str, Any], shell: str) -> list[str]:
    lines: list[str] = []
    for name, value in values.items():
        text = str(value)
        if shell == "powershell":
            escaped = text.replace("'", "''")
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                lines.append(f"$env:{name} = '{escaped}'")
            else:
                lines.append(f"${{env:{name}}} = '{escaped}'")
        else:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError(f"cannot export invalid Bash environment name {name!r}")
            escaped = text.replace("'", "'\"'\"'")
            lines.append(f"export {name}='{escaped}'")
    return lines


def convert_dolphinscheduler_definition(
    item: dict[str, Any],
    setup: str = "",
    *,
    exclude_disabled: bool = False,
    setup_shell: str = "bash",
) -> tuple[dict[str, Any], list[str]]:
    process = item.get("processDefinition") or {}
    process_code = process.get("code")
    if not process_code:
        raise ValueError("export item has no processDefinition.code")
    workflow_name = f"ds_{process_code}"
    definitions = {task["code"]: task for task in item.get("taskDefinitionList", [])}
    relations = item.get("processTaskRelationList", [])
    warnings: list[str] = []
    all_shell_codes = {
        code for code, definition in definitions.items() if definition.get("taskType") == "SHELL"
    }
    unsupported = {
        code: definition
        for code, definition in definitions.items()
        if definition.get("taskType") != "SHELL"
    }
    conditional_dependencies: dict[int, set[int]] = defaultdict(set)
    disabled_by_condition: set[int] = set()
    for code, definition in unsupported.items():
        task_type = definition.get("taskType", "UNKNOWN")
        if exclude_disabled and definition.get("flag") == "NO":
            continue
        if task_type != "CONDITIONS":
            warnings.append(
                f"omitted unsupported {task_type} node code={code} name={definition.get('name')!r}"
            )
            continue
        if definition.get("flag") == "NO":
            warnings.append(
                f"ignored disabled CONDITIONS node code={code} name={definition.get('name')!r}"
            )
            continue
        params = definition.get("taskParams") or {}
        result = params.get("conditionResult") or {}
        incoming = {
            rel.get("preTaskCode")
            for rel in relations
            if rel.get("postTaskCode") == code
            and rel.get("preTaskCode") in all_shell_codes
        }
        for group in ((params.get("dependence") or {}).get("dependTaskList") or []):
            for dependency in group.get("dependItemList") or []:
                if dependency.get("depTaskCode") in all_shell_codes:
                    incoming.add(dependency["depTaskCode"])
        for target in result.get("successNode") or []:
            if target in all_shell_codes:
                conditional_dependencies[target].update(incoming)
        for target in result.get("failedNode") or []:
            if target in all_shell_codes:
                disabled_by_condition.add(target)
        warnings.append(
            f"CONDITIONS code={code}: success branch converted to normal SUCCESS dependencies; "
            "failed branch targets were disabled and require manual review"
        )

    disabled_codes = {
        code
        for code in all_shell_codes
        if definitions[code].get("flag") == "NO"
    } | disabled_by_condition
    shell_codes = (
        all_shell_codes - disabled_codes if exclude_disabled else all_shell_codes
    )
    dependencies: dict[int, set[int]] = defaultdict(set)
    for relation in relations:
        before, after = relation.get("preTaskCode", 0), relation.get("postTaskCode")
        if before in shell_codes and after in shell_codes:
            dependencies[after].add(before)
    for target, incoming in conditional_dependencies.items():
        if target in shell_codes:
            dependencies[target].update(incoming & shell_codes)

    global_env = {
        entry["prop"]: "" if entry.get("value") is None else str(entry.get("value"))
        for entry in process.get("globalParamList") or []
        if entry.get("prop")
    }
    setup_env = dict(global_env)
    for code in shell_codes:
        params = definitions[code].get("taskParams") or {}
        for entry in params.get("localParams") or []:
            name = entry.get("prop")
            if not name:
                continue
            value = "" if entry.get("value") is None else str(entry.get("value"))
            if name in setup_env and setup_env[name] != value:
                raise ValueError(
                    f"task-local variable {name!r} has conflicting values and cannot be "
                    "promoted to workflow setup"
                )
            setup_env[name] = value

    tasks: dict[str, Any] = {}
    for code in sorted(shell_codes):
        definition = definitions[code]
        params = definition.get("taskParams") or {}
        task: dict[str, Any] = {
            "description": definition.get("name") or "",
            "command": params.get("rawScript") or "true",
            "depends": [f"task_{dep}" for dep in sorted(dependencies[code])],
        }
        if definition.get("flag") == "NO" or code in disabled_by_condition:
            task["enabled"] = False
        timeout = definition.get("timeout")
        if definition.get("timeoutFlag") == "OPEN" and isinstance(timeout, int) and timeout > 0:
            task["timeout"] = timeout * 60  # DolphinScheduler export uses minutes.
        tasks[f"task_{code}"] = task

    schedule = item.get("schedule") or {}
    converted_schedule: dict[str, Any] | None = None
    crontab = schedule.get("crontab")
    if isinstance(crontab, str) and crontab.strip():
        try:
            converted_schedule = {
                "cron": convert_dolphinscheduler_crontab(crontab),
                "timezone": schedule.get("timezoneId") or "Asia/Shanghai",
                # Imported schedules must be enabled manually after server deployment.
                "enabled": False,
            }
        except ValueError as exc:
            warnings.append(str(exc))
    combined_setup = "\n".join(
        [*_environment_lines(setup_env, setup_shell), setup.strip()]
    ).strip()
    config: dict[str, Any] = {
        "name": workflow_name,
        "description": process.get("name") or "",
        "tasks": tasks,
        "migration": {
            "source_process_code": process_code,
            "source_process_name": process.get("name") or "",
            "dolphinscheduler_crontab": schedule.get("crontab"),
            "timezone": schedule.get("timezoneId"),
            "source_schedule_release_state": schedule.get("releaseState"),
            "requires_manual_review": bool(warnings),
        },
    }
    if converted_schedule:
        config["schedule"] = converted_schedule
    if combined_setup:
        config = {
            "name": config["name"],
            "description": config["description"],
            "setup": combined_setup,
            "tasks": config["tasks"],
            **({"schedule": config["schedule"]} if "schedule" in config else {}),
            "migration": config["migration"],
        }
    return config, warnings


def convert_source(
    source_type: str,
    source: Path,
    output_dir: Path,
    setup: str = "",
    *,
    exclude_disabled: bool = False,
    timezone_name: str = "Asia/Shanghai",
    setup_shell: str = "bash",
) -> list[Path]:
    if source_type == SOURCE_DOLPHINSCHEDULER:
        return convert_dolphinscheduler_export(
            source,
            output_dir,
            setup,
            exclude_disabled=exclude_disabled,
            setup_shell=setup_shell,
        )
    if source_type == SOURCE_WINDOWS_TASK_SCHEDULER:
        return convert_windows_task_scheduler_export(
            source,
            output_dir,
            setup,
            timezone_name=timezone_name,
        )
    raise ValueError(f"unsupported source type: {source_type}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        required=True,
        choices=SOURCE_TYPES,
        help="source scheduler export format",
    )
    parser.add_argument("files", nargs="+", type=Path, metavar="EXPORT")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="output directory (default: the source export's directory)",
    )
    parser.add_argument(
        "--setup-file",
        type=Path,
        help="optional shell snippet for working-directory changes, conda activation, exports, etc.",
    )
    parser.add_argument(
        "--exclude-disabled",
        action="store_true",
        help="omit disabled nodes and all dependency edges connected to them",
    )
    parser.add_argument(
        "--timezone",
        default="Asia/Shanghai",
        help="IANA timezone for Windows task triggers (default: Asia/Shanghai)",
    )
    args = parser.parse_args(argv)
    try:
        setup = args.setup_file.read_text(encoding="utf-8") if args.setup_file else ""
        setup_shell = (
            "powershell"
            if args.setup_file and args.setup_file.suffix.lower() in {".ps1", ".psm1"}
            else "bash"
        )
        for source in args.files:
            convert_source(
                args.source,
                source,
                args.output_dir or source.parent,
                setup,
                exclude_disabled=args.exclude_disabled,
                timezone_name=args.timezone,
                setup_shell=setup_shell,
            )
        return 0
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
