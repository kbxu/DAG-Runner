"""Feishu (Lark) custom-bot notification channel.

The interface is ready; the actual signing / push implementation is left as a
TODO so the rest of the code can treat Feishu like any other Notifier.
"""
from __future__ import annotations

from dataclasses import dataclass

from .base import Notifier, WatchdogEvent, WorkflowEvent


@dataclass(frozen=True)
class FeishuConfig:
    webhook_url: str
    secret: str = ""
    mention_mobiles: tuple[str, ...] = ()
    subject_prefix: str = "[DAG Runner]"


class FeishuNotifier(Notifier):
    """飞书机器人通知。配置与接口已就绪，发送实现待补充（当前为无操作）。"""

    def __init__(self, config: FeishuConfig):
        self.config = config

    def on_workflow_success(self, event: WorkflowEvent) -> None:
        self._notify(event.workflow_name, event.status)

    def on_workflow_failed(self, event: WorkflowEvent) -> None:
        self._notify(event.workflow_name, event.status)

    def on_watchdog_event(self, event: WatchdogEvent) -> None:
        self._notify(f"WATCHDOG {event.action}", event.role, event.message)

    def _notify(self, title: str, status: str, detail: str = "") -> None:
        # TODO: 实现飞书自定义机器人（webhook + 可选 secret 签名）消息推送。
        return None
