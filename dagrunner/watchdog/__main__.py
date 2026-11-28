"""Command line entry point: ``python -m dagrunner.watchdog``."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ..install import repo_root
from .launch import LaunchError
from .state import is_active, read_state, request_restart, request_stop
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
    parser.add_argument(
        "--restart",
        action="store_true",
        help="通知运行中的看门狗重启子服务",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="通知运行中的看门狗优雅停止子服务并退出",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = (args.root.resolve() if args.root else repo_root()).resolve()
    config = args.config or default_config_path(root, args.role)

    if args.status:
        print(json.dumps(read_state(root, args.role), ensure_ascii=False, indent=2))
        return 0

    if args.restart or args.stop:
        if not is_active(root, args.role):
            print(f"未检测到运行中的 {args.role} 看门狗", file=sys.stderr)
            return EXIT_ERROR
        if args.restart:
            request_restart(root, args.role, pid=os.getpid())
            print(f"已通知 {args.role} 看门狗重启子服务（约数秒内生效）")
        else:
            request_stop(root, args.role, pid=os.getpid())
            print(f"已通知 {args.role} 看门狗停止服务并退出")
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
