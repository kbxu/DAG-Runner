from __future__ import annotations

import unittest

from dagrunner.service import ServiceError
from dagrunner.webapp import _prepare_import_preview


DAG_RUNNER_YAML = """name: demo
description: demo pipeline
tasks:
  task_1:
    description: step one
    command: |-
      cd /srv
      python step.py
    depends: []
"""


class ImportPreviewTests(unittest.TestCase):
    def test_dag_runner_yaml_is_detected(self) -> None:
        preview = _prepare_import_preview("workflow_demo.yaml", DAG_RUNNER_YAML)
        self.assertEqual(preview["source"], "dag-runner")
        self.assertEqual(preview["source_label"], "DAG Runner YAML")
        self.assertEqual(preview["definition"], DAG_RUNNER_YAML)
        self.assertEqual(preview["filename"], "workflow_demo.yaml")

    def test_dag_runner_yaml_with_schedule_and_notification(self) -> None:
        content = (
            DAG_RUNNER_YAML
            + "schedule:\n  crons: ['0 3 * * *']\n  timezone: Asia/Shanghai\n  enabled: false\n"
            + "notification:\n  email:\n    send_on: failure\n    to: [ops@example.com]\n"
        )
        preview = _prepare_import_preview("rich.yaml", content)
        self.assertEqual(preview["source"], "dag-runner")
        self.assertEqual(preview["warnings"], [])

    def test_empty_file_is_rejected(self) -> None:
        with self.assertRaises(ServiceError):
            _prepare_import_preview("empty.yaml", "   \n")

    def test_unknown_text_is_rejected(self) -> None:
        with self.assertRaises(ServiceError):
            _prepare_import_preview("x.yaml", "just some text")


if __name__ == "__main__":
    unittest.main()
