# watchdog —— 进程看门狗

常驻 supervisor：监督主服务 / 卫星代理进程，崩溃或假死时自动重启；更新时接管版本切换。另有 `restart.py` 作为未启用看门狗时的一次性更新看门狗。

## 启动

```bash
python -m dagrunner.watchdog --role master      # 通常由 var/start.bat / start.sh 拉起
python -m dagrunner.watchdog --role satellite
python -m dagrunner.watchdog --status --role master
python -m dagrunner.watchdog --once  --role master   # 只拉起并校验一次
```

## 模块

| 模块 | 职责 |
| --- | --- |
| `__main__.py` | CLI 入口（含 `--status` / `--once`） |
| `supervisor.py` | 守护循环、健康阈值计时、退避重启、信号与 intent、更新交接、告警节流 |
| `health.py` | 主服务 `/health` 判定、卫星 HTTPS→TCP 探测、`HealthTracker` |
| `launch.py` | 子进程拉起/校验/优雅停止与进程树清理（Windows `taskkill /T`） |
| `state.py` | 状态与意图文件、单例锁、handoff 读写与 TTL、活跃度判断 |
| `restart.py` | 一次性更新看门狗（等待旧进程退出→切版本→校验→失败回滚） |

包根 `__init__.py` 惰性导出，避免分离子进程加载整个 supervisor 栈。

## 关键行为

- **崩溃**：立即按指数退避重启（默认 `5s → 300s` 封顶），稳定运行后清零失败计数。
- **假死**：健康检查连续失败 **2 分钟发告警、满 5 分钟才重启**；中途恢复则清零并发 `recovered`。
- **主动停止**：服务退出前写 `intent=stop`，看门狗不重启；若在 `handoff_wait` 内出现 `var/updates/handoff.json` 则执行更新交接。
- **单例**：每角色一把锁（重复实例退出码 3）；端口已被健康实例占用时退出码 4。
- **更新**：`server/update.py` 与 `satellite/update.py` 检测活跃看门狗后写 handoff 并退出，由看门狗完成切版/校验/回滚；否则使用 `restart.py`。

## 配置（watchdog 段）

```yaml
watchdog:
  enabled: true                 # false 时启动脚本回退直跑
  mode: restart                 # restart | notify（只告警）
  interval: 5                   # 心跳/健康间隔（秒）
  timeout: 3                    # 单次探测超时
  alert_after: 120              # 首次告警阈值
  unhealthy_after: 300          # 重启阈值
  handoff_wait: 15              # 停止后等待交接请求的宽限
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
