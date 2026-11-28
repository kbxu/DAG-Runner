"""Git branch inspection and fast-forward update helpers.

Online updates are only supported for ``git clone`` deployments. Non-git
installations (such as extracted release archives) report ``available=False``
and refuse online updates.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


class UpdateError(RuntimeError):
    pass


def _git(root: Path, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _is_git_clone(root: Path) -> bool:
    if not (root / ".git").exists():
        return False
    try:
        result = _git(root, "rev-parse", "--is-inside-work-tree", timeout=10.0)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.decode("utf-8", "replace").strip() == "true"


def git_branch_capability(root: str | Path) -> dict:
    """Cheap check used by satellites to advertise git-update support."""
    root = Path(root)
    if not _is_git_clone(root):
        return {"available": False, "reason": "not_git"}
    try:
        result = _git(root, "rev-parse", "--verify", "--quiet", "HEAD", timeout=10.0)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "reason": f"git_error: {exc}"}
    if result.returncode != 0:
        return {"available": False, "reason": "no_commits"}
    return {"available": True, "reason": ""}


def git_branch_status(root: str | Path, remote: str = "origin", name: str = "main") -> dict:
    """Compare the local checkout with ``remote/name`` without modifying it."""
    root = Path(root)
    info: dict = {
        "available": False,
        "reason": "",
        "remote": str(remote),
        "name": str(name),
        "local_sha": "",
        "remote_sha": "",
        "behind": 0,
        "ahead": 0,
        "dirty": False,
        "latest_subject": "",
        "latest_time": "",
    }
    if not _is_git_clone(root):
        info["reason"] = "not_git"
        return info
    try:
        fetch = _git(root, "fetch", "--quiet", str(remote), str(name), timeout=60.0)
    except (OSError, subprocess.SubprocessError) as exc:
        info["reason"] = f"fetch_failed: {exc}"
        return info
    if fetch.returncode != 0:
        message = fetch.stderr.decode("utf-8", "replace")[:200]
        info["reason"] = f"fetch_failed: {message}"
        return info
    tracking = f"{remote}/{name}"
    try:
        local = _git(root, "rev-parse", "HEAD", timeout=10.0)
        remote_head = _git(root, "rev-parse", "--verify", "--quiet", tracking, timeout=10.0)
    except (OSError, subprocess.SubprocessError) as exc:
        info["reason"] = f"git_error: {exc}"
        return info
    if local.returncode != 0:
        info["reason"] = "no_local_commit"
        return info
    if remote_head.returncode != 0:
        info["reason"] = "remote_branch_missing"
        return info
    info["local_sha"] = local.stdout.decode("utf-8", "replace").strip()
    info["remote_sha"] = remote_head.stdout.decode("utf-8", "replace").strip()
    counts = _git(
        root,
        "rev-list",
        "--left-right",
        "--count",
        f"HEAD...{tracking}",
        timeout=15.0,
    )
    if counts.returncode == 0:
        left, _, right = counts.stdout.decode("utf-8", "replace").partition("\t")
        try:
            info["ahead"] = int(left.strip())
            info["behind"] = int(right.strip())
        except ValueError:
            pass
    # Only tracked modifications/staged changes block updates; untracked files
    # (e.g. local notes) are ignored unless git merge itself detects a conflict.
    status = _git(root, "status", "--porcelain", "--untracked-files=no", timeout=15.0)
    info["dirty"] = bool(status.returncode == 0 and status.stdout.strip())
    log = _git(
        root,
        "log",
        "-1",
        "--format=%h%x09%ci%x09%s",
        tracking,
        timeout=10.0,
    )
    if log.returncode == 0:
        parts = log.stdout.decode("utf-8", "replace").rstrip("\n").split("\t", 2)
        if len(parts) == 3:
            info["latest_subject"] = parts[2]
            info["latest_time"] = parts[1]
            info["latest_sha"] = parts[0]
    info["available"] = True
    return info


def git_branch_sync(
    root: str | Path,
    remote: str = "origin",
    name: str = "main",
    expected_sha: str = "",
) -> dict:
    """Fast-forward the working tree to ``remote/name``.

    Requires clean tracked files and no local unpushed commits; anything else
    raises :class:`UpdateError` without touching the checkout.
    """
    root = Path(root)
    info = git_branch_status(root, remote=remote, name=name)
    if not info["available"]:
        reason = info["reason"]
        if reason == "not_git":
            raise UpdateError("当前目录不是 git 仓库，不支持在线更新（发布包安装请手动替换代码）")
        raise UpdateError(f"分支更新不可用：{reason}")
    if info["dirty"]:
        raise UpdateError("工作区有未提交改动，不执行更新")
    if info["ahead"] > 0:
        raise UpdateError(
            f"本地有 {info['ahead']} 个未推送提交，无法直接快进；请先处理本地提交"
        )
    if info["behind"] == 0:
        return {"updated": False, "old_sha": info["local_sha"], "new_sha": info["local_sha"]}
    if expected_sha and expected_sha != info["remote_sha"]:
        raise UpdateError("远端分支已变化，请重新检查更新")
    old_sha = info["local_sha"]
    merge = _git(root, "merge", "--ff-only", f"{remote}/{name}", timeout=120.0)
    if merge.returncode != 0:
        message = merge.stderr.decode("utf-8", "replace")[:300]
        raise UpdateError(f"git merge --ff-only 失败: {message}")
    new_sha = _git(root, "rev-parse", "HEAD", timeout=10.0)
    return {
        "updated": True,
        "old_sha": old_sha,
        "new_sha": new_sha.stdout.decode("utf-8", "replace").strip(),
    }
