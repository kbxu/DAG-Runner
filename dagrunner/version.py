"""Semantic version parsing and comparison.

The single source of truth is ``dagrunner.__version__``. This module only
provides ordering helpers used by the update checker and the console.
"""
from __future__ import annotations

import re
from typing import NamedTuple


_VERSION_RE = re.compile(
    r"^\s*v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?\s*$"
)


class Version(NamedTuple):
    major: int
    minor: int
    patch: int
    pre: str = ""
    build: str = ""

    def __str__(self) -> str:
        text = f"{self.major}.{self.minor}.{self.patch}"
        if self.pre:
            text += f"-{self.pre}"
        if self.build:
            text += f"+{self.build}"
        return text


def parse_version(value: object) -> Version | None:
    """Parse ``X.Y.Z[-pre][+build]``; return ``None`` when it is not a version."""
    if isinstance(value, Version):
        return value
    match = _VERSION_RE.match(str(value or ""))
    if not match:
        return None
    return Version(
        int(match["major"]),
        int(match["minor"]),
        int(match["patch"]),
        match["pre"] or "",
        match["build"] or "",
    )


def _pre_key(pre: str) -> list[tuple[int, int, str]]:
    # Numeric identifiers sort before alphanumeric ones, per semver.
    key: list[tuple[int, int, str]] = []
    for item in pre.split("."):
        if item.isdigit():
            key.append((0, int(item), ""))
        else:
            key.append((1, 0, item))
    return key


def compare_versions(left: object, right: object) -> int:
    """Return ``-1``/``0``/``1``; raise ``ValueError`` for invalid input."""
    a = parse_version(left)
    b = parse_version(right)
    if a is None or b is None:
        raise ValueError(f"invalid version: {left!r} / {right!r}")
    base_a = (a.major, a.minor, a.patch)
    base_b = (b.major, b.minor, b.patch)
    if base_a != base_b:
        return -1 if base_a < base_b else 1
    if a.pre == b.pre:
        return 0
    if not a.pre:
        return 1
    if not b.pre:
        return -1
    key_a, key_b = _pre_key(a.pre), _pre_key(b.pre)
    return -1 if key_a < key_b else 1


def is_newer(candidate: object, current: object) -> bool:
    return compare_versions(candidate, current) > 0
