"""Command line entry point: ``python -m dagrunner.watchdog``."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..install import repo_root
from .launch import LaunchError
from .state import read_state
from .supervisor import (
    EXIT_DISABLED,
    EXIT_ERROR,
    Watchdog,
    default_config_path,
    load_settings,
)

EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dagrunner.watchdog",
        description="DAG Runner 进程看门狗（主服务 / 卫星）",
    )
    parser.add_argument("--role", choices=("master", "satellite"), required=True)
    parser.add_argument("--root", type=Path, default=None, help="仓库根目录（含 var/）")
    parser.add_argument("--config", type=Path, default=None, help="角色配置文件")
    parser.add_argument(
        "--status",
        action="store_true",
        help="打印看门狗状态 JSON 后退出",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="只拉起一次并校验，不进入守护循环",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = (args.root.resolve() if args.root else repo_root()).resolve()
    config = args.config or default_config_path(root, args.role)

    if args.status:
        print(json.dumps(read_state(root, args.role), ensure_ascii=False, indent=2))
        return 0

    try:
        settings = load_settings(config, args.role)
    except LaunchError as exc:
        print(f"watchdog 配置错误: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except (OSError, ValueError) as exc:
        print(f"watchdog 启动失败: {exc}", file=sys.stderr)
        return EXIT_ERROR

    watchdog = Watchdog(args.role, root, settings)
    if args.once:
        code = watchdog.start_once()
    else:
        code = watchdog.run()
    if code == EXIT_DISABLED:
        # Reserved for launch scripts: fall back to running the service directly.
        return EXIT_DISABLED
    return code


if __name__ == "__main__":
    raise SystemExit(main())
