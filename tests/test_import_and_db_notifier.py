from __future__ import annotations

import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml

from dagrunner.database import StateDatabase
from dagrunner.notifier import (
    DatabaseNotifier,
    FeishuConfig,
    FeishuNotifier,
    WorkflowEvent,
    validate_channel_config,
)
from dagrunner.notifier.feishu import build_feishu_sign
from dagrunner.workflow import Workflow, regenerate_import_definition


class ImportRegenerateTests(unittest.TestCase):
    def test_regenerates_all_task_ids_and_strips_unused_keys(self) -> None:
        raw = """name: Imported Demo
migration:
  source: dolphinscheduler
  extra: 123
notification:
  email:
    send_on: failure
    to: [old@example.com]
unknown_top: junk
tasks:
  legacy_a:
    description: Step A
    command: echo a
    unknown_task_key: drop_me
  legacy_cond:
    description: Check A
    type: condition
    depends: [legacy_a]
    condition:
      relation: AND
      groups:
        - relation: AND
          items:
            - task: legacy_a
              status: SUCCESS
    success: [legacy_ok]
    failure: [legacy_fail]
  legacy_ok:
    description: OK
    command: echo ok
    depends: [legacy_cond]
  legacy_fail:
    description: Fail
    command: echo fail
    depends: [legacy_cond]
"""
        cleaned, replacements = regenerate_import_definition(raw)
        self.assertEqual(len(replacements), 4)
        for old_id, new_id in replacements.items():
            self.assertRegex(new_id, r"^task_[0-9a-f]{8}$")
            self.assertNotEqual(old_id, new_id)

        parsed = yaml.safe_load(cleaned)
        self.assertNotIn("migration", parsed)
        self.assertNotIn("notification", parsed)
        self.assertNotIn("unknown_top", parsed)
        new_a = replacements["legacy_a"]
        new_cond = replacements["legacy_cond"]
        new_ok = replacements["legacy_ok"]
        self.assertNotIn("unknown_task_key", parsed["tasks"][new_a])
        self.assertEqual(parsed["tasks"][new_cond]["depends"], [new_a])
        self.assertEqual(
            parsed["tasks"][new_cond]["condition"]["groups"][0]["items"][0]["task"],
            new_a,
        )
        self.assertEqual(parsed["tasks"][new_ok]["depends"], [new_cond])
        # Resulting YAML must be a completely valid workflow.
        wf = Workflow.from_yaml(cleaned, name_override="workflow_1234567890ab")
        self.assertEqual(set(wf.tasks.keys()), set(replacements.values()))


class DatabaseNotifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.db = StateDatabase(Path(self._tmp.name) / "scheduler.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_channel_and_workflow_rule_crud_and_dispatch(self) -> None:
        email_cfg = validate_channel_config(
            "email",
            {
                "host": "smtp.example.com",
                "port": 465,
                "from": "bot@example.com",
                "security": "ssl",
            },
        )
        feishu_cfg = validate_channel_config(
            "feishu",
            {
                "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/test",
                "secret": "sec123",
                "mention_mobiles": ["13800138000"],
            },
        )
        ch_email = self.db.upsert_notification_channel(
            None, name="运维邮箱", channel_type="email", enabled=True, config=email_cfg
        )
        ch_feishu = self.db.upsert_notification_channel(
            None, name="告警飞书", channel_type="feishu", enabled=True, config=feishu_cfg
        )
        self.assertEqual(len(self.db.list_notification_channels()), 2)

        self.db.replace_workflow_notifications(
            "wf_demo",
            [
                {
                    "channel_id": ch_email["id"],
                    "send_on": "failure",
                    "targets": ["ops@example.com"],
                    "enabled": True,
                },
                {
                    "channel_id": ch_feishu["id"],
                    "send_on": "always",
                    "targets": [],
                    "enabled": True,
                },
            ],
        )
        self.assertEqual(self.db.all_workflow_notification_counts().get("wf_demo"), 2)

        notifier = DatabaseNotifier(self.db)
        event = WorkflowEvent(
            workflow_name="wf_demo",
            run_id="run_1",
            status="FAILED",
            recipients=(),
            error_message="boom",
        )
        with (
            patch("dagrunner.notifier.config.EmailNotifier.on_workflow_failed") as mock_email,
            patch("dagrunner.notifier.config.FeishuNotifier.on_workflow_failed") as mock_feishu,
        ):
            notifier.on_workflow_failed(event)
            self.assertEqual(mock_email.call_count, 1)
            self.assertEqual(mock_email.call_args[0][0].recipients, ("ops@example.com",))
            self.assertEqual(mock_feishu.call_count, 1)

    def test_feishu_signature_non_empty(self) -> None:
        sig = build_feishu_sign("1700000000", "my-secret")
        self.assertTrue(len(sig) > 10)
        fs = FeishuNotifier(FeishuConfig(webhook_url="https://example.com/hook", secret="my-secret"))
        self.assertEqual(fs.config.secret, "my-secret")


if __name__ == "__main__":
    unittest.main()
