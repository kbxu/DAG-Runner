"""Live MySQL 8 integration tests.

These are skipped unless ``DAGRUNNER_MYSQL_PASSWORD`` is set, so the default
test discovery stays usable without a MySQL server. The tests recreate the
target database, so point them at a dedicated test schema:

    set DAGRUNNER_MYSQL_PASSWORD=...
    python -m unittest tests.test_mysql_integration -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import pymysql

from dagrunner.database import create_state_database
from dagrunner.database_mysql8 import Mysql8StateDatabase
from dagrunner.executor import ExecutionResult
from dagrunner.logger import TaskLogManager
from dagrunner.runner import WorkflowRunner
from dagrunner.satellite_protocol import SatelliteConfig
from dagrunner.workflow import Task, Workflow

MYSQL_CONFIG = {
    "type": "mysql8",
    "host": os.environ.get("DAGRUNNER_MYSQL_HOST", "127.0.0.1"),
    "port": int(os.environ.get("DAGRUNNER_MYSQL_PORT", "3306")),
    "user": os.environ.get("DAGRUNNER_MYSQL_USER", "dagrunner"),
    "password": os.environ.get("DAGRUNNER_MYSQL_PASSWORD", ""),
    "database": os.environ.get("DAGRUNNER_MYSQL_DATABASE", "dagr"),
}

DEFINITION = """
description: mysql integration
tasks:
  first:
    command: echo first
  second:
    command: echo second
    depends:
      - first
"""


class FixedExecutor:
    def __init__(self, results: dict[str, ExecutionResult]):
        self.results = results

    def execute(self, task, **kwargs) -> ExecutionResult:
        return self.results[task.name]


def _reset_database() -> None:
    connection = pymysql.connect(
        host=MYSQL_CONFIG["host"],
        port=MYSQL_CONFIG["port"],
        user=MYSQL_CONFIG["user"],
        password=MYSQL_CONFIG["password"],
        autocommit=True,
        charset="utf8mb4",
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("DROP DATABASE IF EXISTS dagr")
            cursor.execute("CREATE DATABASE dagr CHARACTER SET utf8mb4")
    finally:
        connection.close()


@unittest.skipUnless(
    MYSQL_CONFIG["password"],
    "set DAGRUNNER_MYSQL_PASSWORD to run MySQL integration tests",
)
class MysqlIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_database()
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        config = dict(MYSQL_CONFIG, state_dir=str(self.root / "state"))
        self.database = create_state_database(config)
        self.assertIsInstance(self.database, Mysql8StateDatabase)
        self.assertEqual(self.database.state_dir, self.root / "state")

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_schema_created(self) -> None:
        connection = pymysql.connect(
            host=MYSQL_CONFIG["host"],
            port=MYSQL_CONFIG["port"],
            user=MYSQL_CONFIG["user"],
            password=MYSQL_CONFIG["password"],
            database=MYSQL_CONFIG["database"],
            charset="utf8mb4",
        )
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'dagr' ORDER BY table_name"
                )
                tables = {row[0] for row in cursor.fetchall()}
        finally:
            connection.close()
        expected = {
            "workflows",
            "workflow_runs",
            "task_runs",
            "users",
            "auth_sessions",
            "login_attempts",
            "schedules",
            "satellites",
            "satellite_jobs",
        }
        self.assertEqual(tables, expected)

    def test_workflow_crud(self) -> None:
        row = self.database.create_workflow("mysql flow", DEFINITION, "wf_mysql_1")
        self.assertEqual(row["workflow_key"], "wf_mysql_1")
        self.assertEqual(row["runner"], "local")
        self.assertEqual(
            [item["workflow_key"] for item in self.database.list_workflows()],
            ["wf_mysql_1"],
        )
        fetched = self.database.get_workflow("wf_mysql_1")
        self.assertEqual(fetched["name"], "mysql flow")
        self.assertEqual(fetched["definition"], DEFINITION)
        self.assertTrue(self.database.next_workflow_key().startswith("workflow_"))
        self.database.update_workflow("wf_mysql_1", "renamed", "definition: x")
        self.assertEqual(self.database.get_workflow("wf_mysql_1")["name"], "renamed")
        with self.assertRaises(ValueError):
            self.database.create_workflow("dup", "x", "wf_mysql_1")

    def test_auth_and_login_attempts(self) -> None:
        self.database.upsert_user("alice", "hash-a")
        user = self.database.get_user("alice")
        self.assertEqual(user["username"], "alice")
        self.assertEqual({key: user[key] for key in user.keys()}["password_hash"], "hash-a")

        self.database.create_auth_session("token-1", "alice", "2999-01-01T00:00:00+08:00")
        session = self.database.get_auth_session("token-1")
        self.assertEqual(session["username"], "alice")
        self.database.delete_auth_session("token-1")
        self.assertIsNone(self.database.get_auth_session("token-1"))

        now = "2026-01-01T00:00:00+08:00"
        attempts, block = self.database.record_login_failure(
            "10.0.0.1", now=now, blocked_until="", block_after=3
        )
        self.assertEqual((attempts, block), (1, None))
        attempts, block = self.database.record_login_failure(
            "10.0.0.1", now=now, blocked_until="2026-01-01T01:00:00+08:00", block_after=3
        )
        self.assertEqual((attempts, block), (2, None))
        attempts, block = self.database.record_login_failure(
            "10.0.0.1", now=now, blocked_until="2026-01-01T01:00:00+08:00", block_after=3
        )
        self.assertEqual((attempts, block), (3, "2026-01-01T01:00:00+08:00"))
        self.assertEqual(self.database.get_login_attempt("10.0.0.1")["failed_attempts"], 3)
        self.database.clear_login_failures("10.0.0.1")
        self.assertIsNone(self.database.get_login_attempt("10.0.0.1"))

    def test_schedules(self) -> None:
        self.database.upsert_schedule("wf_mysql_1", "desc", "0 9 * * *", "Asia/Shanghai", True)
        schedule = self.database.get_schedule("wf_mysql_1")
        self.assertEqual(schedule["cron_expression"], "0 9 * * *")
        self.assertEqual(schedule["enabled"], 1)
        self.database.update_schedule_description("wf_mysql_1", "new desc")
        self.assertEqual(self.database.get_schedule("wf_mysql_1")["description"], "new desc")
        self.assertEqual(len(self.database.list_schedules()), 1)
        self.database.delete_schedule("wf_mysql_1")
        self.assertIsNone(self.database.get_schedule("wf_mysql_1"))

    def test_runs_queries_and_progress_counts(self) -> None:
        self.database.create_workflow("mysql flow", DEFINITION, "wf_mysql_1")
        self.assertEqual(self.database.latest_run_times(), {})
        run_id = "run-mysql-1"
        self.database.create_run(
            run_id,
            "wf_mysql_1",
            ["first", "second"],
            task_metadata={
                "first": {"description": "First", "depends": [], "task_type": "command"},
                "second": {"description": "Second", "depends": ["first"], "task_type": "command"},
            },
            scope=["first", "second"],
            runner="local",
        )
        states = self.database.task_states(run_id)
        self.assertEqual(set(states), {"first", "second"})
        self.assertEqual(states["first"]["status"], "PENDING")
        self.assertEqual(states["first"]["snapshot_version"], 2)

        self.database.set_task_status(run_id, "first", "RUNNING")
        self.database.set_task_status(run_id, "first", "SUCCESS", exit_code=0)
        self.database.set_task_status(run_id, "second", "RUNNING")
        self.database.set_task_status(
            run_id, "second", "FAILED", exit_code=1, error_message="boom"
        )
        self.database.finish_run(run_id, "FAILED", "boom")

        self.assertEqual(self.database.get_task(run_id, "first")["status"], "SUCCESS")
        self.assertEqual(self.database.get_run(run_id)["status"], "FAILED")
        self.assertEqual(self.database.latest_run("wf_mysql_1")["run_id"], run_id)
        self.assertEqual(
            self.database.latest_run_times()["wf_mysql_1"],
            self.database.get_run(run_id)["start_time"],
        )

        rows = self.database.list_all_runs(limit=10)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["task_count"], 2)
        self.assertEqual(row["success_count"], 1)
        self.assertEqual(row["failed_count"], 1)
        self.assertEqual(row["handled_count"], 0)
        self.assertEqual(row["running_count"], 0)
        self.assertEqual(row["skipped_count"], 0)
        for name in ("task_count", "success_count", "failed_count", "handled_count"):
            self.assertIsInstance(row[name], int)

        listed = self.database.list_runs("wf_mysql_1")
        self.assertEqual([item["run_id"] for item in listed], [run_id])

        found, total, page = self.database.search_runs(workflow_query="mysql")
        self.assertEqual((total, page), (1, 1))
        self.assertEqual(found[0]["workflow_description"], "mysql flow")
        self.assertEqual(found[0]["task_count"], 2)
        found, total, _ = self.database.search_runs(workflow_query="wf_mysql_1")
        self.assertEqual(total, 1)
        found, total, _ = self.database.search_runs(status="FAILED")
        self.assertEqual(total, 1)
        found, total, _ = self.database.search_runs(tag="other")
        self.assertEqual(total, 1)

        counts = self.database.task_status_counts("2000-01-01T00:00:00+08:00")
        self.assertEqual(counts.get("SUCCESS"), 1)
        self.assertEqual(counts.get("FAILED"), 1)

        self.database.delete_run(run_id)
        self.assertIsNone(self.database.get_run(run_id))

    def test_mark_orphaned(self) -> None:
        self.database.create_workflow("mysql flow", DEFINITION, "wf_mysql_1")
        self.database.create_run("run-orphan", "wf_mysql_1", ["first", "second"])
        self.database.set_task_status("run-orphan", "first", "RUNNING")
        self.assertIn("wf_mysql_1", self.database.running_workflow_names())
        self.assertEqual(self.database.mark_orphaned("wf_mysql_1"), 1)
        states = self.database.task_states("run-orphan")
        self.assertEqual(states["first"]["status"], "FAILED")
        self.assertEqual(states["second"]["status"], "SKIPPED")
        self.assertEqual(self.database.get_run("run-orphan")["status"], "FAILED")
        self.assertEqual(self.database.running_workflow_names(), [])

    def test_satellites_and_jobs(self) -> None:
        self.database.upsert_satellite(
            SatelliteConfig(
                alias="sat-1",
                name="Sat One",
                mode="auto",
                url="https://sat.example",
                token="secret",
            )
        )
        configs = self.database.list_satellites()
        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0].alias, "sat-1")
        self.assertEqual(configs[0].status, "offline")
        self.assertEqual(self.database.get_satellite("sat-1").url, "https://sat.example")
        self.assertTrue(self.database.is_satellite("sat-1"))
        self.assertFalse(self.database.is_satellite("missing"))

        self.database.set_satellite_status(
            "sat-1", "online", platform="linux", hostname="host1", agent_version="0.1.0"
        )
        self.assertEqual(self.database.get_satellite("sat-1").status, "online")
        self.database.remember_satellite_fingerprint("sat-1", "AA:BB")
        self.assertEqual(self.database.get_satellite("sat-1").cert_fingerprint, "AA:BB")

        self.database.create_satellite_job(
            "job-1", "sat-1", "wf_mysql_1", "run-1", "first", "var/logs/job-1.log"
        )
        self.assertEqual(self.database.get_satellite_job("job-1")["status"], "pending")
        self.database.update_satellite_job(
            "job-1", status="running", started_time="2026-01-01T00:00:00+08:00"
        )
        self.database.update_satellite_job("job-1", status="success", exit_code=0)
        job = self.database.get_satellite_job("job-1")
        self.assertEqual(job["status"], "success")
        self.assertIsNotNone(job["finished_time"])
        self.assertEqual(len(self.database.list_satellite_jobs("sat-1")), 1)
        self.database.delete_satellite("sat-1")
        self.assertIsNone(self.database.get_satellite("sat-1"))

    def test_end_to_end_runner(self) -> None:
        self.database.create_workflow("pipeline", DEFINITION, "wf_pipe")
        workflow = Workflow(
            name="wf_pipe",
            tasks={
                "first": Task(name="first", command="first"),
                "second": Task(name="second", command="second", depends=("first",)),
            },
            config_dir=self.root,
        )
        runner = WorkflowRunner(
            self.database,
            TaskLogManager(self.root / "logs"),
            executor=FixedExecutor(
                {"first": ExecutionResult(0), "second": ExecutionResult(0)}
            ),
        )
        run_id, status = runner.run(workflow)
        self.assertEqual(status, "SUCCESS")
        states = self.database.task_states(run_id)
        self.assertEqual(states["first"]["status"], "SUCCESS")
        self.assertEqual(states["second"]["status"], "SUCCESS")
        self.assertEqual(self.database.get_run(run_id)["status"], "SUCCESS")

        run_id2, status2 = runner.run(workflow)
        self.assertEqual(status2, "SUCCESS")

        self.database.delete_workflow("wf_pipe")
        self.assertIsNone(self.database.get_workflow("wf_pipe"))


if __name__ == "__main__":
    unittest.main()
