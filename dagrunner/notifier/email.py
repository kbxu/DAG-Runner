"""Email notification channel (SMTP / SMTPS / STARTTLS)."""
from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage

from .base import Notifier, WatchdogEvent, WorkflowEvent


@dataclass(frozen=True)
class EmailConfig:
    host: str
    port: int
    sender: str
    username: str | None = None
    password: str | None = None
    use_ssl: bool = True
    starttls: bool = False
    timeout: float = 10.0
    subject_prefix: str = "[DAG Runner]"


class EmailNotifier(Notifier):
    def __init__(self, config: EmailConfig):
        self.config = config

    def on_workflow_success(self, event: WorkflowEvent) -> None:
        self._send(event)

    def on_workflow_failed(self, event: WorkflowEvent) -> None:
        self._send(event)

    def on_watchdog_event(self, event: WatchdogEvent) -> None:
        if not event.recipients:
            return
        message = build_watchdog_message(self.config, event)
        self._deliver(message)

    def _send(self, event: WorkflowEvent) -> None:
        if not event.recipients:
            return
        message = build_email_message(self.config, event)
        self._deliver(message)

    def _deliver(self, message: EmailMessage) -> None:
        if self.config.use_ssl:
            client = smtplib.SMTP_SSL(
                self.config.host,
                self.config.port,
                timeout=self.config.timeout,
                context=ssl.create_default_context(),
            )
        else:
            client = smtplib.SMTP(
                self.config.host, self.config.port, timeout=self.config.timeout
            )
        with client:
            if self.config.starttls:
                client.starttls(context=ssl.create_default_context())
            if self.config.username:
                client.login(self.config.username, self.config.password or "")
            client.send_message(message)


def build_email_message(config: EmailConfig, event: WorkflowEvent) -> EmailMessage:
    status = "SUCCESS" if event.status == "SUCCESS" else "FAILED"
    message = EmailMessage()
    message["Subject"] = (
        f"{config.subject_prefix} {status} - {event.workflow_name}"
    )
    message["From"] = config.sender
    message["To"] = ", ".join(event.recipients)
    lines = [
        f"Status: {status}",
        f"Workflow: {event.workflow_name}",
        f"Run ID: {event.run_id}",
    ]
    if status == "FAILED":
        lines.append(f"Error: {event.error_message or 'One or more tasks failed.'}")
        for failure in event.failed_tasks:
            lines.extend(
                (
                    "",
                    f"Failed task: {failure.task_name}",
                    f"Exit code: {failure.exit_code if failure.exit_code is not None else 'N/A'}",
                    f"Error: {failure.error_message or 'No error details were provided.'}",
                )
            )
            if failure.log_file:
                lines.append(f"Log: {failure.log_file}")
    message.set_content("\n".join(lines) + "\n")
    return message


def build_watchdog_message(config: EmailConfig, event: WatchdogEvent) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = f"{config.subject_prefix} WATCHDOG {event.action.upper()} - {event.role}"
    message["From"] = config.sender
    message["To"] = ", ".join(event.recipients)
    lines = [
        f"Action: {event.action}",
        f"Role: {event.role}",
        f"Message: {event.message}",
    ]
    if event.version:
        lines.append(f"Version: {event.version}")
    if event.pid is not None:
        lines.append(f"PID: {event.pid}")
    if event.exit_code is not None:
        lines.append(f"Exit code: {event.exit_code}")
    if event.restarts:
        lines.append(f"Restarts: {event.restarts}")
    message.set_content("\n".join(lines) + "\n")
    return message
