from __future__ import annotations

import json
import smtplib
import ssl
from abc import ABC
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG_NAMES = ("notifier.yaml", "notifier.yml", "notifier.json")


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


class Notifier(ABC):
    """Extension point for WeCom, Feishu, email, or webhook notifications."""

    def on_workflow_success(self, event: WorkflowEvent) -> None:
        pass

    def on_workflow_failed(self, event: WorkflowEvent) -> None:
        pass


class NullNotifier(Notifier):
    pass


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

    def _send(self, event: WorkflowEvent) -> None:
        if not event.recipients:
            return
        message = build_email_message(self.config, event)
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


def load_notifier(
    config_path: str | Path | None = None,
    *,
    var_dir: str | Path = "var",
) -> Notifier:
    """Load a notifier config; return a no-op notifier when no config exists."""
    path = Path(config_path) if config_path is not None else _discover_config(Path(var_dir))
    if path is None:
        return NullNotifier()
    if not path.is_file():
        raise ValueError(f"notifier config does not exist: {path}")
    raw = _read_config(path)
    email = raw.get("email")
    if email is None:
        raise ValueError("notifier config must contain an 'email' mapping")
    if not isinstance(email, dict):
        raise ValueError("notifier config 'email' must be a mapping")
    return EmailNotifier(_parse_email_config(email))


def load_email_config(
    config_path: str | Path | None = None,
    *,
    var_dir: str | Path = "var",
) -> EmailConfig | None:
    path = _config_path(config_path, Path(var_dir), create=False)
    if path is None:
        return None
    email = _read_config(path).get("email")
    if not isinstance(email, dict):
        raise ValueError("notifier config must contain an 'email' mapping")
    return _parse_email_config(email)


def save_email_config(
    email: dict[str, Any],
    config_path: str | Path | None = None,
    *,
    var_dir: str | Path = "var",
) -> EmailNotifier:
    """Validate and atomically persist SMTP settings while preserving other channels."""
    parsed = _parse_email_config(email)
    path = _config_path(config_path, Path(var_dir), create=True)
    assert path is not None
    data = _read_config(path) if path.is_file() else {}
    data["email"] = {
        "host": parsed.host,
        "port": parsed.port,
        "username": parsed.username or "",
        "password": parsed.password or "",
        "from": parsed.sender,
        "use_ssl": parsed.use_ssl,
        "starttls": parsed.starttls,
        "timeout": parsed.timeout,
        "subject_prefix": parsed.subject_prefix,
    }
    if path.suffix.lower() == ".json":
        content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    elif path.suffix.lower() in {".yaml", ".yml"}:
        content = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    else:
        raise ValueError("notifier config must use .yaml, .yml, or .json")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        raise ValueError(f"cannot write notifier config {path}: {exc}") from exc
    return EmailNotifier(parsed)


def _config_path(
    config_path: str | Path | None,
    var_dir: Path,
    *,
    create: bool,
) -> Path | None:
    if config_path is not None:
        return Path(config_path)
    discovered = _discover_config(var_dir)
    if discovered is not None or not create:
        return discovered
    return var_dir / DEFAULT_CONFIG_NAMES[0]


def _discover_config(var_dir: Path) -> Path | None:
    for name in DEFAULT_CONFIG_NAMES:
        candidate = var_dir / name
        if candidate.is_file():
            return candidate
    return None


def _read_config(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            data = json.loads(text)
        elif path.suffix.lower() in {".yaml", ".yml"}:
            data = yaml.safe_load(text)
        else:
            raise ValueError("notifier config must use .yaml, .yml, or .json")
    except (OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read notifier config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("notifier config root must be a mapping")
    return data


def _parse_email_config(raw: dict[str, Any]) -> EmailConfig:
    host = _required_string(raw, "host")
    sender = _required_string(raw, "from")
    use_ssl = _boolean(raw, "use_ssl", True)
    starttls = _boolean(raw, "starttls", False)
    if use_ssl and starttls:
        raise ValueError("email use_ssl and starttls cannot both be true")
    default_port = 465 if use_ssl else 587 if starttls else 25
    port = _positive_number(raw, "port", default_port, int)
    timeout = _positive_number(raw, "timeout", 10.0, float)
    username = _optional_string(raw, "username")
    password = _optional_string(raw, "password")
    subject_prefix = _optional_string(raw, "subject_prefix") or "[DAG Runner]"
    return EmailConfig(
        host=host,
        port=port,
        sender=sender,
        username=username,
        password=password,
        use_ssl=use_ssl,
        starttls=starttls,
        timeout=timeout,
        subject_prefix=subject_prefix,
    )


def _required_string(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"email '{key}' must be a non-empty string")
    return value.strip()


def _optional_string(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"email '{key}' must be a string")
    return value


def _boolean(raw: dict[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"email '{key}' must be true or false")
    return value


def _positive_number(raw, key, default, number_type):
    value = raw.get(key, default)
    if isinstance(value, bool):
        raise ValueError(f"email '{key}' must be a positive number")
    try:
        converted = number_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"email '{key}' must be a positive number") from exc
    if converted <= 0:
        raise ValueError(f"email '{key}' must be a positive number")
    return converted
