"""Notifier configuration loading and channel construction."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

import yaml

from .base import Notifier, NullNotifier, WatchdogEvent, WorkflowEvent
from .email import EmailConfig, EmailNotifier
from .feishu import FeishuConfig, FeishuNotifier


DEFAULT_CONFIG_NAMES = ("notifier.yaml", "notifier.yml", "notifier.json")


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


def parse_feishu_config(raw: dict[str, Any]) -> FeishuConfig:
    webhook_url = raw.get("webhook_url")
    if not isinstance(webhook_url, str) or not webhook_url.strip().startswith(("http://", "https://")):
        raise ValueError("飞书 webhook_url 必须是 http:// 或 https:// 链接")
    secret = _optional_string(raw, "secret") or ""
    subject_prefix = _optional_string(raw, "subject_prefix") or "[DAG Runner]"
    timeout = _positive_number(raw, "timeout", 10.0, float)
    raw_mobiles = raw.get("mention_mobiles") or []
    if isinstance(raw_mobiles, str):
        raw_mobiles = [item.strip() for item in raw_mobiles.replace(";", ",").splitlines() if item.strip()]
    mobiles = tuple(str(item).strip() for item in raw_mobiles if str(item).strip())
    return FeishuConfig(
        webhook_url=webhook_url.strip(),
        secret=secret.strip(),
        mention_mobiles=mobiles,
        subject_prefix=subject_prefix,
        timeout=timeout,
    )


def validate_channel_config(channel_type: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a database-stored notification channel config."""
    if channel_type == "email":
        security = str(raw.get("security") or "").lower()
        use_ssl = raw.get("use_ssl")
        starttls = raw.get("starttls")
        if security in {"ssl", "starttls", "plain"}:
            use_ssl = security == "ssl"
            starttls = security == "starttls"
        parsed = _parse_email_config(
            {
                **raw,
                "use_ssl": True if use_ssl is None else bool(use_ssl),
                "starttls": False if starttls is None else bool(starttls),
            }
        )
        return {
            "host": parsed.host,
            "port": parsed.port,
            "username": parsed.username or "",
            "password": parsed.password or "",
            "from": parsed.sender,
            "use_ssl": parsed.use_ssl,
            "starttls": parsed.starttls,
            "security": "ssl" if parsed.use_ssl else "starttls" if parsed.starttls else "plain",
            "timeout": parsed.timeout,
            "subject_prefix": parsed.subject_prefix,
        }
    if channel_type == "feishu":
        parsed_fs = parse_feishu_config(raw)
        return {
            "webhook_url": parsed_fs.webhook_url,
            "secret": parsed_fs.secret,
            "mention_mobiles": list(parsed_fs.mention_mobiles),
            "subject_prefix": parsed_fs.subject_prefix,
            "timeout": parsed_fs.timeout,
        }
    raise ValueError(f"不支持的通知渠道类型：{channel_type}")


def send_test_channel(channel: dict[str, Any], target: str = "") -> None:
    """Send a test notification through a single database channel."""
    channel_type = channel.get("channel_type")
    cfg = channel.get("config") or {}
    if channel_type == "email":
        recipient = target.strip() or str(cfg.get("from") or "").strip()
        if not recipient:
            raise ValueError("测试邮件需要指定收件邮箱")
        notifier = EmailNotifier(_parse_email_config(cfg))
        event = WorkflowEvent(
            workflow_name="test_notification",
            run_id="test",
            status="SUCCESS",
            recipients=(recipient,),
        )
        notifier.on_workflow_success(event)
        return
    if channel_type == "feishu":
        notifier = FeishuNotifier(parse_feishu_config(cfg))
        notifier.send_test()
        return
    raise ValueError(f"不支持的通知渠道类型：{channel_type}")


class DatabaseNotifier(Notifier):
    """Dispatches workflow notifications using database-stored channels and rules."""

    manages_rules = True

    def __init__(self, database):
        self.database = database

    def on_workflow_success(self, event: WorkflowEvent) -> None:
        self._dispatch(event, "success")

    def on_workflow_failed(self, event: WorkflowEvent) -> None:
        self._dispatch(event, "failure")

    def on_watchdog_event(self, event: WatchdogEvent) -> None:
        for channel in self.database.list_notification_channels():
            if not channel["enabled"]:
                continue
            try:
                if channel["channel_type"] == "email" and event.recipients:
                    EmailNotifier(_parse_email_config(channel["config"])).on_watchdog_event(event)
                elif channel["channel_type"] == "feishu":
                    FeishuNotifier(parse_feishu_config(channel["config"])).on_watchdog_event(event)
            except Exception as exc:
                print(
                    f"warning: watchdog notifier {channel['name']!r} failed: {exc}",
                    file=sys.stderr,
                )

    def _dispatch(self, event: WorkflowEvent, outcome: str) -> None:
        rules = self.database.list_workflow_notifications(event.workflow_name)
        channels = {
            ch["id"]: ch for ch in self.database.list_notification_channels() if ch["enabled"]
        }
        for rule in rules:
            if not rule["enabled"]:
                continue
            send_on = rule["send_on"]
            if send_on != "always" and send_on != outcome:
                continue
            channel = channels.get(rule["channel_id"])
            if not channel:
                continue
            targets = tuple(rule.get("targets") or ())
            try:
                if channel["channel_type"] == "email":
                    if not targets:
                        continue
                    scoped = WorkflowEvent(
                        workflow_name=event.workflow_name,
                        run_id=event.run_id,
                        status=event.status,
                        recipients=targets,
                        error_message=event.error_message,
                        failed_tasks=event.failed_tasks,
                    )
                    notifier = EmailNotifier(_parse_email_config(channel["config"]))
                    if outcome == "success":
                        notifier.on_workflow_success(scoped)
                    else:
                        notifier.on_workflow_failed(scoped)
                elif channel["channel_type"] == "feishu":
                    scoped = WorkflowEvent(
                        workflow_name=event.workflow_name,
                        run_id=event.run_id,
                        status=event.status,
                        recipients=targets,
                        error_message=event.error_message,
                        failed_tasks=event.failed_tasks,
                    )
                    notifier = FeishuNotifier(parse_feishu_config(channel["config"]))
                    if outcome == "success":
                        notifier.on_workflow_success(scoped)
                    else:
                        notifier.on_workflow_failed(scoped)
            except Exception as exc:
                print(
                    f"warning: notifier channel {channel['name']!r} failed for {event.workflow_name}: {exc}",
                    file=sys.stderr,
                )
