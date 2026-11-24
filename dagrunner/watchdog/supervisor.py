"""Supervisor loop that keeps one service process alive."""
from __future__ import annotations

import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TextIO

import yaml

from ..install import activate, active_version
from ..notifier import Notifier, NullNotifier
from ..package import UpdateError, git_branch_sync
from .health import HealthTracker, probe_master, probe_satellite
from .launch import (
    LaunchError,
    launch_child,
    terminate_tree,
    verify_process,
)
from .state import (
    INTENT_RUN,
    INTENT_STOP,
    INTENT_UPDATE,
    WatchdogLocked,
    clear_handoff,
    get_intent,
    heartbeat,
    read_handoff,
    record_update_result,
    set_intent,
    update_state,
    watchdog_lock,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_LOCKED = 3
EXIT_PORT_BUSY = 4
EXIT_DISABLED = 5

VERIFY_TIMEOUT = 20.0
Process = object  # subprocess.Popen in production, fake in tests


@dataclass(frozen=True)
class WatchdogConfig:
    enabled: bool = True
    mode: str = "restart"  # restart | notify
    interval: float = 5.0
    timeout: float = 3.0
    alert_after: float = 120.0
    unhealthy_after: float = 300.0
    backoff_base: float = 5.0
    backoff_max: float = 300.0
    stable_window: float = 300.0
    max_consecutive_failures: int = 0
    stop_timeout: float = 20.0
    alert_to: tuple[str, ...] = ()
    alert_cooldown: float = 3600.0
    handoff_wait: float = 15.0
    require_scheduler: bool = True
    require_db: bool = True


@dataclass(frozen=True)
class Settings:
    config: WatchdogConfig
    config_path: Path
    host: str
    port: int
    listen_port: int


def _as_float(raw: object, default: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _as_bool(raw: object, default: bool) -> bool:
    return default if raw is None else bool(raw)


def _as_nonnegative_float(raw: object, default: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def load_settings(path: str | Path, role: str) -> Settings:
    """Read ``watchdog`` settings from the role's YAML config."""
    path = Path(path)
    if role not in ("master", "satellite"):
        raise LaunchError(f"非法角色: {role!r}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except OSError as exc:
        raise LaunchError(f"无法读取配置文件 {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise LaunchError(f"无法解析配置文件 {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise LaunchError(f"配置文件 {path} 必须是 YAML 对象")

    section = raw.get("watchdog") or {}
    if not isinstance(section, dict):
        raise LaunchError("watchdog 配置必须是映射")
    health = section.get("health") or {}
    if not isinstance(health, dict):
        raise LaunchError("watchdog.health 配置必须是映射")

    alert_to = section.get("alert_to") or []
    if isinstance(alert_to, str):
        alert_to = [alert_to]
    if not isinstance(alert_to, (list, tuple)):
        raise LaunchError("watchdog.alert_to 必须是列表")

    mode = str(section.get("mode", "restart"))
    if mode not in ("restart", "notify"):
        raise LaunchError("watchdog.mode 必须是 restart 或 notify")
    interval = _as_float(section.get("interval"), 5.0)
    alert_after = _as_float(section.get("alert_after"), 120.0)
    unhealthy_after = _as_float(section.get("unhealthy_after"), 300.0)
    if unhealthy_after <= alert_after:
        raise LaunchError("watchdog.unhealthy_after 必须大于 watchdog.alert_after")
    if alert_after < interval:
        raise LaunchError("watchdog.alert_after 不能小于 watchdog.interval")

    config = WatchdogConfig(
        enabled=_as_bool(section.get("enabled"), True),
        mode=mode,
        interval=interval,
        timeout=_as_float(section.get("timeout"), 3.0),
        alert_after=alert_after,
        unhealthy_after=unhealthy_after,
        backoff_base=_as_float(section.get("backoff_base"), 5.0),
        backoff_max=_as_float(section.get("backoff_max"), 300.0),
        stable_window=_as_float(section.get("stable_window"), 300.0),
        max_consecutive_failures=int(section.get("max_consecutive_failures") or 0),
        stop_timeout=_as_float(section.get("stop_timeout"), 20.0),
        alert_to=tuple(str(item) for item in alert_to),
        alert_cooldown=_as_float(section.get("alert_cooldown"), 3600.0),
        handoff_wait=_as_nonnegative_float(section.get("handoff_wait"), 15.0),
        require_scheduler=_as_bool(health.get("require_scheduler"), True),
        require_db=_as_bool(health.get("require_db"), True),
    )

    if role == "master":
        host = str(raw.get("host", "127.0.0.1"))
        port = int(raw.get("port", 7119))
        listen_port = int(raw.get("port", 7119))
    else:
        satellite = raw.get("satellite") if isinstance(raw.get("satellite"), dict) else raw
        host = str(satellite.get("listen_host", "0.0.0.0"))
        listen_port = int(satellite.get("listen_port", 7121))
        port = listen_port
    return Settings(
        config=config,
        config_path=path,
        host=host,
        port=port,
        listen_port=listen_port,
    )


class Watchdog:
    """Keep one child process alive according to the configured policy."""

    def __init__(
        self,
        role: str,
        root: str | Path,
        settings: Settings | None = None,
        *,
        config_path: str | Path | None = None,
        spawner: Callable[[str], Process] | None = None,
        probe: Callable[[], object] | None = None,
        startup_probe: Callable[[], object] | None = None,
        terminator: Callable[[Process | None], None] | None = None,
        notifier: Notifier | None = None,
        clock: Callable[[], float] = time.monotonic,
        output: TextIO | None = None,
        error: TextIO | None = None,
    ) -> None:
        self.role = role
        self.root = Path(root).resolve()
        if settings is None:
            path = config_path or (
                self.root / "var" / ("config.yaml" if role == "master" else "satellite.yaml")
            )
            settings = load_settings(path, role)
        self.settings = settings
        self.cfg = settings.config
        self._spawner = spawner or self._default_spawner()
        self._probe = probe or self._default_probe()
        self._startup_probe = startup_probe or self._probe
        self._terminator = terminator or self._terminate
        self.notifier = notifier or NullNotifier()
        self._clock = clock
        self._out = output or sys.stdout
        self._err = error or sys.stderr
        self._stop = threading.Event()
        self._proc: Process | None = None
        self._tracker = HealthTracker(self.cfg.alert_after, self.cfg.unhealthy_after)
        self._consecutive_failures = 0
        self._restart_count = 0
        self._healthy_since: float | None = None
        self._last_exit_code: int | None = None
        self._last_error = ""
        self._give_up_notified = False
        self._last_restart_notify = 0.0
        self._old_handlers: dict[int, object] = {}
        self._lock_cm = None

    # -- public API ---------------------------------------------------------

    def run(self) -> int:
        if not self.cfg.enabled:
            self._log("watchdog.enabled=false，退出（由启动脚本回退直跑）")
            return EXIT_DISABLED
        try:
            self._lock_cm = watchdog_lock(self.root, self.role)
            self._lock_cm.__enter__()
        except WatchdogLocked as exc:
            self._log(str(exc))
            return EXIT_LOCKED
        try:
            return self._run_with_lock()
        except BaseException as exc:  # noqa: BLE001 - never let the guardian vanish silently
            self._log(f"看门狗内部错误: {exc!r}")
            return EXIT_ERROR
        finally:
            self._restore_signals()
            if self._lock_cm is not None:
                try:
                    self._lock_cm.__exit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass
                self._lock_cm = None

    def start_once(self) -> int:
        """Launch once, verify, and exit without entering the supervise loop."""
        if not self.cfg.enabled:
            self._log("watchdog.enabled=false，退出")
            return EXIT_DISABLED
        try:
            self._lock_cm = watchdog_lock(self.root, self.role)
            self._lock_cm.__enter__()
        except WatchdogLocked as exc:
            self._log(str(exc))
            return EXIT_LOCKED
        try:
            version = self._version()
            self._heartbeat(status="starting", last_error="once")
            try:
                process = self._spawner(version)
            except Exception as exc:  # noqa: BLE001
                self._log(f"拉起子进程失败: {exc}")
                update_state(self.root, self.role, status="stopped", child_pid=None)
                return EXIT_ERROR
            healthy = verify_process(
                process,
                probe=self._safe_probe,
                timeout=VERIFY_TIMEOUT,
                abort=self._stop.is_set,
            )
            if healthy:
                self._heartbeat(status="running", child_pid=process.pid)
            else:
                self._last_exit_code = process.poll()
                self._heartbeat(
                    status="stopped",
                    child_pid=None,
                    last_exit_code=self._last_exit_code,
                    last_error="once",
                )
            self._terminator(process)
            return EXIT_OK if healthy else EXIT_ERROR
        finally:
            if self._lock_cm is not None:
                try:
                    self._lock_cm.__exit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass
                self._lock_cm = None

    def shutdown(self) -> None:
        self._stop.set()

    # -- setup --------------------------------------------------------------

    def _default_spawner(self) -> Callable[[str], Process]:
        def spawn(version: str = "") -> Process:
            return launch_child(self.role, self.root, version, self.settings.config_path)

        return spawn

    def _default_probe(self) -> Callable[[], object]:
        if self.role == "master":
            host, port = self.settings.host, self.settings.port

            def default_master_probe() -> object:
                return probe_master(
                    host,
                    port,
                    self.cfg.timeout,
                    require_scheduler=self.cfg.require_scheduler,
                    require_db=self.cfg.require_db,
                )

            return default_master_probe

        listen_port = self.settings.listen_port

        def default_satellite_probe() -> object:
            return probe_satellite(listen_port, self.cfg.timeout)

        return default_satellite_probe

    def _install_signals(self) -> None:
        def handle(signum, frame):  # noqa: ANN001
            self._stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._old_handlers[sig] = signal.signal(sig, handle)
            except (ValueError, OSError):
                pass

    def _restore_signals(self) -> None:
        for sig, handler in self._old_handlers.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
        self._old_handlers.clear()

    # -- logging ------------------------------------------------------------

    def _log(self, message: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        stream = self._err if "错误" in message or "失败" in message else self._out
        try:
            stream.write(f"[watchdog {stamp}] {message}\n")
            stream.flush()
        except Exception:  # noqa: BLE001
            pass

    def _version(self) -> str:
        return active_version(self.root) or ""

    def _heartbeat(self, **fields: object) -> None:
        state = {
            "watchdog_pid": os.getpid(),
            "version": self._version(),
            "child_pid": getattr(self._proc, "pid", None) if self._proc else None,
            "restart_count": self._restart_count,
            "consecutive_failures": self._consecutive_failures,
            "last_exit_code": self._last_exit_code,
            "last_error": self._last_error,
        }
        state.update(fields)
        try:
            heartbeat(self.root, self.role, **state)
        except OSError:
            pass

    def _backoff_delay(self, failures: int) -> float:
        delay = self.cfg.backoff_base * (2.0 ** max(failures - 1, 0))
        return min(delay, self.cfg.backoff_max)

    def _interruptible_sleep(self, seconds: float) -> None:
        self._stop.wait(max(seconds, 0.0))

    # -- notifications ------------------------------------------------------

    def _notify(
        self,
        action: str,
        message: str,
        *,
        force: bool = False,
        exit_code: int | None = None,
    ) -> None:
        pid = getattr(self._proc, "pid", None) if self._proc else None
        self._log(f"{action}: {message}" + (f" (pid={pid})" if pid else ""))
        if not self.cfg.alert_to:
            return
        now = self._clock()
        if action == "restarted":
            if not force and now - self._last_restart_notify < self.cfg.alert_cooldown:
                return
            self._last_restart_notify = now
        handler = getattr(self.notifier, "on_watchdog_event", None)
        if handler is None:
            return
        try:
            from ..notifier import WatchdogEvent
        except Exception:  # noqa: BLE001 - notifier extension not present yet
            return
        try:
            handler(
                WatchdogEvent(
                    role=self.role,
                    action=action,
                    message=message,
                    recipients=tuple(self.cfg.alert_to),
                    version=self._version(),
                    pid=pid,
                    exit_code=exit_code,
                    restarts=self._restart_count,
                )
            )
        except Exception as exc:  # noqa: BLE001
            self._log(f"发送告警失败: {exc}")

    # -- lifecycle ----------------------------------------------------------

    def _run_with_lock(self) -> int:
        self._install_signals()
        version = self._version()
        update_state(
            self.root,
            self.role,
            watchdog_pid=os.getpid(),
            status="starting",
            intent=INTENT_RUN,
            version=version,
            child_pid=None,
            started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            restart_count=0,
            consecutive_failures=0,
            unhealthy_since=None,
            last_exit_code=None,
            last_error="",
        )
        ok, reason = self._call_probe(self._startup_probe)
        if ok:
            self._log(f"端口已有健康实例在运行（{reason}），拒绝双实例")
            update_state(self.root, self.role, status="port_busy")
            return EXIT_PORT_BUSY
        if not self._spawn_until_ready(version, "初始启动"):
            update_state(self.root, self.role, status="stopped")
            return EXIT_OK if self._stop.is_set() else EXIT_ERROR
        return self._supervise()

    def _supervise(self) -> int:
        while not self._stop.is_set():
            if self._stop.wait(self.cfg.interval):
                break
            self._tick()
        self._terminator(self._proc)
        self._proc = None
        update_state(self.root, self.role, status="stopped", child_pid=None)
        return EXIT_OK

    def _tick(self) -> None:
        now = self._clock()
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            ok, reason = self._safe_probe()
            actions = self._tracker.observe(ok, now)
            for action in actions:
                if action == "recovered":
                    self._notify("recovered", "服务恢复健康", force=True)
                elif action == "unhealthy":
                    self._notify(
                        "unhealthy",
                        f"健康检查连续失败：{reason}；达到 {self.cfg.unhealthy_after:g}s 将重启",
                        force=True,
                    )
                elif action == "restart":
                    if self.cfg.mode == "notify":
                        self._notify(
                            "unhealthy",
                            f"健康检查持续失败已达阈值，但 mode=notify 不重启：{reason}",
                            force=True,
                        )
                        continue
                    self._restart_unhealthy(reason)
                    return
            if ok:
                if self._healthy_since is None:
                    self._healthy_since = now
                elif now - self._healthy_since >= self.cfg.stable_window:
                    self._consecutive_failures = 0
                    self._give_up_notified = False
            else:
                self._healthy_since = None
            self._heartbeat(
                status="running",
                unhealthy_since=(
                    time.strftime("%Y-%m-%dT%H:%M:%S%z")
                    if self._tracker.failing
                    else None
                ),
            )
            return
        self._on_child_exit(proc)

    def _on_child_exit(self, proc: Process) -> None:
        code = proc.poll()
        self._last_exit_code = code
        self._proc = None
        handoff = read_handoff(self.root)
        intent = get_intent(self.root, self.role)
        self._log(
            f"子进程退出 code={code} intent={intent} handoff={'有' if handoff else '无'}"
        )
        if intent == INTENT_STOP:
            if not handoff:
                # An update may still be writing its handoff request; give it a
                # short window before treating this as a plain manual stop.
                handoff = self._wait_for_handoff()
            if handoff:
                self._dispatch_handoff(handoff)
                return
            update_state(self.root, self.role, status="stopped", child_pid=None)
            self._stop.set()
            return
        if handoff:
            self._dispatch_handoff(handoff)
            return
        self._crash_restart(code)

    def _dispatch_handoff(self, handoff: dict) -> None:
        if str(handoff.get("action") or "") == "sync_branch":
            self._handle_sync_branch(handoff)
            return
        self._handle_update(handoff)

    def _wait_for_handoff(self) -> dict | None:
        deadline = self._clock() + self.cfg.handoff_wait
        while True:
            handoff = read_handoff(self.root)
            if handoff:
                return handoff
            remaining = deadline - self._clock()
            if remaining <= 0 or self._stop.is_set():
                return None
            self._stop.wait(min(self.cfg.interval, remaining))

    def _crash_restart(self, code: int | None) -> None:
        self._consecutive_failures += 1
        self._last_error = f"子进程退出 code={code}"
        self._notify("restarted", f"子进程退出 (code={code})，准备重启", exit_code=code)
        self._interruptible_sleep(self._backoff_delay(self._consecutive_failures))
        if self._stop.is_set():
            return
        self._spawn_until_ready(self._version(), "崩溃重启")

    def _restart_unhealthy(self, reason: str) -> None:
        old = self._proc
        self._proc = None
        self._terminator(old)
        self._restart_count += 1
        self._tracker.reset()
        self._consecutive_failures = 0
        self._last_error = f"健康检查不健康：{reason}"
        self._notify("restarted", f"健康检查不健康，执行重启：{reason}")
        self._spawn_until_ready(self._version(), "假死重启")

    def _spawn_until_ready(self, version: str, reason: str) -> bool:
        attempts = 0
        while not self._stop.is_set():
            attempts += 1
            self._heartbeat(
                status="restarting",
                consecutive_failures=max(self._consecutive_failures, attempts),
                last_error=reason,
            )
            proc: Process | None = None
            try:
                proc = self._spawner(version)
            except Exception as exc:  # noqa: BLE001
                self._log(f"拉起子进程失败: {exc}")
            if proc is not None and verify_process(
                proc,
                probe=self._safe_probe,
                timeout=VERIFY_TIMEOUT,
                abort=self._stop.is_set,
            ):
                self._proc = proc
                self._consecutive_failures = 0
                self._healthy_since = None
                self._tracker.reset()
                self._give_up_notified = False
                self._heartbeat(
                    status="running",
                    child_pid=proc.pid,
                    consecutive_failures=0,
                    last_error="",
                )
                return True
            if proc is not None:
                self._last_exit_code = proc.poll()
                self._terminator(proc)
            self._consecutive_failures = max(self._consecutive_failures, attempts)
            delay = self._backoff_delay(attempts)
            if (
                self.cfg.max_consecutive_failures
                and attempts >= self.cfg.max_consecutive_failures
            ):
                if not self._give_up_notified:
                    self._give_up_notified = True
                    self._notify(
                        "give_up",
                        f"连续 {attempts} 次启动失败，将以 {self.cfg.backoff_max:g}s 间隔继续重试",
                        force=True,
                    )
                delay = self.cfg.backoff_max
            self._interruptible_sleep(delay)
        return False

    def _handle_update(self, handoff: dict) -> None:
        target = str(handoff.get("version") or "")
        previous = str(handoff.get("previous") or "")
        update_state(
            self.root,
            self.role,
            status="updating",
            intent=INTENT_UPDATE,
            unhealthy_since=None,
        )
        self._log(f"更新交接：切换到 {target}" + (f"（可回滚 {previous}）" if previous else ""))
        try:
            activate(target, self.root)
        except OSError as exc:
            self._log(f"activate {target} 失败: {exc}")
        ok = bool(target) and self._spawn_until_ready(target, "更新交接")
        if ok:
            clear_handoff(self.root)
            set_intent(self.root, self.role, INTENT_RUN)
            self._notify("update_ok", f"已切换版本 {target}", force=True)
            record_update_result(self.root, status="running", version=target)
            self._heartbeat(status="running", version=target)
            return
        message = f"版本 {target} 启动失败"
        self._notify("update_failed", message, force=True)
        rollback_target = previous or target
        if previous:
            try:
                activate(previous, self.root)
            except OSError as exc:
                self._log(f"回滚 activate 失败: {exc}")
        rolled_back = self._spawn_until_ready(rollback_target, "更新回滚")
        clear_handoff(self.root)
        set_intent(self.root, self.role, INTENT_RUN)
        if rolled_back:
            record_update_result(
                self.root,
                status="rolled_back",
                version=rollback_target,
                error=message,
            )
        else:
            record_update_result(self.root, status="rollback_failed", error=message)
        self._heartbeat(
            status="running" if rolled_back else "give_up",
            version=rollback_target,
        )
        if rolled_back:
            self._notify("update_failed", f"已回滚到 {rollback_target}", force=True)
        else:
            self._notify(
                "update_failed",
                f"回滚到 {rollback_target} 仍失败，需要人工介入",
                force=True,
            )

    def _handle_sync_branch(self, handoff: dict) -> None:
        ref = str(handoff.get("ref") or "main")
        remote = str(handoff.get("remote") or "origin")
        expected_sha = str(handoff.get("expected_sha") or "")
        update_state(
            self.root,
            self.role,
            status="updating",
            intent=INTENT_UPDATE,
            unhealthy_since=None,
        )
        self._log(f"分支交接：同步 {remote}/{ref}" + (f"（期望 {expected_sha[:8]}）" if expected_sha else ""))
        failure: str = ""
        result: dict | None = None
        if active_version(self.root):
            failure = "当前为版本化安装（var/current），不支持分支更新；请使用发布包或开发模式"
        else:
            try:
                result = git_branch_sync(
                    self.root, remote=remote, name=ref, expected_sha=expected_sha
                )
            except UpdateError as exc:
                failure = str(exc)
            except Exception as exc:  # noqa: BLE001
                failure = f"分支同步异常: {exc}"
        if failure:
            self._log(f"分支同步失败: {failure}")
            self._notify("update_failed", f"分支同步失败：{failure}", force=True)
            record_update_result(self.root, status="failed", error=failure)
            clear_handoff(self.root)
            set_intent(self.root, self.role, INTENT_RUN)
            self._spawn_until_ready(self._version(), "分支同步失败")
            return
        assert result is not None
        clear_handoff(self.root)
        set_intent(self.root, self.role, INTENT_RUN)
        new_sha = str(result.get("new_sha") or "")
        if result.get("updated"):
            self._notify(
                "update_ok",
                f"已同步 {ref} 到 {new_sha[:8]}，正在重启",
                force=True,
            )
        else:
            self._notify("update_ok", f"{ref} 已是最新，正在重启", force=True)
        record_update_result(self.root, status="running", version=self._version())
        self._spawn_until_ready(self._version(), "分支同步重启")

    def _terminate(self, proc: Process | None) -> None:
        if proc is None:
            return
        try:
            terminate_tree(proc, self.cfg.stop_timeout)
        except Exception as exc:  # noqa: BLE001
            self._log(f"终止子进程失败: {exc}")

    def _call_probe(self, probe: Callable[[], object]) -> tuple[bool, str]:
        try:
            result = probe()
        except Exception as exc:  # noqa: BLE001
            return False, f"探测异常: {exc}"
        if isinstance(result, tuple):
            ok = bool(result[0])
            reason = str(result[1]) if len(result) > 1 else ""
            return ok, reason
        return bool(result), ""

    def _safe_probe(self) -> tuple[bool, str]:
        return self._call_probe(self._probe)


def default_config_path(root: str | Path, role: str) -> Path:
    root = Path(root)
    name = "config.yaml" if role == "master" else "satellite.yaml"
    return root / "var" / name


def status_payload(root: str | Path, role: str) -> dict:
    from .state import read_state

    return read_state(root, role)
