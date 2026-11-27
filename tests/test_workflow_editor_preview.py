from __future__ import annotations

import unittest

from dagrunner.tools.workflow_editor import (
    draft_preview_definition,
    merge_model,
    preview_definition,
)
from dagrunner.workflow import WorkflowError


BASE_YAML = """name: demo
tasks:
  t_a:
    description: 上游任务
    type: command
    command: echo a
    depends: []
  t_cond:
    description: 条件判断
    type: condition
    depends:
      - t_a
    condition:
      relation: AND
      groups:
        - relation: AND
          items:
            - task: t_a
              status: SUCCESS
    success:
      - t_ok
    failure:
      - t_fail
  t_ok:
    description: 成立分支
    type: command
    command: echo ok
    depends:
      - t_cond
  t_fail:
    description: 不成立分支
    type: command
    command: echo fail
    depends:
      - t_cond
"""


class WorkflowEditorDraftPreviewTests(unittest.TestCase):
    def test_condition_without_depends_keeps_draft_nodes(self) -> None:
        model = {
            "setup": "",
            "runner": "local",
            "tasks": [
                {
                    "name": "t_a",
                    "description": "上游任务",
                    "type": "command",
                    "enabled": True,
                    "depends": [],
                    "command": "echo a",
                    "conditionRelation": "AND",
                    "success": [],
                    "failure": [],
                },
                {
                    "name": "t_cond",
                    "description": "条件判断",
                    "type": "condition",
                    "enabled": True,
                    "depends": [],
                    "command": "",
                    "conditionRelation": "AND",
                    "success": ["t_ok"],
                    "failure": ["t_fail"],
                },
                {
                    "name": "t_ok",
                    "description": "成立分支",
                    "type": "command",
                    "enabled": True,
                    "depends": ["t_cond"],
                    "command": "echo ok",
                    "conditionRelation": "AND",
                    "success": [],
                    "failure": [],
                },
                {
                    "name": "t_fail",
                    "description": "不成立分支",
                    "type": "command",
                    "enabled": True,
                    "depends": ["t_cond"],
                    "command": "echo fail",
                    "conditionRelation": "AND",
                    "success": [],
                    "failure": [],
                },
            ],
        }
        merged = merge_model(BASE_YAML, model, validate=False)
        with self.assertRaises(WorkflowError):
            preview_definition(merged)

        draft = draft_preview_definition(merged)
        self.assertEqual([t["name"] for t in draft["tasks"]], ["t_a", "t_cond", "t_ok", "t_fail"])
        cond = next(t for t in draft["tasks"] if t["name"] == "t_cond")
        self.assertEqual(cond["depends"], [])
        self.assertEqual(cond["success"], ["t_ok"])
        self.assertEqual(cond["failure"], ["t_fail"])

    def test_empty_command_keeps_draft_nodes(self) -> None:
        model = {
            "setup": "",
            "tasks": [
                {
                    "name": "t_a",
                    "description": "上游任务",
                    "type": "command",
                    "enabled": True,
                    "depends": [],
                    "command": "",
                    "conditionRelation": "AND",
                    "success": [],
                    "failure": [],
                },
            ],
        }
        merged = merge_model(BASE_YAML, model, validate=False)
        with self.assertRaises(WorkflowError):
            preview_definition(merged)
        draft = draft_preview_definition(merged)
        self.assertEqual(len(draft["tasks"]), 1)
        self.assertEqual(draft["tasks"][0]["name"], "t_a")


if __name__ == "__main__":
    unittest.main()
