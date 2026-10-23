import unittest

from dagrunner.migrate_workflows import convert_crontab_line, dump_workflow_yaml
from dagrunner.workflow import Workflow


class CrontabImportTests(unittest.TestCase):
    def test_converts_five_fields_and_command(self):
        config, warnings = convert_crontab_line(
            "30 16 * * mon-fri /home/user/backup.sh --full"
        )
        self.assertEqual(warnings, [])
        self.assertEqual(config["schedule"]["crons"], ["30 16 * * mon-fri"])
        self.assertEqual(
            config["tasks"]["task_1"]["command"], "/home/user/backup.sh --full"
        )
        workflow = Workflow.from_yaml(dump_workflow_yaml(config), fallback_name="x")
        self.assertIn("task_1", workflow.tasks)

    def test_description_is_a_short_default_not_the_command(self):
        command = "/home/user/backup.sh --full"
        config, _ = convert_crontab_line(f"0 3 * * * {command}")
        description = config["tasks"]["task_1"]["description"]
        self.assertNotEqual(description, command)
        self.assertNotIn("--full", description)
        self.assertLessEqual(len(description), len(command))

    def test_rejects_missing_command(self):
        with self.assertRaises(ValueError):
            convert_crontab_line("30 16 * * mon-fri")

    def test_rejects_invalid_cron(self):
        with self.assertRaises(ValueError):
            convert_crontab_line("99 16 * * mon-fri /bin/true")

    def test_round_trips_yaml_special_characters(self):
        command = "echo 'a: b' # note"
        config, _ = convert_crontab_line(f"0 3 * * * {command}")
        workflow = Workflow.from_yaml(dump_workflow_yaml(config), fallback_name="x")
        self.assertEqual(workflow.tasks["task_1"].command, command)


if __name__ == "__main__":
    unittest.main()
