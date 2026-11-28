"""Repository layout and update state helpers.

Layout under the repository root (the directory that contains ``var/``)::

    var/updates/state.json      last update status and result
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


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


def updates_dir(root: Path | None = None) -> Path:
    return var_dir(root) / "updates"


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
