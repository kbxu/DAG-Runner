[中文](README.md) | [English](README.en.md)

# DAG Runner

A lightweight, cross-platform Python DAG workflow runner. Describe tasks and dependencies in YAML; one long-running service handles scheduling, execution, monitoring, and logs.

![Console overview](docs/images/dashboard.png)
![DAG view](docs/images/dag.png)
![Run history](docs/images/runs.png)
![Satellite nodes](docs/images/satellite.png)

## Features

- **Lightweight, single node**: Flask + SQLite + APScheduler; no Redis, message queue, or external database
- **DAG execution**: dependency and cycle validation, parallel ready tasks, stop, rerun, and resume-after-failure
- **Scheduling**: Linux-style cron (`0`/`7` = Sunday), multiple crons, arbitrary time zones
- **Cross-platform**: bash on Linux, cmd on Windows, PowerShell optional
- **Satellite nodes**: deploy the agent on other machines; the master dispatches commands over an encrypted channel with forward and reverse transport
- **Web console**: import and edit workflows, inspect DAGs and logs, email notifications
- **Migration**: convert DolphinScheduler JSON and Windows Task Scheduler XML into YAML
- **Security**: password login with lockout; satellite traffic uses HTTPS plus a token challenge-response

## Quick start

```bash
conda create -n dagr python=3.11.* -y && conda activate dagr
python -m pip install -r requirements.txt
```

```bash
python -m dagrunner.auth --generate   # create the admin account; the password is shown once
```

```bash
python -m dagrunner.server            # start the service, open http://127.0.0.1:7119
```

## Workflows

Minimal example:

```yaml
description: Daily data report
schedule:
  crons: ["30 8 * * mon-fri"]
  timezone: Asia/Shanghai
  enabled: true
tasks:
  extract:
    command: python extract.py
  transform:
    command: python transform.py
    depends: [extract]
  publish:
    command: python publish.py
    depends: [transform]
```

### Run locations

The top-level `runner` is the workflow default; a task-level `runner` wins. When neither is set, tasks run locally.

```yaml
runner: moon                     # workflow default
tasks:
  on_satellite:
    command: echo hello
  local_check:
    command: echo check
    runner: local                # override to local
```

### Field reference

| Field | Description | Default |
| --- | --- | --- |
| `description` | Display name | file name |
| `setup` | Environment preparation before tasks; platform-specific values supported | empty |
| `timeout` | Whole-workflow timeout (seconds) | none |
| `schedule.crons` | List of cron expressions | none |
| `schedule.timezone` | Time zone | `Asia/Shanghai` |
| `notification.email` | Email switches and recipients | disabled |
| `runner` | Workflow-level default run location | `local` |
| `tasks.<id>.command` | Command (string or array) | required |
| `tasks.<id>.args` | Extra arguments | empty |
| `tasks.<id>.shell` | `bash` / `sh` / `cmd` / `powershell` | per platform |
| `tasks.<id>.depends` | Upstream tasks | empty |
| `tasks.<id>.timeout` | Per-task timeout (seconds) | none |
| `tasks.<id>.runner` | Run location for this task | follows workflow |
| `tasks.<id>.condition` | Conditional branch (`type: condition`) | none |

## Satellite nodes

Deploy the agent on a target machine and let the master dispatch commands to it over an encrypted channel. All three network topologies are supported:

| Topology | Reachability | Mode | Connector |
| --- | --- | --- | --- |
| 1 | Bidirectional | reverse (preferred) | satellite |
| 2 | Master to satellite only | forward | master |
| 3 | Satellite to master only | reverse | satellite |

**1. Enable on the master** (`var/config.yaml`):

```yaml
satellite:
  enabled: true
  host: 0.0.0.0
  port: 7120
  cert: var/master-certs/cert.pem
  key: var/master-certs/key.pem
  public_url: ""      # address used by satellites; derived from the web Host when empty
```

**2. Add a node**: in the web console click **Satellite** → add, fill in alias, mode, URL (forward), and token (leave blank to generate). Use **Test connection**, then **Download config**.

**3. Start the agent**:

```bash
python -m dagrunner.satellite --config var/satellite.yaml
```

**Certificate directories**: the master uses `var/master-certs/`, the agent uses `var/satellite-certs/` (including the `known_master.json` trust record). To migrate a satellite, copy the whole `var/satellite-certs/` directory.

## Configuration

| Option | Description | Default |
| --- | --- | --- |
| `host` / `port` | Web listen address | `127.0.0.1:7119` |
| `db` / `logs` | Database and log directory | `var/scheduler.db` / `var/logs` |
| `threads` | Web worker threads | `8` |
| `notifier_config` | Email config path | auto-detect `var/notifier.yaml` |
| `allow_insecure_remote_login` | Allow HTTP login (trusted LAN only) | `false` |
| `satellite.enabled` | Enable the satellite service | `false` |
| `satellite.host` / `port` | Satellite service listen address | `0.0.0.0:7120` |
| `satellite.public_url` | Address satellites use to reach the master | derived per request |

## Common commands

| Command | Description |
| --- | --- |
| `python -m dagrunner --workflow <id>` | Run a workflow |
| `python -m dagrunner --workflow <id> --from <task>` | Run from a specific task |
| `python -m dagrunner --workflow <id> --list-runs` | Show run history |
| `python -m dagrunner --show-log --run-id <id> --task <task>` | Show a task log |
| `python -m dagrunner.auth --generate` | Create or reset the login account |
| `python -m dagrunner.migrate_workflows --source <ds\|windows-task-scheduler> ...` | Import external tasks |
| `python -m dagrunner.satellite --config <path>` | Start the satellite agent |
| `python -m demo.init_demo --reset` | Build the demo dataset |

## Project layout

```text
dagrunner/        Core code and web assets
  server.py         Main service entry (web + satellite service)
  satellite.py      Satellite agent entry   satellite_*.py  Protocol / crypto / execution
  workflow.py       Workflow parsing        runner.py        DAG engine
  service.py        Scheduling and runs     database.py      SQLite access
demo/             Sample workflows, task scripts, demo data builder
docs/images/      README screenshots
var/              Database, logs, certificates (local data)
```

## Security

- The web UI listens on `127.0.0.1` by default; put a reverse proxy with HTTPS in front for public access
- The satellite channel uses a self-signed certificate with fingerprint pinning (automatic on first contact, or explicit pinned) and a shared-token challenge-response
- Run tasks under a low-privilege account

## License

[MIT](LICENSE)
