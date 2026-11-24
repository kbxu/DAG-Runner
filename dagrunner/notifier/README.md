# notifier —— 通知包

所有通知的统一入口。业务代码只依赖事件类型与 `Notifier` 接口，不直接触碰 SMTP/webhook。

```python
from dagrunner.notifier import WorkflowEvent, WatchdogEvent, load_notifier

notifier = load_notifier(var_dir="var")
notifier.on_workflow_failed(event)
notifier.on_watchdog_event(event)
```

## 模块

| 模块 | 职责 |
| --- | --- |
| `base.py` | 事件：`WorkflowEvent`、`WatchdogEvent`、`TaskFailure`；接口 `Notifier` / `NullNotifier` |
| `email.py` | 邮件通道（已实现）：`EmailConfig`、`EmailNotifier`、邮件正文构建 |
| `feishu.py` | 飞书通道：`FeishuConfig`、`FeishuNotifier` 接口已就绪，发送实现待补充（当前无操作） |
| `config.py` | 配置发现/解析/保存：`load_notifier`、`load_email_config`、`save_email_config` |
| `__init__.py` | 全量再导出，兼容旧 `from dagrunner.notifier import ...` 用法 |

## 事件与钩子

- `on_workflow_success(event)` / `on_workflow_failed(event)`：工作流结束（成功/失败）。
- `on_watchdog_event(event)`：看门狗 `unhealthy / restarted / recovered / give_up / update_ok / update_failed`。
- 钩子默认空实现；未配置通道时 `load_notifier()` 返回 `NullNotifier`。

## 配置（var/notifier.yaml）

```yaml
email:
  host: smtp.example.com
  port: 465
  from: dagrunner@example.com
  username: ""
  password: ""
  use_ssl: true
  starttls: false
  timeout: 10
  subject_prefix: "[DAG Runner]"
```

- 收件人不在通道配置里：工作流用各自 `notification.email`，看门狗用 `watchdog.alert_to`。
- `save_email_config()` 会保留配置中的其他通道字段，便于后续扩展。

## 扩展新通道

1. 新增 `xxx.py`：定义配置数据类与 `XxxNotifier(Notifier)`，实现需要的事件钩子；
2. 在 `config.py` 的加载逻辑中按配置构造（保留其他通道字段）；
3. 从 `__init__.py` 导出。调用方无需改动。
