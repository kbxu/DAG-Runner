"""Entry point for the DAG Runner satellite agent.

Usage:
    python -m dagrunner.satellite --config var/satellite.yaml
"""
from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from .satellite_agent import SatelliteAgent, SatelliteAgentConfig


DEFAULT_CONFIG_PATH = Path("var") / "satellite.yaml"


def load_config(path: Path) -> dict:
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


def build_agent_config(data: dict) -> SatelliteAgentConfig:
    section = data.get("satellite") if isinstance(data.get("satellite"), dict) else data
    alias = str(section.get("alias", "")).strip()
    if not alias:
        raise SystemExit("配置缺少 satellite.alias")
    master_url = str(section.get("master_url", "")).strip()
    if not master_url:
        raise SystemExit("配置缺少 satellite.master_url")
    token = str(section.get("token", ""))
    if not token:
        raise SystemExit("配置缺少 satellite.token")
    mode = str(section.get("mode", "auto")).strip()
    if mode not in ("auto", "forward", "reverse"):
        raise SystemExit("satellite.mode 必须是 auto / forward / reverse")
    host_key_policy = str(section.get("host_key_policy", "accept-new")).strip()
    if host_key_policy not in ("accept-new", "pinned"):
        raise SystemExit("satellite.host_key_policy 必须是 accept-new 或 pinned")
    fingerprint = str(section.get("master_cert_fingerprint", "")).strip()
    if host_key_policy == "pinned" and not fingerprint:
        raise SystemExit("satellite.host_key_policy 为 pinned 时必须填写 master_cert_fingerprint")
    return SatelliteAgentConfig(
        alias=alias,
        master_url=master_url,
        token=token,
        mode=mode,
        listen_host=str(section.get("listen_host", "0.0.0.0")),
        listen_port=int(section.get("listen_port", 7121)),
        cert=str(section.get("cert", "var/satellite-certs/cert.pem")),
        key=str(section.get("key", "var/satellite-certs/key.pem")),
        host_key_policy=host_key_policy,
        master_cert_fingerprint=fingerprint,
        workdir=str(section.get("workdir", "")),
        log_dir=str(section.get("log_dir", "var/satellite-logs")),
        heartbeat_interval=int(section.get("heartbeat_interval", 30)),
        probe_interval=int(section.get("probe_interval", 60)),
        timeout=float(section.get("timeout", 15)),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DAG Runner Satellite Agent")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="satellite YAML config (default: var/satellite.yaml)",
    )
    args = parser.parse_args(argv)
    config = build_agent_config(load_config(args.config))
    return SatelliteAgent(config).run()


if __name__ == "__main__":
    raise SystemExit(main())
