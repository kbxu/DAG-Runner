[中文](README.md) | [English](README.en.md)

# Directed Acyclic Graph (DAG) Runner

轻量、跨平台的 Python DAG 工作流运行器：用 YAML 描述任务与依赖，一个常驻服务负责定时调度、执行、监控与日志。

![控制台总览](docs/images/dashboard.png)
![DAG 视图](docs/images/dag.png)
![运行记录](docs/images/runs.png)
![卫星节点](docs/images/satellite.png)

## 特性

- **轻量单机**：Flask + SQLite + APScheduler，不需要 Redis、消息队列或外部数据库；也可在配置里选用 MySQL 8
- **DAG 执行**：自动校验依赖与环路，并行执行就绪任务，支持停止、重跑、失败续跑
- **定时调度**：Linux 风格 cron（`0`/`7` 为周日），支持多条 cron 与任意时区
- **跨平台**：Linux 用 bash，Windows 用 cmd，也可指定 PowerShell
- **卫星节点**：在其它机器部署卫星版，主程序加密下发命令，支持前向/反向两种连接模式
- **Web 管理**：表单可视化编辑工作流与实时 DAG 预览、只读预览图右键执行单任务/子树、运行详情内嵌日志、多套皮肤切换（青绿/蓝/红/暗黑）与中英文界面
- **多渠道通知**：通知渠道（邮箱 SMTP、飞书自定义机器人）与工作流绑定规则全部存入数据库，支持配置任意数量渠道与一键测试发送
- **迁移导入**：支持 DAG Runner YAML、DolphinScheduler JSON、Windows 计划任务 XML 与单行 Crontab 导入，自动清理第三方多余字段并统一重新生成 ID（`workflow_<12hex>` / `task_<8hex>`）
- **标签分组**：工作流名称以 `【标签】` 或 `[标签]` 开头自动归类筛选
- **安全防护**：账号密码登录、失败封禁，卫星通信用 HTTPS + 令牌挑战-响应
- **进程看门狗**：主服务与卫星崩溃/假死自动重启，指数退避、邮件告警，更新时无缝接管版本切换

## 快速开始

```bash
conda create -n dagr python=3.11.* -y && conda activate dagr
python -m pip install -r requirements.txt
```

```bash
python -m dagrunner.server.auth --generate   # 创建 admin 账号，随机密码只显示一次
```

```bash
python -m dagrunner.server            # 启动服务，访问 http://127.0.0.1:7119
```

## 工作流

最小示例：

```yaml
description: 每日数据报表
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

### 运行位置

顶层 `runner` 是 workflow 级默认，任务级 `runner` 优先；都不写就是本机。

```yaml
runner: moon                     # workflow 级默认
tasks:
  on_satellite:
    command: echo hello
  local_check:
    command: echo check
    runner: local                # 覆盖为本地
```

### 字段速查

| 字段 | 说明 | 默认 |
| --- | --- | --- |
| `description` | 显示名称 | 文件名 |
| `setup` | 任务前的环境准备，可按平台分开写 | 空 |
| `timeout` | 整个 workflow 的超时（秒） | 不限 |
| `schedule.crons` | 多条 cron 表达式 | 无 |
| `schedule.timezone` | 时区 | `Asia/Shanghai` |
| `runner` | workflow 级默认运行位置 | `local` |
| `tasks.<id>.command` | 命令（字符串或数组） | 必填 |
| `tasks.<id>.args` | 附加参数 | 空 |
| `tasks.<id>.shell` | `bash` / `sh` / `cmd` / `powershell` | 按平台 |
| `tasks.<id>.depends` | 依赖任务 | 空 |
| `tasks.<id>.timeout` | 单任务超时（秒） | 不限 |
| `tasks.<id>.runner` | 该任务的运行位置 | 跟随 workflow |
| `tasks.<id>.condition` | 条件分支（`type: condition`） | 无 |

## 卫星节点

在目标机器部署卫星版，主程序把命令加密下发过去执行。三种网络拓扑都支持：

| 拓扑 | 可达性 | 模式 | 连接发起方 |
| --- | --- | --- | --- |
| 1 | 双向可达 | 反向（优先） | 卫星 |
| 2 | 仅主程序 → 卫星 | 前向 | 主程序 |
| 3 | 仅卫星 → 主程序 | 反向 | 卫星 |

**1. 主程序启用**（`var/config.yaml`）：

```yaml
satellite:
  enabled: true
  host: 0.0.0.0
  port: 7120
  cert: var/master-certs/cert.pem
  key: var/master-certs/key.pem
  public_url: ""      # 卫星回连地址，留空按 Web 请求 Host 推导
```

**2. 添加节点**：Web 控制台点「卫星」→ 新增，填别名、模式、URL（前向）、令牌（留空自动生成）；「检查连通」验证后「下载配置」。

**3. 启动卫星**：

```bash
python -m dagrunner.satellite --config var/satellite.yaml
```

**证书目录**：主程序用 `var/master-certs/`，卫星用 `var/satellite-certs/`（含信任记录 `known_master.json`）。迁移卫星直接拷 `var/satellite-certs/` 整个目录。

**版本一致**：主程序与卫星必须使用完全相同的 `dagrunner.__version__`。注册时校验，不一致就断开：反向模式卫星停止重连，前向模式任务直接失败，Web 上的「检查连通」也会提示版本不一致。

## 版本展示与更新

在右上角「⚙ 设置 → 更新」打开版本与更新面板（仅管理员可见），卫星面板同时展示各节点版本，版本不一致会提示告警。

**版本号来源**：`dagrunner/__init__.py::__version__`，`pyproject.toml` 通过 `dynamic version` 读取，发版 tag 必须与它一致。

**在线更新（仅支持 `git clone` 部署）**：

- 直接运行仓库根目录代码，由 `var/start.sh` / `var/start.bat`（卫星机为 `var/start-satellite.sh` / `var/start-satellite.bat`）拉起看门狗守护，不再使用 `var/versions/` 多版本目录；
- 发布包解压安装（目录无 `.git`）不支持在线更新，请手动替换文件；
- 更新流程（Web 手动点击，或命令行 `python -m dagrunner.server.update`）：
  1. 检查本地已跟踪文件有无未提交改动、有无未推送提交，以及有无运行中任务（任一不满足则不执行 pull）；
  2. 原地执行 `git merge --ff-only origin/<branch>` 拉取最新代码（pull 失败不影响当前运行中的服务）；
  3. 同步触发在线卫星机执行同样的检查、pull 与重启（离线卫星机后续重连遇到版本不一致时，若为 git 仓库且看门狗在线会自动拉取更新并重启）；
  4. 主服务写入重启意图后退出，由看门狗立即拉起新进程。

## 进程看门狗

主服务与卫星各自有一个常驻看门狗（独立 supervisor 进程），由启动脚本经 `run_tee.py` 拉起：

```bash
python -m dagrunner.watchdog --role master          # 通常由 var/start.bat / var/start.sh 调用
python -m dagrunner.watchdog --role satellite       # 通常由 var/start-satellite.bat / .sh 调用
python -m dagrunner.watchdog --status --role master  # 查看运行状态
```

- 子进程异常退出：立即按指数退避重启（`5s → 300s` 封顶），连续稳定运行后清零失败计数；
- 健康检查连续失败：**第 2 分钟发告警，满 5 分钟才判定假死并重启**，避免误杀长任务；
- 主动停止（`Ctrl+C` / `SIGTERM`）：先写 `intent=stop`，看门狗不重启并退出；
- 更新重启：服务 pull 成功后写 `intent=restart` 并退出，看门狗跳过退避立即拉起新进程；
- `mode: notify` 可切换为只告警不重启；单例锁防止重复守护。

默认配置见 `var/examples/config.example.yaml` 的 `watchdog` 段。**健康检查失败达到阈值时的自动重启会中断正在运行的任务**，必要时可用 `mode: notify` 人工兜底。

## 配置速查

| 配置项 | 说明 | 默认 |
| --- | --- | --- |
| `host` / `port` | Web 监听地址 | `127.0.0.1:7119` |
| `db` / `logs` | 数据库（SQLite 路径，或 MySQL 8 的 map 配置）与日志目录 | `var/scheduler.db` / `var/logs` |
| `threads` | Web 线程数 | `8` |
| `notifier_config` | 兼容旧版 `var/notifier.yaml`（首次启动若数据库无渠道会自动迁移，现统一在 Web 控制台管理并存库） | `null` |
| `allow_insecure_remote_login` | 允许 HTTP 登录（仅可信内网） | `false` |
| `satellite.enabled` | 启用卫星服务 | `false` |
| `satellite.host` / `port` | 卫星服务监听地址 | `0.0.0.0:7120` |
| `satellite.public_url` | 卫星回连主程序的地址 | 按请求推导 |
| `update.source` | 更新来源：`git` / `release` / `auto` | `auto` |
| `update.release.repo` | GitHub Release 仓库 | 空（用 git 源） |
| `update.branch.remote` / `name` | 分支模式的远端与分支名 | `origin` / `main` |
| `update.enabled` | 是否允许更新 | `true` |
| `maintenance.enabled` | 是否启用运行记录归档 | `true` |
| `maintenance.retention_days` | 归档保留期（天），超过的终态运行移入 `*_archive` 表 | `60` |
| `maintenance.hour` / `minute` | 每日归档执行时间 | `3:15` |
| `watchdog.enabled` | 是否启用看门狗（false 时启动脚本回退直跑） | `true` |
| `watchdog.interval` / `timeout` | 健康检查间隔 / 单次探测超时（秒） | `5` / `3` |
| `watchdog.alert_after` | 连续不健康多久后首次告警（秒） | `120` |
| `watchdog.unhealthy_after` | 连续不健康多久后才重启（秒） | `300` |
| `watchdog.backoff_base` / `backoff_max` | 重启退避基数 / 上限（秒） | `5` / `300` |
| `watchdog.mode` | `restart` 自动重启 / `notify` 只告警 | `restart` |
| `watchdog.alert_to` | 告警收件人列表，空则仅记日志 | 空 |

CLI（`python -m dagrunner`、`python -m dagrunner.server.auth`、`python -m dagrunner.server.accounts`、`python -m dagrunner.server.maintenance`、`python -m demo.init_demo`）加 `--db-config var/config.yaml` 即可复用上面的 `db` 配置；不传时仍用 `--db` 指定的 SQLite 文件。`python -m dagrunner` 只用于查看运行记录（`--list-runs` / `--status` / `--show-log`），运行工作流请通过 Web 控制台或定时调度。

### 运行记录归档

超过 `maintenance.retention_days`（默认 60 天）的**终态**运行记录，会由每日低峰任务分批移入 `workflow_runs_archive` / `task_runs_archive`。这两个表后端服务不读，只供需要时手动查询排查；也可用 `python -m dagrunner.server.maintenance --archive` 手动执行。日志文件不受影响。

## 常用命令

| 命令 | 说明 |
| --- | --- |
| `python -m dagrunner --list-runs` | 查看运行历史（所有工作流） |
| `python -m dagrunner --status --run-id <id>` | 查看某次运行的任务状态 |
| `python -m dagrunner --show-log --run-id <id> --task <task>` | 查看日志 |
| `python -m dagrunner.server.auth --generate` | 创建 / 重置登录账号（`--reset` 为同义入口） |
| `python -m dagrunner.server.accounts --list` | 查看账号与工作流权限 |
| `python -m dagrunner.server.accounts --add <用户名>` | 新建账号（`--password`、`--unrestricted` 可选） |
| `python -m dagrunner.server.accounts --grant <用户名> <工作流> <只读\|执行\|编辑>` | 授予工作流权限 |
| `python -m dagrunner.server.accounts --revoke <用户名> <工作流>` | 移除工作流权限 |
| `python -m dagrunner.server.maintenance --archive` | 手动归档超过保留期的运行记录 |
| `python -m dagrunner.watchdog --status --role master` | 查看看门狗运行状态 |
| `python -m dagrunner --version` | 打印版本号 |
| `python -m dagrunner.server.update --check` | 查看可用版本 |
| `python -m dagrunner.server.update --to <version>` | 命令行更新主程序与在线卫星 |
| `python -m dagrunner.server.update --role satellite --to <version>` | 单独更新卫星 |
| `python -m dagrunner.tools.migrate_workflows --source <ds\|windows-task-scheduler> ...` | 导入外部任务 |
| `python -m dagrunner.satellite --config <path>` | 启动卫星版 |
| `python -m demo.init_demo --reset` | 生成演示数据 |

## 模块文档

- [dagrunner 包与共享内核](dagrunner/README.md)
- [server 主服务](dagrunner/server/README.md)
- [database 状态存取](dagrunner/database/README.md)
- [satellite 卫星节点](dagrunner/satellite/README.md)
- [notifier 通知](dagrunner/notifier/README.md)
- [watchdog 进程看门狗](dagrunner/watchdog/README.md)
- [tools 命令行工具](dagrunner/tools/README.md)
- [看门狗设计文档](dagrunner/watchdog/DESIGN.md)

## 项目结构

```text
dagrunner/
  __init__.py       版本号              version.py      语义化版本比较
  install.py        版本化安装布局
  workflow.py       工作流解析（共用）  scripting.py   脚本封装（共用）
  cron.py           cron 解析（共用）   package.py     更新归档工具（共用）
  __main__.py       薄入口（python -m dagrunner）
  server/           主服务包：app / webapp / service / runner / executor /
                    pool / accounts / auth / logger / maintenance / update
                    （含 templates / static）
  database/         状态存取包：base / sqlite / mysql8（SQLite 与 MySQL 8）
  satellite/        卫星节点包：agent / 协议 / 加密 / 主机通道 / 更新
  watchdog/         进程看门狗（状态 / 健康 / 拉起 / 守护循环）+ restart 更新看门狗
  notifier/         通知包：邮件 SMTP 与飞书自定义机器人已实现，数据库多渠道配置与分发
  tools/            命令行工具：cli（运行记录）/ migrate_workflows（导入）/ workflow_editor
run_server.py       版本目录启动器     run_satellite.py 卫星启动器
demo/             示例工作流、任务脚本与演示数据初始化
docs/images/      README 截图
var/              数据库、日志、证书、版本化安装（本地数据）
```

## 安全

- Web 默认只监听 `127.0.0.1`；开放到公网请用反向代理加 HTTPS
- 卫星通道用自签名证书 + 指纹固定（首次自动固定，可改 pinned）与共享令牌挑战-响应
- 任务请以低权限账户运行
- 账号体系：用户名 `admin` 为管理员，负责账号管理、通知发件、卫星、版本更新等全局设置；其它账号按工作流单独授权（只读 / 执行 / 编辑），未授权的工作流不可见。旧账号升级后保留全部工作流权限
- 手动运行会记录触发用户；密码至少 16 位，登录失败 3 次封禁 10 分钟，禁用或重置密码会立即失效其会话
- `/health` 为看门狗探活端点，免登录，只返回版本、调度/数据库可用性与更新状态；管理端口仍请勿直接暴露公网

## License

[MIT](LICENSE)
