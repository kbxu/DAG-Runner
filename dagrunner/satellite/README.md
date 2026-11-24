# satellite —— 卫星节点

在目标机器上执行任务的代理：注册到主程序、接收加密下发的命令、本地执行并回传日志/结果。

## 启动

```bash
python -m dagrunner.satellite --config var/satellite.yaml
# 或版本化安装：run_satellite.py
```

## 网络拓扑与模式

| 模式 | 说明 |
| --- | --- |
| `auto` | 优先反向连接主程序，不可达则监听前向连接 |
| `reverse` | 仅主动连接主程序（卫星能访问主程序端口） |
| `forward` | 仅监听，等主程序连接（主程序能访问卫星端口） |

配置示例见 `../../var/example_configs/satellite.example.yaml`；证书放 `var/satellite-certs/`（迁移整机拷目录）。

## 模块

| 模块 | 职责 |
| --- | --- |
| `cli.py` / `__main__.py` | 入口与配置解析（`alias/master_url/token/mode/证书/心跳`） |
| `agent.py` | 代理生命周期：HTTPS 服务、反向注册/SSE 流、任务执行、优雅关闭、信号 intent |
| `master.py` | 主机侧通道：在线状态、下发任务、更新下发与提交（被 server/webapp 使用） |
| `http.py` | 挑战-响应认证 HTTP 客户端/请求封装 |
| `crypto.py` | 自签证书、指纹固定、TLS 上下文、信任存储 |
| `protocol.py` | 常量与编解码：API 路径、头、作业规格 |
| `server.py` | 基于 waitress 的 HTTPS 服务线程 |
| `executor.py` | 卫星侧任务执行器 `SatelliteTaskExecutor`（被 server/service 调用） |
| `update.py` | 卫星侧更新：下载、sha256 校验、暂存、交接/重启 |

## 约束

- 主程序与卫星必须使用完全相同的 `dagrunner.__version__`；不一致拒绝连接（更新宽限期内例外）。
- 通信用自签证书 + 指纹固定（`accept-new` 或 `pinned`）+ 共享令牌挑战-响应。
- 更新时卫星先重启、主机后重启；有活跃任务时拒绝更新。

## 依赖方向

`satellite → database(仅类型/协议) / workflow / scripting / package / install / cron`，不依赖 `server/`（归档工具来自根级 `package.py`）。
