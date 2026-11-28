# server —— 主服务

常驻的 Flask + APScheduler 服务：Web 控制台、定时调度、DAG 执行、账号权限、卫星通道与更新协调。

## 启动

```bash
python -m dagrunner.server --config var/config.yaml   # 默认 var/config.yaml
```

也可用仓库根 `run_server.py` 启动。

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
| `update.py` | `UpdateService`：git 分支检查、原地快进拉取、通知在线卫星更新并触发看门狗重启 |
| `templates/` `static/` | 控制台页面与前端资源 |

## 关键行为

- **调度**：cron 解析在 `dagrunner.cron`；错过触发不补跑；更新期间暂停并重设计划。
- **执行**：同一工作流同时只允许一个活动运行；任务默认并发受线程池限制。
- **健康检查**：`GET /health`（免登录）返回 `ok`、`version`、`scheduler_running`、`db_ok`、`active_runs`、`update`，供看门狗探活。
- **在线更新**：仅支持 git clone 部署（发布包安装不支持）。预检本地已跟踪文件无改动、无未推送提交、无运行中任务后，原地执行 `git merge --ff-only`，通知在线卫星同步更新并写 `intent=restart` 退出，由看门狗重启服务；配置见 `update.branch`。
- **优雅退出**：`SIGINT/SIGTERM` 先写看门狗 intent，再取消运行、关闭调度器与卫星通道。

## 配置

读取 `var/config.yaml`：`host/port/threads/db/logs/language/notifier_config`、`satellite.*`、`update.*`、`maintenance.*`、`watchdog.*`。示例见 `../../var/examples/config.example.yaml`。

## 依赖方向

`server → database / workflow / scripting / cron / package / install / version / notifier / satellite.master / watchdog.state`。不依赖 `tools/`。
