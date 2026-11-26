"""Account management: per-workflow permissions and the admin bootstrap.

The admin account is the user named ``admin`` (see :data:`ADMIN_USERNAME`).
Every other account carries per-workflow grants. A grant is a set of
``read``/``execute``/``edit`` flags: single legacy words keep their old
cumulative meaning, while JSON lists store arbitrary combinations.
Accounts created before this feature existed keep full workflow access
through the ``unrestricted`` flag.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

from .auth import (
    client_password_hash,
    generate_complex_password,
    store_password,
    store_password_hash,
)
from ..database import (
    BaseStateDatabase,
    StateDatabase,
    create_state_database,
    load_database_config,
)


ADMIN_USERNAME = "admin"
PERMISSIONS = {"read": 1, "execute": 2, "edit": 3}
USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,64}")
# 旧数据里单个权限词表示「该等级及其以下」的全部权限。
CUMULATIVE_PERMISSIONS = {
    "read": frozenset({"read"}),
    "execute": frozenset({"read", "execute"}),
    "edit": frozenset({"read", "execute", "edit"}),
}


class AccountError(RuntimeError):
    pass


def _normalize_flags(items: object) -> frozenset:
    flags = frozenset(str(item).strip().lower() for item in items)
    if not flags or not flags.issubset(PERMISSIONS):
        raise AccountError("permission must be read, execute, or edit")
    return flags


def parse_permission_set(value: object) -> frozenset:
    """解析存储的权限：单词沿用旧的逐级包含语义，JSON 数组表示独立权限组合。"""
    text = str(value or "").strip()
    if not text:
        return frozenset()
    lowered = text.lower()
    if lowered in CUMULATIVE_PERMISSIONS:
        return CUMULATIVE_PERMISSIONS[lowered]
    if lowered.startswith("["):
        try:
            items = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise AccountError("permission must be read, execute, or edit") from exc
        if not isinstance(items, list):
            raise AccountError("permission must be read, execute, or edit")
        return _normalize_flags(items)
    parts = [part.strip().lower() for part in lowered.split(",") if part.strip()]
    if parts:
        return _normalize_flags(parts)
    raise AccountError("permission must be read, execute, or edit")


def serialize_permission_set(flags: object) -> str:
    normalized = _normalize_flags(flags)
    for word, cumulative in CUMULATIVE_PERMISSIONS.items():
        if normalized == cumulative:
            return word
    return json.dumps(sorted(normalized, key=PERMISSIONS.__getitem__), ensure_ascii=False)


def normalize_permission(value: object) -> str:
    if isinstance(value, str):
        return serialize_permission_set(parse_permission_set(value))
    if isinstance(value, (list, tuple, set, frozenset)):
        return serialize_permission_set(value)
    raise AccountError("permission must be read, execute, or edit")


def permission_at_least(permission: str, required: str) -> bool:
    return required in parse_permission_set(permission)


class AccountService:
    def __init__(self, database: BaseStateDatabase):
        self.database = database

    def is_admin(self, username: str) -> bool:
        return username == ADMIN_USERNAME

    def is_unrestricted(self, username: str) -> bool:
        user = self.database.get_user(username)
        return bool(user and user["unrestricted"])

    def permissions_for(self, username: str) -> dict[str, str]:
        return self.database.user_workflow_map(username)

    def can(self, username: str, workflow_key: str, required: str = "read") -> bool:
        if self.is_admin(username) or self.is_unrestricted(username):
            return True
        permission = self.permissions_for(username).get(workflow_key)
        return permission is not None and permission_at_least(permission, required)

    def can_create(self, username: str) -> bool:
        if self.is_admin(username) or self.is_unrestricted(username):
            return True
        return any(
            "edit" in parse_permission_set(permission)
            for permission in self.permissions_for(username).values()
        )

    def visible_workflows(self, username: str) -> set[str] | None:
        """Return visible workflow keys, or ``None`` when all are visible."""
        if self.is_admin(username) or self.is_unrestricted(username):
            return None
        return set(self.permissions_for(username))

    def list_accounts(self) -> list[dict]:
        accounts = []
        for user in self.database.list_users():
            grants = self.database.list_user_workflows(user["username"])
            accounts.append(
                {
                    "username": user["username"],
                    "enabled": bool(user["enabled"]),
                    "unrestricted": bool(user["unrestricted"]),
                    "is_admin": user["username"] == ADMIN_USERNAME,
                    "created_time": user["created_time"],
                    "updated_time": user["updated_time"],
                    "last_login_time": user["last_login_time"],
                    "workflows": [
                        {"key": row["workflow_key"], "permission": row["permission"]}
                        for row in grants
                    ],
                }
            )
        return accounts

    def create_account(
        self,
        username: str,
        password: str,
        *,
        unrestricted: bool = False,
        enabled: bool = True,
    ) -> None:
        if not USERNAME_PATTERN.fullmatch(username):
            raise AccountError("username may contain only letters, digits, _, . and -")
        if self.database.get_user(username) is not None:
            raise AccountError(f"user already exists: {username}")
        store_password(self.database, username, password, unrestricted=unrestricted)
        self.database.set_user_enabled(username, enabled)

    def update_account(
        self,
        username: str,
        *,
        enabled: bool | None = None,
        unrestricted: bool | None = None,
        workflows: dict[str, str] | None = None,
    ) -> None:
        user = self.database.get_user(username)
        if user is None:
            raise AccountError(f"user not found: {username}")
        if username == ADMIN_USERNAME and enabled is False:
            raise AccountError("the admin account cannot be disabled")
        if enabled is not None:
            self.database.set_user_enabled(username, enabled)
        if unrestricted is not None:
            self.database.set_user_unrestricted(username, unrestricted)
        if workflows is not None:
            self.set_grants(username, workflows)

    def set_grants(self, username: str, grants: dict[str, str]) -> None:
        user = self.database.get_user(username)
        if user is None:
            raise AccountError(f"user not found: {username}")
        normalized = {
            str(key): normalize_permission(permission)
            for key, permission in grants.items()
        }
        for key in normalized:
            if self.database.get_workflow(key) is None:
                raise AccountError(f"workflow not found: {key}")
        self.database.clear_user_workflows(username)
        for key, permission in normalized.items():
            self.database.set_user_workflow(username, key, permission)

    def delete_account(self, username: str) -> None:
        if username == ADMIN_USERNAME:
            raise AccountError("the admin account cannot be deleted")
        if self.database.get_user(username) is None:
            raise AccountError(f"user not found: {username}")
        self.database.delete_user(username)

    def reset_password(self, username: str, password: str) -> None:
        user = self.database.get_user(username)
        if user is None:
            raise AccountError(f"user not found: {username}")
        store_password_hash(
            self.database, username, client_password_hash(password)
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dagrunner.server.accounts",
        description="Manage DAG Runner accounts and workflow permissions",
    )
    parser.add_argument("--db", type=Path, default=Path("var") / "scheduler.db")
    parser.add_argument(
        "--db-config",
        type=Path,
        default=None,
        help="startup YAML config; reads its db entry (SQLite path or MySQL 8 mapping)",
    )
    parser.add_argument("--list", action="store_true", help="list accounts")
    parser.add_argument("--add", metavar="USERNAME", help="create an account")
    parser.add_argument("--password", help="password for --add / --reset-password")
    parser.add_argument(
        "--unrestricted",
        action="store_true",
        help="with --add: grant access to every workflow",
    )
    parser.add_argument(
        "--grant",
        nargs=3,
        metavar=("USERNAME", "WORKFLOW", "PERMISSION"),
        action="append",
        help="grant read/execute/edit on a workflow; repeatable",
    )
    parser.add_argument(
        "--revoke",
        nargs=2,
        metavar=("USERNAME", "WORKFLOW"),
        action="append",
        help="remove a workflow grant; repeatable",
    )
    parser.add_argument("--enable", metavar="USERNAME", help="enable an account")
    parser.add_argument("--disable", metavar="USERNAME", help="disable an account")
    parser.add_argument(
        "--reset-password", metavar="USERNAME", help="reset an account password"
    )
    parser.add_argument("--delete", metavar="USERNAME", help="delete an account")
    return parser


def _print_accounts(accounts: list[dict]) -> None:
    print("username\trole\tenabled\tworkflows\tlast_login")
    for account in accounts:
        if account["is_admin"]:
            scope = "all (admin)"
        elif account["unrestricted"]:
            scope = "all"
        else:
            scope = (
                ", ".join(
                    f"{item['key']}:{item['permission']}"
                    for item in account["workflows"]
                )
                or "-"
            )
        print(
            "\t".join(
                (
                    account["username"],
                    "admin" if account["is_admin"] else "user",
                    "yes" if account["enabled"] else "no",
                    scope,
                    account["last_login_time"] or "-",
                )
            )
        )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.db_config is not None:
            database = create_state_database(load_database_config(args.db_config))
        else:
            database = StateDatabase(args.db)
        service = AccountService(database)
        if args.list:
            _print_accounts(service.list_accounts())
            return 0
        if args.add:
            password = args.password or generate_complex_password()
            service.create_account(
                args.add, password, unrestricted=args.unrestricted
            )
            print(f"created account: {args.add}")
            if args.password is None:
                print(f"password: {password}")
            return 0
        if args.grant:
            grants = service.permissions_for(args.grant[0][0])
            for username, workflow_key, permission in args.grant:
                grants[workflow_key] = normalize_permission(permission)
            service.set_grants(args.grant[0][0], grants)
            return 0
        if args.revoke:
            grants = service.permissions_for(args.revoke[0][0])
            for username, workflow_key in args.revoke:
                grants.pop(workflow_key, None)
            service.set_grants(args.revoke[0][0], grants)
            return 0
        if args.enable:
            service.update_account(args.enable, enabled=True)
            return 0
        if args.disable:
            service.update_account(args.disable, enabled=False)
            return 0
        if args.reset_password:
            password = args.password or generate_complex_password()
            service.reset_password(args.reset_password, password)
            print(f"password reset: {args.reset_password}")
            if args.password is None:
                print(f"password: {password}")
            return 0
        if args.delete:
            service.delete_account(args.delete)
            print(f"deleted account: {args.delete}")
            return 0
        parser.print_help()
        return 2
    except (AccountError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"unexpected account error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
