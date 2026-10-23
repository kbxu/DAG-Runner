from __future__ import annotations

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from dagrunner.auth import client_password_hash, store_password
from dagrunner.database import StateDatabase
from dagrunner.webapp import create_app


class RunScopeApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.database = StateDatabase(root / "scheduler.db")
        store_password(self.database, "tester", "test-password-1234")
        self.row = self.database.create_workflow(
            "Pipeline",
            """
name: Pipeline
tasks:
  first:
    description: first
    command: echo first
  second:
    description: second
    command: echo second
    depends: [first]
  third:
    description: third
    command: echo third
    depends: [second]
""",
        )
        self.app = create_app(
            database_path=self.database.path,
            logs_path=root / "logs",
            start_scheduler=False,
            allow_insecure_remote_login=True,
        )
        self.client = self.app.test_client()
        login = self.client.post(
            "/api/auth/login",
            json={
                "username": "tester",
                "password_hash": client_password_hash("test-password-1234"),
            },
        )
        self.assertEqual(login.status_code, 200)

    def tearDown(self) -> None:
        self.app.extensions["schedule_service"].shutdown()
        self.app.extensions["execution_service"].shutdown()
        self._tmp.cleanup()

    @property
    def workflow_key(self) -> str:
        return self.row["workflow_key"]

    def test_only_task_requires_from_task(self) -> None:
        response = self.client.post(
            f"/api/workflows/{self.workflow_key}/run", json={"only_task": True}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("only_task", response.get_json()["error"])

    def test_unknown_task_is_rejected(self) -> None:
        response = self.client.post(
            f"/api/workflows/{self.workflow_key}/run",
            json={"from_task": "missing"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("任务不存在", response.get_json()["error"])

    def test_only_task_run_is_accepted(self) -> None:
        response = self.client.post(
            f"/api/workflows/{self.workflow_key}/run",
            json={"from_task": "second", "only_task": True},
        )
        self.assertEqual(response.status_code, 202)
        run_id = response.get_json()["run_id"]
        deadline = time.monotonic() + 5
        states = {}
        while time.monotonic() < deadline:
            runs = self.database.list_runs(self.workflow_key, limit=1)
            if runs:
                states = self.database.task_states(run_id)
                if states and states["second"]["status"] != "RUNNING":
                    break
            time.sleep(0.1)
        self.assertEqual(states["second"]["status"], "SUCCESS")
        self.assertEqual(states["first"]["status"], "SKIPPED")
        self.assertEqual(states["third"]["status"], "SKIPPED")

    def test_run_detail_orders_tasks_in_dag_order(self) -> None:
        response = self.client.post(
            f"/api/workflows/{self.workflow_key}/run",
            json={"from_task": "second"},
        )
        self.assertEqual(response.status_code, 202)
        run_id = response.get_json()["run_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.database.list_runs(self.workflow_key, limit=1):
                break
            time.sleep(0.1)
        detail = self.client.get(f"/api/runs/{run_id}").get_json()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            detail = self.client.get(f"/api/runs/{run_id}").get_json()
            if detail["run"]["status"] in {"SUCCESS", "FAILED"}:
                break
            time.sleep(0.1)
        self.assertEqual(
            [task["task_name"] for task in detail["tasks"]],
            ["first", "second", "third"],
        )
        by_name = {task["task_name"]: task for task in detail["tasks"]}
        self.assertEqual(by_name["first"]["skip_kind"], "OUTSIDE_SCOPE")
        self.assertEqual(by_name["first"]["status"], "SKIPPED")
        self.assertIsNone(by_name["second"]["skip_kind"])

    def test_run_stats_counts_tasks_in_window(self) -> None:
        self.client.post(f"/api/workflows/{self.workflow_key}/run", json={})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            runs = self.database.list_runs(self.workflow_key, limit=1)
            if runs and runs[0]["status"] in {"SUCCESS", "FAILED"}:
                break
            time.sleep(0.1)
        stats = self.client.get("/api/runs/stats?hours=48").get_json()
        self.assertEqual(stats["window_hours"], 48)
        self.assertEqual(stats["success"], 3)
        self.assertEqual(stats["running"], 0)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(
            self.client.get("/api/runs/stats?hours=0").status_code, 400
        )

    def test_descendants_run_is_accepted(self) -> None:
        self.client.post(f"/api/workflows/{self.workflow_key}/run", json={})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            runs = self.database.list_runs(self.workflow_key, limit=1)
            if runs and runs[0]["status"] in {"SUCCESS", "FAILED"}:
                break
            time.sleep(0.1)
        response = self.client.post(
            f"/api/workflows/{self.workflow_key}/run",
            json={"from_task": "second"},
        )
        self.assertEqual(response.status_code, 202)
        run_id = response.get_json()["run_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            states = self.database.task_states(run_id)
            if states and states["third"]["status"] not in {"RUNNING", "PENDING"}:
                break
            time.sleep(0.1)
        self.assertEqual(states["second"]["status"], "SUCCESS")
        self.assertEqual(states["third"]["status"], "SUCCESS")
        self.assertEqual(states["first"]["status"], "SKIPPED")
        self.assertTrue(
            all(row["reused_from_run_id"] is None for row in states.values())
        )


if __name__ == "__main__":
    unittest.main()
