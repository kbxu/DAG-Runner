# DAG Runner 看门狗机制设计

> 状态：设计方案（已实施）。范围：**主服务进程守护 + 卫星进程守护**；形态：**独立 supervisor 进程**；代码位于 **`dagrunner/watchdog/` 包**。实施状态：P1–P4 已完成，全部测试通过（242 项）。本文件是设计与实施说明，包级概览见同目录 [README.md](README.md)。

## 1. 背景与现状

当前项目只有一个「更新看门狗」`dagrunner/watchdog/restart.py`：它是一次性的，由 `server/update.py` / `satellite/update.py` 在提交更新时以分离进程拉起，负责等待旧进程退出、切换 `var/current`、启动新版本并校验，失败则回滚。

日常运行没有常驻守护：

- `var/start.bat` / `var/start-satellite.bat` 直接用 `run_tee.py` 前台运行服务；
- 进程一旦崩溃或假死，脚本只打印退出码然后停在窗口，**不会自动重启**；
- 服务器重启后也不会自动拉起服务。

本设计新增一个常驻的 `dagrunner.watchdog` 包，作为独立 supervisor 进程监督主服务与卫星进程。

## 2. 目标与非目标

### 目标

1. **进程存活**：子进程异常退出、被 OOM/外部杀死后自动重启。
2. **健康探测**：主服务通过 HTTP `/health` 探活；卫星通过本机 HTTPS `/api/satellite/v1/health` 探活。进程还在但不再响应（假死、端口卡住）时，**连续 5 分钟不健康才重启**（第 2 分钟先发告警，见 §6/§7）。
3. **区分主动停止与崩溃**：更新重启、`Ctrl+C` / `SIGTERM`、手动停止不触发自动重启；意外退出才重启。
4. **防雪崩**：指数退避 + 连续失败计数 + 告警节流，避免崩溃循环刷日志、打爆邮件。
5. **无缝接管更新**：与现有 `watchdog/restart.py` 更新流程协调，更新期间不出现「两个看守抢着拉进程」。
6. **复用现有设施**：版本化安装（`var/current`、`var/versions/<v>/`）、日志目录、邮件通知、账号体系均复用。

### 非目标（本期不做，可后续扩展）

- 调度漏跑检测 / 自动补跑（`ScheduleService` 已有 `missed_count`、`resume_and_reinstall` 基础）。
- 卫星离线告警（`satellite_manager` 已有在线状态，可后续加通知）。
- 注册为 Windows 服务 / systemd unit（需要额外打包与权限，建议作为收尾项）。
- 多实例、负载均衡、集群。
- 看门狗自身的双进程互保，以及启动脚本外层循环兜底（评审已明确不做，见 §12）。

## 3. 总体架构

主程序与卫星各自运行一个看门狗进程，互相独立：

```text
var/start.bat                       var/start-satellite.bat
      |                                   |
      v                                   v
run_tee.py                          run_tee.py
      |                                   |
      v                                   v
python -m dagrunner.watchdog        python -m dagrunner.watchdog
      --role master                       --role satellite
      |  ^                                |  ^
      |  | health /health                 |  | health /api/satellite/v1/health
      v  |                                v  |
run_server.py (child)              run_satellite.py (child)
```

要点：

- 看门狗在原来的控制台窗口里前台运行（沿用 `run_tee.py`），控制台保持打开，输出仍是「窗口 + `var/logs/*.log`」双份。
- 子进程由看门狗启动，日志继续写入既有 `var/logs/server.{out,err}.log` / `satellite.{out,err}.log`；看门狗自己的日志写 `var/logs/watchdog.log`（并由 `run_tee` 镜像到 `watchdog.out/err.log`）。
- 看门狗与业务进程完全解耦：业务代码只多了一个「退出前写 intent」的动作。

## 4. 组件与改动清单

看门狗全部收在 `dagrunner/watchdog/` 包内，避免 `dagrunner/` 根目录继续膨胀：

| 文件 | 作用 |
| --- | --- |
| `dagrunner/watchdog/__init__.py` | 包声明、对外导出 |
| `dagrunner/watchdog/__main__.py` | `python -m dagrunner.watchdog` 入口（参数解析、`--status` / `--once`） |
| `dagrunner/watchdog/state.py` | 状态/控制文件原子读写、intent 读写、活跃度判断 |
| `dagrunner/watchdog/health.py` | 主服务 / 卫星健康探测与判定（含阈值计时） |
| `dagrunner/watchdog/launch.py` | 子进程拉起、校验、优雅停止与进程树清理（供看门狗与 `watchdog/restart.py` 共用） |
| `dagrunner/watchdog/supervisor.py` | 主循环、退避重启、信号处理、更新交接 |

配套改动：

| 文件 | 改动 |
| --- | --- |
| `dagrunner/watchdog/restart.py` | 重构为复用 `watchdog/launch.py`；保留一次性更新看门狗能力（未启用看门狗时仍用它） |
| `dagrunner/server/app.py` | 信号处理器退出前调用 `watchdog.state.request_stop("master")` |
| `dagrunner/satellite/agent.py` | 信号处理器退出前调用 `watchdog.state.request_stop("satellite")` |
| `dagrunner/server/update.py` | 检测到活跃看门狗时改为「写交接请求 + 退出」，否则维持现有 `watchdog/restart.py` 流程 |
| `dagrunner/satellite/update.py` | 同上（卫星角色） |
| `dagrunner/webapp.py` | `/health` 增加 `db_ok`、`active_runs` 字段；`_schedule_restart` 无需改动 |
| `dagrunner/database/base.py` | 新增 `ping()`（`SELECT 1`） |
| `dagrunner/notifier/` | 新增 `WatchdogEvent` 与 `Notifier.on_watchdog_event()` 默认空实现 + 邮件构建（通知已拆为独立包） |
| `var/examples/*`、`var/*.bat`、`var/*.sh`、`README.md` | 配置项与启动脚本改造 |

## 5. 控制协议与状态机

### 5.1 文件

| 路径 | 作用 |
| --- | --- |
| `var/watchdog-<role>.json` | 运行时状态 + intent，原子写 |
| `var/locks/watchdog-<role>.lock` | 单例锁（复用 `workflow_lock` 的 msvcrt/flock 实现） |
| `var/updates/handoff.json` | 更新交接请求（仅更新流程写，看门狗消费后删除） |

`var/watchdog-<role>.json` 示例：

```json
{
  "role": "master",
  "watchdog_pid": 1234,
  "status": "running",
  "intent": "run",
  "version": "0.3.0",
  "child_pid": 5678,
  "started_at": "2026-11-24T10:00:00+08:00",
  "last_heartbeat": "2026-11-24T10:05:00+08:00",
  "restart_count": 2,
  "consecutive_failures": 0,
  "unhealthy_since": null,
  "last_exit_code": null,
  "last_error": "",
  "updated_at": "2026-11-24T10:05:00+08:00"
}
```

字段语义：

- `intent`：`run`（默认，允许拉起）/ `stop`（主动停止，不要重启）/ `update`（更新交接中）。
- `status`：`starting` / `running` / `restarting` / `updating` / `stopped` / `give_up`。
- `consecutive_failures`：最近一轮连续**启动失败**次数；子进程稳定运行超过 `stable_window` 后清零。
- `unhealthy_since`：本轮健康检查连续失败的起点；恢复健康即置空。
- `last_heartbeat`：由看门狗主循环每 `interval` 秒原子刷新。

### 5.2 状态迁移

```text
                  ┌──────────────┐
   启动看门狗 ───▶│  starting    │
                  └──────┬───────┘
                         │ 拉起子进程成功且健康
                         ▼
                  ┌──────────────┐   子进程死亡/不健康超时，intent=run
        ┌────────│   running    │──────────────────────┐
        │        └──────┬───────┘                      ▼
        │               │ 子进程死亡，intent=stop        ┌────────────┐
        │               │ 且无 handoff 文件             │ restarting │
        │               ▼                                └─────┬──────┘
        │        ┌──────────────┐                              │ 退避后按原版本拉起
        │        │   stopped    │◀─────────────────────────────┘
        │        └──────────────┘
        │               ▲
        │               │ 子进程死亡，intent=stop 且有 handoff
        │               ▼
        │        ┌──────────────┐   activate(previous)+回滚  ┌────────────┐
        └───────▶│   updating   │─────────────────────────────▶│ give_up /  │
                 └──────┬───────┘                              │ rollback   │
                        │ 新版本拉起并校验通过                  └────────────┘
                        ▼
                 ┌──────────────┐
                 │   running    │
                 └──────────────┘
```

判定规则（子进程结束时）：

| intent | `handoff.json` | 看门狗动作 |
| --- | --- | --- |
| `run` | 无 | 意外退出 → 记事件 → 退避 → 原版本重启 |
| `stop` | 无 | 主动停止 → 置 `stopped` → **看门狗退出，不重启** |
| `stop` / `update` | 有 | 更新交接 → 切版本、启动、校验，失败回滚 → 完成后删 handoff、置 `run` |

进程存活但健康检查失败不走上面的结束分支，而是走 §6 的阈值策略：累计不健康达 `alert_after`（默认 2 分钟）发告警，达 `unhealthy_after`（默认 5 分钟）才执行重启。

### 5.3 intent 的写入时机

- `server/app.py` / `satellite/agent.py` 的信号处理器：先原子写 `intent=stop`，再走原有退出逻辑。
- 看门狗：启动子进程前写 `intent=run`；重启/交接期间写 `restarting` / `updating`。
- 更新服务：提交/回滚时写 `handoff.json`，然后按原逻辑让进程退出（`intent` 由进程自己写成 `stop`）。

这样即使进程是被 `os._exit(0)` 硬杀，intent 也已落盘，看门狗不会误判为崩溃。

### 5.4 单例与活跃度

- 启动时尝试 `workflow_lock(var/locks/watchdog-<role>.lock)`；拿不到锁说明已有看门狗，直接退出（退出码 3）。
- 「是否有活跃看门狗」的判断（供更新流程查询）：锁被占用 **且** `last_heartbeat` 在 `interval * 3 + 5s` 内。锁遗留但心跳过期的，视为无看门狗，更新流程回退到 `watchdog/restart.py`。
- 看门狗启动时若发现端口已被占用且 `/health` 正常响应，说明有一个未被托管的实例在跑：记录错误并退出（退出码 4），避免双实例写库。

### 5.5 分支更新模式（git clone / 开发部署）

针对从 git 仓库克隆运行的实例，新增 `action: "sync_branch"` 交接，用于「检查远端分支 → 确认 → 拉取 → 重启」：

- **检测**（`package.git_branch_status`）：`fetch` 后对比本地 HEAD 与 `<remote>/<name>`，得到 `local_sha/remote_sha/behind/ahead/dirty/latest_subject`；工作区不干净或本地领先时不可执行。
- **提交路径**：服务器只做预检并写 handoff（`ref/remote/expected_sha`），随后退出；不落版本目录、不打包。
- **看门狗执行**：`git fetch` → 校验 `expected_sha` 未漂移 → 要求工作区干净且 `ahead=0` → `git merge --ff-only` → 重启子进程。
- **结果记录**：成功写 `restart.status=running`，失败（dirty/分叉/远端变化/版本化安装）写 `failed` 并**用旧代码重启**，服务不中断；通知 `update_ok/update_failed`。
- **边界**：仅适用于运行目录本身是 git clone（无 `var/current` 版本化安装）；发布包安装继续使用 release/tag 手动更新。卫星在面板上展示是否具备分支条件（`.git`+远端），实际同步为下一期。

## 6. 重启策略

### 6.1 进程死亡（立即进入退避）

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `backoff_base` | 5s | 首次重启延迟 |
| `backoff_max` | 300s | 退避上限 |
| `stable_window` | 300s | 子进程稳定运行超过该时长则清零连续失败计数 |
| `max_consecutive_failures` | 0 | 0 = 永不放弃，一直以 `backoff_max` 重试；>0 时达到阈值进入 `give_up` 并告警 |
| `stop_timeout` | 20s | 停止子进程时先宽限，超时再强杀 |

退避公式：`delay = min(backoff_base * 2^(consecutive_failures-1), backoff_max)`。

### 6.2 健康检查失败（延迟确认，避免误杀）

默认 `interval=5s` 的前提下：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `alert_after` | 120s | 连续不健康累计达到 2 分钟，发出本次事件的第一封告警 |
| `unhealthy_after` | 300s | 连续不健康累计达到 5 分钟，才判定假死并重启 |

计时规则：

- 每次健康检查失败累加 `interval`，成功则清零并关闭本轮事件；
- 到达 `alert_after` 发一次 `unhealthy` 告警（同一轮只发一次；恢复健康发 `recovered`）；
- 到达 `unhealthy_after` 执行重启：先优雅停止（`stop_timeout`），再等待端口释放并按原版本拉起；
- `unhealthy_after` 必须大于 `alert_after`，且 `alert_after >= interval`，配置校验时强制；
- 该阈值只用于「进程还在但不响应」的场景；进程直接死亡仍按 §6.1 立即退避重启；
- 子进程以 `intent=stop` 退出时，看门狗还会最多等待 `handoff_wait`（默认 15 秒）用于接收更新交接请求，避免更新与手动停止的竞态误判。

清理动作：只清理自己启动的 `child_pid` 及其子进程，绝不按端口盲杀未知进程。Windows 用 `taskkill /PID <pid> /T /F`（必须带 `/T`，防止用户任务的孙子进程残留），Unix 杀进程组。

## 7. 健康检查

### 7.1 主服务

`GET http://<host>:<port>/health`（`0.0.0.0`/`::` 归一化为 `127.0.0.1`）。

扩展后的响应：

```json
{
  "ok": true,
  "version": "0.3.0",
  "scheduler_running": true,
  "db_ok": true,
  "active_runs": 2,
  "update": {"active": false, "state": "idle"}
}
```

单次判定不健康的条件（可配置）：

- 连接失败 / 超时 / 非 2xx；
- `ok != true`；
- `require_scheduler=true` 且 `scheduler_running=false`（捕获调度线程假死）；
- `require_db=true` 且 `db_ok=false`（捕获数据库损坏/连接耗尽）。

`db_ok` 由新增的 `BaseStateDatabase.ping()` 实现（`SELECT 1`），开销可忽略。

### 7.2 卫星

优先 `GET https://127.0.0.1:<listen_port>/api/satellite/v1/health`，使用不校验证书的 SSL context（仅本机访问自签证书）。证书/路径异常时退化为 TCP `connect_ex(listen_port)` 探测。卫星没有 HTTP 服务时（极早期启动阶段）视为「正在启动」，连续多个周期都探测不到才判不健康，同样受 §6.2 阈值约束。

### 7.3 说明

5 分钟阈值是刻意的保守设计：避免网络抖动、备份占锁、重启瞬间被误判为假死而中断正在运行的任务。代价是真实假死的发现时间最长为 5 分钟；如果进程还在但任务已经卡死，由任务级 `timeout` 负责兜底，不看门狗重复处理。

## 8. 通知

复用 `notifier_config` 指向的邮件配置，新增看门狗事件通道：

```python
@dataclass(frozen=True)
class WatchdogEvent:
    role: str                  # master | satellite
    action: str                # unhealthy | restarted | recovered | give_up | update_ok | update_failed
    message: str
    recipients: tuple[str, ...]
    version: str = ""
    pid: int | None = None
    exit_code: int | None = None
    restarts: int = 0
```

- `Notifier` 新增 `on_watchdog_event(event)`，默认空实现，旧通知插件不受影响；`EmailNotifier` 实现邮件发送。
- 收件人来自配置 `watchdog.alert_to`（列表）。为空则只写日志、不发信。
- 时机与节流：
  - 健康连续失败满 `alert_after`（2 分钟）：发 `unhealthy` 告警，同一轮只发一次；
  - 达到 `unhealthy_after` 触发重启：发 `restarted`；
  - 恢复健康：发 `recovered`；
  - 崩溃重启：第一次发 `restarted`，之后每 `alert_cooldown`（默认 1 小时）最多提醒一次；
  - 更新交接成功/失败：发 `update_ok` / `update_failed`。
- 所有事件同时写入 `var/logs/watchdog.log`（含时间戳、角色、动作、原因、版本、PID、退出码）。

## 9. 配置

`var/config.yaml`（主程序）/ `var/satellite.yaml`（卫星）新增段：

```yaml
watchdog:
  enabled: true
  mode: restart                 # restart | notify（notify 只告警，不重启）
  interval: 5                   # 健康检查间隔
  timeout: 3                    # 单次健康探测超时
  alert_after: 120              # 连续不健康 2 分钟：首次告警
  unhealthy_after: 300          # 连续不健康 5 分钟：判定假死并重启
  handoff_wait: 15              # 主动停止后等待更新交接请求的宽限（秒）
  backoff_base: 5
  backoff_max: 300
  stable_window: 300
  max_consecutive_failures: 0   # 0 = 永不放弃
  stop_timeout: 20
  alert_to: []                  # 告警收件人；空 = 仅记日志
  alert_cooldown: 3600
  health:
    require_scheduler: true     # 仅 master 有意义
    require_db: true
```

说明：

- 看门狗按 `--role` 读取对应配置文件里的 `watchdog` 段；`enabled: false` 时看门狗立即退出，启动脚本回退为直接运行子进程（开发模式）。
- 卫星段里的 `require_scheduler` 忽略（卫星没有调度器）。
- `unhealthy_after > alert_after > interval` 由配置校验保证，否则启动报错退出。
- 参数不支持热加载，改配置后重启看门狗生效。

## 10. CLI 与启动脚本

```bash
# 启动守护（通常由 start.bat / start.sh 调用）
python -m dagrunner.watchdog --role master   --root <root> [--config var/config.yaml]
python -m dagrunner.watchdog --role satellite --root <root> [--config var/satellite.yaml]

# 查看状态
python -m dagrunner.watchdog --status --role master --root <root>

# 只拉起一次并校验后退出（排障/测试用）
python -m dagrunner.watchdog --once --role master --root <root>
```

启动脚本改造（`var/start.bat` 关键部分）：

```bat
REM 先尝试看门狗；watchdog.enabled=false 或看门狗不可用时回退直跑
"%DAGR_PYTHON%" "%ROOT%\run_tee.py" "%LOG_WD_OUT%" "%LOG_WD_ERR%" ^
    "%DAGR_PYTHON%" -m dagrunner.watchdog --role master --root "%ROOT%"
if errorlevel 5 goto direct
goto :eof
:direct
...原有直跑逻辑...
```

退出码约定：

| 码 | 含义 | 启动脚本行为 |
| --- | --- | --- |
| 0 | 正常结束（主动停止） | 提示已停止 |
| 1 | 内部错误 | 打印日志路径，提示人工检查 |
| 2 | 配置错误 / 用法错误 | 提示检查 `var/config.yaml` |
| 3 | 已有看门狗实例（单例锁冲突） | 提示已在运行 |
| 4 | 端口已被健康实例占用 | 提示已有未托管实例 |
| 5 | `watchdog.enabled=false` | **回退为直接运行子进程** |

`start.sh` / `start-satellite.bat` / `start-satellite.sh` 同构改造。看门狗内部用 `resolve_code_dir(root)` 决定代码目录，`var/current` 存在时用 `var/versions/<v>/run_*.py`，否则用仓库根目录（开发模式），与现有逻辑一致。

## 11. 风险与边界

1. **看门狗自身崩溃**：评审已明确不做外层循环兜底、不做双进程互保。缓解措施：逻辑保持极简、顶层 `try/except` 兜底、状态文件记录崩溃点；残余风险是看门狗挂掉后需人工重启，接受此风险。
2. **更新交接竞争**：交接依赖 `handoff.json` + intent。若看门狗在交接途中挂掉，`handoff.json` 会残留；实现上加 TTL（默认 10 分钟）与「一次性消费即删除」，过期后由下一次看门狗忽略，更新流程侧的回滚/重试不受影响。
3. **端口被非本看门狗管理的实例占用**：看门狗启动时探测到健康实例直接报错退出，不抢端口、不杀未知进程。
4. **`os._exit` 与 intent 落盘的先后**：必须先写 intent 再退出；信号处理器里写完 intent 后仅做 0.5s 宽限（现有逻辑）即退出，窗口足够。
5. **假死发现延迟**：健康失败需 5 分钟才重启，期间靠 `unhealthy` 告警提醒人工介入；这是为避免误杀长任务所做的权衡。
6. **Windows 进程树**：强杀必须带 `/T`，否则孙子进程（用户任务）会残留；这也是现有 `run_tee.py` 场景需要注意的点。
7. **升级兼容**：已部署环境需要更换启动脚本与示例配置才能启用；老脚本仍可直接运行服务（看门狗不存在时更新流程完全不变）。

## 12. 决策记录（已确认）

| # | 决策点 | 结论 |
| --- | --- | --- |
| 1 | 更新流程是否由看门狗统一接管？ | **是**：`server/update.py` / `satellite/update.py` 检测活跃看门狗后写 `handoff.json` 并退出，由看门狗完成切版/校验/回滚；无看门狗时仍走 `watchdog/restart.py` |
| 2 | 主动停止后看门狗是否退出？ | **退出**：需要再运行时重新执行启动脚本，避免「关不掉」的守护 |
| 3 | 健康检查失败的处理节奏？ | **连续 5 分钟失败才重启；第 2 分钟发出首次告警**（恢复健康则计时清零并发 `recovered`） |
| 4 | 是否现在就改启动脚本？ | **是**，同时提供 `watchdog.enabled=false` 回退直跑 |
| 5 | 告警收件人放哪？ | 各角色的 `watchdog.alert_to`；本期不做 Web 配置页 |
| 6 | 是否加外层启动脚本循环兜底？ | **不做**（看门狗自身崩溃靠顶层异常兜底，风险接受） |
| 7 | 是否加 Web「停止服务」按钮？ | **不做**（`intent=stop` 已由信号处理器覆盖，Web 停止留作后续） |

## 13. 测试计划

沿用 `unittest` 风格，全部在临时目录中进行，真实拉起短命子进程：

1. 子进程崩溃后按原版本重启，`restart_count` 递增；
2. 健康检查连续失败：满 `alert_after`（2 分钟）发出一次 `unhealthy` 告警；满 `unhealthy_after`（5 分钟）才重启；期间任一时刻恢复健康则计时清零并取消重启，恢复后发 `recovered`；
3. `mode: notify` 时健康失败只告警、不重启；
4. 子进程稳定运行超过 `stable_window` 后失败计数清零；
5. 退避上限生效（连续失败时延迟封顶 `backoff_max`）；
6. `intent=stop` 时看门狗不重启并退出（退出码 0）；
7. 有 `handoff.json` 时按新版本目录拉起，成功后删除 handoff；新版本启动失败时回滚到 `previous`；
8. 第二个看门狗实例因单例锁退出（退出码 3）；
9. 端口被健康实例占用时退出（退出码 4）；
10. 看门狗收到 `SIGINT/SIGTERM` 时终止子进程并退出；
11. `--once` 只拉起并校验一次；
12. `--status` 正确打印状态 JSON；
13. 状态文件心跳与 `unhealthy_since` 字段随时间正确更新；
14. 延迟到达的 `handoff.json` 在 `handoff_wait` 宽限内仍被消费并切版本；
15. 默认探测按配置读取 host/port（master HTTP、卫星 HTTPS→TCP 回退）。

## 14. 实施阶段

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| P1 | `watchdog/state.py`、`watchdog/health.py`、`watchdog/launch.py`、`watchdog/supervisor.py`、`watchdog/__main__.py`：单例、拉起、健康阈值、退避重启、信号与 intent | ✅ 已完成 |
| P2 | 更新交接：`server/update.py` / `satellite/update.py` 检测活跃看门狗并写 handoff；`watchdog/restart.py` 改用 Windows 安全进程检查 | ✅ 已完成 |
| P3 | 通知、`/health` 扩展、`ping()`、启动脚本与示例配置、README | ✅ 已完成 |
| P4 | 测试补齐（`tests/test_watchdog.py`、`tests/test_update.py`）、文档定稿 | ✅ 已完成 |

## 15. 验收标准

- 手动 `taskkill` 主服务进程后，看门狗在 `interval + backoff` 内重新拉起，`/health` 恢复，`watchdog.log` 有记录；
- 健康检查持续失败时：第 2 分钟收到 `unhealthy` 告警，满 5 分钟才重启；中途恢复则不清零误重启，并收到 `recovered`；
- 更新提交后，版本切换、卫星先重启、主机后重启的行为与现状一致，且不出现双进程；
- `Ctrl+C` / `taskkill /PID <watchdog>` 能干净停止看门狗与子进程，再次启动不残留；
- 连续崩溃时退避封顶、日志与邮件不刷屏；
- 无看门狗场景（开发模式、老部署）行为完全不变。
