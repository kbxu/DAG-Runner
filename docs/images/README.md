# Homepage images

Public screenshots used by the repository README:

- `dashboard.png`: workflow list with schedules and run status;
- `dag.png`: DAG structure modal;
- `runs.png`: run history with statuses and resume actions;
- `satellite.png`: satellite node management panel.

Regenerate them against a clean demo database:

```bash
python -m demo.init_demo --reset
python -m dagrunner.server
# then capture the four views with a headless browser
```
