from __future__ import annotations

from abc import ABC
from dataclasses import dataclass


@dataclass(frozen=True)
class TaskEvent:
    workflow_name: str
    run_id: str
    task_name: str
    status: str
    log_file: str
    exit_code: int | None = None
    error_message: str | None = None


class Notifier(ABC):
    """Extension point for WeCom, Feishu, email, or webhook notifications."""

    def on_task_success(self, event: TaskEvent) -> None:
        pass

    def on_task_failed(self, event: TaskEvent) -> None:
        pass


class NullNotifier(Notifier):
    pass
