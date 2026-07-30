# Public demo

This directory contains all non-production examples:

- `dolphinscheduler_exports/`: a completely fictional DolphinScheduler JSON export;
- `workflows/`: a runnable example and its fictional converted YAML;
- `tasks/`: small scripts used by `example_pipeline.yaml`;
- `production_setup.sh`: an example shell setup snippet for migration.

All schedules in the demo YAML files are disabled. The web console can display
them with:

```bash
python -m dagrunner.server --config-dir demo/workflows
```

The **Run** button executes only the small local scripts under `tasks/`. No
private or production workflow data belongs in this directory.
