# Public demo examples

This directory contains all non-production example configurations:

- `ds_*.json`: fictional DolphinScheduler exports;
- `ts_*.xml`: fictional Windows Task Scheduler exports;
- `dagr_*.yaml`: converted or directly runnable DAG Runner workflows.

Start the server, then import `dagr_example_pipeline.yaml` with the web console:

```bash
python -m dagrunner.server
```

The **Run** button executes only the small local scripts under `../tasks/`. No
private or production workflow data belongs in this directory.
