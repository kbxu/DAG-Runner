"""Version discovery, staging and coordinated master/satellite updates.

Two update sources converge on the same artifact: a versioned code directory
under ``var/versions/<version>/`` plus a ``sha256`` tree hash.

* :class:`GitSource` exports a tag or branch with ``git archive`` (the working
  tree is never touched, so local changes cannot break an update).
* :class:`ReleaseSource` downloads a GitHub release archive and verifies it.

:class:`UpdateService` coordinates the master side: preflight, staging,
distribution to online satellites, the simultaneous commit and the restart
watchdog. The satellite side lives in :mod:`dagrunner.satellite.update`.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .. import __version__
from ..install import (
    activate,
    active_version,
    archives_dir,
    code_dir,
    read_state,
    repo_root,
    tree_hash,
    versions_dir,
    write_state,
)
from ..package import (
    UpdateError,
    _check_member,
    archive_name,
    extract_archive,
    git_branch_status,
    make_archive,
    strip_single_root,
)
from ..version import is_newer, parse_version


def current_platform() -> str:
    return "windows" if os.name == "nt" else "linux"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class UpdatePackage:
    version: str
    source: str
    path: Path
    sha256: str
    changelog: str = ""
    url: str = ""
    size: int = 0
    archive: Path | None = None


# ---------------------------------------------------------------------------
# Update sources
# ---------------------------------------------------------------------------


class UpdateSource:
    name = "base"

    def __init__(self, root: Path, config: dict):
        self.root = Path(root)
        self.config = config or {}

    def available(self) -> bool:
        return True

    def list_versions(self) -> list[str]:
        raise NotImplementedError

    def fetch(self, version: str, dest: Path) -> UpdatePackage:
        raise NotImplementedError

    def download_url(self, version: str) -> str:
        return ""

    def _normalise(self, package: UpdatePackage, dest: Path) -> UpdatePackage:
        if package.archive is None:
            package.archive = make_archive(dest, archives_dir(self.root) / archive_name(package.version))
        return package


class GitSource(UpdateSource):
    """Export a git tag/branch into the versioned install directory."""

    name = "git"

    def __init__(self, root: Path, config: dict):
        super().__init__(root, config)
        self.remote = str(self.config.get("remote", "origin"))
        self.ref = str(self.config.get("branch_or_tag", "")).strip()

    def available(self) -> bool:
        try:
            result = subprocess.run(
                ["git", "-C", str(self.root), "rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0 and result.stdout.strip() == "true"

    def _git(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            timeout=timeout,
        )

    def list_versions(self) -> list[str]:
        versions: list[str] = []
        result = self._git("ls-remote", "--tags", self.remote, timeout=30)
        if result.returncode == 0:
            tags = [
                line.split("\t", 1)[-1].strip()[len("refs/tags/"):]
                for line in result.stdout.decode("utf-8", "replace").splitlines()
                if line.split("\t", 1)[-1].strip().startswith("refs/tags/")
            ]
        else:
            # No reachable remote: fall back to the tags already in this clone.
            local = self._git("tag", "--list", timeout=15)
            if local.returncode != 0:
                message = result.stderr.decode("utf-8", "replace")[:300]
                raise UpdateError(f"git ls-remote 失败: {message}")
            tags = local.stdout.decode("utf-8", "replace").splitlines()
        for tag in tags:
            version = parse_version(tag)
            if version and str(version) not in versions:
                versions.append(str(version))
        return sorted(versions, key=parse_version, reverse=True)

    def _resolve_ref(self, version: str) -> str:
        candidates = [f"v{version}", version]
        if self.ref:
            candidates.insert(0, self.ref)
        for candidate in candidates:
            result = self._git("rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}")
            if result.returncode == 0:
                return candidate
        raise UpdateError(f"git 中找不到版本 {version}（尝试了 {', '.join(candidates)}）")

    def fetch(self, version: str, dest: Path) -> UpdatePackage:
        ref = self._resolve_ref(version)
        result = self._git("archive", "--format=tar", ref, timeout=120)
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", "replace")[:300]
            raise UpdateError(f"git archive 失败: {message}")
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as tar:
            for member in tar.getmembers():
                _check_member(member.name)
            tar.extractall(dest)
        strip_single_root(dest)
        package = UpdatePackage(
            version=version,
            source=self.name,
            path=dest,
            sha256=tree_hash(dest),
            size=sum(path.stat().st_size for path in dest.rglob("*") if path.is_file()),
        )
        return self._normalise(package, dest)


class ReleaseSource(UpdateSource):
    """Download a GitHub release archive."""

    name = "release"

    def __init__(self, root: Path, config: dict):
        super().__init__(root, config)
        self.repo = str(self.config.get("repo", "")).strip()
        self.download_url = str(self.config.get("download_url", "")).strip()
        self.assets = dict(self.config.get("assets") or {})

    def available(self) -> bool:
        return bool(self.repo)

    def _auth(self) -> dict[str, str]:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _get_json(self, url: str, timeout: float = 20):
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "dagrunner-update",
                **self._auth(),
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _release_url(self, version: str = "") -> str:
        base = f"https://api.github.com/repos/{self.repo}/releases"
        return f"{base}/tags/v{version}" if version else base

    def list_versions(self) -> list[str]:
        data = self._get_json(self._release_url())
        versions: list[str] = []
        for item in data if isinstance(data, list) else []:
            version = parse_version(item.get("tag_name", ""))
            if version and str(version) not in versions:
                versions.append(str(version))
        return sorted(versions, key=parse_version, reverse=True)

    def _release(self, version: str) -> dict:
        try:
            return self._get_json(self._release_url(version))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise UpdateError(f"GitHub 上找不到版本 v{version}") from exc
            raise UpdateError(f"查询 GitHub Release 失败: {exc}") from exc
        except urllib.error.URLError as exc:
            raise UpdateError(f"无法连接 GitHub: {exc}") from exc

    def _asset(self, release: dict, version: str) -> dict:
        wanted = (
            self.assets.get(current_platform())
            or self.assets.get("default")
            or f"dagrunner-{{version}}.tar.gz"
        ).format(version=version)
        assets = release.get("assets") or []
        for asset in assets:
            if asset.get("name") == wanted:
                return asset
        fallback = f"dagrunner-{version}.tar.gz"
        for asset in assets:
            if asset.get("name") == fallback:
                return asset
        names = [str(asset.get("name", "")) for asset in assets]
        raise UpdateError(f"Release v{version} 找不到资产 {wanted}；可用: {', '.join(names)}")

    def _sha256_asset(self, release: dict, asset: dict) -> str:
        target = f"{asset.get('name', '')}.sha256"
        for candidate in release.get("assets") or []:
            if candidate.get("name") != target:
                continue
            try:
                request = urllib.request.Request(
                    candidate["browser_download_url"],
                    headers={"User-Agent": "dagrunner-update", **self._auth()},
                )
                with urllib.request.urlopen(request, timeout=20) as response:
                    text = response.read().decode("utf-8", "replace")
            except (urllib.error.URLError, OSError, KeyError):
                return ""
            for token in text.split():
                if len(token) == 64:
                    return token.lower()
        return ""

    def _download(self, url: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        request = urllib.request.Request(
            url, headers={"User-Agent": "dagrunner-update", **self._auth()}
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response, tmp.open("wb") as handle:
                shutil.copyfileobj(response, handle)
        except (urllib.error.URLError, OSError) as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise UpdateError(f"下载失败: {exc}") from exc
        os.replace(tmp, dest)

    def download_url(self, version: str) -> str:
        if self.download_url:
            return self.download_url.format(repo=self.repo, version=version)
        return ""

    def fetch(self, version: str, dest: Path) -> UpdatePackage:
        release = self._release(version)
        asset = self._asset(release, version)
        url = str(asset.get("browser_download_url", ""))
        archive = archives_dir(self.root) / str(asset.get("name") or archive_name(version))
        self._download(url, archive)
        expected = self._sha256_asset(release, asset)
        if expected and _file_sha256(archive) != expected:
            raise UpdateError(f"Release 资产 {archive.name} sha256 校验失败")
        dest.mkdir(parents=True, exist_ok=True)
        extract_archive(archive, dest)
        strip_single_root(dest)
        package = UpdatePackage(
            version=version,
            source=self.name,
            path=dest,
            sha256=tree_hash(dest),
            changelog=str(release.get("body") or ""),
            url=url,
            size=archive.stat().st_size,
        )
        return self._normalise(package, dest)


# ---------------------------------------------------------------------------
# Master-side coordinator
# ---------------------------------------------------------------------------


@dataclass
class SatellitePlan:
    alias: str
    current_version: str = ""
    online: bool = False
    state: str = "pending"
    message: str = ""


class UpdateService:
    """Owns the update state machine and the master-side restart."""

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
        self._package: UpdatePackage | None = None
        self._plans: dict[str, SatellitePlan] = {}
        self._state = read_state(self.root)
        if not isinstance(self._state, dict):
            self._state = {}
        self._state.setdefault("state", "idle")

    # -- state -------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self._state.get("state") not in (
            None,
            "",
            "idle",
            "done",
            "failed",
            "aborted",
            "rolled_back",
        )

    def _save(self) -> None:
        write_state(self._state, self.root)

    def status(self) -> dict:
        with self._lock:
            return {
                "enabled": bool(self.config.get("enabled", True)),
                "active": self.active,
                "state": self._state.get("state", "idle"),
                "current_version": __version__,
                "target_version": self._state.get("version", ""),
                "previous_version": self._state.get("previous_version", ""),
                "source": self._state.get("source", ""),
                "message": self._state.get("message", ""),
                "started": self._state.get("started", ""),
                "updated": self._state.get("updated", ""),
                "skipped_schedules": self._state.get("skipped_schedules", 0),
                "satellites": self._state.get("satellites") or {},
                "restart": self._state.get("restart") or {},
                "branch": {
                    "configured": dict(
                        zip(("remote", "name"), self._branch_config())
                    ),
                    "available": (self.root / ".git").exists(),
                },
            }

    def recover(self) -> None:
        """Reconcile the persisted state with the watchdog result on startup."""
        restart = self._state.get("restart") or {}
        status = str(restart.get("status", ""))
        if self._state.get("state") != "committing":
            return
        if status == "running":
            self._state["state"] = "done"
        elif status == "rolled_back":
            self._state["state"] = "rolled_back"
        elif status in ("failed", "rollback_failed"):
            self._state["state"] = "failed"
            self._state["message"] = str(restart.get("error", ""))
        else:
            return
        self._state["updated"] = _now()
        self._save()

    # -- sources -----------------------------------------------------------

    def sources(self) -> dict[str, UpdateSource]:
        sources: dict[str, UpdateSource] = {}
        git = GitSource(self.root, self.config.get("git") or {})
        if git.available():
            sources["git"] = git
        release = ReleaseSource(self.root, self.config.get("release") or {})
        if release.available():
            sources["release"] = release
        return sources

    def _select_source(self, name: str = "") -> UpdateSource:
        sources = self.sources()
        preferred = str(name or self.config.get("source", "auto")).strip().lower()
        if preferred and preferred not in ("auto", ""):
            if preferred not in sources:
                raise UpdateError(f"更新源不可用: {preferred}")
            return sources[preferred]
        if "release" in sources:
            return sources["release"]
        if "git" in sources:
            return sources["git"]
        raise UpdateError("没有可用的更新源（配置 update.source / update.release.repo）")

    def check(self, target: str = "", source_name: str = "") -> dict:
        with self._lock:
            if str(source_name or "").strip().lower() == "branch":
                return self.check_branch(target)
            source = self._select_source(source_name)
            versions = source.list_versions()
            target_version = ""
            if target:
                parsed = parse_version(target)
                if parsed is None:
                    raise UpdateError(f"版本号无效: {target}")
                target_version = str(parsed)
                if target_version not in versions:
                    raise UpdateError(f"源中不存在版本 {target_version}")
            latest = versions[0] if versions else ""
            return {
                "source": source.name,
                "current": __version__,
                "versions": versions,
                "latest": latest,
                "target": target_version,
                "download_url": source.download_url(target_version) if target_version else "",
            }

    # -- branch tracking ---------------------------------------------------

    def _branch_config(self) -> tuple[str, str]:
        section = self.config.get("branch") or {}
        if not isinstance(section, dict):
            section = {}
        remote = str(section.get("remote", "origin")).strip() or "origin"
        name = str(section.get("name", "main")).strip() or "main"
        return remote, name

    def branch_status(self) -> dict:
        remote, name = self._branch_config()
        info = git_branch_status(self.root, remote=remote, name=name)
        info["configured"] = {"remote": remote, "name": name}
        return info

    def check_branch(self, expected_sha: str = "") -> dict:
        with self._lock:
            info = self.branch_status()
            target = str(expected_sha or "").strip()
            if target and info.get("available") and target != info.get("remote_sha"):
                raise UpdateError("远端分支已变化，请重新检查更新")
            return {
                "source": "branch",
                "current": __version__,
                "versions": [],
                "latest": str(info.get("remote_sha") or ""),
                "target": target,
                "branch": info,
            }

    def begin_branch(self, expected_sha: str = "") -> dict:
        """Write a sync handoff for the watchdog; no package staging needed."""
        with self._lock:
            if self.active:
                raise UpdateError(f"已有更新正在进行（{self._state.get('state')}）")
            info = self.branch_status()
            if not info.get("available"):
                raise UpdateError(
                    f"分支更新不可用：{info.get('reason') or '未知原因'}；"
                    "发布包安装请手动下载更新包"
                )
            if info.get("dirty"):
                raise UpdateError("工作区有未提交改动，无法执行分支更新")
            if int(info.get("ahead") or 0) > 0:
                raise UpdateError("本地有未推送提交，无法直接快进；请先处理本地提交")
            if int(info.get("behind") or 0) == 0:
                raise UpdateError("本地已是最新，无需更新")
            target_sha = str(info.get("remote_sha") or "")
            if expected_sha and expected_sha != target_sha:
                raise UpdateError("远端分支已变化，请重新检查更新")
            from ..watchdog.state import is_active, write_handoff

            if not is_active(self.root, "master"):
                raise UpdateError("分支更新需要看门狗在线，请先启用 dagrunner.watchdog")
            self._preflight()
            remote, name = self._branch_config()
            write_handoff(
                self.root,
                action="sync_branch",
                ref=name,
                remote=remote,
                expected_sha=target_sha,
                requested_by=os.getpid(),
            )
            self._state = {
                "state": "committing",
                "version": "",
                "previous_version": active_version(self.root) or "",
                "source": "branch",
                "started": _now(),
                "updated": "",
                "message": f"正在同步 {remote}/{name} ({target_sha[:8]})",
                "skipped_schedules": self.schedules.missed_count(),
                "satellites": {},
                "restart": {"status": "handoff", "version": "", "action": "sync_branch"},
            }
            self._save()
            return {
                "ok": True,
                "sha": target_sha,
                "behind": int(info.get("behind") or 0),
                "latest_subject": info.get("latest_subject", ""),
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

    # -- flow --------------------------------------------------------------

    def begin(self, target: str, source_name: str = "") -> dict:
        with self._lock:
            if self.active:
                raise UpdateError(f"已有更新正在进行（{self._state.get('state')}）")
            parsed = parse_version(target)
            if parsed is None:
                raise UpdateError(f"版本号无效: {target}")
            target = str(parsed)
            if not is_newer(target, __version__):
                raise UpdateError(f"目标版本 {target} 不高于当前版本 {__version__}")
            self._preflight()
            source = self._select_source(source_name)
            dest = versions_dir(self.root) / target
            if dest.exists():
                shutil.rmtree(dest)
            package = source.fetch(target, dest)
            previous = active_version(self.root) or ""
            self._package = package
            self._plans = {}
            self.schedules.pause()
            self._state = {
                "state": "staged",
                "version": target,
                "previous_version": previous,
                "source": source.name,
                "started": _now(),
                "updated": "",
                "message": "",
                "skipped_schedules": 0,
                "satellites": {},
                "restart": self._state.get("restart") if isinstance(self._state.get("restart"), dict) else {},
            }
            self._save()
            return {
                "version": target,
                "sha256": package.sha256,
                "size": package.size,
                "archive": str(package.archive),
                "previous_version": previous,
            }

    def prepare(self) -> dict:
        with self._lock:
            if self._state.get("state") != "staged":
                raise UpdateError("当前没有已暂存的更新")
            package = self._package
            if package is None:
                raise UpdateError("更新包丢失，请重新检查更新")
            payload = {
                "version": package.version,
                "sha256": package.sha256,
                "size": package.size,
                "url": package.url,
                "package_path": archive_name(package.version),
            }
            plans: dict[str, SatellitePlan] = {}
            for config in self.database.list_satellites():
                plan = SatellitePlan(
                    alias=config.alias,
                    current_version=config.agent_version or "",
                    online=config.status == "online",
                )
                if config.status != "online":
                    plan.state = "skipped"
                    plan.message = "离线，跳过"
                elif config.agent_version == package.version:
                    plan.state = "skipped"
                    plan.message = "已是目标版本"
                elif self.satellite_manager is None:
                    plan.state = "skipped"
                    plan.message = "卫星服务未启用"
                else:
                    try:
                        result = self.satellite_manager.prepare_update(alias=config.alias, package=payload)
                        state = str(result.get("state", ""))
                        plan.state = "ready" if state == "ready" else "failed"
                        plan.message = str(result.get("message", ""))
                    except Exception as exc:  # noqa: BLE001 - report per satellite
                        plan.state = "failed"
                        plan.message = str(exc)[:300]
                plans[config.alias] = plan
            self._plans = plans
            failed = [plan.alias for plan in plans.values() if plan.state == "failed"]
            self._state["satellites"] = {
                alias: {
                    "state": plan.state,
                    "message": plan.message,
                    "current_version": plan.current_version,
                    "online": plan.online,
                }
                for alias, plan in plans.items()
            }
            if failed:
                self._state["state"] = "prepare_failed"
                self._state["message"] = f"部分卫星准备失败：{', '.join(failed)}"
            else:
                self._state["state"] = "prepared"
                self._state["message"] = ""
            self._save()
            return {"state": self._state["state"], "satellites": self._state["satellites"]}

    def commit(self) -> dict:
        with self._lock:
            if self._state.get("state") != "prepared":
                raise UpdateError("当前状态不能提交更新（尚有卫星未准备好）")
            target = str(self._state.get("version", ""))
            previous = str(self._state.get("previous_version", ""))
            if not target:
                raise UpdateError("缺少目标版本")
            try:
                self._state["skipped_schedules"] = self.schedules.missed_count()
            except Exception:
                self._state["skipped_schedules"] = 0
            committed: list[str] = []
            for config in self.database.list_satellites():
                info = (self._state.get("satellites") or {}).get(config.alias) or {}
                if info.get("state") != "ready" or self.satellite_manager is None:
                    continue
                try:
                    self.satellite_manager.commit_update(config.alias, target)
                    committed.append(config.alias)
                except Exception as exc:  # noqa: BLE001
                    info["state"] = "commit_failed"
                    info["message"] = str(exc)[:300]
            self._state["state"] = "committing"
            self._state["updated"] = _now()
            self._save()
            self._handoff_or_spawn("master", target, previous)
            return {"ok": True, "restarting": True, "satellites": committed}

    def abort(self) -> dict:
        with self._lock:
            if not self.active:
                return {"ok": True, "state": self._state.get("state", "idle")}
            try:
                self._state["skipped_schedules"] = self.schedules.resume_and_reinstall()
            except Exception:
                pass
            self._state["state"] = "aborted"
            self._state["updated"] = _now()
            self._save()
            return {"ok": True, "state": "aborted"}

    def rollback(self, version: str = "") -> dict:
        with self._lock:
            target = str(version or self._state.get("previous_version") or "")
            if not target:
                raise UpdateError("没有可回滚的版本")
            for config in self.database.list_satellites():
                if config.status != "online" or self.satellite_manager is None:
                    continue
                try:
                    self.satellite_manager.rollback_update(config.alias, target)
                except Exception:
                    pass
            self._state["state"] = "rolled_back"
            self._state["updated"] = _now()
            self._save()
            self._handoff_or_spawn("master", target, "")
            return {"ok": True, "restarting": True, "version": target}

    # -- restart -----------------------------------------------------------

    def _config_path(self, role: str) -> Path:
        name = "config.yaml" if role == "master" else "satellite.yaml"
        return self.root / "var" / name

    def _handoff_or_spawn(self, role: str, apply: str, previous: str) -> None:
        """Let a live watchdog perform the version switch when one is running.

        Without a watchdog the one-shot ``dagrunner.watchdog.restart`` process keeps
        doing the work exactly as before.
        """
        from ..watchdog.state import is_active, write_handoff

        try:
            active = is_active(self.root, role)
        except Exception:  # noqa: BLE001 - never block an update on state errors
            active = False
        if not active:
            self._spawn_restart(role, apply, previous)
            return
        write_handoff(
            self.root, version=apply, previous=previous, requested_by=os.getpid()
        )
        self._state["restart"] = {
            "role": role,
            "status": "handoff",
            "version": str(apply),
            "previous": str(previous),
        }
        self._save()
        print(f"update handoff to watchdog: {role} {apply}", file=sys.stderr)

    def _spawn_restart(self, role: str, apply: str, previous: str) -> None:
        code = code_dir()
        command = [
            sys.executable,
            "-m",
            "dagrunner.watchdog.restart",
            "--role",
            role,
            "--root",
            str(self.root),
            "--config",
            str(self._config_path(role)),
            "--apply",
            str(apply),
            "--previous",
            str(previous),
            "--pid",
            str(os.getpid()),
        ]
        if role == "master":
            command += ["--host", self.verify_host, "--port", str(self.verify_port)]
        log_dir = self.root / "var" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        prefix = "server" if role == "master" else "satellite"
        out = (log_dir / f"{prefix}.out.log").open("ab")
        err = (log_dir / f"{prefix}.err.log").open("ab")
        env = dict(os.environ)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(code) + (os.pathsep + existing if existing else "")
        env["DAGRUNNER_ROOT"] = str(self.root)
        kwargs: dict = {}
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            )
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(
            command,
            cwd=str(code),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            **kwargs,
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _port_in_use(host: str, port: int) -> bool:
    import socket

    if host in ("", "0.0.0.0", "::", "[::]"):
        host = "127.0.0.1"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex((host, int(port))) == 0


def _build_satellite_manager(config: dict, database):
    from .app import build_satellite_manager

    return build_satellite_manager(config, database)


def _run_master_cli(args) -> int:
    from ..database import create_state_database
    from .app import load_config_file, serve_satellite_manager
    from .webapp import create_app

    config_path = args.config or Path("var") / "config.yaml"
    config = load_config_file(config_path)
    db_config = config.get("db", "var/scheduler.db")
    database = create_state_database(db_config)
    host = str(config.get("host", "127.0.0.1"))
    port = int(config.get("port", 7119))
    manager = None
    if not args.check:
        if _port_in_use(host, port):
            print(
                f"error: 主程序已在 {host}:{port} 运行；请在 Web 控制台更新，"
                "或先停止主程序再使用命令行更新",
                file=sys.stderr,
            )
            return 2
        manager = _build_satellite_manager(config, database)
        if manager is not None:
            # The CLI update must reach reverse-mode satellites too, so serve
            # the satellite channel exactly like the web service does.
            serve_satellite_manager(config, manager)
    app = create_app(
        database=database,
        state_dir=config.get("state_dir"),
        logs_path=Path(config.get("logs", "var/logs")),
        allow_insecure_remote_login=bool(config.get("allow_insecure_remote_login", False)),
        language=str(config.get("language", "zh-CN")),
        notifier_config_path=Path(config["notifier_config"]) if config.get("notifier_config") else None,
        satellite_manager=manager,
        update_config=config.get("update") or {},
        update_host=host,
        update_port=port,
    )
    service = app.extensions["update_service"]
    if args.check:
        print(json.dumps(service.check(args.to, args.source), ensure_ascii=False, indent=2))
        return 0
    if not args.to:
        print("error: --to <version> is required", file=sys.stderr)
        return 2
    result = service.check(args.to, args.source)
    print(f"source: {result['source']}  target: {result['target']}  current: {result['current']}")
    if not args.yes:
        answer = input(f"update to {result['target']}? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("cancelled")
            return 0
    print("staging ...")
    staged = service.begin(result["target"], args.source)
    print(f"staged {staged['version']} sha256={staged['sha256']}")
    print("distributing to satellites ...")
    prepared = service.prepare()
    for alias, info in (prepared.get("satellites") or {}).items():
        print(f"  {alias}: {info['state']} {info.get('message', '')}".rstrip())
    if prepared["state"] != "prepared":
        service.abort()
        print("prepare failed; aborted", file=sys.stderr)
        return 1
    if args.prepare_only:
        print("prepared; not committing (--prepare-only)")
        return 0
    print("committing; the service will restart ...")
    service.commit()
    time.sleep(1.5)
    os._exit(0)


def _run_satellite_cli(args) -> int:
    from ..satellite import load_config
    from ..satellite.update import SatelliteUpdater

    config_path = args.config or Path("var") / "satellite.yaml"
    data = load_config(config_path)
    section = data.get("satellite") if isinstance(data.get("satellite"), dict) else data
    alias = args.alias or str(section.get("alias", "")).strip()
    master_url = args.master_url or str(section.get("master_url", "")).strip()
    token = args.token or str(section.get("token", ""))
    if not alias or not master_url:
        print("error: satellite alias/master_url are required", file=sys.stderr)
        return 2
    updater = SatelliteUpdater(
        root=repo_root(),
        alias=alias,
        master_url=master_url,
        token=token,
        timeout=float(section.get("timeout", 15)),
        host_key_policy=str(section.get("host_key_policy", "accept-new")),
        master_cert_fingerprint=str(section.get("master_cert_fingerprint", "")),
        listen_host=str(section.get("listen_host", "127.0.0.1")),
        listen_port=int(section.get("listen_port", 7121)),
    )
    if args.check:
        print(json.dumps(updater.metadata(args.to), ensure_ascii=False, indent=2))
        return 0
    if not args.to:
        print("error: --to <version> is required", file=sys.stderr)
        return 2
    metadata = updater.metadata(args.to)
    print(f"target: {args.to}  sha256: {metadata.get('sha256', '')}")
    if not args.yes:
        answer = input(f"update satellite {alias} to {args.to}? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("cancelled")
            return 0
    print("downloading ...")
    updater.prepare(
        {
            "version": args.to,
            "sha256": metadata.get("sha256", ""),
            "size": metadata.get("size", 0),
            "url": metadata.get("url", ""),
            "package_path": metadata.get("package_path", ""),
        }
    )
    print("committing; the satellite will restart ...")
    updater.commit(args.to)
    time.sleep(1.5)
    os._exit(0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m dagrunner.server.update",
        description="Check and apply dagrunner updates",
    )
    parser.add_argument("--config", type=Path, help="config file (master: var/config.yaml)")
    parser.add_argument("--role", choices=("master", "satellite"), default="master")
    parser.add_argument("--to", default="", help="target version, e.g. 0.2.0")
    parser.add_argument("--source", default="", help="update source: git / release")
    parser.add_argument("--check", action="store_true", help="list versions and exit")
    parser.add_argument("--prepare-only", action="store_true", help="stage and distribute, do not restart")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    parser.add_argument("--master-url", default="")
    parser.add_argument("--token", default="")
    parser.add_argument("--alias", default="")
    args = parser.parse_args(argv)
    if args.role == "satellite":
        return _run_satellite_cli(args)
    return _run_master_cli(args)


if __name__ == "__main__":
    raise SystemExit(main())
