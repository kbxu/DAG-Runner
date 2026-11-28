from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dagrunner.notifier import WatchdogEvent  # noqa: E402
from dagrunner.watchdog.health import (  # noqa: E402
    HealthTracker,
    judge_master_payload,
    master_health_url,
    probe_master,
    probe_satellite,
    satellite_health_url,
)
from dagrunner.watchdog.launch import (  # noqa: E402
    LaunchError,
    build_environment,
    child_script,
    normalize_host,
    port_free,
    terminate_tree,
)
from dagrunner.watchdog.state import (  # noqa: E402
    INTENT_RESTART,
    INTENT_RUN,
    INTENT_STOP,
    WatchdogLocked,
    get_intent,
    is_active,
    request_restart,
    request_stop,
    set_intent,
    update_state,
    watchdog_lock,
)
from dagrunner.watchdog.supervisor import (  # noqa: E402
    EXIT_OK,
    Settings,
    Watchdog,
    WatchdogConfig,
    load_settings,
)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FakeProc:
    """Minimal stand-in for subprocess.Popen in supervisor tests."""

    _next_pid = 40000

    def __init__(self, code: int | None = None) -> None:
        FakeProc._next_pid += 1
        self.pid = FakeProc._next_pid
        self.code = code

    def poll(self) -> int | None:
        return self.code

    def wait(self, timeout: float | None = None) -> int:
        if self.code is None:
            raise TimeoutError
        return self.code


class FakeNotifier:
    def __init__(self) -> None:
        self.events: list[WatchdogEvent] = []

    def on_watchdog_event(self, event: WatchdogEvent) -> None:
        self.events.append(event)


def make_settings(root: Path, **overrides) -> Settings:
    values = dict(
        interval=0.02,
        backoff_base=0.01,
        backoff_max=0.02,
        alert_after=0.05,
        unhealthy_after=0.1,
        stop_timeout=0.1,
        alert_to=("ops@example.com",),
    )
    values.update(overrides)
    config = WatchdogConfig(**values)
    config_path = root / "var" / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    return Settings(
        config=config,
        config_path=config_path,
        host="127.0.0.1",
        port=free_port(),
        listen_port=free_port(),
    )


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)


class StateTests(TempDirTestCase):
    def test_intent_roundtrip(self) -> None:
        self.assertEqual(get_intent(self.root, "master"), "run")
        set_intent(self.root, "master", INTENT_STOP, pid=123)
        self.assertEqual(get_intent(self.root, "master"), "stop")
        request_stop(self.root, "master")
        self.assertEqual(get_intent(self.root, "master"), "stop")
        set_intent(self.root, "master", INTENT_RUN)
        self.assertEqual(get_intent(self.root, "master"), "run")

    def test_intent_persists_other_fields(self) -> None:
        update_state(self.root, "master", status="running", child_pid=42)
        set_intent(self.root, "master", INTENT_STOP)
        state = update_state(self.root, "master")
        self.assertEqual(state["status"], "running")
        self.assertEqual(state["child_pid"], 42)
        self.assertEqual(state["intent"], "stop")

    def test_is_active_requires_fresh_heartbeat_and_live_pid(self) -> None:
        self.assertFalse(is_active(self.root, "master"))
        update_state(
            self.root,
            "master",
            watchdog_pid=os.getpid(),
            last_heartbeat=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        )
        self.assertTrue(is_active(self.root, "master", interval=5.0))
        update_state(
            self.root,
            "master",
            watchdog_pid=99999999,
            last_heartbeat=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        )
        self.assertFalse(is_active(self.root, "master", interval=5.0))
        update_state(
            self.root,
            "master",
            watchdog_pid=os.getpid(),
            last_heartbeat="2020-01-01T00:00:00+08:00",
        )
        self.assertFalse(is_active(self.root, "master", interval=5.0))

    def test_request_restart_sets_intent(self) -> None:
        request_restart(self.root, "master", pid=12345)
        self.assertEqual(get_intent(self.root, "master"), INTENT_RESTART)

    def test_lock_is_exclusive(self) -> None:
        cm = watchdog_lock(self.root, "master")
        cm.__enter__()
        try:
            with self.assertRaises(WatchdogLocked):
                with watchdog_lock(self.root, "master"):
                    pass
        finally:
            cm.__exit__(None, None, None)
        with watchdog_lock(self.root, "master"):
            pass


class HealthTests(unittest.TestCase):
    def test_normalize_host(self) -> None:
        self.assertEqual(normalize_host("0.0.0.0"), "127.0.0.1")
        self.assertEqual(normalize_host("::"), "127.0.0.1")
        self.assertEqual(normalize_host("example.com"), "example.com")

    def test_urls(self) -> None:
        self.assertEqual(master_health_url("0.0.0.0", 7119), "http://127.0.0.1:7119/health")
        self.assertEqual(
            satellite_health_url(7121),
            "https://127.0.0.1:7121/api/satellite/v1/health",
        )

    def test_judge_master_payload(self) -> None:
        ok, reason = judge_master_payload(
            {"ok": True, "scheduler_running": True, "db_ok": True}
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "")
        ok, reason = judge_master_payload(
            {"ok": True, "scheduler_running": False, "db_ok": True}
        )
        self.assertFalse(ok)
        self.assertIn("调度器", reason)
        ok, _ = judge_master_payload({"ok": True, "db_ok": False})
        self.assertFalse(ok)
        ok, _ = judge_master_payload({"ok": True, "db_ok": False}, require_db=False)
        self.assertTrue(ok)
        self.assertFalse(judge_master_payload(None)[0])
        self.assertFalse(judge_master_payload({"ok": False})[0])

    def test_tracker_alert_and_restart_thresholds(self) -> None:
        tracker = HealthTracker(alert_after=120.0, unhealthy_after=300.0)
        self.assertEqual(tracker.observe(False, 0.0), [])
        self.assertEqual(tracker.observe(False, 119.0), [])
        self.assertEqual(tracker.observe(False, 120.0), ["unhealthy"])
        self.assertEqual(tracker.observe(False, 299.0), [])
        self.assertEqual(tracker.observe(False, 300.0), ["restart"])
        self.assertEqual(tracker.observe(False, 400.0), [])
        self.assertEqual(tracker.observe(True, 401.0), ["recovered"])
        self.assertEqual(tracker.observe(False, 402.0), [])

    def test_tracker_recovers_mid_window(self) -> None:
        tracker = HealthTracker(alert_after=10.0, unhealthy_after=20.0)
        tracker.observe(False, 0.0)
        tracker.observe(False, 9.0)
        self.assertEqual(tracker.observe(True, 9.5), ["recovered"])
        tracker.observe(False, 10.0)
        self.assertEqual(tracker.observe(False, 19.9), [])
        self.assertNotIn("unhealthy", tracker.observe(False, 19.95))

    def test_tracker_validation(self) -> None:
        with self.assertRaises(ValueError):
            HealthTracker(alert_after=20.0, unhealthy_after=10.0)
        with self.assertRaises(ValueError):
            HealthTracker(alert_after=0.0, unhealthy_after=10.0)

    @contextmanager
    def _http_health(self, payload: dict, status: int = 200):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield int(server.server_address[1])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)

    def test_probe_master_success_and_scheduler_failure(self) -> None:
        with self._http_health(
            {"ok": True, "scheduler_running": True, "db_ok": True}
        ) as port:
            ok, _ = probe_master("127.0.0.1", port, timeout=2.0)
            self.assertTrue(ok)
        with self._http_health(
            {"ok": True, "scheduler_running": False, "db_ok": True}
        ) as port:
            ok, reason = probe_master("127.0.0.1", port, timeout=2.0)
            self.assertFalse(ok)
            self.assertIn("调度器", reason)

    def test_probe_master_http_error(self) -> None:
        with self._http_health({}, status=500) as port:
            ok, reason = probe_master("127.0.0.1", port, timeout=2.0)
            self.assertFalse(ok)
            self.assertIn("500", reason)

    def test_probe_satellite_tcp_fallback(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(10)
            port = int(listener.getsockname()[1])
            ok, _ = probe_satellite(port, timeout=1.0)
            self.assertTrue(ok)
        closed = free_port()
        ok, _ = probe_satellite(closed, timeout=1.0)
        self.assertFalse(ok)

    def test_port_free(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = int(listener.getsockname()[1])
            self.assertFalse(port_free("127.0.0.1", port))
        self.assertTrue(port_free("127.0.0.1", port))


class LaunchTests(TempDirTestCase):
    def test_child_script_missing(self) -> None:
        with self.assertRaises(LaunchError):
            child_script(self.root, "master")

    def test_build_environment(self) -> None:
        code = Path("C:/code")
        root = Path("C:/root")
        env = build_environment(code, root)
        self.assertTrue(env["PYTHONPATH"].startswith(str(code)))
        self.assertEqual(env["DAGRUNNER_ROOT"], str(root))

    def test_terminate_tree_kills_real_process(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.assertIsNone(process.poll())
        terminate_tree(process, stop_timeout=2.0)
        self.assertIsNotNone(process.poll())


class SettingsTests(TempDirTestCase):
    def _write(self, text: str) -> Path:
        path = self.root / "var" / "config.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_master_defaults_and_overrides(self) -> None:
        path = self._write(
            "host: 127.0.0.1\n"
            "port: 7119\n"
            "watchdog:\n"
            "  interval: 2\n"
            "  alert_after: 10\n"
            "  unhealthy_after: 20\n"
            "  alert_to: [\"ops@example.com\"]\n"
        )
        settings = load_settings(path, "master")
        self.assertEqual(settings.host, "127.0.0.1")
        self.assertEqual(settings.port, 7119)
        self.assertEqual(settings.config.interval, 2.0)
        self.assertEqual(settings.config.alert_after, 10.0)
        self.assertEqual(settings.config.unhealthy_after, 20.0)
        self.assertEqual(settings.config.alert_to, ("ops@example.com",))
        self.assertTrue(settings.config.enabled)

    def test_satellite_settings(self) -> None:
        path = self._write(
            "satellite:\n  listen_port: 7121\nwatchdog:\n  enabled: false\n"
        )
        settings = load_settings(path, "satellite")
        self.assertEqual(settings.listen_port, 7121)
        self.assertFalse(settings.config.enabled)

    def test_invalid_threshold_order(self) -> None:
        path = self._write(
            "watchdog:\n  interval: 5\n  alert_after: 20\n  unhealthy_after: 10\n"
        )
        with self.assertRaises(LaunchError):
            load_settings(path, "master")

    def test_alert_after_must_not_be_below_interval(self) -> None:
        path = self._write(
            "watchdog:\n  interval: 5\n  alert_after: 3\n  unhealthy_after: 10\n"
        )
        with self.assertRaises(LaunchError):
            load_settings(path, "master")


class SupervisorDefaultProbeTests(TempDirTestCase):
    def test_master_default_probe_reads_settings(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                body = json.dumps(
                    {"ok": True, "scheduler_running": True, "db_ok": True}
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            from dataclasses import replace

            settings = replace(make_settings(self.root), port=server.server_address[1])
            watchdog = Watchdog(
                "master",
                self.root,
                settings,
                output=io.StringIO(),
                error=io.StringIO(),
            )
            ok, reason = watchdog._safe_probe()
            self.assertTrue(ok, reason)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)

    def test_satellite_default_probe_falls_back_to_tcp(self) -> None:
        from dataclasses import replace

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(10)
            settings = replace(
                make_settings(self.root), listen_port=listener.getsockname()[1]
            )
            watchdog = Watchdog(
                "satellite",
                self.root,
                settings,
                output=io.StringIO(),
                error=io.StringIO(),
            )
            ok, reason = watchdog._safe_probe()
            self.assertTrue(ok, reason)


class SupervisorTests(TempDirTestCase):
    def _watchdog(
        self, *, probe, startup_probe=None, notifier=None, terminator=None, **config
    ):
        settings = make_settings(self.root, **config)
        return Watchdog(
            "master",
            self.root,
            settings,
            spawner=self._spawner,
            probe=probe,
            startup_probe=startup_probe or (lambda: (False, "")),
            terminator=terminator or (lambda proc: None),
            notifier=notifier or FakeNotifier(),
            output=io.StringIO(),
            error=io.StringIO(),
        )

    def _run_async(self, watchdog: Watchdog):
        result: list[int] = []
        thread = threading.Thread(
            target=lambda: result.append(watchdog.run()), daemon=True
        )
        thread.start()
        # Cleanups run LIFO: signal stop, join, then release the temp dir.
        self.addCleanup(thread.join, 5.0)
        self.addCleanup(watchdog.shutdown)
        return result, thread

    def test_crash_triggers_restart(self) -> None:
        self.spawned: list[FakeProc] = []

        def spawner(version: str = "") -> FakeProc:
            proc = FakeProc(code=None)
            self.spawned.append(proc)
            return proc

        self._spawner = spawner
        healthy = {"value": True}
        notifier = FakeNotifier()
        watchdog = self._watchdog(
            probe=lambda: (healthy["value"], ""), notifier=notifier
        )
        result, thread = self._run_async(watchdog)
        self.assertTrue(wait_for(lambda: len(self.spawned) >= 1))
        first = self.spawned[0]
        first.code = 0
        self.assertTrue(wait_for(lambda: len(self.spawned) >= 2))
        watchdog.shutdown()
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [EXIT_OK])
        self.assertTrue(any(event.action == "restarted" for event in notifier.events))

    def test_intent_stop_exits_without_restart(self) -> None:
        self.spawned = []

        def spawner(version: str = "") -> FakeProc:
            proc = FakeProc(code=None)
            self.spawned.append(proc)
            return proc

        self._spawner = spawner
        request_stop(self.root, "master")
        # The supervisor resets intent to "run" when it boots, so signal the
        # intentional stop only once the child is up (as a real service does).
        notifier = FakeNotifier()
        watchdog = self._watchdog(probe=lambda: (True, ""), notifier=notifier)
        result, thread = self._run_async(watchdog)
        self.assertTrue(wait_for(lambda: len(self.spawned) == 1))
        request_stop(self.root, "master")
        self.spawned[0].code = 0
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [EXIT_OK])
        self.assertEqual(len(self.spawned), 1)

    def test_request_restart_relaunches_immediately(self) -> None:
        self.spawned = []

        def spawner() -> FakeProc:
            proc = FakeProc(code=None)
            self.spawned.append(proc)
            return proc

        self._spawner = spawner
        notifier = FakeNotifier()
        watchdog = self._watchdog(probe=lambda: (True, ""), notifier=notifier)
        result, thread = self._run_async(watchdog)
        self.assertTrue(wait_for(lambda: len(self.spawned) == 1))
        request_restart(self.root, "master")
        self.spawned[0].code = 0
        self.assertTrue(wait_for(lambda: len(self.spawned) >= 2))
        watchdog.shutdown()
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(get_intent(self.root, "master"), INTENT_RUN)
        self.assertIn("update_ok", [event.action for event in notifier.events])
        self.assertEqual(result, [EXIT_OK])

    def test_external_restart_intent_restarts_running_child(self) -> None:
        self.spawned = []
        terminated = []

        def spawner() -> FakeProc:
            proc = FakeProc(code=None)
            self.spawned.append(proc)
            return proc

        self._spawner = spawner
        notifier = FakeNotifier()
        watchdog = self._watchdog(
            probe=lambda: (True, ""),
            notifier=notifier,
            terminator=lambda proc: terminated.append(proc.pid),
        )
        result, thread = self._run_async(watchdog)
        self.assertTrue(wait_for(lambda: len(self.spawned) == 1))
        first_pid = self.spawned[0].pid
        request_restart(self.root, "master", pid=first_pid + 999)
        self.assertTrue(
            wait_for(lambda: len(self.spawned) >= 2, timeout=5.0),
            "外部 restart 指令未触发重启",
        )
        watchdog.shutdown()
        thread.join(timeout=5.0)
        self.assertIn(first_pid, terminated)
        self.assertEqual(get_intent(self.root, "master"), INTENT_RUN)
        self.assertIn("update_ok", [event.action for event in notifier.events])
        self.assertEqual(result, [EXIT_OK])

    def test_external_stop_intent_stops_watchdog(self) -> None:
        self.spawned = []

        def spawner() -> FakeProc:
            proc = FakeProc(code=None)
            self.spawned.append(proc)
            return proc

        self._spawner = spawner
        watchdog = self._watchdog(probe=lambda: (True, ""))
        result, thread = self._run_async(watchdog)
        self.assertTrue(wait_for(lambda: len(self.spawned) == 1))
        request_stop(self.root, "master", pid=self.spawned[0].pid + 999)
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [EXIT_OK])

    def test_unhealthy_threshold_restarts_and_alerts(self) -> None:
        self.spawned = []
        healthy = {"value": True}

        def spawner() -> FakeProc:
            proc = FakeProc(code=None)
            self.spawned.append(proc)
            return proc

        self._spawner = spawner
        notifier = FakeNotifier()
        watchdog = self._watchdog(
            probe=lambda: (healthy["value"], "boom"), notifier=notifier
        )
        result, thread = self._run_async(watchdog)
        self.assertTrue(wait_for(lambda: len(self.spawned) == 1))
        time.sleep(0.08)
        healthy["value"] = False
        self.assertTrue(
            wait_for(lambda: len(self.spawned) >= 2, timeout=5.0),
            "看门狗未在阈值后重启",
        )
        watchdog.shutdown()
        thread.join(timeout=5.0)
        actions = [event.action for event in notifier.events]
        self.assertIn("unhealthy", actions)
        self.assertIn("restarted", actions)
        self.assertEqual(result, [EXIT_OK])

    def test_start_once_single_attempt_healthy(self) -> None:
        spawned: list[FakeProc] = []
        self._spawner = lambda: (
            spawned.append(FakeProc(code=None)) or spawned[-1]
        )
        watchdog = self._watchdog(probe=lambda: (True, ""))
        self.assertEqual(watchdog.start_once(), EXIT_OK)
        self.assertEqual(len(spawned), 1)

    def test_start_once_single_attempt_unhealthy(self) -> None:
        from dagrunner.watchdog.supervisor import EXIT_ERROR

        spawned: list[FakeProc] = []
        self._spawner = lambda: (
            spawned.append(FakeProc(code=None)) or spawned[-1]
        )
        watchdog = self._watchdog(probe=lambda: (False, "down"))
        self.assertEqual(watchdog.start_once(), EXIT_ERROR)
        self.assertEqual(len(spawned), 1)

    def test_notify_mode_does_not_restart(self) -> None:
        self.spawned = []
        healthy = {"value": True}

        def spawner(version: str = "") -> FakeProc:
            proc = FakeProc(code=None)
            self.spawned.append(proc)
            return proc

        self._spawner = spawner
        notifier = FakeNotifier()
        watchdog = self._watchdog(
            probe=lambda: (healthy["value"], "boom"),
            notifier=notifier,
            mode="notify",
        )
        result, thread = self._run_async(watchdog)
        self.assertTrue(wait_for(lambda: len(self.spawned) == 1))
        healthy["value"] = False
        time.sleep(0.4)
        self.assertEqual(len(self.spawned), 1)
        watchdog.shutdown()
        thread.join(timeout=5.0)
        self.assertTrue(
            any(event.action == "unhealthy" for event in notifier.events)
        )
        self.assertEqual(result, [EXIT_OK])


class DatabasePingTests(TempDirTestCase):
    def test_ping_reports_reachable_database(self) -> None:
        from dagrunner.database import create_state_database

        database = create_state_database(str(self.root / "scheduler.db"))
        self.assertIs(database.ping(), True)


if __name__ == "__main__":
    unittest.main()
