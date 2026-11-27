# server —— 主服务

常驻的 Flask + APScheduler 服务：Web 控制台、定时调度、DAG 执行、账号权限、卫星通道与更新协调。

## 启动

```bash
python -m dagrunner.server --config var/config.yaml   # 默认 var/config.yaml
```

也可用仓库根 `run_server.py`（版本化安装模式下从 `var/versions/<v>/` 启动）。

## 模块职责

| 模块 | 职责 |
| --- | --- |
| `app.py` | 入口装配：加载配置、启动卫星 HTTPS 通道、创建 Flask app、安装信号处理器 |
| `webapp.py` | Web/API：登录与账号、工作流编辑/导入预览、运行记录、卫星面板、更新面板、`/health` |
| `service.py` | `ExecutionService`（并发执行/停止/重跑/恢复孤儿运行）、`ScheduleService`（APScheduler 作业）、`WorkflowRegistry` |
| `runner.py` | DAG 执行引擎 `WorkflowRunner`：依赖拓扑、并行就绪任务、失败续跑、流程锁；另含运行记录查看 CLI `main` |
| `executor.py` | 本地任务执行器 `TaskExecutor`、`ExecutionResult` |
| `pool.py` | 守护线程池 `DaemonPool` |
| `accounts.py` / `auth.py` | 账号授权与登录会话（含 CLI：`python -m dagrunner.server.accounts`、`.auth`） |
| `logger.py` | 任务日志管理 `TaskLogManager`、终端编码解码 |
| `maintenance.py` | 终态运行记录归档（保留期、批处理），CLI `python -m dagrunner.server.maintenance` |
| `update.py` | `UpdateService`：预检、暂存、下发卫星、提交/回滚；归档工具来自 `dagrunner.package` |
| `templates/` `static/` | 控制台页面与前端资源 |

## 关键行为

- **调度**：cron 解析在 `dagrunner.cron`；错过触发不补跑；更新期间暂停并重设计划。
- **执行**：同一工作流同时只允许一个活动运行；任务默认并发受线程池限制。
- **健康检查**：`GET /health`（免登录）返回 `ok`、`version`、`scheduler_running`、`db_ok`、`active_runs`、`update`，供看门狗探活。
- **更新交接**：检测到活跃看门狗时写 `var/updates/handoff.json` 并退出，由看门狗切版本；否则交给 `dagrunner.watchdog.restart`。
- **分支模式**：git clone 部署可在 Web 选择「git 分支」，检查 `origin/<name>` 与本地差异，确认后由看门狗执行 `git pull --ff-only` 并重启（要求工作区干净、仅快进）；配置见 `update.branch`。
- **优雅退出**：`SIGINT/SIGTERM` 先写看门狗 intent，再取消运行、关闭调度器与卫星通道。

## 配置

读取 `var/config.yaml`：`host/port/threads/db/logs/language/notifier_config`、`satellite.*`、`update.*`、`maintenance.*`、`watchdog.*`。示例见 `../../var/examples/config.example.yaml`。

## 依赖方向

`server → database / workflow / scripting / cron / package / install / version / notifier / satellite.master / watchdog.state`。不依赖 `tools/`。
