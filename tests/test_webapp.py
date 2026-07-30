from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from dagrunner.webapp import create_app


WORKFLOW_YAML = """
name: web_test
description: API-only test workflow
workdir: .
schedule:
  cron: "0 9 * * mon-fri"
  timezone: Asia/Shanghai
  enabled: false
tasks:
  only_task:
    command: [python, never_executed.py]
    depends: []
"""


class WebAppTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        config = self.root / "config"
        config.mkdir()
        self.workflow_path = config / "web_test.yaml"
        self.workflow_path.write_text(WORKFLOW_YAML, encoding="utf-8")
        self.app = create_app(
            config_dir=config,
            database_path=self.root / "state.db",
            logs_path=self.root / "logs",
            start_scheduler=False,
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.app.extensions["schedule_service"].shutdown()
        self.app.extensions["execution_service"].shutdown()
        self.temporary.cleanup()

    def test_dashboard_and_workflow_api(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'id="serviceStatus"', page.data)
        self.assertIn(b'checking', page.data)
        response = self.client.get("/api/workflows")
        self.assertEqual(response.status_code, 200)
        workflow = response.get_json()["workflows"][0]
        self.assertEqual(workflow["name"], "web_test")
        self.assertEqual(workflow["tasks"][0]["name"], "only_task")

    def test_workflow_yaml_can_be_exported(self):
        response = self.client.get("/api/workflows/web_test/yaml")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertIn("web_test.yaml", response.headers["Content-Disposition"])
        self.assertEqual(response.data, self.workflow_path.read_bytes())
        response.close()

    def test_schedule_update_is_persisted(self):
        response = self.client.put(
            "/api/workflows/web_test/schedule",
            json={"cron": "15 8 * * mon-fri", "timezone": "Asia/Shanghai", "enabled": False},
        )
        self.assertEqual(response.status_code, 200)
        database = self.app.extensions["state_database"]
        row = database.get_schedule("web_test")
        self.assertEqual(row["cron_expression"], "15 8 * * mon-fri")
        self.assertEqual(row["enabled"], 0)

        invalid = self.client.put(
            "/api/workflows/web_test/schedule",
            json={"cron": "not a cron", "timezone": "Asia/Shanghai", "enabled": True},
        )
        self.assertEqual(invalid.status_code, 400)

    def test_runs_api_returns_progress_counts(self):
        database = self.app.extensions["state_database"]
        database.create_run("run_1", "web_test", ["only_task"], trigger_type="manual")
        database.set_task_status("run_1", "only_task", "RUNNING")
        database.set_task_status("run_1", "only_task", "SUCCESS", exit_code=0)
        database.finish_run("run_1", "SUCCESS")

        response = self.client.get("/api/runs")
        self.assertEqual(response.status_code, 200)
        run = response.get_json()["runs"][0]
        self.assertEqual(run["run_id"], "run_1")
        self.assertEqual(run["success_count"], 1)
        self.assertEqual(run["task_count"], 1)

    def test_run_api_submits_without_executing_command(self):
        executions = self.app.extensions["execution_service"]
        with patch.object(executions, "start_run", return_value="mock_run_id") as start:
            response = self.client.post("/api/workflows/web_test/run", json={})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["run_id"], "mock_run_id")
        start.assert_called_once_with("web_test", trigger_type="manual")


if __name__ == "__main__":
    unittest.main()
