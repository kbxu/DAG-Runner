from __future__ import annotations

import json
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from xml.etree import ElementTree

from dagrunner.auth import client_password_hash, store_password
from dagrunner.database import StateDatabase
from dagrunner.migrate_workflows import convert_windows_task_scheduler_definition
from dagrunner.service import (
    ScheduleService,
    WorkflowRegistry,
    decode_cron_expressions,
    encode_cron_expressions,
)
from dagrunner.server import main as server_main
from dagrunner.workflow import Workflow, WorkflowError
from dagrunner.webapp import _prepare_import_preview, create_app


WINDOWS_TASK_WITH_TWO_TRIGGERS = """\
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><URI>\\Reports\\DailyReport</URI></RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-08-04T09:00:00</StartBoundary>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
    <CalendarTrigger>
      <StartBoundary>2026-08-04T18:30:00</StartBoundary>
      <ScheduleByWeek>
        <WeeksInterval>1</WeeksInterval>
        <DaysOfWeek><Monday/><Friday/></DaysOfWeek>
      </ScheduleByWeek>
    </CalendarTrigger>
  </Triggers>
  <Actions Context="Author">
    <Exec><Command>C:\\Tools\\report.exe</Command></Exec>
  </Actions>
</Task>
"""

DOLPHINSCHEDULER_EXPORT = {
    "processDefinition": {
        "code": 12345,
        "name": "daily report",
        "globalParamList": [],
    },
    "processTaskRelationList": [],
    "taskDefinitionList": [
        {
            "code": 12346,
            "name": "report",
            "taskType": "SHELL",
            "flag": "YES",
            "taskParams": {"rawScript": "echo report"},
        }
    ],
    "schedule": {
        "crontab": "0 0 9 * * ?",
        "timezoneId": "Asia/Shanghai",
        "releaseState": "ONLINE",
    },
}


class MultipleScheduleTests(unittest.TestCase):
    def test_web_default_language_and_server_option(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_app(
                database_path=Path(directory) / "state.db",
                logs_path=Path(directory) / "logs",
                start_scheduler=False,
                language="en",
            )
            response = app.test_client().get("/login")
            page = response.get_data(as_text=True)
            self.assertIn('<html lang="en">', page)
            self.assertIn('data-default-language="en"', page)

        with patch("dagrunner.server.create_app") as create, patch(
            "dagrunner.server.serve"
        ) as serve:
            self.assertEqual(server_main(["--language", "en"]), 0)
            self.assertEqual(create.call_args.kwargs["language"], "en")
            serve.assert_called_once()

    def test_invalid_web_language_is_rejected(self) -> None:
        with TemporaryDirectory() as directory, self.assertRaises(ValueError):
            create_app(
                database_path=Path(directory) / "state.db",
                logs_path=Path(directory) / "logs",
                start_scheduler=False,
                language="fr",
            )

    def test_import_preview_detects_and_converts_dolphinscheduler(self) -> None:
        preview = _prepare_import_preview(
            "daily-report.json",
            json.dumps(DOLPHINSCHEDULER_EXPORT),
        )

        self.assertEqual(preview["source"], "dolphinscheduler")
        self.assertEqual(preview["source_label"], "DolphinScheduler")
        self.assertEqual(preview["filename"], "dagr_daily-report.yaml")
        workflow = Workflow.from_yaml(preview["definition"])
        self.assertEqual(workflow.name, "ds_12345")
        self.assertEqual(workflow.schedule.crons, ("0 9 * * *",))

    def test_import_preview_detects_and_converts_windows_task(self) -> None:
        preview = _prepare_import_preview(
            "daily-report.xml",
            WINDOWS_TASK_WITH_TWO_TRIGGERS,
        )

        self.assertEqual(preview["source"], "windows-task-scheduler")
        self.assertEqual(preview["source_label"], "Windows Task Scheduler")
        workflow = Workflow.from_yaml(preview["definition"])
        self.assertEqual(workflow.schedule.crons, ("0 9 * * *", "30 18 * * mon,fri"))

    def test_import_preview_keeps_dag_runner_yaml(self) -> None:
        definition = """
description: report
tasks:
  report:
    command: echo report
"""
        preview = _prepare_import_preview("report.yaml", definition)

        self.assertEqual(preview["source"], "dag-runner")
        self.assertEqual(preview["definition"], definition)

    def test_schedule_yaml_accepts_multiple_crons_and_deduplicates(self) -> None:
        workflow = Workflow.from_yaml(
            """
description: report
schedule:
  crons:
    - 0 9 * * *
    - 30 18 * * mon-fri
    - 0 9 * * *
  timezone: Asia/Shanghai
tasks:
  report:
    command: echo report
"""
        )

        self.assertEqual(workflow.schedule.crons, ("0 9 * * *", "30 18 * * mon-fri"))
        self.assertEqual(workflow.schedule.cron, "0 9 * * *")

    def test_schedule_yaml_remains_compatible_with_single_cron(self) -> None:
        workflow = Workflow.from_yaml(
            """
schedule:
  cron: 0 9 * * *
tasks:
  report:
    command: echo report
"""
        )
        self.assertEqual(workflow.schedule.crons, ("0 9 * * *",))

    def test_schedule_yaml_requires_at_least_one_cron(self) -> None:
        with self.assertRaises(WorkflowError):
            Workflow.from_yaml(
                """
schedule:
  crons: []
tasks:
  report:
    command: echo report
"""
            )

    def test_database_text_format_supports_new_and_legacy_values(self) -> None:
        encoded = encode_cron_expressions(("0 9 * * *", "30 18 * * mon-fri"))
        self.assertEqual(
            decode_cron_expressions(encoded),
            ("0 9 * * *", "30 18 * * mon-fri"),
        )
        self.assertEqual(decode_cron_expressions("0 9 * * *"), ("0 9 * * *",))

    def test_windows_triggers_stay_in_one_converted_workflow(self) -> None:
        configs, warnings = convert_windows_task_scheduler_definition(
            ElementTree.fromstring(WINDOWS_TASK_WITH_TWO_TRIGGERS),
            "DailyReport",
            timezone_name="Asia/Shanghai",
        )

        self.assertEqual(warnings, [])
        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0]["name"], "wts_DailyReport")
        self.assertEqual(
            configs[0]["schedule"],
            {
                "crons": ["0 9 * * *", "30 18 * * mon,fri"],
                "timezone": "Asia/Shanghai",
                "enabled": False,
            },
        )

    def test_schedule_service_installs_one_job_per_cron(self) -> None:
        with TemporaryDirectory() as directory:
            database = StateDatabase(Path(directory) / "state.db")
            row = database.create_workflow(
                "report",
                """
description: report
tasks:
  report:
    command: echo report
""",
            )
            registry = WorkflowRegistry(database)
            service = ScheduleService(database, registry, object())
            service.start()
            try:
                service.update(
                    row["workflow_key"],
                    ["0 9 * * *", "30 18 * * mon-fri"],
                    "Asia/Shanghai",
                    True,
                )

                self.assertEqual(len(service.scheduler.get_jobs()), 2)
                stored = database.get_schedule(row["workflow_key"])
                self.assertEqual(
                    decode_cron_expressions(stored["cron_expression"]),
                    ("0 9 * * *", "30 18 * * mon-fri"),
                )
                entries = service.schedule_entries(row["workflow_key"])
                self.assertEqual(
                    [entry["cron"] for entry in entries],
                    list(decode_cron_expressions(stored["cron_expression"])),
                )
                self.assertTrue(all(entry["next_run_time"] for entry in entries))
                self.assertIsNotNone(service.next_run_time(row["workflow_key"]))
            finally:
                service.shutdown()

    def test_workflow_api_selects_cron_with_nearest_next_run(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "state.db"
            logs_path = Path(directory) / "logs"
            database = StateDatabase(database_path)
            store_password(database, "tester", "test-password-1234")
            row = database.create_workflow(
                "report",
                """
description: report
tasks:
  report:
    command: echo report
""",
            )
            app = create_app(
                database_path=database_path,
                logs_path=logs_path,
                start_scheduler=False,
                allow_insecure_remote_login=True,
            )
            registry = app.extensions["workflow_registry"]
            schedules = app.extensions["schedule_service"]
            registry.refresh()
            schedules.scheduler.start()
            try:
                schedules.update(
                    row["workflow_key"],
                    ["0 0 1 1 *", "* * * * *"],
                    "Asia/Shanghai",
                    True,
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

                preview_response = client.post(
                    "/api/workflows/import-preview",
                    data={
                        "file": (
                            BytesIO(json.dumps(DOLPHINSCHEDULER_EXPORT).encode()),
                            "daily-report.json",
                        )
                    },
                    content_type="multipart/form-data",
                )
                self.assertEqual(preview_response.status_code, 200)
                self.assertEqual(
                    preview_response.get_json()["source"],
                    "dolphinscheduler",
                )
                invalid_preview = client.post(
                    "/api/workflows/import-preview",
                    data={"file": (BytesIO(b""), "empty.yaml")},
                    content_type="multipart/form-data",
                    headers={"X-DAGRunner-Language": "en"},
                )
                self.assertEqual(invalid_preview.status_code, 400)
                self.assertEqual(
                    invalid_preview.get_json()["error"],
                    "The workflow file cannot be empty",
                )

                response = client.get("/api/workflows")
                self.assertEqual(response.status_code, 200)
                schedule = response.get_json()["workflows"][0]["schedule"]
                self.assertEqual(schedule["cron"], "* * * * *")
                self.assertEqual(schedule["crons"], ["0 0 1 1 *", "* * * * *"])
                self.assertEqual(len(schedule["entries"]), 2)
            finally:
                schedules.shutdown()


if __name__ == "__main__":
    unittest.main()
