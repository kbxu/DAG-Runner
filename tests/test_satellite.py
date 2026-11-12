from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from dagrunner.auth import client_password_hash, store_password
from dagrunner.database import StateDatabase
from dagrunner.executor import TaskExecutor
from dagrunner.logger import TaskLogManager
from dagrunner.runner import WorkflowRunner
from dagrunner.satellite_agent import (
    SatelliteAgent,
    SatelliteAgentConfig,
    create_satellite_agent_app,
)
from dagrunner.satellite_crypto import (
    FileTrustStore,
    NonceStore,
    build_auth_headers,
    certificate_fingerprint,
    client_ssl_context,
    ensure_certificate,
    peer_fingerprint,
    server_ssl_context,
    sign_auth,
    verify_auth,
)
from dagrunner.satellite_executor import SatelliteTaskExecutor
from dagrunner.satellite_http import SatelliteClient
from dagrunner.satellite_master import SatelliteManager, create_satellite_master_app
from dagrunner.satellite_server import serve_ssl
from dagrunner.satellite_protocol import (
    JOB_FAILED,
    JOB_SUCCESS,
    PATH_STREAM,
    JobSpec,
    ProtocolError,
    SatelliteConfig,
    job_spec_from_task,
    new_job_id,
)
from dagrunner.webapp import create_app
from dagrunner.workflow import Task, Workflow, WorkflowError


TOKEN = "unit-test-token-0123456789"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _Server:
    def __init__(self, app, cert: Path, key: Path, host: str = "127.0.0.1", port: int | None = None):
        self.port = port or _free_port()
        context = server_ssl_context(cert, key)
        self.server, self.terminator = serve_ssl(
            app, host=host, port=self.port, threads=8, ssl_context=context
        )

    def stop(self) -> None:
        try:
            self.terminator.stop()
        except Exception:
            pass
        try:
            self.server.request_shutdown()
        except Exception:
            pass


class SatelliteCryptoTests(unittest.TestCase):
    def test_example_configs_use_distinct_cert_paths(self) -> None:
        import yaml

        root = Path(__file__).resolve().parent.parent
        master = yaml.safe_load(
            (root / "var" / "config.example.yaml").read_text(encoding="utf-8")
        )
        agent = yaml.safe_load(
            (root / "var" / "satellite.example.yaml").read_text(encoding="utf-8")
        )
        self.assertNotEqual(
            master["satellite"]["cert"], agent["satellite"]["cert"]
        )
        self.assertNotEqual(
            master["satellite"]["key"], agent["satellite"]["key"]
        )

    def test_certificate_generation_is_reproducible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cert, key = ensure_certificate(root / "cert.pem", root / "key.pem")
            self.assertTrue(cert.is_file())
            self.assertTrue(key.is_file())
            first = certificate_fingerprint(cert)
            self.assertEqual(len(first), 64)
            ensure_certificate(cert, key)
            self.assertEqual(certificate_fingerprint(cert), first)

    def test_sign_and_verify_auth(self) -> None:
        headers = build_auth_headers(
            TOKEN, nonce="abc", alias="sat-01", method="POST", path="/api/satellite/v1/jobs"
        )
        self.assertTrue(
            verify_auth(
                TOKEN,
                nonce="abc",
                alias="sat-01",
                timestamp=headers["X-DAGRunner-Timestamp"],
                method="POST",
                path="/api/satellite/v1/jobs",
                provided=headers["X-DAGRunner-Auth"],
            )
        )
        self.assertFalse(
            verify_auth(
                TOKEN,
                nonce="abc",
                alias="sat-01",
                timestamp=headers["X-DAGRunner-Timestamp"],
                method="POST",
                path="/api/satellite/v1/jobs",
                provided=sign_auth(
                    "other",
                    nonce="abc",
                    alias="sat-01",
                    timestamp=headers["X-DAGRunner-Timestamp"],
                    method="POST",
                    path="/api/satellite/v1/jobs",
                ),
            )
        )

    def test_nonce_is_single_use(self) -> None:
        store = NonceStore()
        nonce = store.issue()
        self.assertTrue(store.consume(nonce))
        self.assertFalse(store.consume(nonce))

    def test_file_trust_store_pins_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = FileTrustStore(Path(directory) / "known.json")
            self.assertTrue(store.check("a" * 64))
            self.assertFalse(store.check("b" * 64))
            store2 = FileTrustStore(Path(directory) / "known.json")
            self.assertFalse(store2.check("b" * 64))

    def test_peer_fingerprint_reads_certificate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cert, key = ensure_certificate(root / "cert.pem", root / "key.pem")
            server = _Server(create_satellite_health_app(), cert, key, port=_free_port())
            try:
                context = client_ssl_context()
                raw = socket.create_connection(("127.0.0.1", server.port), timeout=10)
                sock = context.wrap_socket(raw, server_side=False)
                self.assertEqual(peer_fingerprint(sock), certificate_fingerprint(cert))
                sock.close()
            finally:
                server.stop()


def create_satellite_health_app():
    from flask import Flask

    app = Flask(__name__, template_folder=None, static_folder=None)

    @app.get("/health")
    def health():
        return {"ok": True}

    return app


class SatelliteProtocolTests(unittest.TestCase):
    def test_job_spec_validation(self) -> None:
        spec = JobSpec(
            job_id="20261111_120000_ab12cd34",
            workflow_name="workflow_x",
            run_id="run_x",
            task_name="task_x",
            command="echo hi",
        )
        self.assertEqual(JobSpec.from_json(spec.to_json()).command, "echo hi")
        with self.assertRaises(ProtocolError):
            JobSpec.from_json({**spec.to_json(), "shell": "zsh"})
        with self.assertRaises(ProtocolError):
            JobSpec.from_json({**spec.to_json(), "command": ""})
        with self.assertRaises(ProtocolError):
            JobSpec.from_json({**spec.to_json(), "job_id": "bad id"})

    def test_job_spec_from_task(self) -> None:
        task = Task(name="run", command="echo hi", args=("a", "b"))
        spec = job_spec_from_task(
            task,
            job_id="j1",
            workflow_name="wf",
            run_id="r1",
            setup="export A=1",
            workdir="/tmp",
        )
        self.assertEqual(spec.task_name, "run")
        self.assertEqual(spec.args, ("a", "b"))
        self.assertEqual(spec.workdir, "/tmp")
        self.assertEqual(spec.setup, "export A=1")


class SatelliteEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._directory.name)
        self.database = StateDatabase(self.root / "scheduler.db")
        self.master_cert = self.root / "master_cert.pem"
        self.master_key = self.root / "master_key.pem"
        self.agent_cert = self.root / "agent_cert.pem"
        self.agent_key = self.root / "agent_key.pem"
        ensure_certificate(self.master_cert, self.master_key)
        ensure_certificate(self.agent_cert, self.agent_key)
        self.manager = SatelliteManager(self.database, timeout=10, poll_interval=0.3)
        self.master_app = create_satellite_master_app(self.manager)
        self.master = _Server(self.master_app, self.master_cert, self.master_key)
        self.manager.start()
        self.agents: list[SatelliteAgent] = []
        self.agent_servers: list[_Server] = []

    def tearDown(self) -> None:
        for agent in self.agents:
            agent.shutdown()
        for server in self.agent_servers:
            server.stop()
        self.manager.shutdown()
        self.master.stop()
        self._directory.cleanup()

    def _start_agent(
        self,
        mode: str,
        *,
        alias: str = "sat-01",
        master_cert_fingerprint: str = "",
    ) -> SatelliteAgent:
        config = SatelliteAgentConfig(
            alias=alias,
            master_url=f"https://127.0.0.1:{self.master.port}",
            token=TOKEN,
            mode=mode,
            listen_host="127.0.0.1",
            listen_port=_free_port(),
            cert=str(self.agent_cert),
            key=str(self.agent_key),
            host_key_policy="pinned" if master_cert_fingerprint else "accept-new",
            master_cert_fingerprint=master_cert_fingerprint,
            log_dir=str(self.root / "agent-logs"),
            heartbeat_interval=5,
            probe_interval=5,
            timeout=10,
        )
        agent = SatelliteAgent(config)
        app = create_satellite_agent_app(agent)
        server = _Server(app, self.agent_cert, self.agent_key, port=config.listen_port)
        thread = threading.Thread(target=agent._agent_loop, daemon=True)
        thread.start()
        self.agents.append(agent)
        self.agent_servers.append(server)
        return agent

    def _register_satellite(
        self, *, alias: str = "sat-01", mode: str, url: str = ""
    ) -> None:
        self.database.upsert_satellite(
            SatelliteConfig(
                alias=alias,
                name=alias,
                mode=mode,
                url=url,
                token=TOKEN,
            )
        )

    def _wait_for(self, predicate, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.1)
        return False

    def _submit(self, task: Task, *, alias: str = "sat-01") -> tuple[int, str, Path]:
        log_file = (
            self.root / "logs" / "workflow_test" / f"run_{alias}" / f"{task.name}.log"
        )
        executor = SatelliteTaskExecutor(self.manager, alias)
        result = executor.execute(
            task,
            cwd=self.root,
            workflow_setup="",
            log_file=log_file,
            run_id="run_test",
            workflow_name="workflow_test",
        )
        text = log_file.read_text(encoding="utf-8", errors="replace") if log_file.is_file() else ""
        return result.exit_code, text, log_file

    def test_forward_mode_runs_command(self) -> None:
        self._start_agent("forward")
        port = self.agent_servers[-1].port
        self._register_satellite(mode="forward", url=f"https://127.0.0.1:{port}")
        exit_code, text, _ = self._submit(Task(name="run", command="echo hello-satellite"))
        self.assertEqual(exit_code, 0)
        self.assertIn("hello-satellite", text)
        self.assertIn("Executing command:", text)

    def test_forward_mode_reports_failure(self) -> None:
        self._start_agent("forward")
        port = self.agent_servers[-1].port
        self._register_satellite(mode="forward", url=f"https://127.0.0.1:{port}")
        task = Task(
            name="run",
            command=[sys_executable(), "-c", "import sys; sys.exit(3)"],
        )
        exit_code, text, _ = self._submit(task)
        self.assertEqual(exit_code, 3)
        self.assertIn("code 3", text)

    def test_reverse_mode_runs_command(self) -> None:
        self._start_agent("reverse")
        self._register_satellite(mode="reverse")
        self.assertTrue(
            self._wait_for(lambda: self.manager._get_stream("sat-01") is not None),
            "satellite stream did not register",
        )
        exit_code, text, _ = self._submit(Task(name="run", command="echo hello-reverse"))
        self.assertEqual(exit_code, 0)
        self.assertIn("hello-reverse", text)

    def test_reverse_mode_reports_failure(self) -> None:
        self._start_agent("reverse")
        self._register_satellite(mode="reverse")
        self.assertTrue(
            self._wait_for(lambda: self.manager._get_stream("sat-01") is not None),
            "satellite stream did not register",
        )
        task = Task(
            name="run",
            command=[sys_executable(), "-c", "import sys; sys.exit(5)"],
        )
        exit_code, text, _ = self._submit(task)
        self.assertEqual(exit_code, 5)

    def test_mixed_forward_and_reverse_satellites(self) -> None:
        self._start_agent("forward", alias="sat-fwd")
        forward_port = self.agent_servers[-1].port
        self._register_satellite(
            alias="sat-fwd", mode="forward", url=f"https://127.0.0.1:{forward_port}"
        )

        self._start_agent("reverse", alias="sat-rev")
        self._register_satellite(alias="sat-rev", mode="reverse")
        self.assertTrue(
            self._wait_for(lambda: self.manager._get_stream("sat-rev") is not None),
            "reverse stream did not register",
        )

        forward_code, forward_text, _ = self._submit(
            Task(name="run", command="echo mixed-forward"), alias="sat-fwd"
        )
        reverse_code, reverse_text, _ = self._submit(
            Task(name="run", command="echo mixed-reverse"), alias="sat-rev"
        )
        self.assertEqual(forward_code, 0)
        self.assertIn("mixed-forward", forward_text)
        self.assertEqual(reverse_code, 0)
        self.assertIn("mixed-reverse", reverse_text)

    def test_forward_job_reuses_session_token(self) -> None:
        agent = self._start_agent("forward")
        port = self.agent_servers[-1].port
        self._register_satellite(mode="forward", url=f"https://127.0.0.1:{port}")
        nonces = {"count": 0}
        original_issue = agent.nonce_store.issue

        def counting_issue() -> str:
            nonces["count"] += 1
            return original_issue()

        agent.nonce_store.issue = counting_issue
        task = Task(
            name="run", command=[sys_executable(), "-c", "import time; time.sleep(2)"]
        )
        exit_code, _, _ = self._submit(task)
        self.assertEqual(exit_code, 0)
        # One registration challenge is enough; polling must reuse the session.
        self.assertLessEqual(nonces["count"], 2)

    def _executor_resolver(self):
        def resolver(alias):
            if alias in ("", "local"):
                return TaskExecutor()
            if not self.database.is_satellite(alias):
                raise WorkflowError(f"卫星不存在：{alias!r}")
            return SatelliteTaskExecutor(self.manager, alias)

        return resolver

    def _run_workflow(self, definition, *, runner="local"):
        row = self.database.create_workflow("wf", definition)
        key = row["workflow_key"]
        workflow = Workflow.from_yaml(definition, name_override=key)
        run_id, status = WorkflowRunner(
            self.database,
            TaskLogManager(self.root / "logs"),
            executor_resolver=self._executor_resolver(),
        ).run(workflow, runner_alias=runner)
        return key, run_id, status

    def test_workflow_runs_tasks_on_different_runners(self) -> None:
        self._start_agent("forward")
        port = self.agent_servers[-1].port
        self._register_satellite(mode="forward", url=f"https://127.0.0.1:{port}")
        definition = (
            "description: mixed\n"
            "runner: sat-01\n"
            "tasks:\n"
            "  local_task:\n"
            "    command: echo local-ok\n"
            "    runner: local\n"
            "  sat_task:\n"
            "    command: echo sat-ok\n"
        )
        key, run_id, status = self._run_workflow(definition)
        self.assertEqual(status, "SUCCESS")
        root = self.root / "logs" / key / run_id
        local_log = (root / "local_task.log").read_text(
            encoding="utf-8", errors="replace"
        )
        sat_log = (root / "sat_task.log").read_text(
            encoding="utf-8", errors="replace"
        )
        self.assertIn("local-ok", local_log)
        self.assertIn("sat-ok", sat_log)

    def test_task_on_missing_satellite_fails_with_error(self) -> None:
        definition = (
            "description: missing\n"
            "tasks:\n"
            "  a:\n"
            "    command: echo a\n"
            "    runner: ghost\n"
        )
        _, run_id, status = self._run_workflow(definition)
        self.assertEqual(status, "FAILED")
        task = self.database.task_states(run_id)["a"]
        self.assertEqual(task["status"], "FAILED")
        self.assertIn("卫星不存在", task["error_message"] or "")

    def test_task_runner_selects_platform_setup(self) -> None:
        self._start_agent("forward")
        port = self.agent_servers[-1].port
        self._register_satellite(mode="forward", url=f"https://127.0.0.1:{port}")
        definition = (
            "description: setup\n"
            "runner: sat-01\n"
            "setup:\n  linux: echo LINUX-SETUP\n  windows: echo WINDOWS-SETUP\n"
            "tasks:\n"
            "  a:\n"
            "    command: echo a\n"
        )
        key, run_id, status = self._run_workflow(definition)
        self.assertEqual(status, "SUCCESS")
        text = (self.root / "logs" / key / run_id / "a.log").read_text(
            encoding="utf-8", errors="replace"
        )
        expected = "WINDOWS-SETUP" if os.name == "nt" else "LINUX-SETUP"
        self.assertIn(expected, text)

    def test_reverse_stream_flushes_headers_immediately(self) -> None:
        self._start_agent("reverse")
        self._register_satellite(mode="reverse")
        self.assertTrue(
            self._wait_for(lambda: self.manager._get_stream("sat-01") is not None)
        )
        client = SatelliteClient(
            f"https://127.0.0.1:{self.master.port}",
            token=TOKEN,
            alias="sat-01",
            timeout=10,
            trust=lambda fp: True,
        )
        client.register(json_body={"alias": "sat-01", "platform": "linux"})
        started = time.monotonic()
        connection, response = client.open_stream(PATH_STREAM)
        line = response.readline()
        elapsed = time.monotonic() - started
        connection.close()
        self.assertIn(b"event: ping", line)
        self.assertLess(elapsed, 2.0)

    def test_superseded_stream_does_not_mark_satellite_offline(self) -> None:
        self._start_agent("reverse")
        self._register_satellite(mode="reverse")
        self.assertTrue(
            self._wait_for(lambda: self.manager._get_stream("sat-01") is not None)
        )
        old_state = self.manager._get_stream("sat-01")
        new_state = self.manager._register_stream("sat-01")
        self.manager._drop_stream("sat-01", old_state)
        self.assertEqual(self.database.get_satellite("sat-01").status, "online")
        self.manager._drop_stream("sat-01", new_state)
        self.assertEqual(self.database.get_satellite("sat-01").status, "offline")

    def test_reverse_mode_accepts_pinned_fingerprint(self) -> None:
        fingerprint = certificate_fingerprint(self.master_cert)
        self._start_agent("reverse", master_cert_fingerprint=fingerprint)
        self._register_satellite(mode="reverse")
        self.assertTrue(
            self._wait_for(lambda: self.manager._get_stream("sat-01") is not None),
            "pinned satellite stream did not register",
        )

    def test_reverse_mode_rejects_wrong_pinned_fingerprint(self) -> None:
        self._start_agent("reverse", master_cert_fingerprint="0" * 64)
        self._register_satellite(mode="reverse")
        time.sleep(3)
        self.assertIsNone(
            self.manager._get_stream("sat-01"),
            "satellite registered with a wrong pinned fingerprint",
        )

    def test_executor_uses_satellite_platform(self) -> None:
        self._start_agent("reverse")
        self._register_satellite(mode="reverse")
        self.assertTrue(
            self._wait_for(lambda: self.manager._get_stream("sat-01") is not None),
        )
        executor = SatelliteTaskExecutor(self.manager, "sat-01")
        self.assertEqual(executor.platform, "windows" if os.name == "nt" else "linux")


class RunnerAssignmentTests(unittest.TestCase):
    def test_task_runner_overrides_workflow_default(self) -> None:
        workflow = Workflow.from_yaml(
            "description: wf\nrunner: sat-01\ntasks:\n"
            "  a:\n    command: echo a\n"
            "  b:\n    command: echo b\n    runner: sat-02\n",
            name_override="wf",
        )
        self.assertEqual(workflow.runner, "sat-01")
        self.assertEqual(workflow.task_runner(workflow.tasks["a"]), "sat-01")
        self.assertEqual(workflow.task_runner(workflow.tasks["b"]), "sat-02")

    def test_runner_defaults_to_local(self) -> None:
        workflow = Workflow.from_yaml(
            "description: wf\ntasks:\n  a:\n    command: echo a\n",
            name_override="wf",
        )
        self.assertIsNone(workflow.runner)
        self.assertEqual(workflow.task_runner(workflow.tasks["a"]), "local")
        self.assertEqual(workflow.runner_locations(), [("local", 1)])

    def test_runner_locations_summary(self) -> None:
        workflow = Workflow.from_yaml(
            "description: wf\nrunner: sat-01\ntasks:\n"
            "  a:\n    command: echo a\n"
            "  b:\n    command: echo b\n    runner: sat-02\n"
            "  c:\n    command: echo c\n    runner: local\n",
            name_override="wf",
        )
        self.assertEqual(
            workflow.runner_locations(),
            [("sat-01", 1), ("sat-02", 1), ("local", 1)],
        )

    def test_invalid_runner_rejected(self) -> None:
        with self.assertRaises(WorkflowError):
            Workflow.from_yaml(
                "description: wf\nrunner: bad alias\ntasks:\n"
                "  a:\n    command: echo a\n",
                name_override="wf",
            )
        with self.assertRaises(WorkflowError):
            Workflow.from_yaml(
                "description: wf\ntasks:\n  a:\n    command: echo a\n"
                "    runner: bad alias\n",
                name_override="wf",
            )


class SatelliteManagementApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self._directory.name)
        self.root = root
        self.database = StateDatabase(root / "scheduler.db")
        store_password(self.database, "tester", "test-password-1234")
        self.app = create_app(
            database_path=root / "scheduler.db",
            logs_path=root / "logs",
            start_scheduler=False,
            allow_insecure_remote_login=True,
            satellite_url="https://master.example.com:7120",
            satellite_port=7120,
            satellite_fingerprint="ab" * 32,
        )
        self.client = self.app.test_client()
        self.client.post(
            "/api/auth/login",
            json={
                "username": "tester",
                "password_hash": client_password_hash("test-password-1234"),
            },
        )

    def tearDown(self) -> None:
        self.app.extensions["schedule_service"].shutdown()
        self.app.extensions["execution_service"].shutdown()
        self._directory.cleanup()

    def test_list_satellites_empty(self) -> None:
        payload = self.client.get("/api/satellites").get_json()
        self.assertEqual(payload["satellites"], [])

    def test_satellite_service_flag_disabled(self) -> None:
        payload = self.client.get("/api/satellites").get_json()
        self.assertFalse(payload["enabled"])
        overview = self.client.get("/api/system/overview").get_json()
        self.assertFalse(overview["satellite_enabled"])

    def test_run_records_runner_locations(self) -> None:
        definition = (
            "description: wf\ntasks:\n  a:\n    command: echo a\n"
            "  b:\n    command: echo b\n"
        )
        key = self.database.create_workflow("wf", definition)["workflow_key"]
        workflow = Workflow.from_yaml(definition, name_override=key)
        run_id, status = WorkflowRunner(
            self.database, TaskLogManager(self.root / "logs")
        ).run(workflow, runner_alias="local")
        self.assertEqual(status, "SUCCESS")
        runs = self.client.get("/api/runs").get_json()["runs"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["runner"], "local")
        self.assertEqual(
            runs[0]["runner_locations"], [{"alias": "local", "count": 2}]
        )

    def test_run_label_uses_recorded_locations_not_latest(self) -> None:
        definition = "description: wf\ntasks:\n  a:\n    command: echo a\n"
        key = self.database.create_workflow("wf", definition)["workflow_key"]
        workflow = Workflow.from_yaml(definition, name_override=key)
        WorkflowRunner(self.database, TaskLogManager(self.root / "logs")).run(
            workflow, runner_alias="local"
        )
        self.client.put(
            "/api/satellites/sat-01",
            json={"mode": "forward", "url": "https://10.0.0.2:7121"},
        )
        self.client.put(
            f"/api/workflows/{key}",
            json={
                "definition": "description: wf\nrunner: sat-01\ntasks:\n"
                "  a:\n    command: echo a\n",
                "name": "wf",
                "runner": "sat-01",
            },
        )
        runs = self.client.get("/api/runs").get_json()["runs"]
        self.assertEqual(runs[0]["runner"], "local")
        self.assertEqual(
            runs[0]["runner_locations"], [{"alias": "local", "count": 1}]
        )

    def test_satellite_service_flag_enabled(self) -> None:
        app = create_app(
            database_path=self.root / "scheduler.db",
            logs_path=self.root / "logs",
            start_scheduler=False,
            allow_insecure_remote_login=True,
            satellite_manager=object(),
        )
        client = app.test_client()
        client.post(
            "/api/auth/login",
            json={
                "username": "tester",
                "password_hash": client_password_hash("test-password-1234"),
            },
        )
        payload = client.get("/api/satellites").get_json()
        self.assertTrue(payload["enabled"])
        overview = client.get("/api/system/overview").get_json()
        self.assertTrue(overview["satellite_enabled"])
        app.extensions["schedule_service"].shutdown()
        app.extensions["execution_service"].shutdown()

    def test_upsert_generates_token(self) -> None:
        response = self.client.put(
            "/api/satellites/sat-01",
            json={"name": "Sat 01", "mode": "forward", "url": "https://10.0.0.2:7121"},
        )
        self.assertEqual(response.status_code, 200)
        token = response.get_json()["token"]
        self.assertTrue(token)
        satellites = self.client.get("/api/satellites").get_json()["satellites"]
        self.assertEqual(len(satellites), 1)
        self.assertEqual(satellites[0]["alias"], "sat-01")
        self.assertTrue(satellites[0]["has_token"])
        self.assertNotIn("token", satellites[0])

    def test_upsert_keeps_existing_token(self) -> None:
        self.client.put("/api/satellites/sat-01", json={"name": "Sat 01", "mode": "auto"})
        first = self.database.get_satellite("sat-01").token
        response = self.client.put(
            "/api/satellites/sat-01", json={"name": "Renamed", "mode": "reverse"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get_json()["token"])
        self.assertEqual(self.database.get_satellite("sat-01").token, first)
        self.assertEqual(self.database.get_satellite("sat-01").name, "Renamed")

    def test_upsert_rejects_bad_mode(self) -> None:
        response = self.client.put("/api/satellites/sat-01", json={"mode": "sideways"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("mode", response.get_json()["field_errors"])

    def test_upsert_rejects_bad_url(self) -> None:
        response = self.client.put(
            "/api/satellites/sat-01", json={"url": "http://10.0.0.2"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("url", response.get_json()["field_errors"])

    def test_delete_blocked_while_in_use(self) -> None:
        self.client.put("/api/satellites/sat-01", json={"mode": "auto"})
        row = self.database.create_workflow(
            "wf", "description: wf\ntasks:\n  a:\n    command: echo a\n"
        )
        self.database.set_workflow_runner(row["workflow_key"], "sat-01")
        response = self.client.delete("/api/satellites/sat-01")
        self.assertEqual(response.status_code, 400)
        self.database.set_workflow_runner(row["workflow_key"], "local")
        self.assertEqual(self.client.delete("/api/satellites/sat-01").status_code, 200)

    def test_config_download(self) -> None:
        self.client.put(
            "/api/satellites/sat-01",
            json={"mode": "forward", "url": "https://10.0.0.2:7121"},
        )
        response = self.client.get("/api/satellites/sat-01/config")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn("alias: sat-01", text)
        self.assertIn("master_url: https://master.example.com:7120", text)
        self.assertIn("token:", text)

    def test_workflow_payload_includes_runner_locations(self) -> None:
        self.database.create_workflow(
            "wf",
            "description: wf\nrunner: sat-01\ntasks:\n"
            "  a:\n    command: echo a\n"
            "  b:\n    command: echo b\n    runner: local\n",
        )
        workflows = self.client.get("/api/workflows").get_json()["workflows"]
        self.assertEqual(len(workflows), 1)
        self.assertEqual(workflows[0]["runner"], "sat-01")
        self.assertEqual(
            workflows[0]["runner_locations"],
            [{"alias": "sat-01", "count": 1}, {"alias": "local", "count": 1}],
        )
        self.assertEqual(workflows[0]["tasks"][0]["runner"], "sat-01")
        self.assertEqual(workflows[0]["tasks"][1]["runner"], "local")

    def test_edit_workflow_applies_runner_to_yaml(self) -> None:
        self.client.put(
            "/api/satellites/sat-01",
            json={"mode": "forward", "url": "https://10.0.0.2:7121"},
        )
        row = self.database.create_workflow(
            "wf", "description: wf\ntasks:\n  a:\n    command: echo a\n"
        )
        key = row["workflow_key"]
        response = self.client.put(
            f"/api/workflows/{key}",
            json={
                "definition": "description: wf\ntasks:\n  a:\n    command: echo a\n",
                "name": "wf",
                "runner": "sat-01",
            },
        )
        self.assertEqual(response.status_code, 200)
        stored = self.database.get_workflow(key)
        self.assertIn("runner: sat-01", stored["definition"])
        self.assertEqual(stored["runner"], "sat-01")

    def test_edit_workflow_local_runner_removes_yaml_key(self) -> None:
        row = self.database.create_workflow(
            "wf",
            "description: wf\nrunner: sat-01\ntasks:\n  a:\n    command: echo a\n",
        )
        key = row["workflow_key"]
        response = self.client.put(
            f"/api/workflows/{key}",
            json={
                "definition": "description: wf\nrunner: sat-01\ntasks:\n"
                "  a:\n    command: echo a\n",
                "name": "wf",
                "runner": "local",
            },
        )
        self.assertEqual(response.status_code, 200)
        stored = self.database.get_workflow(key)
        self.assertNotIn("runner:", stored["definition"])
        self.assertEqual(stored["runner"], "local")

    def test_test_endpoint_reports_unreachable(self) -> None:
        self.client.put(
            "/api/satellites/sat-01",
            json={"mode": "forward", "url": "https://127.0.0.1:1"},
        )
        response = self.client.post("/api/satellites/sat-01/test")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertFalse(payload["reachable"])


def sys_executable() -> str:
    import sys

    return sys.executable


if __name__ == "__main__":
    unittest.main()
