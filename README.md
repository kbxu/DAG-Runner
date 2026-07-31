# DAG Runner

一个简单、轻量、跨平台的 Python DAG 工作流运行器。

用 YAML 定义任务和依赖，启动一个服务，就可以在浏览器里配置定时、查看 DAG、运行任务、停止任务、失败续跑和查看日志。不需要为每个工作流维护系统 cron，也不需要部署分布式调度集群。

![DAG Runner Web Console](docs/images/dashboard.png)
![DAG Runner DAG View](docs/images/dag.png)

## 特性

- YAML 定义 DAG，自动校验依赖和环路；就绪任务并行执行，依赖成功后自动解锁下游
- Flask Web 控制台，支持手动导入、编辑、删除工作流以及查看实时运行 DAG
- SQLite 作为工作流、定时和运行状态的唯一数据源，Server 启动时不扫描 YAML
- APScheduler 内置五字段 cron；导入后的定时默认关闭，由页面人工启用
- 支持立即运行、停止、完整重跑，以及使用最新配置复用已成功任务后续跑
- 每个任务独立保存 stdout/stderr 日志，可在页面查看和复制
- Linux 使用 Bash，Windows 使用 PowerShell
- 支持在分平台 `setup` 中切换目录、定义环境变量和激活 Conda 环境
- 支持 DolphinScheduler JSON 和 Windows Task Scheduler XML 转换
- 单机运行，不依赖 Redis、消息队列或外部数据库

## 快速开始

需要 Python 3.10 或更高版本。

```bash
git clone https://github.com/kbxu/DAG-Runner.git
cd dag-runner
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

启动 Server：

```bash
.venv/bin/python -m dagrunner.server
```

Windows 可使用：

```powershell
python -m pip install -r requirements.txt
python -m dagrunner.server
```

访问 <http://127.0.0.1:7119>，点击“导入工作流”，选择
`demo/examples/dagr_example_pipeline.yaml`。文件会先在编辑窗口中打开并显示拟分配 ID；
确认内容后点击“导入”。Server 不扫描本地 YAML，导入后自动分配工作流 ID，定时默认关闭。

## 定义工作流

```yaml
name: example_pipeline
description: 示例数据处理流程

setup:
  linux: |
    cd /srv/my_project
    export APP_ENV='production'
    source /opt/miniconda3/etc/profile.d/conda.sh
    conda activate analytics
  windows: |
    Set-Location "C:\Projects\my_project"
    $env:APP_ENV = 'production'
    conda activate analytics

schedule:
  cron: "0 9 * * mon-fri"
  timezone: Asia/Shanghai
  enabled: false

tasks:
  extract_data:
    description: 提取数据
    command: [python, jobs/extract.py]
    depends: []

  transform_data:
    description: 清洗数据
    command: [python, jobs/transform.py]
    depends: [extract_data]

  publish_report:
    description: 发布报告
    command: [python, jobs/publish.py]
    depends: [transform_data]
    timeout: 1800
```

配置不再支持顶层或任务级 `env`。环境变量统一在 `setup` 中定义：Bash 使用
`export NAME='value'`，PowerShell 使用 `$env:NAME = 'value'`。`setup` 与任务
命令在同一个 Shell 进程中执行，因此变量会传递给任务命令。

顶层 `workdir` 也不再支持。工作目录应在 `setup` 开头通过 Bash 的 `cd` 或
PowerShell 的 `Set-Location` 切换；只有确实需要单独目录的任务才使用任务级 `cwd`。
可复制 [`templates/production_setup.sh`](templates/production_setup.sh) 或
[`templates/production_setup.ps1`](templates/production_setup.ps1) 后填写项目路径、
Conda 安装位置和环境名称，再通过迁移命令的 `--setup-file` 使用。

任务只有在所有依赖均为 `SUCCESS` 时才会执行。依赖失败后，下游任务自动记为 `SKIPPED`。
同一工作流默认最多同时执行 4 个已就绪任务；任务完成后会立即重新计算并解锁下游。

完整虚构示例见 [`demo/examples/dagr_example_pipeline.yaml`](demo/examples/dagr_example_pipeline.yaml)。

## Web 控制台

页面包含两个主要表格：

- **工作流与定时**：中文名称在前、数据库 ID 在后；可手动导入 YAML、查看任务和 DAG、配置定时、运行、编辑、导出或删除工作流。编辑窗口只会在保存或主动关闭时退出，并高亮实际参与执行的字段和变量。
- **运行记录**：按时间倒序查看进度和状态；详情中展示带任务状态的历史 DAG 和任务表，支持手动刷新、停止、重跑、失败续跑或删除记录。日志使用独立弹窗，显示“中文名（任务 ID）”并提供复制按钮。

每次运行都会保存任务中文名和依赖快照，后续编辑工作流不会改变历史 DAG。
删除单条运行记录会同时删除对应任务记录和日志；删除工作流会级联删除其定时、
全部运行记录、任务记录和日志。运行中的工作流或运行记录不能删除。

失败续跑会重新加载数据库中的最新工作流正文：旧运行中同 ID 且已成功的任务
直接复用，其余任务按最新 DAG 继续执行。

定时使用标准五字段 Cron（分、时、日、月、星期）。例如 `10 * * * *`
表示每小时第 10 分钟，`0 10 * * *` 表示每天 10:00。DolphinScheduler
导出的 Quartz 表达式 `0 10 * * * ? *` 会自动转换为 `10 * * * *`。
运行记录写入 SQLite 并在页面展示时均使用服务所在系统的本地时区。

工作流只能通过页面手动导入，数据库自动分配 ID；YAML 中的原工作流 ID
不再使用，中文显示名取自 YAML 的 `description`。来源 Cron 和时区作为初始值，但定时一律
关闭；没有定时信息时使用 `0 18 * * mon-fri` 和 `Asia/Shanghai`。
定时下线且工作流未运行时，可以在页面编辑完整 YAML 正文。
导入后不再保留 YAML 文件所在目录；相对路径以 Server 启动目录为基准，
生产配置建议在 `setup` 或任务 `cwd` 中使用明确路径。

默认数据位置：

- SQLite：`var/scheduler.db`，保存工作流正文、中文名称、定时、运行及任务状态
- 日志：`var/logs/`
- 运行锁：`var/locks/`

数据库写入和页面展示均使用 Server 所在系统的本地时间与时区。`var/` 和 `data/`
都不会进入 Git；`data/` 仅用于存放私有迁移源文件和本地转换结果。

## CLI

```bash
# 运行工作流
python -m dagrunner --workflow workflow_000001

# 查看历史
python -m dagrunner --workflow workflow_000001 --list-runs

# 从指定失败任务继续
python -m dagrunner --workflow workflow_000001 --from transform_data

# 查看任务日志
python -m dagrunner --show-log --run-id <run_id> --task transform_data
```

## 从外部调度器迁移

仓库包含完全虚构的 DolphinScheduler 和 Windows 任务计划程序导出示例：

- [`demo/examples/ds_9000001001.json`](demo/examples/ds_9000001001.json)
- [`demo/examples/dagr_ds_9000001001.yaml`](demo/examples/dagr_ds_9000001001.yaml)
- [`demo/examples/ts_market_report.xml`](demo/examples/ts_market_report.xml)
- [`demo/examples/dagr_ts_market_report.yaml`](demo/examples/dagr_ts_market_report.yaml)

转换命令只读写配置，不会执行任务：

```bash
python -m dagrunner.migrate_workflows \
  --source dolphinscheduler \
  demo/examples/ds_9000001001.json \
  --setup-file templates/production_setup.sh \
  --exclude-disabled
```

Windows 任务计划程序 XML：

```bash
python -m dagrunner.migrate_workflows \
  --source windows-task-scheduler \
  demo/examples/ts_market_report.xml \
  --timezone Asia/Shanghai
```

`--source` 接受 `dolphinscheduler` 和 `windows-task-scheduler`。
未传 `--output-dir` 时，转换结果写入源文件所在目录，默认文件名为
`dagr_<源文件名>.yaml`；一个源文件包含多个工作流时，从第二个开始追加 `_2`、`_3`。
迁移器会把 DolphinScheduler SHELL 脚本中的 CRLF/CR 换行统一为 LF，生成的
YAML 文件也固定使用 LF，避免 Bash 执行时因残留 `\r` 报错。
`--exclude-disabled` 会过滤 DolphinScheduler 禁用节点以及与这些节点相连的
依赖边；不传时则保留禁用的 SHELL 节点。转换器支持 SHELL 节点、
依赖、全局参数、启用状态和 timeout。DolphinScheduler 全局及无冲突的任务
局部参数会写入 `setup`；`.sh` setup 生成 `export`，`.ps1` setup 生成 `$env:`。
启用的 `CONDITIONS` 成功分支会降级为
普通成功依赖，失败分支会禁用并提示人工复核；禁用的 `CONDITIONS` 不参与
依赖计算。迁移器不再生成 `workdir`；需要切换工作目录时，应在 setup 文件中
先执行 `cd`（Bash）或 `Set-Location`（PowerShell）。

Windows 导入器支持 `Exec` 动作以及每日、每周和每月日期触发器，并保留参数、
工作目录和执行时限。同一计划任务有多个无法合并的触发时间时，会生成多份
工作流以保证时间精确。所有导入的定时默认关闭，必须在 Server 页面人工开启；
账户和登录方式仅保留为迁移提示，实际使用 DAG Runner 服务账户执行。

## 项目结构

```text
dagrunner/        核心代码、Web 页面和静态资源
demo/tasks/       可安全运行的虚构示例任务脚本
demo/examples/    ds_*.json、ts_*.xml 和 dagr_*.yaml 公开示例
templates/        Bash 和 PowerShell 生产 setup 模板
data/             私有迁移数据，Git 忽略
var/              SQLite、日志和本地运行数据，Git 忽略
docs/images/      README 首页图片
```

## 部署与安全

服务默认只监听 `127.0.0.1`。开放到局域网或互联网前，请通过反向代理增加身份认证和 HTTPS。工作流 YAML 可以执行系统命令，应当按可信代码管理，并使用低权限服务账户运行。

操作系统只需负责常驻 `python -m dagrunner.server` 进程；具体工作流定时由服务内部管理。

## 开发

```bash
python -m pip install -r requirements.txt
```

## License

[MIT](LICENSE)
