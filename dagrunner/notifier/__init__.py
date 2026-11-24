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
    load_email_config,
    load_notifier,
    save_email_config,
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
    "save_email_config",
]
