import unittest

import yaml

from dagrunner.migrate_workflows import dump_workflow_yaml
from dagrunner.workflow import Workflow, migrate_legacy_env, uniquify_task_ids


def workflow_yaml(command: str) -> str:
    return dump_workflow_yaml(
        {
            "name": "demo",
            "tasks": {
                "task_1": {"description": "demo", "command": command, "depends": []}
            },
        }
    )


class LiteralBlockDumpTests(unittest.TestCase):
    def test_multiline_command_keeps_literal_block(self):
        dumped = workflow_yaml("cd aaa\npython bbb")
        self.assertIn("command: |-", dumped)
        self.assertNotIn('"cd aaa', dumped)

    def test_trailing_whitespace_keeps_literal_block(self):
        dumped = workflow_yaml("cd aaa \npython bbb\t")
        self.assertIn("command: |-", dumped)
        self.assertNotIn('"cd aaa', dumped)

    def test_trailing_whitespace_is_stripped(self):
        dumped = workflow_yaml("cd aaa \npython bbb\t")
        workflow = Workflow.from_yaml(dumped, fallback_name="demo")
        self.assertEqual(workflow.tasks["task_1"].command, "cd aaa\npython bbb")

    def test_mid_line_tab_keeps_literal_block(self):
        dumped = workflow_yaml("cd aaa\npython\tbbb")
        self.assertIn("command: |-", dumped)
        workflow = Workflow.from_yaml(dumped, fallback_name="demo")
        self.assertEqual(workflow.tasks["task_1"].command, "cd aaa\npython\tbbb")

    def test_single_line_command_is_unchanged(self):
        self.assertIn("command: cd aaa", workflow_yaml("cd aaa"))

    def test_crlf_is_normalized_inside_literal_block(self):
        dumped = workflow_yaml("cd aaa\r\npython bbb")
        self.assertIn("command: |-", dumped)
        workflow = Workflow.from_yaml(dumped, fallback_name="demo")
        self.assertEqual(workflow.tasks["task_1"].command, "cd aaa\npython bbb")

    def test_collision_rewrite_keeps_literal_block(self):
        dumped = workflow_yaml("cd aaa \npython bbb")
        rewritten, replacements = uniquify_task_ids(dumped, {"task_1"})
        self.assertTrue(replacements)
        self.assertIn("command: |-", rewritten)
        workflow = Workflow.from_yaml(rewritten, fallback_name="demo")
        self.assertEqual(
            list(workflow.tasks.values())[0].command, "cd aaa\npython bbb"
        )


class LegacyEnvMigrationTests(unittest.TestCase):
    def test_empty_env_keeps_literal_block(self):
        content = (
            "name: demo\n"
            "tasks:\n"
            "  task_1:\n"
            "    description: demo\n"
            "    command: |-\n"
            "      cd aaa \n"
            "      python bbb\n"
            "    depends: []\n"
            "env: {}\n"
        )
        migrated, changed = migrate_legacy_env(content)
        self.assertTrue(changed)
        self.assertIn("command: |-", migrated)
        self.assertNotIn('"cd aaa', migrated)
        workflow = Workflow.from_yaml(migrated, fallback_name="demo")
        self.assertEqual(workflow.tasks["task_1"].command, "cd aaa\npython bbb")

    def test_env_values_are_promoted_into_setup(self):
        content = (
            "name: demo\n"
            "tasks:\n"
            "  task_1:\n"
            "    description: demo\n"
            "    command: |-\n"
            "      cd aaa \n"
            "      python bbb\n"
            "    depends: []\n"
            "env:\n"
            "  DEMO: local\n"
        )
        migrated, changed = migrate_legacy_env(content)
        self.assertTrue(changed)
        self.assertIn("command: |-", migrated)
        data = yaml.safe_load(migrated)
        self.assertNotIn("env", data)
        self.assertEqual(data["setup"]["linux"], "export DEMO='local'")
        self.assertEqual(data["setup"]["windows"], 'set "DEMO=local"')


if __name__ == "__main__":
    unittest.main()
