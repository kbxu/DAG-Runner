"""Satellite-side git branch update: verify clean worktree, fast-forward pull, restart via watchdog."""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from pathlib import Path

from ..install import read_state, repo_root, write_state
from ..package import UpdateError, git_branch_status, git_branch_sync


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class SatelliteUpdater:
    """Checks local git cleanliness, pulls fast-forward, and asks the watchdog to restart."""

    def __init__(self, *, root: Path | None = None):
        self.root = Path(root or repo_root()).resolve()
        self.state: dict = {}

    def sync_branch(
        self,
        *,
        remote: str = "origin",
        branch: str = "main",
        expected_sha: str = "",
        require_watchdog: bool = True,
    ) -> dict:
        """Pull ``remote/branch`` if clean and request a watchdog restart."""
        remote = str(remote or "origin").strip() or "origin"
        branch = str(branch or "main").strip() or "main"
        info = git_branch_status(self.root, remote=remote, name=branch)
        if not info.get("available"):
            reason = info.get("reason") or "未知原因"
            if reason == "not_git":
                raise UpdateError("卫星目录不是 git 仓库，不支持在线更新")
            raise UpdateError(f"卫星分支检查失败：{reason}")
        if info.get("dirty"):
            raise UpdateError("卫星工作区有未提交改动，不执行更新")
        if int(info.get("ahead") or 0) > 0:
            raise UpdateError("卫星本地有未推送提交，无法直接快进")
        if int(info.get("behind") or 0) == 0:
            self.state = {
                "state": "up_to_date",
                "updated": False,
                "sha": info.get("local_sha", ""),
                "message": "本地已是最新",
            }
            return {"ok": True, **self.state}
        if require_watchdog:
            from ..watchdog.state import is_active

            if not is_active(self.root, "satellite"):
                raise UpdateError("卫星在线更新需要看门狗在线，请先通过 start-satellite 脚本启动")
        result = git_branch_sync(
            self.root,
            remote=remote,
            name=branch,
            expected_sha=expected_sha,
        )
        if not result.get("updated"):
            self.state = {
                "state": "up_to_date",
                "updated": False,
                "sha": result.get("new_sha", ""),
                "message": "本地已是最新",
            }
            return {"ok": True, **self.state}
        if require_watchdog:
            from ..watchdog.state import request_restart

            request_restart(self.root, "satellite", pid=os.getpid())
        write_state(
            {
                "state": "restarting",
                "old_sha": result.get("old_sha", ""),
                "new_sha": result.get("new_sha", ""),
                "updated": _now(),
            },
            self.root,
        )
        self.state = {
            "state": "restarting",
            "updated": True,
            "old_sha": result.get("old_sha", ""),
            "new_sha": result.get("new_sha", ""),
            "message": f"已更新到 {str(result.get('new_sha', ''))[:8]}，正在重启",
        }
        return {"ok": True, **self.state}


def schedule_satellite_exit(delay: float = 1.0) -> None:
    """Exit the satellite process shortly so the HTTP response can be flushed."""
    def _exit() -> None:
        os._exit(0)

    threading.Timer(delay, _exit).start()


def recover_grace(root: Path, seconds: float) -> float:
    """Deadline for retrying a version mismatch after a restart.

    When the satellite restarts faster than the master after a coordinated
    update, it keeps retrying instead of treating the temporary mismatch as
    permanent.
    """
    if str(read_state(root).get("state", "")) == "restarting":
        return time.monotonic() + seconds
    return 0.0


def mark_update_done(root: Path) -> None:
    state = read_state(root)
    if str(state.get("state", "")) == "restarting":
        write_state({**state, "state": "done", "updated": _now()}, root)
