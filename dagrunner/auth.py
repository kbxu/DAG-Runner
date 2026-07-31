from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta
import getpass
import hashlib
from pathlib import Path
import re
import secrets
import string

from werkzeug.security import check_password_hash, generate_password_hash

from .database import StateDatabase


SESSION_COOKIE = "dagrunner_session"
SESSION_HOURS = 36
FAILED_ATTEMPT_LIMIT = 3
IP_BLOCK_MINUTES = 10
CLIENT_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PASSWORD_HASH_METHOD = "pbkdf2:sha256:600000"


@dataclass(frozen=True)
class LoginResult:
    status: str
    token: str | None = None
    expires_at: datetime | None = None
    remaining_attempts: int = 0
    retry_after: int = 0


class AuthService:
    def __init__(self, database: StateDatabase):
        self.database = database
        self._dummy_hash = generate_password_hash(
            "0" * 64, method=PASSWORD_HASH_METHOD
        )

    def authenticate(self, token: str | None) -> str | None:
        if not token:
            return None
        token_hash = _token_hash(token)
        session = self.database.get_auth_session(token_hash)
        if session is None:
            return None
        expires_at = _parse_time(session["expires_at"])
        if expires_at <= _now():
            self.database.delete_auth_session(token_hash)
            return None
        return str(session["username"])

    def login(self, username: str, client_hash: str, ip_address: str) -> LoginResult:
        now = _now()
        blocked_for = self._blocked_seconds(ip_address, now)
        if blocked_for > 0:
            return LoginResult("blocked", retry_after=blocked_for)

        normalized_hash = client_hash.strip().lower()
        user = self.database.get_user(username) if 1 <= len(username) <= 64 else None
        stored_hash = user["password_hash"] if user is not None else self._dummy_hash
        valid_shape = bool(CLIENT_HASH_PATTERN.fullmatch(normalized_hash))
        password_matches = check_password_hash(
            stored_hash, normalized_hash if valid_shape else "0" * 64
        )
        if user is None or not valid_shape or not password_matches:
            block_until = now + timedelta(minutes=IP_BLOCK_MINUTES)
            attempts, blocked_until = self.database.record_login_failure(
                ip_address,
                now=now.isoformat(timespec="seconds"),
                blocked_until=block_until.isoformat(timespec="seconds"),
                block_after=FAILED_ATTEMPT_LIMIT,
            )
            if blocked_until:
                return LoginResult(
                    "blocked", retry_after=IP_BLOCK_MINUTES * 60
                )
            return LoginResult(
                "invalid",
                remaining_attempts=max(0, FAILED_ATTEMPT_LIMIT - attempts),
            )

        self.database.clear_login_failures(ip_address)
        token = secrets.token_urlsafe(48)
        expires_at = now + timedelta(hours=SESSION_HOURS)
        self.database.create_auth_session(
            _token_hash(token),
            username,
            expires_at.isoformat(timespec="seconds"),
        )
        return LoginResult("success", token=token, expires_at=expires_at)

    def logout(self, token: str | None) -> None:
        if token:
            self.database.delete_auth_session(_token_hash(token))

    def _blocked_seconds(self, ip_address: str, now: datetime) -> int:
        attempt = self.database.get_login_attempt(ip_address)
        if attempt is None or not attempt["blocked_until"]:
            return 0
        blocked_until = _parse_time(attempt["blocked_until"])
        if blocked_until <= now:
            self.database.clear_login_failures(ip_address)
            return 0
        return max(1, int((blocked_until - now).total_seconds()))


def client_password_hash(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def store_password(database: StateDatabase, username: str, password: str) -> None:
    if len(password) < 16:
        raise ValueError("password must contain at least 16 characters")
    password_hash = generate_password_hash(
        client_password_hash(password), method=PASSWORD_HASH_METHOD
    )
    database.upsert_user(username, password_hash)


def generate_complex_password(length: int = 28) -> str:
    if length < 16:
        raise ValueError("generated password length must be at least 16")
    groups = (
        string.ascii_lowercase,
        string.ascii_uppercase,
        string.digits,
        "!@#$%&*+-=?",
    )
    characters = [secrets.choice(group) for group in groups]
    alphabet = "".join(groups)
    characters.extend(secrets.choice(alphabet) for _ in range(length - len(characters)))
    secrets.SystemRandom().shuffle(characters)
    return "".join(characters)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now().astimezone()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Configure a DAG Runner login account")
    parser.add_argument("--db", type=Path, default=Path("var") / "scheduler.db")
    parser.add_argument("--username", default="admin")
    parser.add_argument(
        "--generate",
        action="store_true",
        help="generate and print a strong password instead of prompting",
    )
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", args.username):
        parser.error("username may contain only letters, digits, _, . and -")

    if args.generate:
        password = generate_complex_password()
    else:
        password = getpass.getpass("Password: ")
        confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation:
            parser.error("password confirmation does not match")
    try:
        store_password(StateDatabase(args.db), args.username, password)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"configured account: {args.username}")
    if args.generate:
        print(f"password: {password}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
