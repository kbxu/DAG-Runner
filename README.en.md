[中文](README.md) | [English](README.en.md)

# DAG Runner

A simple, lightweight, cross-platform Python DAG workflow runner.

Define tasks and dependencies in YAML, start one service, and use the browser to configure schedules, inspect DAGs, start or stop runs, resume failed runs, and read logs. There is no need to maintain an operating-system cron entry for every workflow or deploy a distributed scheduler cluster.

![DAG Runner Web Console](docs/images/dashboard.png)
![Import from DolphinScheduler](docs/images/import-from-ds.png)
![DAG Runner DAG View](docs/images/dag.png)

## Architecture

```mermaid
flowchart LR
    user["Web console / CLI"] --> service["DAG Runner service<br/>Workflow management and scheduling"]
    yaml["YAML workflow"] --> service
    service --> engine["DAG execution engine<br/>Dependency resolution and parallel scheduling"]
    engine --> tasks["Task processes<br/>Bash / PowerShell / Python"]
    service <--> db[("SQLite<br/>Configuration and run state")]
    engine --> logs[("File logs")]
```

## Features

- **Cron weekdays:** Linux numbering uses `0`/`7` for Sunday and `1-6` for Monday through Saturday; names such as `mon-sat` are also supported. `40 8,23 * * 1-6` and `40 8,23 * * mon-sat` are equivalent. Existing numeric weekday schedules follow this rule after upgrading.
- **Lightweight and single-node:** Flask, SQLite, and APScheduler; no Redis, message queue, or external database
- **Easy migration:** import DolphinScheduler JSON and Windows Task Scheduler XML as DAG Runner workflows
- **DAG execution:** dependency and cycle validation, parallel execution of ready tasks, stop, rerun, and resume support
- **Centralized management:** import and edit workflows, configure schedules, inspect DAGs, and read task logs in the web console
- **Login protection:** username/password login, 36-hour sessions, and a 10-minute IP block after three consecutive failures
- **Cross-platform:** Bash on Linux and PowerShell on Windows, including Conda activation in workflow `setup`
- **Satellite nodes:** deploy the satellite agent on other machines and dispatch commands over an encrypted channel; forward and reverse transport cover bidirectional, master-to-satellite, and satellite-to-master networks

## Quick start

### 1. Create the runtime environment

Using a dedicated Conda environment is recommended:

```bash
git clone https://github.com/kbxu/DAG-Runner.git
cd DAG-Runner
conda create -n dagr python=3.11.* -y
conda activate dagr
python -m pip install -r requirements.txt
```

### 2. Create the first login account

After activating the environment and installing dependencies, run:

```bash
python -m dagrunner.auth --generate
```

DAG Runner has no default password. This command creates or resets the `admin` account and prints a randomly generated strong password once. Save it immediately. Alternatively, omit `--generate` and enter a password of at least 16 characters when prompted. Run the command again to reset a lost password; existing sessions are invalidated immediately.

If the server uses a custom database path, use the same path when creating the account:

```bash
python -m dagrunner.auth --db /path/to/scheduler.db --generate
```

### 3. Start the service and sign in

```bash
python -m dagrunner.server
```

Open <http://127.0.0.1:7119> and sign in as `admin` with the generated password. Database tables are created automatically when the service starts.

`dagrunner.server` accepts one option:

| Option | Default | Description |
| --- | --- | --- |
| `--config` | `var/config.example.yaml` | Startup YAML config; all other settings are read from this file |

See `var/config.example.yaml` for the available settings:

```yaml
host: 127.0.0.1
port: 7119
db: var/scheduler.db
logs: var/logs
threads: 8
language: zh-CN
notifier_config: null
allow_insecure_remote_login: false
```

`notifier_config` points to the notification (email SMTP) config file. `null` (the default) means no
explicit path: the service auto-discovers `notifier.yaml`, `notifier.yml`, or `notifier.json` in the
directory of `db` (default `var/`); if none exists, saving the sender settings in the Web console's
"Notification" dialog creates `var/notifier.yaml` automatically. Set an explicit path when needed,
e.g. `notifier_config: var/notifier.yaml`. `var/notifier.example.yaml` is only a template and is never
read or updated automatically; copy it to `var/notifier.yaml` and edit that file if desired.

For example, to allow LAN access and HTTP login on a trusted network, set in the config file:

```yaml
host: 0.0.0.0
port: 7119
db: var/scheduler.db
logs: var/logs
threads: 8
allow_insecure_remote_login: true
```

To use another config, copy it to `var/config.yaml` and pass `--config var/config.yaml`.

Two launcher examples are provided under `var/`:

- Windows: `var/start.example.bat`
- Linux: `var/start.example.sh`

`DAGR_PYTHON` in each script selects the Python executable and defaults to `python`. Edit it to use another interpreter, e.g. `set DAGR_PYTHON=C:\Python311\python.exe` or `DAGR_PYTHON=/opt/python3.11/bin/python`. Run them with:

```bash
bash var/start.example.sh
```

```bat
var\start.example.bat
```

The browser first hashes the login password with SHA-256. The backend then stores a salted PBKDF2-HMAC-SHA256 verifier with 600,000 iterations. The browser hash is still a replayable credential and does not replace transport encryption. Non-local deployments must use HTTPS and set `DAGRUNNER_COOKIE_SECURE=1` so session cookies are sent only over HTTPS.

For temporary HTTP use on a trusted LAN, set `host: 0.0.0.0` and `allow_insecure_remote_login: true` in the config file. This permits HTTP session cookies and enables the page's built-in SHA-256 implementation when Web Crypto is unavailable. It also permits HTTP login even when `DAGRUNNER_COOKIE_SECURE=1` is set, so never use it on the public internet.

### 4. Define or import a workflow

Workflows use YAML to describe tasks, dependencies, setup scripts, and schedules. See the complete [example workflow](demo/examples/dagr_example_pipeline.yaml). In the web console, select **New workflow** to edit the example or **Import workflow** to choose an existing file. Imported schedules are disabled by default; review commands, dependencies, working directories, and schedule times before enabling them.

New workflows use random hash-style IDs. If an imported task ID is already used by another workflow, the preview assigns a new hash-style ID to that task and updates dependency and condition-branch references with it.

The optional `timeout` value is measured in seconds. A top-level `timeout` limits the entire workflow, while a task-level `timeout` limits only that task. On timeout, active task process trees are terminated. Omitting the field means there is no timeout:

```yaml
timeout: 3600
tasks:
  generate_report:
    command: python report.py
    timeout: 600
```

The import dialog automatically recognizes:

- DAG Runner YAML
- DolphinScheduler JSON, such as this [example export](demo/examples/ds_9000001001.json)
- Windows Task Scheduler XML, such as this [example export](demo/examples/ts_market_report.xml)

DolphinScheduler and Windows exports are converted in memory with default options. The detected source is shown above the editor and no intermediate file is written. Multiple convertible calendar triggers in one Windows task remain in a single workflow and share one timezone.

The DolphinScheduler converter currently supports only `SHELL` (Bash) and `CONDITIONS` nodes. Other node types are omitted and the import preview asks for manual review.

#### Manual conversion with environment options

Use the command-line converter when a migration needs a working directory, Conda environment, or environment variables. For example:

```bash
python -m dagrunner.migrate_workflows \
  --source dolphinscheduler \
  data/ds_workflow.json \
  --setup-file templates/production_setup.sh
```

For Windows Task Scheduler, use `--source windows-task-scheduler` with the exported XML file. A PowerShell setup script can be supplied with `--setup-file templates/production_setup.ps1`. Then import the generated `dagr_<source filename>.yaml` from the web console.

Manual conversion options:

| Option | Required | Description |
| --- | --- | --- |
| `--source` | yes | `dolphinscheduler` or `windows-task-scheduler` |
| `EXPORT` | yes | One or more JSON/XML export paths; this is a positional placeholder, not a literal argument |
| `--output-dir` | no | YAML output directory; defaults to the source file's directory |
| `--setup-file` | no | Workflow setup script; `.ps1`/`.psm1` uses PowerShell, all other files use Bash |
| `--exclude-disabled` | no | Omit disabled nodes and dependency edges connected to them |
| `--timezone` | no | Timezone, default `Asia/Shanghai`; mainly used for Windows triggers |

Conversion reads and writes configuration only; it never executes commands from an export.

## CLI

```bash
# Run a workflow
python -m dagrunner --workflow workflow_a1b2c3d4e5f6

# List run history
python -m dagrunner --workflow workflow_a1b2c3d4e5f6 --list-runs

# Resume from a specific failed task
python -m dagrunner --workflow workflow_a1b2c3d4e5f6 --from transform_data

# Read a task log
python -m dagrunner --show-log --run-id <run_id> --task transform_data
```

## Satellite nodes (cross-machine execution)

The satellite agent is a standalone backend deployed on a target machine. It receives commands from the master and runs them locally: bash on Linux and cmd on Windows by default. The master and satellites communicate over HTTPS and authenticate with a shared-token challenge-response; the certificate fingerprint is pinned automatically on first contact, or fill it in pinned mode.

All three network topologies are supported:

| Topology | Reachability | Transport | Connector |
| --- | --- | --- | --- |
| 1 | Bidirectional | reverse (preferred) | satellite |
| 2 | Master to satellite only | forward | master |
| 3 | Satellite to master only | reverse | satellite |

### 1. Enable the satellite service on the master

Enable it in `var/config.yaml` (a self-signed certificate is generated on first start):

```yaml
satellite:
  enabled: true
  host: 0.0.0.0
  port: 7120
  # Master-side certificate, generated on first start
  cert: var/master-certs/cert.pem
  key: var/master-certs/key.pem
  # Address satellites use to reach the master; derived from the web Host when empty
  public_url: ""
```

### 2. Add a satellite node in the web console

After signing in, click the **Satellite** button at the top, add a node with an alias, mode, URL (forward mode), and shared token (leave blank to generate one), then use **Test connection** to validate the configuration and **Download config** to get a ready-to-use `satellite.yaml`.

### 3. Start the agent on the target machine

```bash
python -m dagrunner.satellite --config var/satellite.yaml
```

Master and satellite certificates live in separate directories: `var/master-certs/` for the master and `var/satellite-certs/` for the agent (its `known_master.json` trust record lives there too). To migrate a satellite, copy the whole `var/satellite-certs/` directory to the new machine; no certificate re-issuance is needed.

Set a workflow's run location to the satellite alias to execute its tasks on that machine; logs stream back to the master in near real time. The default `auto` mode prefers the reverse channel and falls back to listening for forward connections, switching automatically when the network changes.

### 4. Assign run locations (workflow level / task level)

The run location can be written in the workflow YAML at two levels, and **the task level wins**:

```yaml
runner: sat-01          # workflow default; local when omitted
tasks:
  extract:
    command: python extract.py
  report:               # no runner: follows the workflow default (sat-01)
    command: python report.py
  notify:
    command: python notify.py
    runner: local       # override: always run on the local machine
```

- Neither level set → everything runs locally.
- Workflow set, task not set → the task follows the workflow.
- Task set → the task wins.
- If a satellite is unreachable or unknown, the affected task fails with the reason in its log and error message.

The "Run location" dropdown in the web editor maps to the workflow-level `runner` (written to the top level of the YAML on save). In the DAG view, nodes that run on a satellite are labelled with its alias in the top-right corner. The tag beside the title in the workflow list and run records summarises the locations: empty when everything is local, the alias when a single satellite is used, and `mixed` otherwise (hover for the per-location task counts).

## Project structure

```text
dagrunner/        Core code, web templates, and static assets
  satellite.py          Satellite agent entry point
  satellite_agent.py    Satellite process and job executor
  satellite_master.py   Master-side satellite manager and port 7120 endpoints
  satellite_protocol.py Protocol types, job specs, and script building
  satellite_crypto.py   Certificates, TLS, nonce, and HMAC authentication
  satellite_http.py     HTTPS client for the satellite protocol
  satellite_server.py   waitress plus in-process TLS termination
demo/tasks/       Safe, fictional example task scripts
demo/examples/    Public ds_*.json, ts_*.xml, and dagr_*.yaml examples
templates/        Production Bash and PowerShell setup templates
data/             Private migration data, ignored by Git
var/              SQLite, logs, satellite certificates, and local runtime data, ignored by Git
docs/images/      README images
```

## Deployment and security

The service listens on `127.0.0.1` by default. Add authentication and HTTPS through a reverse proxy before opening it to a LAN or the internet. Workflow YAML can execute system commands; manage it as trusted code and run the service under a low-privilege account.

The operating system only needs to keep the `python -m dagrunner.server` process alive. DAG Runner manages individual workflow schedules internally.

## License

[MIT](LICENSE)
