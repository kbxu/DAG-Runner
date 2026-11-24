# database —— 状态存取

统一的状态存储层，支持 SQLite（默认）与 MySQL 8。业务表与归档表都在这一层维护。

## 使用

```python
from dagrunner.database import create_state_database, load_database_config

db = create_state_database(load_database_config("var/config.yaml"))
```

- `create_state_database(config)`：字符串/路径 → SQLite；映射按 `type` 选择后端（缺省 sqlite，可用 `path`）。
- `load_database_config(path)`：从 `var/config.yaml` 读取 `db`（含 `state_dir` 转发）。
- `reset_state_database(config)`：删除 SQLite 文件或重建 MySQL 库（测试用）。

## 模块

| 模块 | 职责 |
| --- | --- |
| `__init__.py` | 对外出口：`StateDatabase`、`Mysql8StateDatabase`、`BaseStateDatabase`、`create_state_database` 等 |
| `base.py` | 抽象基类：schema/迁移、连接管理 `connect()`、健康探测 `ping()`、用户/会话/登录失败、工作流与计划、卫星配置、运行记录与归档表 |
| `sqlite.py` | SQLite 实现（`StateDatabase`），本地文件 + WAL/超时处理由基类约定 |
| `mysql8.py` | MySQL 8 实现（pymysql，DictCursor） |

## 约定

- **state_dir**：锁文件、通知状态等本地文件目录；SQLite 为库文件所在目录，MySQL 配置可显式指定。
- **连接**：每次操作通过 `connect()` 上下文获取连接，自动 commit/rollback/close。
- **健康检查**：`ping()` 执行 `SELECT 1`，供看门狗 `/health` 使用。
- **归档表**：`workflow_runs_archive` / `task_runs_archive` 只供排查，后端服务不读。

## 配置示例

```yaml
db: var/scheduler.db          # 默认 SQLite
# db: {type: mysql8, host: 127.0.0.1, port: 3306, user: dagrunner,
#      password: "", database: dagrunner, charset: utf8mb4, state_dir: var}
```
