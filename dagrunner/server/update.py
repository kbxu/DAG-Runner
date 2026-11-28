"""Git-clone online update coordinator for master and connected satellites.

Online updates are only supported for ``git clone`` deployments supervised by
the watchdog (``var/start.sh`` / ``var/start.bat``). Release archive installs
without ``.git`` refuse online updates.

Flow:
1. Check local worktree cleanliness (tracked files) and remote branch status.
2. If clean and behind (with no local unpushed commits), pull fast-forward in place.
3. Trigger the same pull + restart on online satellites (offline satellites
   auto-trigger an update when they reconnect and hit a version mismatch).
4. Mark ``intent=restart`` for the watchdog and exit so the watchdog relaunches
   the service cleanly from the repository root.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from .. import __version__
from ..install import read_state, repo_root, write_state
from ..package import UpdateError, git_branch_status, git_branch_sync


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class UpdateService:
    """Coordinates git-pull updates on the master and online satellites."""

    def __init__(
        self,
        *,
        config: dict,
        database,
        executions,
        schedules,
        satellite_manager=None,
        root: Path | None = None,
        verify_host: str = "",
        verify_port: int = 0,
    ):
        self.config = config or {}
        self.database = database
        self.executions = executions
        self.schedules = schedules
        self.satellite_manager = satellite_manager
        self.root = Path(root or repo_root())
        self.verify_host = verify_host or "127.0.0.1"
        self.verify_port = int(verify_port or 0)
        self._lock = threading.RLock()
        self._state = read_state(self.root)
        if not isinstance(self._state, dict):
            self._state = {}
        self._state.setdefault("state", "idle")

    # -- state -------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self._state.get("state") in ("updating", "restarting")

    def _save(self) -> None:
        write_state(self._state, self.root)

    def _branch_config(self) -> tuple[str, str]:
        section = self.config.get("branch") or {}
        if not isinstance(section, dict):
            section = {}
        remote = str(section.get("remote", "origin")).strip() or "origin"
        name = str(section.get("name", "main")).strip() or "main"
        return remote, name

    def status(self) -> dict:
        with self._lock:
            remote, name = self._branch_config()
            return {
                "enabled": bool(self.config.get("enabled", True)),
                "active": self.active,
                "state": self._state.get("state", "idle"),
                "current_version": __version__,
                "message": self._state.get("message", ""),
                "started": self._state.get("started", ""),
                "updated": self._state.get("updated", ""),
                "satellites": self._state.get("satellites") or {},
                "branch": {
                    "configured": {"remote": remote, "name": name},
                    "available": (self.root / ".git").exists(),
                },
            }

    def recover(self) -> None:
        """Mark a pending restart as done once the new process boots up."""
        if self._state.get("state") in ("updating", "restarting"):
            self._state["state"] = "done"
            self._state["updated"] = _now()
            self._save()

    # -- check -------------------------------------------------------------

    def branch_status(self) -> dict:
        remote, name = self._branch_config()
        info = git_branch_status(self.root, remote=remote, name=name)
        info["configured"] = {"remote": remote, "name": name}
        return info

    def check(self, expected_sha: str = "", source_name: str = "") -> dict:
        del source_name  # Only git branch mode is supported.
        with self._lock:
            info = self.branch_status()
            target = str(expected_sha or "").strip()
            if target and info.get("available") and target != info.get("remote_sha"):
                raise UpdateError("远端分支已变化，请重新检查更新")
            return {
                "source": "branch",
                "current": __version__,
                "latest": str(info.get("remote_sha") or ""),
                "target": target,
                "branch": info,
            }

    # -- preflight ---------------------------------------------------------

    def _preflight(self) -> None:
        active_runs = sorted(self.executions.active_run_ids())
        if active_runs:
            raise UpdateError(f"有运行中的任务，无法更新：{', '.join(active_runs)}")
        running = sorted(self.database.running_workflow_names())
        if running:
            raise UpdateError(f"有未完成的运行记录，无法更新：{', '.join(running)}")
        busy = [
            str(job["job_id"])
            for job in self.database.list_satellite_jobs(limit=500)
            if job["status"] in ("pending", "running")
        ]
        if busy:
            raise UpdateError(f"有卫星任务尚未结束，无法更新：{', '.join(busy[:5])}")

    # -- apply -------------------------------------------------------------

    def apply(self, expected_sha: str = "", *, require_watchdog: bool = True) -> dict:
        """Verify cleanliness, pull fast-forward, trigger online satellites, and request restart."""
        with self._lock:
            if self.active:
                raise UpdateError(f"已有更新正在进行（{self._state.get('state')}）")
            info = self.branch_status()
            if not info.get("available"):
                if info.get("reason") == "not_git":
                    raise UpdateError("当前目录不是 git 仓库，不支持在线更新（发布包安装请手动替换代码）")
                raise UpdateError(f"分支更新不可用：{info.get('reason') or '未知原因'}")
            if info.get("dirty"):
                raise UpdateError("工作区有未提交改动，不执行更新")
            if int(info.get("ahead") or 0) > 0:
                raise UpdateError("本地有未推送提交，无法直接快进；请先处理本地提交")
            if int(info.get("behind") or 0) == 0:
                raise UpdateError("本地已是最新，无需更新")
            target_sha = str(info.get("remote_sha") or "")
            if expected_sha and expected_sha != target_sha:
                raise UpdateError("远端分支已变化，请重新检查更新")

            if require_watchdog:
                from ..watchdog.state import is_active

                if not is_active(self.root, "master"):
                    raise UpdateError("在线更新需要看门狗在线，请先通过 start.sh / start.bat 启动")

            self._preflight()
            remote, name = self._branch_config()

            # 1. Pull master code first; if pull fails, the service stays up untouched.
            sync = git_branch_sync(
                self.root,
                remote=remote,
                name=name,
                expected_sha=target_sha,
            )
            new_sha = str(sync.get("new_sha") or target_sha)

            # 2. Trigger online satellites to pull and restart too.
            satellites = self._update_satellites(remote=remote, branch=name, expected_sha=new_sha)

            # 3. Ask the watchdog to restart master immediately after this process exits.
            if require_watchdog:
                from ..watchdog.state import request_restart

                request_restart(self.root, "master", pid=os.getpid())

            self._state = {
                "state": "restarting",
                "started": _now(),
                "updated": _now(),
                "old_sha": str(sync.get("old_sha") or ""),
                "new_sha": new_sha,
                "message": f"已拉取 {remote}/{name} ({new_sha[:8]})，正在重启",
                "satellites": satellites,
            }
            self._save()
            return {
                "ok": True,
                "restarting": True,
                "old_sha": str(sync.get("old_sha") or ""),
                "sha": new_sha,
                "behind": int(info.get("behind") or 0),
                "latest_subject": info.get("latest_subject", ""),
                "satellites": satellites,
            }

    def _update_satellites(self, *, remote: str, branch: str, expected_sha: str) -> dict:
        results: dict[str, dict] = {}
        for config in self.database.list_satellites():
            base = {
                "current_version": config.agent_version or "",
                "online": config.status == "online",
            }
            if config.status != "online":
                results[config.alias] = {
                    **base,
                    "state": "skipped",
                    "message": "离线，上线后若版本不一致将自动更新",
                }
                continue
            if self.satellite_manager is None:
                results[config.alias] = {
                    **base,
                    "state": "skipped",
                    "message": "卫星服务未启用",
                }
                continue
            try:
                reply = self.satellite_manager.sync_satellite_branch(
                    config.alias,
                    remote=remote,
                    branch=branch,
                    expected_sha=expected_sha,
                )
                ok = bool(reply.get("ok", True))
                results[config.alias] = {
                    **base,
                    "state": str(reply.get("state") or ("restarting" if ok else "failed")),
                    "message": str(reply.get("message") or ""),
                }
            except Exception as exc:  # noqa: BLE001 - report per satellite
                results[config.alias] = {
                    **base,
                    "state": "failed",
                    "message": str(exc)[:300],
                }
        return results

    def abort(self) -> dict:
        with self._lock:
            self._state["state"] = "idle"
            self._state["updated"] = _now()
            self._save()
            return {"ok": True, "state": "idle"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _run_cli(args) -> int:
    root = repo_root()
    remote = str(args.remote or "origin").strip() or "origin"
    branch = str(args.branch or "main").strip() or "main"
    if args.check:
        info = git_branch_status(root, remote=remote, name=branch)
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0 if info.get("available") else 1
    info = git_branch_status(root, remote=remote, name=branch)
    if not info.get("available"):
        print(f"error: 不支持在线更新 ({info.get('reason')})", file=sys.stderr)
        return 1
    if info.get("dirty"):
        print("error: 工作区有未提交改动，不执行更新", file=sys.stderr)
        return 1
    if int(info.get("ahead") or 0) > 0:
        print("error: 本地有未推送提交，无法直接快进", file=sys.stderr)
        return 1
    if int(info.get("behind") or 0) == 0:
        print("本地已是最新，无需更新")
        return 0
    print(
        f"{remote}/{branch}: 落后 {info['behind']} 个提交 "
        f"({info['local_sha'][:8]} -> {info['remote_sha'][:8]})"
    )
    if not args.yes:
        answer = input("确认执行 git pull --ff-only 并重启？ [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("已取消")
            return 0
    sync = git_branch_sync(root, remote=remote, name=branch, expected_sha=info["remote_sha"])
    print(f"已更新到 {sync['new_sha'][:8]}")
    from ..watchdog.state import is_active, request_restart

    if is_active(root, args.role):
        request_restart(root, args.role, pid=os.getpid())
        print(f"已通知 {args.role} 看门狗重启")
    else:
        print(f"提示: 未检测到运行中的 {args.role} 看门狗，请手动重启服务")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m dagrunner.server.update",
        description="Check and apply git-branch dagrunner updates",
    )
    parser.add_argument("--role", choices=("master", "satellite"), default="master")
    parser.add_argument("--remote", default="origin", help="git remote name")
    parser.add_argument("--branch", default="main", help="git branch name")
    parser.add_argument("--check", action="store_true", help="check branch status and exit")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args(argv)
    return _run_cli(args)


if __name__ == "__main__":
    raise SystemExit(main())
