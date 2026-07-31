# DAG Runner

一个简单、轻量、跨平台的 Python DAG 工作流运行器。

用 YAML 定义任务和依赖，启动一个服务，就可以在浏览器里配置定时、查看 DAG、运行任务、停止任务、失败续跑和查看日志。不需要为每个工作流维护系统 cron，也不需要部署分布式调度集群。

![DAG Runner Web Console](docs/images/dashboard.png)
![DAG Runner DAG View](docs/images/dag.png)

## 特性

- **单机轻量**：Flask、SQLite 和 APScheduler 即可运行，不依赖 Redis、消息队列或外部数据库
- **方便迁移**：可把 DolphinScheduler JSON 和 Windows Task Scheduler XML 转换为 DAG Runner YAML
- **DAG 执行**：自动校验依赖和环路，并行执行就绪任务，支持停止、重跑和失败续跑
- **集中管理**：通过 Web 页面导入和编辑工作流、配置定时、查看运行 DAG 与任务日志
- **登录保护**：账号密码登录、36 小时会话，以及连续失败 3 次后按 IP 封禁 10 分钟
- **跨平台**：Linux 使用 Bash，Windows 使用 PowerShell，支持在 `setup` 中激活 Conda 环境

## 快速开始

推荐使用 Conda 创建独立环境：

```bash
git clone https://github.com/kbxu/DAG-Runner.git
cd DAG-Runner
conda create -n dagr python=3.11.* -y
conda activate dagr
python -m pip install -r requirements.txt
python -m dagrunner.auth --generate
python -m dagrunner.server
```

`dagrunner.auth --generate` 默认创建或重置 `admin`，并且只在命令输出中显示一次随机
强密码；重置密码会让该账号已有会话立即失效。也可以不传 `--generate`，按提示手工输入
至少 16 位的密码。

`dagrunner.server` 参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--host` | `127.0.0.1` | HTTP 服务监听地址；需要允许其他机器访问时可设为 `0.0.0.0` |
| `--port` | `7119` | HTTP 服务监听端口 |
| `--db` | `var/scheduler.db` | SQLite 数据库文件路径 |
| `--logs` | `var/logs` | 工作流和任务日志目录 |
| `--threads` | `8` | 任务执行线程池大小 |

例如，允许局域网访问并使用自定义数据目录：

```bash
python -m dagrunner.server \
  --host 0.0.0.0 \
  --port 7119 \
  --db var/scheduler.db \
  --logs var/logs \
  --threads 8
```

启动后访问 <http://127.0.0.1:7119>。数据库表会在服务启动时自动创建，无需手工建表。

Web 登录的密码会先在浏览器中计算 SHA-256，再由后端使用随机盐和
PBKDF2-HMAC-SHA256（600,000 次迭代）生成数据库校验值。前端摘要仍然属于可重放的
密码凭据，不能代替传输加密；非本机部署必须启用 HTTPS，并设置
`DAGRUNNER_COOKIE_SECURE=1`，确保会话 Cookie 只通过 HTTPS 发送。

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

登录 Web 控制台后点击“导入工作流”，选择 YAML 文件并确认即可。导入后调度默认关闭，检查配置无误后再手动启用。

## 从外部调度器迁移

以 DolphinScheduler 为例：

### 1. 从 DolphinScheduler 导出工作流

在 DolphinScheduler 的工作流定义页面选择需要迁移的工作流并导出，得到 JSON 文件。

### 2. 使用命令行转换

```bash
python -m dagrunner.migrate_workflows \
  --source dolphinscheduler \
  data/ds_workflow.json \
  --setup-file templates/production_setup.sh \
  --output-dir data/converted
```

转换参数：

| 参数 | 是否必填 | 说明 |
| --- | --- | --- |
| `--source` | 是 | 导出文件来源；DolphinScheduler 使用 `dolphinscheduler` |
| `EXPORT` | 是 | 一个或多个导出的 JSON 文件路径 |
| `--output-dir` | 否 | YAML 输出目录；默认写入源文件所在目录 |
| `--setup-file` | 否 | 注入工作流级准备脚本；`.ps1`/`.psm1` 按 PowerShell 处理，其余按 Bash 处理 |
| `--exclude-disabled` | 否 | 不转换已禁用节点及与其相连的依赖边 |
| `--timezone` | 否 | 时区，默认 `Asia/Shanghai`；主要用于转换 Windows 触发器 |

输出文件默认命名为 `dagr_<源文件名>.yaml`。转换过程只读写配置，不会执行导出文件中的命令。

### 3. 导入转换后的文件

登录 DAG Runner，在 Web 控制台点击“导入工作流”，选择生成的 YAML 文件。导入后检查命令、依赖关系、工作目录和调度时间，再手动启用调度。

### Windows 任务计划程序示例

先在 Windows 任务计划程序中将任务导出为 XML，然后在 PowerShell 中执行：

```powershell
python -m dagrunner.migrate_workflows `
  --source windows-task-scheduler `
  C:\exports\daily-report.xml `
  --output-dir C:\exports\converted `
  --timezone Asia/Shanghai
```

转换完成后，在 Web 控制台中导入 `C:\exports\converted\dagr_daily-report.yaml`。

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
