# watchdog —— 进程看门狗

常驻 supervisor：监督主服务 / 卫星代理进程，崩溃、假死或代码更新后自动重启。

## 启动

```bash
python -m dagrunner.watchdog --role master      # 通常由 var/start.bat / var/start.sh 拉起
python -m dagrunner.watchdog --role satellite   # 通常由 var/start-satellite.bat / .sh 拉起
python -m dagrunner.watchdog --status --role master
python -m dagrunner.watchdog --restart --role master # 通知运行中的看门狗重启子服务
python -m dagrunner.watchdog --stop --role master    # 通知运行中的看门狗优雅停止并退出
python -m dagrunner.watchdog --once  --role master   # 只拉起并校验一次
```

## 模块

| 模块 | 职责 |
| --- | --- |
| `__main__.py` | CLI 入口（含 `--status` / `--once`） |
| `supervisor.py` | 守护循环、健康阈值计时、退避重启、信号与 intent、告警节流 |
| `health.py` | 主服务 `/health` 判定、卫星 HTTPS→TCP 探测、`HealthTracker` |
| `launch.py` | 直接从仓库根目录拉起/校验/优雅停止子进程与进程树清理（Windows `taskkill /T`） |
| `state.py` | 状态与意图文件（`run` / `stop` / `restart`）、单例锁、活跃度判断 |

包根 `__init__.py` 惰性导出，避免子模块导入加载整个 supervisor 栈。

## 关键行为

- **崩溃**：立即按指数退避重启（默认 `5s → 300s` 封顶），稳定运行后清零失败计数。
- **假死**：健康检查连续失败 **2 分钟发告警、满 5 分钟才重启**；中途恢复则清零并发 `recovered`。
- **主动停止**：服务退出前写 `intent=stop`，看门狗不重启并退出。
- **更新重启**：服务完成 `git pull --ff-only` 后写 `intent=restart` 并退出，看门狗跳过退避立即拉起新进程。
- **单例**：每角色一把锁（重复实例退出码 3）；端口已被健康实例占用时退出码 4。

## 配置（watchdog 段）

```yaml
watchdog:
  enabled: true                 # false 时启动脚本回退直跑
  mode: restart                 # restart | notify（只告警）
  interval: 5                   # 心跳/健康间隔（秒）
  timeout: 3                    # 单次探测超时
  alert_after: 120              # 首次告警阈值
  unhealthy_after: 300          # 重启阈值
  backoff_base: 5
  backoff_max: 300
  stable_window: 300
  max_consecutive_failures: 0   # 0 = 永不放弃
  stop_timeout: 20
  alert_to: []                  # 邮件收件人，空 = 仅日志
  alert_cooldown: 3600
  health: {require_scheduler: true, require_db: true}
```

状态文件：`var/watchdog-<role>.json`；锁：`var/locks/watchdog-<role>.lock`；日志：`var/logs/watchdog.log`。

完整设计与状态机见 [DESIGN.md](DESIGN.md)。
