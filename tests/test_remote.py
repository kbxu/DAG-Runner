from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from dagrunner.auth import client_password_hash, store_password
from dagrunner.database import StateDatabase
from dagrunner.remote import (
    RemoteConfig,
    RemoteSettings,
    RemoteTaskExecutor,
    check_remote,
    detect_ssh_path,
)
from dagrunner.webapp import create_app


def _remote(alias: str = "box", **overrides) -> RemoteConfig:
    values = {
        "alias": alias,
        "username": "ops",
        "host": "192.168.1.10",
        "port": 22,
        "workdir": "~/work",
        "ssh_path": "/usr/bin/ssh",
        "key_path": "/home/ops/.ssh/id_rsa",
        "host_key_policy": "accept-new",
        "connect_timeout": 7,
    }
    values.update(overrides)
    return RemoteConfig(**values)


class RemoteExecutorTests(unittest.TestCase):
    def test_ssh_command_contains_key_port_and_batch_mode(self) -> None:
        executor = RemoteTaskExecutor(_remote())
        command = executor.build_ssh_command("bash")
        self.assertEqual(command[0], "/usr/bin/ssh")
        self.assertIn("-i", command)
        self.assertIn("/home/ops/.ssh/id_rsa", command)
        self.assertIn("-p", command)
        self.assertIn("22", command)
        self.assertIn("BatchMode=yes", command)
        self.assertIn("StrictHostKeyChecking=accept-new", command)
        self.assertIn("ops@192.168.1.10", command)
        self.assertEqual(command[-1], "bash -s")

    def test_script_changes_into_workdir_and_runs_setup(self) -> None:
        from dagrunner.workflow import Task

        executor = RemoteTaskExecutor(_remote())
        script = executor.build_script(Task(name="run", command="echo hi"), "export A=1")
        self.assertEqual(script.splitlines()[0], "set -e")
        self.assertIn('cd "$HOME"/work', script)
        self.assertIn("export A=1", script)
        self.assertTrue(script.endswith("echo hi"))

    def test_workdir_absolute_path_is_quoted(self) -> None:
        from dagrunner.workflow import Task

        executor = RemoteTaskExecutor(_remote(workdir="/srv/my dir"))
        script = executor.build_script(Task(name="run", command="echo hi"), "")
        self.assertIn("cd '/srv/my dir'", script)

    def test_check_remote_reports_missing_ssh(self) -> None:
        result = check_remote(_remote(ssh_path="/definitely/not/here/ssh"))
        self.assertFalse(result.reachable)
        self.assertIn("ssh not found", result.error_message or "")

    def test_detect_ssh_path_returns_string(self) -> None:
        self.assertIsInstance(detect_ssh_path(), str)

    def _fake_ssh(self, root: Path, code: int) -> str:
        if os.name != "nt":
            self.skipTest("fake ssh batch wrapper is Windows-only")
        batch = root / "fake-ssh.bat"
        batch.write_text(
            "@echo off\r\n"
            "echo remote-output\r\n"
            "echo remote-error 1>&2\r\n"
            f"exit /b {code}\r\n",
            encoding="ascii",
        )
        return str(batch)

    def test_execute_streams_remote_output_to_log(self) -> None:
        from dagrunner.workflow import Task

        with TemporaryDirectory() as directory:
            root = Path(directory)
            ssh = self._fake_ssh(root, 0)
            config = _remote(ssh_path=ssh, key_path="", host="h", username="u")
            log_file = root / "logs" / "task.log"
            result = RemoteTaskExecutor(config).execute(
                Task(name="run", command="echo hi"),
                cwd=root,
                workflow_setup="",
                log_file=log_file,
            )
            self.assertEqual(result.exit_code, 0)
            text = log_file.read_text(encoding="utf-8")
            self.assertIn("remote-output", text)
            self.assertIn("remote-error", text)

    def test_execute_reports_remote_exit_code(self) -> None:
        from dagrunner.workflow import Task

        with TemporaryDirectory() as directory:
            root = Path(directory)
            ssh = self._fake_ssh(root, 3)
            config = _remote(ssh_path=ssh, key_path="", host="h", username="u")
            result = RemoteTaskExecutor(config).execute(
                Task(name="run", command="echo hi"),
                cwd=root,
                workflow_setup="",
                log_file=root / "logs" / "task.log",
            )
            self.assertEqual(result.exit_code, 3)
            self.assertIn("code 3", result.error_message or "")


class RemoteDatabaseTests(unittest.TestCase):
    def test_crud_and_status_round_trip(self) -> None:
        with TemporaryDirectory() as directory:
            database = StateDatabase(Path(directory) / "scheduler.db")
            database.upsert_remote(_remote())
            configs = database.list_remotes()
            self.assertEqual(len(configs), 1)
            self.assertEqual(configs[0].alias, "box")
            self.assertEqual(configs[0].workdir, "~/work")
            self.assertEqual(configs[0].ssh_path, "")

            database.upsert_remote(_remote(host="10.0.0.2"))
            self.assertEqual(database.get_remote("box").host, "10.0.0.2")

            database.set_remote_status("box", True, None, 42)
            status = database.list_remote_status()["box"]
            self.assertEqual(status["reachable"], 1)
            self.assertEqual(status["latency_ms"], 42)

            database.delete_remote("box")
            self.assertIsNone(database.get_remote("box"))
            self.assertEqual(database.list_remotes(), [])

    def test_settings_round_trip(self) -> None:
        with TemporaryDirectory() as directory:
            database = StateDatabase(Path(directory) / "scheduler.db")
            database.update_remote_settings(
                RemoteSettings(ssh_path="/usr/bin/ssh", key_path="/k", connect_timeout=5)
            )
            settings = database.get_remote_settings()
            self.assertEqual(settings.ssh_path, "/usr/bin/ssh")
            self.assertEqual(settings.key_path, "/k")
            self.assertEqual(settings.connect_timeout, 5)
            self.assertEqual(settings.host_key_policy, "accept-new")

    def test_workflow_runner_and_usage(self) -> None:
        with TemporaryDirectory() as directory:
            database = StateDatabase(Path(directory) / "scheduler.db")
            row = database.create_workflow("wf", "description: wf\ntasks:\n  a:\n    command: echo a\n")
            key = row["workflow_key"]
            self.assertEqual(row["runner"], "local")

            database.upsert_remote(_remote())
            database.set_workflow_runner(key, "box")
            self.assertEqual(database.get_workflow(key)["runner"], "box")
            self.assertEqual(database.workflows_using_remote("box"), [key])
            self.assertEqual(database.workflows_using_remote("missing"), [])


class RemoteApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        root = Path(self._directory.name)
        self.root = root
        self.database = StateDatabase(root / "scheduler.db")
        store_password(self.database, "tester", "test-password-1234")
        self.app = create_app(
            database_path=root / "scheduler.db",
            logs_path=root / "logs",
            start_scheduler=False,
            allow_insecure_remote_login=True,
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

    def test_list_remotes_includes_detected_defaults(self) -> None:
        payload = self.client.get("/api/remotes").get_json()
        self.assertEqual(payload["remotes"], [])
        self.assertIn("detected_ssh_path", payload["settings"])
        self.assertIn("detected_key_path", payload["settings"])

    def test_upsert_and_list_remote(self) -> None:
        response = self.client.put(
            "/api/remotes/box",
            json={"username": "ops", "host": "10.1.1.1", "port": 2222, "workdir": "/srv"},
        )
        self.assertEqual(response.status_code, 200)
        remotes = self.client.get("/api/remotes").get_json()["remotes"]
        self.assertEqual(len(remotes), 1)
        self.assertEqual(remotes[0]["alias"], "box")
        self.assertEqual(remotes[0]["port"], 2222)
        self.assertEqual(remotes[0]["workdir"], "/srv")
        self.assertFalse(remotes[0]["status"]["reachable"])

    def test_upsert_rejects_bad_alias(self) -> None:
        response = self.client.put(
            "/api/remotes/bad alias",
            json={"username": "ops", "host": "10.1.1.1"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("alias", response.get_json()["field_errors"])

    def test_settings_rejects_missing_ssh_file(self) -> None:
        response = self.client.put(
            "/api/remotes/settings",
            json={"ssh_path": "/definitely/missing/ssh", "connect_timeout": 10},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("ssh_path", response.get_json()["field_errors"])

    def test_test_endpoint_persists_status_for_saved_remote(self) -> None:
        self.client.put(
            "/api/remotes/box",
            json={"username": "ops", "host": "127.0.0.1", "port": 1},
        )
        response = self.client.post(
            "/api/remotes/test",
            json={"alias": "box", "username": "ops", "host": "127.0.0.1", "port": 1},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["reachable"])
        status = self.database.list_remote_status()["box"]
        self.assertEqual(status["reachable"], 0)

    def test_test_endpoint_does_not_persist_unsaved_alias(self) -> None:
        response = self.client.post(
            "/api/remotes/test",
            json={"alias": "ghost", "username": "ops", "host": "127.0.0.1", "port": 1},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.database.list_remote_status(), {})

    def test_test_endpoint_rejects_missing_host(self) -> None:
        response = self.client.post(
            "/api/remotes/test",
            json={"alias": "box", "username": "ops", "host": ""},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("host", response.get_json()["field_errors"])

    def test_delete_blocked_while_workflow_uses_remote(self) -> None:
        self.client.put(
            "/api/remotes/box",
            json={"username": "ops", "host": "10.1.1.1"},
        )
        row = self.database.create_workflow(
            "wf", "description: wf\ntasks:\n  a:\n    command: echo a\n"
        )
        self.database.set_workflow_runner(row["workflow_key"], "box")
        response = self.client.delete("/api/remotes/box")
        self.assertEqual(response.status_code, 400)
        self.assertIn("远程被占用", response.get_json()["error"])

        self.database.set_workflow_runner(row["workflow_key"], "local")
        self.assertEqual(self.client.delete("/api/remotes/box").status_code, 200)

    def test_run_records_runner_at_run_time(self) -> None:
        # Port 1 on localhost refuses immediately so the remote run finishes fast.
        self.client.put(
            "/api/remotes/box",
            json={"username": "ops", "host": "127.0.0.1", "port": 1},
        )
        row = self.database.create_workflow(
            "wf", "description: wf\ntasks:\n  a:\n    command: echo a\n"
        )
        key = row["workflow_key"]
        self.database.set_workflow_runner(key, "box")
        executions = self.app.extensions["execution_service"]
        run_id = executions.start_run(key, trigger_type="manual")
        for _ in range(200):
            if run_id not in executions.active_run_ids():
                break
            time.sleep(0.05)
        runs = self.client.get("/api/runs").get_json()["runs"]
        self.assertEqual(runs[0]["runner"], "box")
        self.database.set_workflow_runner(key, "local")
        runs = self.client.get("/api/runs").get_json()["runs"]
        self.assertEqual(runs[0]["runner"], "box")

    def test_workflow_edit_sets_runner(self) -> None:
        self.client.put(
            "/api/remotes/box",
            json={"username": "ops", "host": "10.1.1.1"},
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
                "runner": "box",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.database.get_workflow(key)["runner"], "box")
        listing = self.client.get("/api/workflows").get_json()["workflows"][0]
        self.assertEqual(listing["runner"], "box")

        response = self.client.put(
            f"/api/workflows/{key}",
            json={
                "definition": "description: wf\ntasks:\n  a:\n    command: echo a\n",
                "name": "wf",
                "runner": "ghost",
            },
        )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
