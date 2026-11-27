[中文](README.md) | [English](README.en.md)

# Directed Acyclic Graph (DAG) Runner

A lightweight, cross-platform Python DAG workflow runner. Describe tasks and dependencies in YAML; one long-running service handles scheduling, execution, monitoring, and logs.

![Console overview](docs/images/dashboard.png)
![DAG view](docs/images/dag.png)
![Run history](docs/images/runs.png)
![Satellite nodes](docs/images/satellite.png)

## Features

- **Lightweight, single node**: Flask + SQLite + APScheduler; no Redis, message queue, or external database (MySQL 8 optional)
- **DAG execution**: dependency and cycle validation, parallel ready tasks, stop, rerun, and resume-after-failure
- **Scheduling**: Linux-style cron (`0`/`7` = Sunday), multiple crons, arbitrary time zones
- **Cross-platform**: bash on Linux, cmd on Windows, PowerShell optional
- **Satellite nodes**: deploy the agent on other machines; the master dispatches commands over an encrypted channel with forward and reverse transport
- **Web console**: import and edit workflows, inspect DAGs and logs, email notifications
- **Migration**: convert DolphinScheduler JSON and Windows Task Scheduler XML into YAML
- **Security**: password login with lockout; satellite traffic uses HTTPS plus a token challenge-response
- **Process watchdog**: the master service and each satellite restart automatically after a crash or a hung health check, with exponential backoff, email alerts, and seamless handoff during updates

## Quick start

```bash
conda create -n dagr python=3.11.* -y && conda activate dagr
python -m pip install -r requirements.txt
```

```bash
python -m dagrunner.server.auth --generate   # create the admin account; the password is shown once
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

**Version match**: the master and the satellite must run the exact same `dagrunner.__version__`. Registration is refused on a mismatch and the channel is dropped: a reverse satellite stops reconnecting, a forward task fails immediately, and **Test connection** reports the mismatch.

## Version display and updates

The top bar shows the current version `vX.Y.Z`; the satellite panel shows each satellite's version and flags a mismatch. Click **Update** in the top bar to open the update panel.

**Version source**: `dagrunner/__init__.py::__version__`. `pyproject.toml` reads it through a `dynamic version`, and release tags must match it.

**Versioned install**: code lives in `var/versions/<version>/` and `var/current` holds the active version. The launchers (`var/start.bat` / `var/start.sh`) read `var/current` to pick the directory; without that file they fall back to the repository root (development mode). An update only switches `var/current`, and so does a rollback.

**Update sources** (the `update` block in `var/config.yaml`):

| Source | Description |
| --- | --- |
| `git` | `git archive <tag>` into `var/versions/<version>/`; the working tree is never touched, so local changes cannot break an update |
| `release` | Download `dagrunner-<version>.tar.gz` from a GitHub release and verify its sha256 |

**Update flow** (manual from the web console, or `python -m dagrunner.server.update`; both run the same steps):

1. Preflight: refuse while a task is running, a run record is unfinished, or a satellite job is pending/running;
2. Stage the new version and pause the scheduler;
3. Distribute the package to online satellites (they pull it from the master, falling back to the release URL) and verify the sha256;
4. Commit together: satellites restart first, the master last; scheduled runs missed during the update are not replayed;
5. Verify every satellite version after startup and roll back to the previous version on failure.

Offline satellites are skipped; a satellite can also be updated on its own with `python -m dagrunner.server.update --role satellite --to <version>`. An update is refused while tasks are running.

### Branch mode (git clone / development installs)

When the service runs from a git checkout, the web update panel offers **Git branch** as the source:

- The check compares `origin/<name>` with the local HEAD and shows how many commits are behind plus the latest short sha/subject.
- After confirmation the **watchdog runs `git pull --ff-only` and restarts** (the server only writes the handoff request).
- Dirty worktrees, unpushed local commits or a moved remote cause refusal with a message.
- Release-package installs (no `.git`) do not support this mode; keep using release/tag updates.
- The satellite panel shows whether each node could use branch updates (`.git` + remote); actual satellite sync lands in a later phase.

## Process watchdog

Each role runs a resident supervisor process, launched by the start scripts through `run_tee.py`:

```bash
python -m dagrunner.watchdog --role master          # normally started by start.bat / start.sh
python -m dagrunner.watchdog --role satellite
python -m dagrunner.watchdog --status --role master  # print supervisor status
```

- Unexpected child exit: immediate restart with exponential backoff (`5s -> 300s`); the failure counter clears after a stable run.
- Health probe failures: alert after 2 minutes, restart only after 5 consecutive minutes so long tasks are not killed by a transient hiccup.
- Intentional stop (`Ctrl+C` / `SIGTERM`): the service records its intent first and the watchdog exits without restarting it.
- Updates: when a watchdog is active it performs the version switch, verification and rollback; otherwise the original `watchdog/restart.py` path is used unchanged.
- `mode: notify` disables automatic restarts (alert only); a per-role lock prevents duplicate supervisors.

Defaults live in the `watchdog` section of `var/examples/config.example.yaml`. Restarting on an unhealthy process interrupts running tasks; use `mode: notify` if that is not acceptable.

## Configuration

| Option | Description | Default |
| --- | --- | --- |
| `host` / `port` | Web listen address | `127.0.0.1:7119` |
| `db` / `logs` | Database (SQLite path, or a MySQL 8 mapping) and log directory | `var/scheduler.db` / `var/logs` |
| `threads` | Web worker threads | `8` |
| `notifier_config` | Email config path | auto-detect `var/notifier.yaml` |
| `allow_insecure_remote_login` | Allow HTTP login (trusted LAN only) | `false` |
| `satellite.enabled` | Enable the satellite service | `false` |
| `satellite.host` / `port` | Satellite service listen address | `0.0.0.0:7120` |
| `satellite.public_url` | Address satellites use to reach the master | derived per request |
| `update.source` | Update source: `git` / `release` / `auto` | `auto` |
| `update.release.repo` | GitHub release repository | empty (git source) |
| `update.branch.remote` / `name` | Remote and branch for branch mode | `origin` / `main` |
| `update.enabled` | Allow updates | `true` |
| `maintenance.enabled` | Enable run-record archiving | `true` |
| `maintenance.retention_days` | Retention window (days); older terminal runs move to `*_archive` | `60` |
| `maintenance.hour` / `minute` | Daily archive time | `3:15` |

CLI commands (`python -m dagrunner`, `python -m dagrunner.server.auth`, `python -m dagrunner.server.accounts`, `python -m dagrunner.server.maintenance`, `python -m demo.init_demo`) accept `--db-config var/config.yaml` to reuse the `db` entry above; without it they use the SQLite file from `--db`. `python -m dagrunner` is read-only: it inspects runs (`--list-runs` / `--status` / `--show-log`); start workflows from the web console or the scheduler.

### Run-record archiving

Terminal runs older than `maintenance.retention_days` (default 60) are moved in batches by a daily off-peak job into `workflow_runs_archive` / `task_runs_archive`. The backend never reads those tables; they exist for manual inspection. Run `python -m dagrunner.server.maintenance --archive` to archive on demand. Log files are untouched.

## Common commands

| Command | Description |
| --- | --- |
| `python -m dagrunner --list-runs` | Show run history (all workflows) |
| `python -m dagrunner --status --run-id <id>` | Show task status for a run |
| `python -m dagrunner --show-log --run-id <id> --task <task>` | Show a task log |
| `python -m dagrunner.server.auth --generate` | Create or reset the login account (`--reset` is an alias) |
| `python -m dagrunner.server.accounts --list` | List accounts and workflow permissions |
| `python -m dagrunner.server.accounts --add <username>` | Create an account (`--password`, `--unrestricted` optional) |
| `python -m dagrunner.server.accounts --grant <username> <workflow> <read\|execute\|edit>` | Grant a workflow permission |
| `python -m dagrunner.server.accounts --revoke <username> <workflow>` | Remove a workflow permission |
| `python -m dagrunner.server.maintenance --archive` | Archive runs older than the retention window |
| `python -m dagrunner.watchdog --status --role master` | Print supervisor status |
| `python -m dagrunner --version` | Print the version |
| `python -m dagrunner.server.update --check` | List available versions |
| `python -m dagrunner.server.update --to <version>` | Update the master and online satellites from the CLI |
| `python -m dagrunner.server.update --role satellite --to <version>` | Update one satellite |
| `python -m dagrunner.tools.migrate_workflows --source <ds\|windows-task-scheduler> ...` | Import external tasks |
| `python -m dagrunner.satellite --config <path>` | Start the satellite agent |
| `python -m demo.init_demo --reset` | Build the demo dataset |

## Module documentation

- [dagrunner package and shared kernel](dagrunner/README.md)
- [server — main service](dagrunner/server/README.md)
- [database — state storage](dagrunner/database/README.md)
- [satellite — satellite nodes](dagrunner/satellite/README.md)
- [notifier — notifications](dagrunner/notifier/README.md)
- [watchdog — process supervisor](dagrunner/watchdog/README.md)
- [tools — CLI utilities](dagrunner/tools/README.md)
- [Watchdog design notes](dagrunner/watchdog/DESIGN.md)

## Project layout

```text
dagrunner/
  __init__.py       Version number       version.py      Semantic version ordering
  install.py        Versioned layout
  workflow.py       Workflow parsing (shared)  scripting.py  Command scripting (shared)
  cron.py           Cron parsing (shared)     package.py    Update archive helpers (shared)
  __main__.py       Thin entry point (python -m dagrunner)
  server/           Main service package: app / webapp / service / runner / executor /
                    pool / accounts / auth / logger / maintenance / update
                    (includes templates / static)
  database/         State storage package: base / sqlite / mysql8
  satellite/        Satellite package: agent / protocol / crypto / master channel / updates
  watchdog/         Process watchdog (state, health probes, launch, loop) + restart updater
  notifier/         Notification package: email implemented; Feishu and others stubbed
  tools/            CLI tools: cli (run history) / migrate_workflows (import) / workflow_editor
run_server.py       Version-directory launcher  run_satellite.py Satellite launcher
demo/             Sample workflows, task scripts, demo data builder
docs/images/      README screenshots
var/              Database, logs, certificates, versioned installs (local data)
```

## Security

- The web UI listens on `127.0.0.1` by default; put a reverse proxy with HTTPS in front for public access
- The satellite channel uses a self-signed certificate with fingerprint pinning (automatic on first contact, or explicit pinned) and a shared-token challenge-response
- Run tasks under a low-privilege account
- Accounts: the user named `admin` is the administrator and owns global settings (accounts, email sender, satellites, updates); other accounts get per-workflow grants (read / execute / edit) and cannot see ungranted workflows. Accounts created before this feature keep full workflow access
- Manual runs record the triggering user; passwords need at least 16 characters, 3 failed sign-ins block an IP for 10 minutes, and disabling or resetting a password signs out its sessions immediately
- `/health` is the watchdog probe endpoint: unauthenticated, returning only version, scheduler/database availability and update state; still avoid exposing the admin port publicly

## License

[MIT](LICENSE)
