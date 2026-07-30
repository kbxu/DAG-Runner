#!/usr/bin/env python3
"""Convert DolphinScheduler JSON exports into reviewable mini-scheduler YAML files.

This tool only reads JSON and writes YAML. It never executes a task command.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml


class LiteralDumper(yaml.SafeDumper):
    pass


def _str_presenter(dumper, value):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


LiteralDumper.add_representer(str, _str_presenter)


def convert_export(
    source: Path, output_dir: Path, workdir: str, setup: str = ""
) -> list[Path]:
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot parse {source}: {exc}") from exc
    definitions = payload if isinstance(payload, list) else [payload]
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for item in definitions:
        config, warnings = convert_definition(item, workdir, setup)
        destination = output_dir / f"{config['name']}.yaml"
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


def convert_definition(
    item: dict[str, Any], workdir: str, setup: str = ""
) -> tuple[dict[str, Any], list[str]]:
    process = item.get("processDefinition") or {}
    process_code = process.get("code")
    if not process_code:
        raise ValueError("export item has no processDefinition.code")
    workflow_name = f"ds_{process_code}"
    definitions = {task["code"]: task for task in item.get("taskDefinitionList", [])}
    relations = item.get("processTaskRelationList", [])
    warnings: list[str] = []
    shell_codes = {
        code for code, definition in definitions.items() if definition.get("taskType") == "SHELL"
    }
    unsupported = {
        code: definition
        for code, definition in definitions.items()
        if definition.get("taskType") != "SHELL"
    }
    dependencies: dict[int, set[int]] = defaultdict(set)
    for relation in relations:
        before, after = relation.get("preTaskCode", 0), relation.get("postTaskCode")
        if before in shell_codes and after in shell_codes:
            dependencies[after].add(before)

    disabled_by_condition: set[int] = set()
    for code, definition in unsupported.items():
        task_type = definition.get("taskType", "UNKNOWN")
        if task_type != "CONDITIONS":
            warnings.append(
                f"omitted unsupported {task_type} node code={code} name={definition.get('name')!r}"
            )
            continue
        params = definition.get("taskParams") or {}
        result = params.get("conditionResult") or {}
        incoming = {
            rel.get("preTaskCode")
            for rel in relations
            if rel.get("postTaskCode") == code and rel.get("preTaskCode") in shell_codes
        }
        for group in ((params.get("dependence") or {}).get("dependTaskList") or []):
            for dependency in group.get("dependItemList") or []:
                if dependency.get("depTaskCode") in shell_codes:
                    incoming.add(dependency["depTaskCode"])
        for target in result.get("successNode") or []:
            if target in shell_codes:
                dependencies[target].update(incoming)
        for target in result.get("failedNode") or []:
            if target in shell_codes:
                disabled_by_condition.add(target)
        warnings.append(
            f"CONDITIONS code={code}: success branch converted to normal SUCCESS dependencies; "
            "failed branch targets were disabled and require manual review"
        )

    tasks: dict[str, Any] = {}
    for code in sorted(shell_codes):
        definition = definitions[code]
        params = definition.get("taskParams") or {}
        local_env = {
            entry["prop"]: entry.get("value", "")
            for entry in params.get("localParams") or []
            if entry.get("prop")
        }
        task: dict[str, Any] = {
            "description": definition.get("name") or "",
            "command": params.get("rawScript") or "true",
            "depends": [f"task_{dep}" for dep in sorted(dependencies[code])],
        }
        if local_env:
            task["env"] = local_env
        if definition.get("flag") == "NO" or code in disabled_by_condition:
            task["enabled"] = False
        timeout = definition.get("timeout")
        if definition.get("timeoutFlag") == "OPEN" and isinstance(timeout, int) and timeout > 0:
            task["timeout"] = timeout * 60  # DolphinScheduler export uses minutes.
        tasks[f"task_{code}"] = task

    global_env = {
        entry["prop"]: entry.get("value", "")
        for entry in process.get("globalParamList") or []
        if entry.get("prop")
    }
    schedule = item.get("schedule") or {}
    config: dict[str, Any] = {
        "name": workflow_name,
        "description": process.get("name") or "",
        "workdir": workdir,
        "env": global_env,
        "tasks": tasks,
        "migration": {
            "source_process_code": process_code,
            "source_process_name": process.get("name") or "",
            "dolphinscheduler_crontab": schedule.get("crontab"),
            "timezone": schedule.get("timezoneId"),
            "requires_manual_review": bool(warnings),
        },
    }
    if setup.strip():
        # Keep it next to env/workdir in generated YAML for easy deployment review.
        config = {
            "name": config["name"],
            "description": config["description"],
            "workdir": config["workdir"],
            "env": config["env"],
            "setup": setup.strip(),
            "tasks": config["tasks"],
            "migration": config["migration"],
        }
    return config, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="+", type=Path, help="DolphinScheduler export JSON")
    parser.add_argument("--output-dir", type=Path, default=Path("demo") / "workflows")
    parser.add_argument(
        "--workdir",
        default="..",
        help="workdir written to YAML, relative to the generated YAML file",
    )
    parser.add_argument(
        "--setup-file",
        type=Path,
        help="optional shell snippet written as workflow setup (for conda activation, exports, etc.)",
    )
    args = parser.parse_args(argv)
    try:
        setup = args.setup_file.read_text(encoding="utf-8") if args.setup_file else ""
        for source in args.sources:
            convert_export(source, args.output_dir, args.workdir, setup)
        return 0
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
