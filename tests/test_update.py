from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dagrunner.database import StateDatabase  # noqa: E402
from dagrunner.package import UpdateError, git_branch_status  # noqa: E402
from dagrunner.satellite.protocol import SatelliteConfig  # noqa: E402
from dagrunner.satellite.update import SatelliteUpdater  # noqa: E402
from dagrunner.server.update import UpdateService  # noqa: E402
from dagrunner.watchdog import state as watchdog_state  # noqa: E402


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _commit(path: Path, name: str, text: str) -> str:
    (path / name).write_text(text, encoding="utf-8")
    _git(path, "add", "-A")
    _git(
        path,
        "-c",
        "user.email=test@example.com",
        "-c",
        "user.name=Test",
        "commit",
        "-m",
        f"commit {name}",
    )
    result = _git(path, "rev-parse", "HEAD")
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


class _FakeExecutions:
    def __init__(self, active: list[str] | None = None):
        self._active = list(active or [])

    def active_run_ids(self) -> list[str]:
        return list(self._active)


class _FakeSchedules:
    pass


class _FakeSatelliteManager:
    def __init__(self, reply: dict | None = None):
        self.calls: list[dict] = []
        self.reply = reply or {"ok": True, "state": "restarting", "updated": True, "message": "ok"}

    def sync_satellite_branch(
        self, alias: str, *, remote: str = "origin", branch: str = "main", expected_sha: str = ""
    ) -> dict:
        self.calls.append(
            {"alias": alias, "remote": remote, "branch": branch, "expected_sha": expected_sha}
        )
        return dict(self.reply)


class UpdateServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.remote = self.base / "remote.git"
        self.seed = self.base / "seed"
        self.root = self.base / "master"
        self._init_repos()
        self.db = StateDatabase(self.root / "var" / "scheduler.db")

    def _init_repos(self) -> None:
        self.remote.mkdir()
        _git(self.remote, "init", "--bare", "-b", "main")
        self.seed.mkdir()
        _git(self.seed, "init", "-b", "main")
        _git(self.seed, "remote", "add", "origin", str(self.remote))
        _commit(self.seed, "app.py", "v1\n")
        _git(self.seed, "push", "-u", "origin", "main")
        res = _git(self.base, "clone", str(self.remote), str(self.root))
        self.assertEqual(res.returncode, 0, res.stderr)

    def _service(self, *, executions=None, satellite_manager=None, root=None) -> UpdateService:
        return UpdateService(
            config={"enabled": True, "branch": {"remote": "origin", "name": "main"}},
            database=self.db,
            executions=executions or _FakeExecutions(),
            schedules=_FakeSchedules(),
            satellite_manager=satellite_manager,
            root=root or self.root,
        )

    def _mark_watchdog_alive(self, root: Path, role: str) -> None:
        watchdog_state.heartbeat(root, role, watchdog_pid=os.getpid(), status="running")

    def test_release_mode_without_git_refuses_update(self) -> None:
        plain = self.base / "release-install"
        plain.mkdir()
        svc = self._service(root=plain)
        checked = svc.check()
        self.assertFalse(checked["branch"]["available"])
        self.assertEqual(checked["branch"]["reason"], "not_git")
        with self.assertRaises(UpdateError) as ctx:
            svc.apply()
        self.assertIn("不是 git 仓库", str(ctx.exception))

    def test_dirty_tracked_file_refuses_pull(self) -> None:
        _commit(self.seed, "app.py", "v2\n")
        _git(self.seed, "push", "origin", "main")
        (self.root / "app.py").write_text("local edit\n", encoding="utf-8")
        self._mark_watchdog_alive(self.root, "master")
        svc = self._service()
        with self.assertRaises(UpdateError) as ctx:
            svc.apply()
        self.assertIn("未提交改动", str(ctx.exception))
        # Working tree untouched
        self.assertEqual((self.root / "app.py").read_text(encoding="utf-8"), "local edit\n")

    def test_requires_live_watchdog(self) -> None:
        _commit(self.seed, "app.py", "v2\n")
        _git(self.seed, "push", "origin", "main")
        svc = self._service()
        with self.assertRaises(UpdateError) as ctx:
            svc.apply()
        self.assertIn("看门狗", str(ctx.exception))

    def test_preflight_blocks_when_tasks_running(self) -> None:
        _commit(self.seed, "app.py", "v2\n")
        _git(self.seed, "push", "origin", "main")
        self._mark_watchdog_alive(self.root, "master")
        svc = self._service(executions=_FakeExecutions(active=["run_1"]))
        with self.assertRaises(UpdateError) as ctx:
            svc.apply()
        self.assertIn("run_1", str(ctx.exception))

    def test_apply_pulls_syncs_online_satellites_and_requests_restart(self) -> None:
        new_sha = _commit(self.seed, "app.py", "v2\n")
        _git(self.seed, "push", "origin", "main")
        self._mark_watchdog_alive(self.root, "master")

        self.db.upsert_satellite(
            SatelliteConfig(alias="sat-online", status="online", agent_version="0.1.0")
        )
        self.db.set_satellite_status("sat-online", "online", agent_version="0.1.0")
        self.db.upsert_satellite(
            SatelliteConfig(alias="sat-offline", status="offline", agent_version="0.1.0")
        )
        sat_mgr = _FakeSatelliteManager()
        svc = self._service(satellite_manager=sat_mgr)

        result = svc.apply(expected_sha=new_sha)
        self.assertTrue(result["ok"])
        self.assertTrue(result["restarting"])
        self.assertEqual(result["sha"], new_sha)
        self.assertEqual((self.root / "app.py").read_text(encoding="utf-8"), "v2\n")
        self.assertEqual(
            watchdog_state.get_intent(self.root, "master"),
            watchdog_state.INTENT_RESTART,
        )
        # Only online satellite was triggered; offline was skipped
        self.assertEqual(len(sat_mgr.calls), 1)
        self.assertEqual(sat_mgr.calls[0]["alias"], "sat-online")
        self.assertEqual(result["satellites"]["sat-online"]["state"], "restarting")
        self.assertEqual(result["satellites"]["sat-offline"]["state"], "skipped")

        # On next startup, recover() transitions restarting -> done and clears restart notice
        svc2 = self._service()
        svc2.recover()
        self.assertEqual(svc2.status()["state"], "done")
        self.assertEqual(svc2.status()["message"], "")

    def test_satellite_updater_sync_branch(self) -> None:
        sat_root = self.base / "sat"
        res = _git(self.base, "clone", str(self.remote), str(sat_root))
        self.assertEqual(res.returncode, 0, res.stderr)
        _commit(self.seed, "app.py", "v3\n")
        _git(self.seed, "push", "origin", "main")
        self._mark_watchdog_alive(sat_root, "satellite")

        updater = SatelliteUpdater(root=sat_root)
        reply = updater.sync_branch()
        self.assertTrue(reply["ok"])
        self.assertTrue(reply["updated"])
        self.assertEqual(reply["state"], "restarting")
        self.assertEqual((sat_root / "app.py").read_text(encoding="utf-8"), "v3\n")
        self.assertEqual(
            watchdog_state.get_intent(sat_root, "satellite"),
            watchdog_state.INTENT_RESTART,
        )
        # Already up to date on second run
        reply2 = updater.sync_branch()
        self.assertTrue(reply2["ok"])
        self.assertFalse(reply2["updated"])
        self.assertEqual(reply2["state"], "up_to_date")


if __name__ == "__main__":
    unittest.main()
