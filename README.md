# DAG Runner

一个简单、轻量、跨平台的 Python DAG 工作流运行器。

用 YAML 定义任务和依赖，启动一个服务，就可以在浏览器里配置定时、查看 DAG、运行任务、停止任务、失败续跑和查看日志。不需要为每个工作流维护系统 cron，也不需要部署分布式调度集群。

![DAG Runner Web Console](docs/images/dashboard.png)
![DAG Runner DAG View](docs/images/dag.png)

## 特性

- YAML 定义 DAG，自动校验依赖、环路并按拓扑顺序执行
- Flask Web 控制台，展示工作流、DAG 结构和历史运行状态
- APScheduler 内置五字段 cron，定时配置保存到 SQLite
- 支持立即运行、停止、完整重跑和从最新失败节点续跑
- 每个任务独立保存 stdout/stderr 日志
- Linux 使用 Bash，Windows 使用 PowerShell
- 支持工作流级环境变量、Conda 激活和分平台 setup
- 提供 DolphinScheduler SHELL 工作流 JSON 转换工具
- 单机运行，不依赖 Redis、消息队列或外部数据库

## 快速开始

需要 Python 3.10 或更高版本。

```bash
git clone https://github.com/kbxu/DAG-Runner.git
cd dag-runner
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

启动公开 demo：

```bash
.venv/bin/python -m dagrunner.server --config-dir demo/workflows
```

Windows 可使用：

```powershell
python -m pip install -r requirements.txt
python -m dagrunner.server --config-dir demo\workflows
```

访问 <http://127.0.0.1:8080>。Demo 的定时默认关闭；点击“运行”会执行 `demo/tasks/` 中的小型本地示例。

生产环境把 YAML 放入 `workflows/`，直接启动：

```bash
python -m dagrunner.server
```

## 定义工作流

```yaml
name: example_pipeline
description: 示例数据处理流程
workdir: .

env:
  APP_ENV: production

setup:
  linux: |
    source /opt/miniconda3/etc/profile.d/conda.sh
    conda activate analytics
  windows: |
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

任务只有在所有依赖均为 `SUCCESS` 时才会执行。依赖失败后，下游任务自动记为 `SKIPPED`。

完整虚构示例见 [`demo/workflows/example_pipeline.yaml`](demo/workflows/example_pipeline.yaml)。

## Web 控制台

页面包含两个主要表格：

- **工作流与定时**：查看任务数量、cron、时区和下次运行；打开 DAG 图、编辑定时、立即运行或导出 YAML。
- **运行记录**：按时间倒序查看进度和状态；打开任务日志、停止运行、完整重跑或失败续跑。

默认数据位置：

- 工作流：`workflows/`
- SQLite：`var/scheduler.db`
- 日志：`var/logs/`

`var/` 和 `data/` 都不会进入 Git。

## CLI

```bash
# 运行工作流
python -m dagrunner --workflow example_pipeline --config-dir demo/workflows

# 查看历史
python -m dagrunner --workflow example_pipeline --list-runs

# 从指定失败任务继续
python -m dagrunner --workflow example_pipeline --config-dir demo/workflows --from transform_data

# 查看任务日志
python -m dagrunner --show-log --run-id <run_id> --task transform_data
```

## 从 DolphinScheduler 迁移

仓库包含一份完全虚构的 DolphinScheduler SHELL 工作流导出和对应转换结果：

- [`demo/dolphinscheduler_exports/example_workflow.json`](demo/dolphinscheduler_exports/example_workflow.json)
- [`demo/workflows/ds_9000001001.yaml`](demo/workflows/ds_9000001001.yaml)

转换命令只读写配置，不会执行任务：

```bash
python -m dagrunner.migrate_dolphinscheduler demo/dolphinscheduler_exports/example_workflow.json \
  --output-dir demo/workflows \
  --workdir .. \
  --setup-file demo/production_setup.sh
```

转换器支持 SHELL 节点、依赖、全局参数、启用状态和 timeout。`CONDITIONS` 成功分支会降级为普通成功依赖，失败分支会禁用并提示人工复核。

## 项目结构

```text
dagrunner/        核心代码、Web 页面和静态资源
tests/            单元测试和 API 测试
workflows/        生产工作流目录
demo/             完全虚构的公开示例
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
python -m unittest discover -s tests -v
```

测试使用 mock/fake executor，不会执行真实工作流命令。

## License

[MIT](LICENSE)
