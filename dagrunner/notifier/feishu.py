"""Feishu (Lark) custom-bot notification channel."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request

from .base import Notifier, WatchdogEvent, WorkflowEvent


@dataclass(frozen=True)
class FeishuConfig:
    webhook_url: str
    secret: str = ""
    mention_mobiles: tuple[str, ...] = ()
    subject_prefix: str = "[DAG Runner]"
    timeout: float = 10.0


def build_feishu_sign(timestamp: str, secret: str) -> str:
    """Compute the Feishu custom-bot HMAC-SHA256 signature."""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def build_feishu_text(
    title: str,
    lines: list[str],
    *,
    mention_mobiles: tuple[str, ...] = (),
) -> str:
    body = [title, *lines]
    if mention_mobiles:
        mentions = " ".join(f"@{mobile}" for mobile in mention_mobiles if mobile)
        if mentions:
            body.append(mentions)
    return "\n".join(line for line in body if line is not None)


class FeishuNotifier(Notifier):
    """飞书自定义机器人通知（支持 Webhook + 可选密钥签名）。"""

    def __init__(self, config: FeishuConfig):
        self.config = config

    def on_workflow_success(self, event: WorkflowEvent) -> None:
        title = f"{self.config.subject_prefix} 工作流成功：{event.workflow_name}"
        lines = [
            f"状态：{event.status}",
            f"Run ID：{event.run_id}",
        ]
        self._send(build_feishu_text(title, lines, mention_mobiles=event.recipients or self.config.mention_mobiles))

    def on_workflow_failed(self, event: WorkflowEvent) -> None:
        title = f"{self.config.subject_prefix} 工作流失败：{event.workflow_name}"
        lines = [
            f"状态：{event.status}",
            f"Run ID：{event.run_id}",
        ]
        if event.error_message:
            lines.append(f"错误信息：{event.error_message}")
        for failure in event.failed_tasks[:5]:
            detail = f"- {failure.task_name}"
            if failure.exit_code is not None:
                detail += f" (exit {failure.exit_code})"
            if failure.error_message:
                detail += f": {failure.error_message}"
            lines.append(detail)
        self._send(build_feishu_text(title, lines, mention_mobiles=event.recipients or self.config.mention_mobiles))

    def on_watchdog_event(self, event: WatchdogEvent) -> None:
        title = f"{self.config.subject_prefix} WATCHDOG {event.action} ({event.role})"
        lines = [event.message]
        self._send(build_feishu_text(title, lines, mention_mobiles=self.config.mention_mobiles))

    def send_test(self, message: str = "这是一条来自 DAG Runner 的飞书通知测试消息。") -> None:
        title = f"{self.config.subject_prefix} 通知渠道测试"
        self._send(build_feishu_text(title, [message], mention_mobiles=self.config.mention_mobiles))

    def _send(self, text: str) -> None:
        payload: dict[str, object] = {
            "msg_type": "text",
            "content": {"text": text},
        }
        if self.config.secret:
            timestamp = str(int(time.time()))
            payload["timestamp"] = timestamp
            payload["sign"] = build_feishu_sign(timestamp, self.config.secret)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.config.webhook_url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError(f"飞书推送失败：{exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        code = data.get("code", data.get("StatusCode", 0))
        if code not in (0, None):
            msg = data.get("msg", data.get("StatusMessage", raw))
            raise RuntimeError(f"飞书返回错误（code={code}）：{msg}")
