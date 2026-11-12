#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path

import yaml
from waitress import serve

from .satellite_crypto import (
    certificate_fingerprint,
    ensure_certificate,
    server_ssl_context,
)
from .satellite_master import SatelliteManager, create_satellite_master_app
from .satellite_server import serve_ssl
from .webapp import create_app


DEFAULT_CONFIG_PATH = Path("var") / "config.example.yaml"


def load_config_file(path: Path) -> dict:
    """Read startup settings from a YAML config file."""
    if not path.is_file():
        raise SystemExit(f"配置文件不存在: {path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise SystemExit(f"无法解析配置文件 {path}: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise SystemExit(f"配置文件 {path} 必须是 YAML 对象")
    return data


def _start_satellite_service(config: dict, database_path: Path):
    """Start the satellite-facing HTTPS service in a background thread."""
    satellite_config = config.get("satellite") or {}
    if not satellite_config.get("enabled", False):
        return None
    from .database import StateDatabase

    database = StateDatabase(database_path)
    manager = SatelliteManager(
        database,
        timeout=float(satellite_config.get("timeout", 15)),
        poll_interval=float(satellite_config.get("poll_interval", 1.0)),
    )
    cert = Path(satellite_config.get("cert", "var/satellite/cert.pem"))
    key = Path(satellite_config.get("key", "var/satellite/key.pem"))
    ensure_certificate(cert, key, common_name="dagrunner-satellite-master")
    app = create_satellite_master_app(manager)
    host = str(satellite_config.get("host", "0.0.0.0"))
    port = int(satellite_config.get("port", 7120))
    threads = int(satellite_config.get("threads", 16))
    context = server_ssl_context(cert, key)
    try:
        serve_ssl(
            app, host=host, port=port, threads=threads, ssl_context=context
        )
    except OSError as exc:
        raise SystemExit(f"卫星服务端口绑定失败 {host}:{port}: {exc}") from exc
    manager.start()
    print(f"dagrunner satellite service listening on https://{host}:{port}")
    return {
        "manager": manager,
        "url": str(satellite_config.get("public_url", "") or ""),
        "port": port,
        "fingerprint": certificate_fingerprint(cert),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DAG Workflow Runner Web Service")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="startup YAML config (default: var/config.example.yaml)",
    )
    args = parser.parse_args(argv)
    config = load_config_file(args.config)
    host = str(config.get("host", "127.0.0.1"))
    port = int(config.get("port", 7119))
    threads = int(config.get("threads", 8))
    language = config.get("language", "zh-CN")
    db = Path(config.get("db", "var/scheduler.db"))
    logs = Path(config.get("logs", "var/logs"))
    notifier_config = config.get("notifier_config")
    allow_insecure_remote_login = bool(config.get("allow_insecure_remote_login", False))
    satellite = _start_satellite_service(config, db)
    app = create_app(
        database_path=db,
        logs_path=logs,
        allow_insecure_remote_login=allow_insecure_remote_login,
        language=language,
        notifier_config_path=Path(notifier_config) if notifier_config else None,
        satellite_manager=satellite["manager"] if satellite else None,
        satellite_url=satellite["url"] if satellite else "",
        satellite_port=satellite["port"] if satellite else 7120,
        satellite_fingerprint=satellite["fingerprint"] if satellite else "",
    )
    _install_signal_handlers(app)
    print(f"dag-runner service listening on http://{host}:{port}")
    if allow_insecure_remote_login:
        print("warning: insecure remote HTTP login is enabled")
    serve(app, host=host, port=port, threads=threads)
    return 0


def _install_signal_handlers(app) -> None:
    """Stop runs and exit at once on Ctrl+C / SIGTERM.

    Waitress swallows KeyboardInterrupt, and interpreter shutdown joins
    non-daemon executor threads before atexit handlers run, so a running task
    could keep the process alive. Force the exit here instead.
    """
    executions = app.extensions["execution_service"]
    schedules = app.extensions["schedule_service"]
    satellite_manager = app.extensions.get("satellite_manager")

    def _handle(signum, frame):
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            executions.cancel_all()
        except Exception:
            pass
        # Give running tasks a moment to terminate their child processes so a
        # lingering command does not keep the terminal attached.
        time.sleep(0.5)
        try:
            schedules.shutdown()
        except Exception:
            pass
        if satellite_manager is not None:
            try:
                satellite_manager.shutdown()
            except Exception:
                pass
        os._exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError):
            pass


if __name__ == "__main__":
    raise SystemExit(main())
