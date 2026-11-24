"""Health probes and the delayed-confirmation timer."""
from __future__ import annotations

import json
import socket
import ssl
import urllib.error
import urllib.request

MASTER_HEALTH_PATH = "/health"
SATELLITE_HEALTH_PATH = "/api/satellite/v1/health"


def master_health_url(host: str, port: int) -> str:
    from .launch import normalize_host

    return f"http://{normalize_host(str(host))}:{int(port)}{MASTER_HEALTH_PATH}"


def satellite_health_url(listen_port: int) -> str:
    return f"https://127.0.0.1:{int(listen_port)}{SATELLITE_HEALTH_PATH}"


def _fetch(url: str, timeout: float, *, verify: bool = True) -> tuple[int, dict | None]:
    context = None
    if url.startswith("https"):
        context = ssl.create_default_context()
        if not verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            body = response.read(65536).decode("utf-8", "replace")
            try:
                payload = json.loads(body)
            except ValueError:
                payload = None
            return response.status, payload if isinstance(payload, dict) else None
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, OSError, ValueError):
        return 0, None


def judge_master_payload(
    payload: dict | None,
    *,
    require_scheduler: bool = True,
    require_db: bool = True,
) -> tuple[bool, str]:
    """Turn a ``/health`` payload into an overall verdict plus a reason."""
    if not payload:
        return False, "健康检查无响应"
    if not payload.get("ok"):
        return False, "服务自报异常 (ok=false)"
    if require_scheduler and not payload.get("scheduler_running", True):
        return False, "调度器未运行"
    if require_db and not payload.get("db_ok", True):
        return False, "数据库不可用"
    return True, ""


def probe_master(
    host: str,
    port: int,
    timeout: float = 3.0,
    *,
    require_scheduler: bool = True,
    require_db: bool = True,
) -> tuple[bool, str]:
    status, payload = _fetch(master_health_url(host, port), timeout)
    if status == 0:
        return False, f"健康检查连接失败 {host}:{port}"
    if not (200 <= status < 500):
        return False, f"健康检查返回 HTTP {status}"
    return judge_master_payload(
        payload, require_scheduler=require_scheduler, require_db=require_db
    )


def _tcp_reachable(port: int, timeout: float) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex(("127.0.0.1", int(port))) == 0


def probe_satellite(
    listen_port: int,
    timeout: float = 3.0,
    *,
    path: str = SATELLITE_HEALTH_PATH,
) -> tuple[bool, str]:
    """Probe the satellite agent over localhost HTTPS, falling back to TCP."""
    url = satellite_health_url(listen_port)
    if path != SATELLITE_HEALTH_PATH:
        url = f"https://127.0.0.1:{int(listen_port)}{path}"
    status, payload = _fetch(url, timeout, verify=False)
    if status == 0:
        # The agent serves with a self-signed certificate; TLS setup problems
        # should not be reported differently from a dead listener.
        if _tcp_reachable(listen_port, timeout):
            return True, ""
        return False, f"卫星端口不可达 {listen_port}"
    if not (200 <= status < 500):
        return False, f"卫星健康检查返回 HTTP {status}"
    if payload and payload.get("ok") is False:
        return False, "卫星自报异常 (ok=false)"
    return True, ""


class HealthTracker:
    """Accumulates consecutive failed probes and emits timed actions.

    ``observe(ok, now)`` returns a list containing ``"recovered"``,
    ``"unhealthy"`` (alert threshold reached) and/or ``"restart"`` (unhealthy
    threshold reached). All times are monotonically increasing values supplied
    by the caller so tests stay deterministic.
    """

    def __init__(self, alert_after: float, unhealthy_after: float) -> None:
        alert_after = float(alert_after)
        unhealthy_after = float(unhealthy_after)
        if alert_after <= 0 or unhealthy_after <= 0:
            raise ValueError("健康阈值必须为正数")
        if unhealthy_after < alert_after:
            raise ValueError("unhealthy_after 不能小于 alert_after")
        self.alert_after = alert_after
        self.unhealthy_after = unhealthy_after
        self.failing = False
        self.since = 0.0
        self.alerted = False
        self.restarted = False

    def reset(self) -> None:
        self.failing = False
        self.since = 0.0
        self.alerted = False
        self.restarted = False

    def observe(self, ok: bool, now: float) -> list[str]:
        actions: list[str] = []
        if ok:
            if self.failing:
                actions.append("recovered")
            self.reset()
            return actions
        if not self.failing:
            self.failing = True
            self.since = float(now)
            self.alerted = False
            self.restarted = False
        elapsed = max(0.0, float(now) - self.since)
        if elapsed >= self.alert_after and not self.alerted:
            self.alerted = True
            actions.append("unhealthy")
        if elapsed >= self.unhealthy_after and not self.restarted:
            self.restarted = True
            actions.append("restart")
        return actions
