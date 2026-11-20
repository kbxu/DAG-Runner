"""Satellite-side update: download from the master, verify, stage, restart."""
from __future__ import annotations

import hmac
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from .install import (
    active_version,
    archives_dir,
    code_dir,
    read_state,
    tree_hash,
    versions_dir,
    write_state,
)
from .satellite_http import SatelliteClient, SatelliteHttpError
from .satellite_protocol import (
    AGENT_VERSION,
    PATH_UPDATE_PACKAGE,
    PATH_UPDATE_STATUS,
)
from .update import (
    UpdateError,
    archive_name,
    extract_archive,
    make_archive,
)


class SatelliteUpdater:
    """Applies a staged package on the satellite and restarts the agent."""

    def __init__(
        self,
        *,
        root: Path | None = None,
        alias: str,
        master_url: str,
        token: str,
        timeout: float = 15,
        client: SatelliteClient | None = None,
        host_key_policy: str = "accept-new",
        master_cert_fingerprint: str = "",
        cert_dir: Path | None = None,
        listen_host: str = "127.0.0.1",
        listen_port: int = 0,
    ):
        self.root = Path(root or Path.cwd()).resolve()
        self.alias = alias
        self.master_url = master_url
        self.token = token
        self.timeout = timeout
        self._client = client
        self.host_key_policy = host_key_policy
        self.master_cert_fingerprint = master_cert_fingerprint
        self.cert_dir = Path(cert_dir or (self.root / "var" / "satellite-certs"))
        self.listen_host = listen_host
        self.listen_port = int(listen_port or 0)
        self.state: dict = {}

    # -- client ------------------------------------------------------------

    def _master_client(self) -> SatelliteClient:
        if self._client is None:
            from .satellite_crypto import FileTrustStore

            trust_store = FileTrustStore(self.cert_dir / "known_master.json")

            def trust(fingerprint: str) -> bool:
                if self.host_key_policy == "pinned":
                    return hmac.compare_digest(
                        self.master_cert_fingerprint.lower(), fingerprint.lower()
                    )
                return trust_store.check(fingerprint)

            self._client = SatelliteClient(
                self.master_url,
                token=self.token,
                alias=self.alias,
                timeout=self.timeout,
                trust=trust,
                version=AGENT_VERSION,
            )
        return self._client

    # -- metadata ----------------------------------------------------------

    def metadata(self, version: str) -> dict:
        path = f"{PATH_UPDATE_STATUS}?version={urllib.parse.quote(str(version))}"
        return self._master_client().request("GET", path)

    # -- staging -----------------------------------------------------------

    def prepare(self, payload: dict) -> dict:
        version = str(payload.get("version", "")).strip()
        if not version:
            raise UpdateError("缺少版本号")
        expected = str(payload.get("sha256", "")).strip()
        dest = versions_dir(self.root) / version
        if dest.exists():
            shutil.rmtree(dest)
        archive = archives_dir(self.root) / str(
            payload.get("package_path") or archive_name(version)
        )
        errors: list[str] = []
        downloaded = False
        try:
            self._master_client().download(
                f"{PATH_UPDATE_PACKAGE}?version={urllib.parse.quote(version)}", archive
            )
            downloaded = True
        except (SatelliteHttpError, OSError, ValueError) as exc:
            errors.append(f"master: {exc}")
        if not downloaded and payload.get("url"):
            try:
                self._download_url(str(payload["url"]), archive)
                downloaded = True
            except (OSError, ValueError) as exc:
                errors.append(f"url: {exc}")
        if not downloaded:
            raise UpdateError("无法下载更新包: " + "; ".join(errors))
        extract_archive(archive, dest)
        actual = tree_hash(dest)
        if expected and actual != expected:
            raise UpdateError(f"sha256 校验失败: {actual} != {expected}")
        normalized = archives_dir(self.root) / archive_name(version)
        if archive.resolve() != normalized.resolve():
            make_archive(dest, normalized)
        self.state = {
            "version": version,
            "state": "ready",
            "sha256": actual,
            "path": str(dest),
        }
        return {"ok": True, **self.state}

    def _download_url(self, url: str, dest: Path) -> None:
        import urllib.request

        dest.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(url, headers={"User-Agent": "dagrunner-update"})
        with urllib.request.urlopen(request, timeout=60) as response, dest.open("wb") as handle:
            shutil.copyfileobj(response, handle)

    # -- commit ------------------------------------------------------------

    def commit(self, version: str) -> dict:
        previous = active_version(self.root) or ""
        self._spawn_restart(version, previous)
        return {"ok": True, "restarting": True, "version": version}

    def rollback(self, version: str) -> dict:
        self._spawn_restart(version, "")
        return {"ok": True, "restarting": True, "version": version}

    def _spawn_restart(self, apply: str, previous: str) -> None:
        code = code_dir()
        config = self.root / "var" / "satellite.yaml"
        command = [
            sys.executable,
            "-m",
            "dagrunner.restart",
            "--role",
            "satellite",
            "--root",
            str(self.root),
            "--config",
            str(config),
            "--apply",
            str(apply),
            "--previous",
            str(previous),
            "--pid",
            str(os.getpid()),
        ]
        if self.listen_port:
            command += ["--host", self.listen_host, "--port", str(self.listen_port)]
        log_dir = self.root / "var" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        out = (log_dir / "satellite.out.log").open("ab")
        err = (log_dir / "satellite.err.log").open("ab")
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
        write_state(
            {
                **read_state(self.root),
                "state": "committing",
                "version": str(apply),
                "previous_version": str(previous),
            },
            self.root,
        )


def handle_commit(updater: SatelliteUpdater, version: str) -> dict:
    """Commit and exit soon, so the HTTP response can be delivered first."""
    result = updater.commit(version)

    def _exit() -> None:
        os._exit(0)

    import threading

    threading.Timer(1.0, _exit).start()
    return result


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def recover_grace(root: Path, seconds: float) -> float:
    """Deadline for retrying a version mismatch after a restart.

    The watchdog records ``state: committing`` before switching versions. The
    restarted agent reads it so it keeps reconnecting while the master catches
    up instead of treating the mismatch as permanent.
    """
    if str(read_state(root).get("state", "")) == "committing":
        return time.monotonic() + seconds
    return 0.0


def mark_update_done(root: Path) -> None:
    state = read_state(root)
    if str(state.get("state", "")) == "committing":
        write_state({**state, "state": "done", "updated": _now()}, root)
