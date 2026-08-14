from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from dagrunner.auth import client_password_hash, store_password
from dagrunner.database import StateDatabase
from dagrunner.executor import ExecutionResult
from dagrunner.logger import TaskLogManager
from dagrunner.notifier import (
    EmailConfig,
    EmailNotifier,
    Notifier,
    NullNotifier,
    TaskFailure,
    WorkflowEvent,
    build_email_message,
    load_email_config,
    load_notifier,
)
from dagrunner.runner import WorkflowRunner
from dagrunner.webapp import create_app
from dagrunner.workflow import EmailNotification, Task, Workflow, WorkflowError


class RecordingNotifier(Notifier):
    def __init__(self):
        self.events: list[WorkflowEvent] = []

    def on_workflow_success(self, event: WorkflowEvent) -> None:
        self.events.append(event)

    def on_workflow_failed(self, event: WorkflowEvent) -> None:
        self.events.append(event)


class FixedExecutor:
    def __init__(self, results: dict[str, ExecutionResult]):
        self.results = results

    def execute(self, task, **kwargs) -> ExecutionResult:
        return self.results[task.name]


class EmailNotifierTests(unittest.TestCase):
    def test_loads_yaml_or_json_from_var_directory(self) -> None:
        config = {
            "email": {
                "host": "smtp.example.com",
                "from": "sender@example.com",
                "use_ssl": False,
                "starttls": True,
            }
        }
        with TemporaryDirectory() as directory:
            var_dir = Path(directory)
            (var_dir / "notifier.json").write_text(
                json.dumps(config), encoding="utf-8"
            )
            notifier = load_notifier(var_dir=var_dir)

        self.assertIsInstance(notifier, EmailNotifier)
        self.assertEqual(notifier.config.port, 587)

    def test_missing_config_disables_notifications(self) -> None:
        with TemporaryDirectory() as directory:
            self.assertIsInstance(
                load_notifier(var_dir=directory),
                NullNotifier,
            )

    def test_success_and_failure_templates(self) -> None:
        config = EmailConfig(
            host="smtp.example.com",
            port=465,
            sender="sender@example.com",
        )
        success = build_email_message(
            config,
            WorkflowEvent(
                "daily_report", "run-1", "SUCCESS", ("operator@example.com",)
            ),
        )
        self.assertIn("SUCCESS - daily_report", success["Subject"])
        self.assertIn("Status: SUCCESS", success.get_content())
        self.assertNotIn("Error:", success.get_content())

        failure = build_email_message(
            config,
            WorkflowEvent(
                "daily_report",
                "run-2",
                "FAILED",
                ("operator@example.com",),
                error_message="workflow timed out",
                failed_tasks=(
                    TaskFailure("extract", 124, "task timed out", "extract.log"),
                ),
            ),
        )
        body = failure.get_content()
        self.assertIn("FAILED - daily_report", failure["Subject"])
        self.assertIn("Error: workflow timed out", body)
        self.assertIn("Failed task: extract", body)
        self.assertIn("Error: task timed out", body)

    def test_workflow_sends_one_notification_not_one_per_task(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = StateDatabase(root / "scheduler.db")
            notifier = RecordingNotifier()
            workflow = Workflow(
                name="pipeline",
                tasks={
                    "first": Task(name="first", command="first"),
                    "second": Task(
                        name="second", command="second", depends=("first",)
                    ),
                },
                config_dir=root,
                email_notification=EmailNotification(
                    send_on="always",
                    recipients=("operator@example.com",),
                ),
            )
            executor = FixedExecutor(
                {
                    "first": ExecutionResult(0),
                    "second": ExecutionResult(1, "publish failed"),
                }
            )
            _, status = WorkflowRunner(
                database,
                TaskLogManager(root / "logs"),
                executor=executor,
                notifier=notifier,
            ).run(workflow)

        self.assertEqual(status, "FAILED")
        self.assertEqual(len(notifier.events), 1)
        self.assertEqual(notifier.events[0].status, "FAILED")
        self.assertEqual(notifier.events[0].failed_tasks[0].task_name, "second")
        self.assertEqual(
            notifier.events[0].failed_tasks[0].error_message,
            "publish failed",
        )

    def test_workflow_parses_email_notification_settings(self) -> None:
        workflow = Workflow.from_yaml(
            """
name: report
notification:
  email:
    send_on: failure
    to:
      - one@example.com
      - two@example.com
tasks:
  run:
    command: echo ok
"""
        )
        self.assertEqual(workflow.email_notification.send_on, "failure")
        self.assertEqual(
            workflow.email_notification.recipients,
            ("one@example.com", "two@example.com"),
        )

    def test_enabled_workflow_notification_requires_recipient(self) -> None:
        with self.assertRaisesRegex(WorkflowError, "at least one address"):
            Workflow.from_yaml(
                """
name: report
notification:
  email:
    send_on: always
tasks:
  run:
    command: echo ok
"""
            )

    def test_email_send_on_modes(self) -> None:
        success = EmailNotification("success", ("ops@example.com",))
        failure = EmailNotification("failure", ("ops@example.com",))
        always = EmailNotification("always", ("ops@example.com",))
        disabled = EmailNotification()
        self.assertTrue(success.should_send("SUCCESS"))
        self.assertFalse(success.should_send("FAILED"))
        self.assertFalse(failure.should_send("SUCCESS"))
        self.assertTrue(failure.should_send("FAILED"))
        self.assertTrue(always.should_send("SUCCESS"))
        self.assertTrue(always.should_send("FAILED"))
        self.assertFalse(disabled.should_send("SUCCESS"))

    def test_notification_api_updates_complete_workflow_yaml(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "scheduler.db"
            database = StateDatabase(database_path)
            store_password(database, "tester", "test-password-1234")
            row = database.create_workflow(
                "Report workflow",
                """
description: Report workflow
setup: echo setup
tasks:
  report:
    command: echo report
""",
            )
            app = create_app(
                database_path=database_path,
                logs_path=root / "logs",
                start_scheduler=False,
                allow_insecure_remote_login=True,
            )
            client = app.test_client()
            login = client.post(
                "/api/auth/login",
                json={
                    "username": "tester",
                    "password_hash": client_password_hash("test-password-1234"),
                },
            )
            self.assertEqual(login.status_code, 200)
            try:
                response = client.put(
                    f"/api/workflows/{row['workflow_key']}/notification",
                    json={
                        "send_on": "failure",
                        "to": ["ops@example.com", "owner@example.com"],
                    },
                )
                self.assertEqual(response.status_code, 200)
                stored = database.get_workflow(row["workflow_key"])
                parsed = Workflow.from_yaml(
                    stored["definition"], name_override=row["workflow_key"]
                )
                self.assertEqual(parsed.setup, "echo setup")
                self.assertIn("report", parsed.tasks)
                self.assertEqual(parsed.email_notification.send_on, "failure")

                listing = client.get("/api/workflows").get_json()["workflows"][0]
                self.assertEqual(listing["notification"]["email"]["send_on"], "failure")
                self.assertEqual(
                    listing["notification"]["email"]["to"],
                    ["ops@example.com", "owner@example.com"],
                )
            finally:
                app.extensions["schedule_service"].shutdown()
                app.extensions["execution_service"].shutdown()

    def test_global_email_settings_api_saves_and_reloads_sender(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = StateDatabase(root / "scheduler.db")
            store_password(database, "tester", "test-password-1234")
            app = create_app(
                database_path=database.path,
                logs_path=root / "logs",
                start_scheduler=False,
                allow_insecure_remote_login=True,
            )
            client = app.test_client()
            client.post(
                "/api/auth/login",
                json={
                    "username": "tester",
                    "password_hash": client_password_hash("test-password-1234"),
                },
            )
            try:
                initial = client.get("/api/notifier").get_json()["email"]
                self.assertFalse(initial["configured"])
                response = client.put(
                    "/api/notifier",
                    json={
                        "email": {
                            "host": "smtp.example.com",
                            "port": 587,
                            "username": "sender@example.com",
                            "password": "secret-app-password",
                            "from": "sender@example.com",
                            "security": "starttls",
                            "timeout": 12,
                            "subject_prefix": "[Reports]",
                        }
                    },
                )
                self.assertEqual(response.status_code, 200)
                saved = load_email_config(var_dir=root)
                self.assertEqual(saved.host, "smtp.example.com")
                self.assertEqual(saved.password, "secret-app-password")
                self.assertTrue(saved.starttls)
                self.assertIsInstance(
                    app.extensions["execution_service"].notifier, EmailNotifier
                )

                public = client.get("/api/notifier").get_json()["email"]
                self.assertTrue(public["has_password"])
                self.assertNotIn("password", public)
                public["host"] = "smtp2.example.com"
                public["password"] = ""
                response = client.put("/api/notifier", json={"email": public})
                self.assertEqual(response.status_code, 200)
                updated = load_email_config(var_dir=root)
                self.assertEqual(updated.host, "smtp2.example.com")
                self.assertEqual(updated.password, "secret-app-password")
            finally:
                app.extensions["schedule_service"].shutdown()
                app.extensions["execution_service"].shutdown()


if __name__ == "__main__":
    unittest.main()
