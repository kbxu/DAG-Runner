# tools —— 命令行工具

不属于常驻服务的 CLI 集合。

## 运行记录查看（cli.py）

```bash
python -m dagrunner --list-runs
python -m dagrunner --status --run-id <id>
python -m dagrunner --show-log --run-id <id> --task <task>
```

包根 `__main__.py` 是薄入口，实际实现在 `tools/cli.py`（复用 `server/runner.py` 的 CLI）。

## 外部任务导入（migrate_workflows.py）

把外部调度系统的导出转换成可评审的 YAML 工作流（只读 JSON/XML、只写 YAML，从不执行命令）：

```bash
python -m dagrunner.tools.migrate_workflows --source dolphinscheduler --input export.json --output-dir workflows/
python -m dagrunner.tools.migrate_workflows --source windows-task-scheduler --input Task.xml --output-dir workflows/
```

常用参数：`--setup-file`（环境准备片段）、`--timezone`、`--exclude-disabled`。cron 解析复用根级 `dagrunner.cron`；YAML 序列化复用 `workflow.dump_workflow_yaml`。

## 编辑预览（workflow_editor.py）

给 Web 控制台提供“未保存工作流”的校验/扩展辅助（`demo_setup`、`demo_command`、`preview_definition`、`extend_definition`），被 `server/webapp.py` 调用，一般不单独执行。

## 约定

- 工具之间不互相依赖业务包；需要共享能力时引用根级共享模块（`workflow` / `cron` / `package`）。
- 新增工具直接放入本目录，并在根 `../README.md` 命令表中登记。
