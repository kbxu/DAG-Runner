"""Notification event types and the channel-agnostic interface.

All notification code should import from :mod:`dagrunner.notifier` instead of
talking to SMTP / webhooks directly.
"""
from __future__ import annotations

from abc import ABC
from dataclasses import dataclass


@dataclass(frozen=True)
class TaskFailure:
    task_name: str
    exit_code: int | None = None
    error_message: str | None = None
    log_file: str | None = None


@dataclass(frozen=True)
class WorkflowEvent:
    workflow_name: str
    run_id: str
    status: str
    recipients: tuple[str, ...]
    error_message: str | None = None
    failed_tasks: tuple[TaskFailure, ...] = ()


@dataclass(frozen=True)
class WatchdogEvent:
    """A supervisor lifecycle event: restart, recovery, failure, update."""

    role: str
    action: str
    message: str
    recipients: tuple[str, ...]
    version: str = ""
    pid: int | None = None
    exit_code: int | None = None
    restarts: int = 0


class Notifier(ABC):
    """Extension point for email, Feishu, WeCom, webhook notifications.

    Channels implement the hooks they support; the default no-op body keeps
    partial implementations safe.
    """

    def on_workflow_success(self, event: WorkflowEvent) -> None:
        pass

    def on_workflow_failed(self, event: WorkflowEvent) -> None:
        pass

    def on_watchdog_event(self, event: WatchdogEvent) -> None:
        pass


class NullNotifier(Notifier):
    """No-op notifier used when no channel is configured."""
