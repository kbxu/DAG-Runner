# dagrunner 包总览（共享内核）

本目录是 DAG Runner 的主体包。根级文件只保留**跨包复用的共享设施**，业务功能都在各子包里。

## 根级共享模块

| 模块 | 职责 | 主要消费者 |
| --- | --- | --- |
| `__init__.py` | 包标识与 `__version__`（版本号唯一来源） | 所有模块、发版 tag |
| `install.py` | 版本化安装布局：`var/current`、`var/versions/<v>/`、`activate()`、原子写状态 | server、watchdog、update、restart |
| `version.py` | 语义化版本解析与比较 | server/update |
| `workflow.py` | 工作流 YAML 解析/校验、任务 ID 规范化、YAML 序列化（`dump_workflow_yaml`） | server、satellite/executor、tools |
| `scripting.py` | shell 命令封装（bash/sh/cmd/powershell）、`ExecutionResult` | server/executor、satellite/executor |
| `cron.py` | Linux 风格 cron 解析（`linux_cron_trigger`，0/7=周日） | server/service、tools/migrate_workflows |
| `package.py` | 更新归档工具：`make_archive`/`extract_archive`/`archive_name`/`UpdateError` | server/update、satellite/update、satellite/master |

## 子包

| 子包 | 说明 | 文档 |
| --- | --- | --- |
| `server/` | 主服务（Web、调度、执行、账号、更新协调） | [server/README.md](server/README.md) |
| `database/` | 状态存取（SQLite / MySQL 8） | [database/README.md](database/README.md) |
| `satellite/` | 卫星节点（代理、协议、加密、主机通道、更新） | [satellite/README.md](satellite/README.md) |
| `notifier/` | 通知包（邮件已实现，飞书等接口预留） | [notifier/README.md](notifier/README.md) |
| `watchdog/` | 进程看门狗与一次性更新看门狗 | [watchdog/README.md](watchdog/README.md) |
| `tools/` | 命令行工具（运行记录、外部导入、编辑预览） | [tools/README.md](tools/README.md) |

## 运行入口

- `python -m dagrunner.server`：主服务（由仓库根 `run_server.py` 包装）。
- `python -m dagrunner.satellite`：卫星代理（由 `run_satellite.py` 包装）。
- `python -m dagrunner.watchdog --role master|satellite`：进程看门狗。
- 其他 CLI 见 [tools/README.md](tools/README.md) 与根目录 `../README.md`。

## 目录约定

- 代码：本目录及子包；Web 资源在 `server/templates`、`server/static`。
- 本地数据：仓库根 `var/`（数据库、日志、证书、`versions/`、`updates/`）。
