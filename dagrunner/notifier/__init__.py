"""Notification package: one import point for every notification channel.

Channels:
    - :class:`EmailNotifier` (implemented, SMTP / SMTPS / STARTTLS)
    - :class:`FeishuNotifier` (interface ready, implementation pending)

Callers should depend on :class:`Notifier` / the ``load_notifier`` factory and
the event dataclasses here instead of using ``smtplib`` or webhooks directly.
"""
from __future__ import annotations

from .base import Notifier, NullNotifier, TaskFailure, WatchdogEvent, WorkflowEvent
from .config import (
    DEFAULT_CONFIG_NAMES,
    DatabaseNotifier,
    load_email_config,
    load_notifier,
    parse_feishu_config,
    save_email_config,
    send_test_channel,
    validate_channel_config,
)
from .email import (
    EmailConfig,
    EmailNotifier,
    build_email_message,
    build_watchdog_message,
)
from .feishu import FeishuConfig, FeishuNotifier

__all__ = [
    "DEFAULT_CONFIG_NAMES",
    "DatabaseNotifier",
    "EmailConfig",
    "EmailNotifier",
    "FeishuConfig",
    "FeishuNotifier",
    "Notifier",
    "NullNotifier",
    "TaskFailure",
    "WatchdogEvent",
    "WorkflowEvent",
    "build_email_message",
    "build_watchdog_message",
    "load_email_config",
    "load_notifier",
    "parse_feishu_config",
    "save_email_config",
    "send_test_channel",
    "validate_channel_config",
]
