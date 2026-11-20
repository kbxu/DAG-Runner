"""Versioned install layout helpers.

Layout under the repository root (the directory that contains ``var/``)::

    var/current                 text file with the active version, e.g. "0.1.0"
    var/versions/<version>/     installed code (also where an update is staged)
    var/updates/state.json      update state machine and last result
    var/updates/archives/       downloaded / normalised package archives

``var/current`` is the switch. Activating a version rewrites it atomically;
rolling back only rewrites it again. The code itself is never modified in
place, so a failed update can always fall back to the previous directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path


# Never copy these into a package archive or a tree hash.
IGNORE_NAMES = {"__pycache__", ".git", ".hg", ".svn", ".venv", "venv", ".pytest_cache"}
IGNORE_SUFFIXES = (".pyc", ".pyo")


def repo_root() -> Path:
    """Root that contains ``var/`` and the launchers.

    The launcher changes the working directory to this root before starting the
    server, so the current directory is the root. ``DAGRUNNER_ROOT`` overrides
    it for tests and unusual deployments.
    """
    override = os.environ.get("DAGRUNNER_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return Path.cwd().resolve()


def var_dir(root: Path | None = None) -> Path:
    return (root or repo_root()) / "var"


def package_dir() -> Path:
    """Directory of the ``dagrunner`` package that is currently imported."""
    return Path(__file__).resolve().parent


def code_dir() -> Path:
    """Directory that contains the running ``dagrunner`` package."""
    return package_dir().parent


def active_version(root: Path | None = None) -> str | None:
    try:
        text = (var_dir(root) / "current").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text.splitlines()[0].strip() if text else None


def resolve_code_dir(
    root: Path | None = None, version: str | None = None
) -> Path:
    """Return the code directory for ``version`` (default: the active one)."""
    root = root or repo_root()
    version = version or active_version(root)
    if version:
        candidate = versions_dir(root) / version
        if candidate.is_dir():
            return candidate
    return root


def versions_dir(root: Path | None = None) -> Path:
    return var_dir(root) / "versions"


def updates_dir(root: Path | None = None) -> Path:
    return var_dir(root) / "updates"


def archives_dir(root: Path | None = None) -> Path:
    return updates_dir(root) / "archives"


def state_path(root: Path | None = None) -> Path:
    return updates_dir(root) / "state.json"


def read_state(root: Path | None = None) -> dict:
    try:
        data = json.loads(state_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(state: dict, root: Path | None = None) -> None:
    path = state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def activate(version: str, root: Path | None = None) -> None:
    """Atomically point ``var/current`` at ``version``."""
    path = var_dir(root) / "current"
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, f"{version}\n")


def _atomic_write(path: Path, text: str) -> None:
    handle_fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", text=True
    )
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _ignored(relative: Path) -> bool:
    if any(part in IGNORE_NAMES for part in relative.parts):
        return True
    return relative.name.endswith(IGNORE_SUFFIXES)


def tree_hash(root: Path) -> str:
    """Stable sha256 over the relative path and contents of every file."""
    digest = hashlib.sha256()
    files = sorted(
        path for path in Path(root).rglob("*") if path.is_file() and not _ignored(path.relative_to(root))
    )
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()
