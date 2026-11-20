from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from dagrunner import __version__
from dagrunner.auth import client_password_hash, store_password
from dagrunner.database import StateDatabase
from dagrunner.install import (
    activate,
    active_version,
    resolve_code_dir,
    tree_hash,
    versions_dir,
)
from dagrunner.service import ServiceError
from dagrunner.update import GitSource, UpdateError, make_archive
from dagrunner.version import compare_versions, is_newer, parse_version
from dagrunner.webapp import create_app


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "tester",
        "GIT_AUTHOR_EMAIL": "tester@example.com",
        "GIT_COMMITTER_NAME": "tester",
        "GIT_COMMITTER_EMAIL": "tester@example.com",
    }
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )


def _make_git_repo(root: Path, tag: str) -> None:
    _git(root, "init", "-q")
    package = root / "dagrunner"
    package.mkdir()
    (package / "__init__.py").write_text('__version__ = "0.2.0"\n', encoding="utf-8")
    (root / "run_server.py").write_text(
        'print("versioned")\n', encoding="utf-8"
    )
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "initial")
    _git(root, "tag", tag)


class VersionTests(unittest.TestCase):
    def test_parse_and_str(self) -> None:
        self.assertEqual(str(parse_version("v0.2.0")), "0.2.0")
        self.assertEqual(str(parse_version("0.2.0-rc.1+build")), "0.2.0-rc.1+build")
        self.assertIsNone(parse_version("not-a-version"))

    def test_compare(self) -> None:
        self.assertEqual(compare_versions("0.1.0", "0.1.0"), 0)
        self.assertLess(compare_versions("0.1.0", "0.2.0"), 0)
        self.assertGreater(compare_versions("0.10.0", "0.9.0"), 0)

    def test_prerelease_ordering(self) -> None:
        self.assertLess(compare_versions("0.2.0-rc1", "0.2.0"), 0)
        self.assertLess(compare_versions("0.2.0-alpha", "0.2.0-alpha.1"), 0)
        self.assertLess(compare_versions("0.2.0-alpha.1", "0.2.0-beta"), 0)
        self.assertTrue(is_newer("0.2.0", "0.2.0-rc1"))
        self.assertFalse(is_newer("0.1.0", "0.2.0"))


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._directory.name)
        self._previous = os.environ.get("DAGRUNNER_ROOT")
        os.environ["DAGRUNNER_ROOT"] = str(self.root)

    def tearDown(self) -> None:
        if self._previous is None:
            os.environ.pop("DAGRUNNER_ROOT", None)
        else:
            os.environ["DAGRUNNER_ROOT"] = self._previous
        self._directory.cleanup()

    def test_activate_and_resolve(self) -> None:
        version_dir = versions_dir(self.root) / "0.2.0"
        version_dir.mkdir(parents=True)
        activate("0.2.0", self.root)
        self.assertEqual(active_version(self.root), "0.2.0")
        self.assertEqual(resolve_code_dir(self.root), version_dir)
        activate("", self.root)
        self.assertIsNone(active_version(self.root))

    def test_tree_hash_ignores_caches(self) -> None:
        source = self.root / "code"
        (source / "dagrunner").mkdir(parents=True)
        (source / "dagrunner" / "__init__.py").write_text("x", encoding="utf-8")
        (source / "dagrunner" / "__pycache__").mkdir()
        (source / "dagrunner" / "__pycache__" / "x.pyc").write_text("y", encoding="utf-8")
        first = tree_hash(source)
        (source / "dagrunner" / "__pycache__" / "x.pyc").write_text("z", encoding="utf-8")
        self.assertEqual(first, tree_hash(source))
        (source / "dagrunner" / "__init__.py").write_text("changed", encoding="utf-8")
        self.assertNotEqual(first, tree_hash(source))

    def test_make_archive_roundtrip(self) -> None:
        from dagrunner.update import extract_archive, strip_single_root

        source = self.root / "code"
        (source / "dagrunner").mkdir(parents=True)
        (source / "dagrunner" / "__init__.py").write_text("x", encoding="utf-8")
        archive = self.root / "out" / "dagrunner-0.2.0.tar.gz"
        make_archive(source, archive)
        dest = self.root / "extracted"
        extract_archive(archive, dest)
        self.assertTrue((dest / "dagrunner" / "__init__.py").is_file())
        self.assertEqual(tree_hash(source), tree_hash(dest))

    def test_strip_single_root(self) -> None:
        from dagrunner.update import strip_single_root

        dest = self.root / "staged"
        (dest / "dagrunner-0.2.0" / "dagrunner").mkdir(parents=True)
        (dest / "dagrunner-0.2.0" / "run_server.py").write_text("x", encoding="utf-8")
        strip_single_root(dest)
        self.assertTrue((dest / "dagrunner").is_dir())
        self.assertTrue((dest / "run_server.py").is_file())
        self.assertFalse((dest / "dagrunner-0.2.0").exists())


class GitSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._directory.name)
        _make_git_repo(self.root, "v0.2.0")

    def tearDown(self) -> None:
        self._directory.cleanup()

    def test_list_and_fetch(self) -> None:
        source = GitSource(self.root, {"remote": "."})
        self.assertTrue(source.available())
        self.assertEqual(source.list_versions(), ["0.2.0"])
        dest = self.root / "staged"
        package = source.fetch("0.2.0", dest)
        self.assertEqual(package.version, "0.2.0")
        self.assertTrue((dest / "dagrunner" / "__init__.py").is_file())
        self.assertEqual(len(package.sha256), 64)
        self.assertTrue(package.archive.is_file())

    def test_fetch_unknown_version(self) -> None:
        source = GitSource(self.root, {"remote": "."})
        with self.assertRaises(UpdateError):
            source.fetch("9.9.9", self.root / "staged")

    def test_fetch_populates_version_directory(self) -> None:
        from dagrunner.install import versions_dir

        source = GitSource(self.root, {"remote": "."})
        dest = versions_dir(self.root) / "0.2.0"
        source.fetch("0.2.0", dest)
        self.assertTrue((dest / "dagrunner" / "__init__.py").is_file())
        self.assertTrue((dest / "run_server.py").is_file())


class UpdateApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._directory.name)
        _make_git_repo(self.root, "v0.2.0")
        self._previous = os.environ.get("DAGRUNNER_ROOT")
        os.environ["DAGRUNNER_ROOT"] = str(self.root)
        self.database = StateDatabase(self.root / "scheduler.db")
        store_password(self.database, "tester", "test-password-1234")
        self.app = create_app(
            database=self.database,
            state_dir=self.root / "var",
            logs_path=self.root / "logs",
            start_scheduler=False,
            allow_insecure_remote_login=True,
            update_config={"source": "git", "git": {"remote": "."}},
        )
        self.client = self.app.test_client()
        self.client.post(
            "/api/auth/login",
            json={
                "username": "tester",
                "password_hash": client_password_hash("test-password-1234"),
            },
        )
        self.service = self.app.extensions["update_service"]
        self.executions = self.app.extensions["execution_service"]
        self.schedules = self.app.extensions["schedule_service"]

    def tearDown(self) -> None:
        try:
            if self.service.active:
                self.service.abort()
        except Exception:
            pass
        self.schedules.shutdown()
        self.executions.shutdown()
        if self._previous is None:
            os.environ.pop("DAGRUNNER_ROOT", None)
        else:
            os.environ["DAGRUNNER_ROOT"] = self._previous
        self._directory.cleanup()

    def test_status_and_health_include_version(self) -> None:
        status = self.client.get("/api/update/status").get_json()
        self.assertEqual(status["current_version"], __version__)
        health = self.client.get("/health").get_json()
        self.assertEqual(health["version"], __version__)

    def test_check_git_source(self) -> None:
        payload = self.client.post(
            "/api/update/check", json={"target": "0.2.0", "source": "git"}
        ).get_json()
        self.assertEqual(payload["source"], "git")
        self.assertEqual(payload["target"], "0.2.0")
        self.assertIn("0.2.0", payload["versions"])

    def test_update_blocked_by_running_run(self) -> None:
        self.database.create_run("run-1", "wf", ["a"], scope=["a"])
        response = self.client.post(
            "/api/update", json={"target": "0.2.0", "source": "git"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("运行", response.get_json()["error"])

    def test_begin_stages_and_blocks_new_runs(self) -> None:
        from dagrunner.install import versions_dir

        staged = self.service.begin("0.2.0", "git")
        self.assertEqual(staged["version"], "0.2.0")
        staged_dir = versions_dir(self.root) / "0.2.0"
        self.assertTrue((staged_dir / "run_server.py").is_file())
        self.assertTrue(self.service.active)
        with self.assertRaises(ServiceError):
            self.executions.start_run("wf")
        prepared = self.service.prepare()
        self.assertEqual(prepared["state"], "prepared")
        aborted = self.service.abort()
        self.assertEqual(aborted["state"], "aborted")
        self.assertFalse(self.service.active)

    def test_begin_rejects_same_version(self) -> None:
        with self.assertRaises(UpdateError):
            self.service.begin(__version__, "git")

    def test_commit_requires_prepared_state(self) -> None:
        with self.assertRaises(UpdateError):
            self.service.commit()

    def test_commit_marks_committing_and_spawns_watchdog(self) -> None:
        from unittest import mock

        self.service.begin("0.2.0", "git")
        self.service.prepare()
        with mock.patch.object(self.service, "_spawn_restart") as spawn:
            result = self.service.commit()
        self.assertTrue(result["restarting"])
        self.assertEqual(self.service.status()["state"], "committing")
        spawn.assert_called_once()
        args = spawn.call_args[0]
        self.assertEqual(args[0], "master")
        self.assertEqual(args[1], "0.2.0")

    def test_rollback_spawns_watchdog(self) -> None:
        from unittest import mock

        self.service.begin("0.2.0", "git")
        with mock.patch.object(self.service, "_spawn_restart") as spawn:
            result = self.service.rollback("0.1.0")
        self.assertTrue(result["restarting"])
        spawn.assert_called_once()
        self.assertEqual(spawn.call_args[0][1], "0.1.0")


class RestartHelpersTests(unittest.TestCase):
    def test_process_alive(self) -> None:
        from unittest import mock

        from dagrunner import restart

        with mock.patch.object(restart.os, "kill") as kill:
            self.assertTrue(restart._process_alive(123))
            kill.assert_called_once_with(123, 0)
        with mock.patch.object(restart.os, "kill", side_effect=ProcessLookupError):
            self.assertFalse(restart._process_alive(123))
        self.assertFalse(restart._process_alive(-1))

    def test_port_free(self) -> None:
        from unittest import mock

        from dagrunner import restart

        with mock.patch.object(restart.socket, "socket") as sock:
            sock.return_value.__enter__.return_value.connect_ex.return_value = 0
            self.assertFalse(restart._port_free("127.0.0.1", 7119))
            sock.return_value.__enter__.return_value.connect_ex.return_value = 1
            self.assertTrue(restart._port_free("127.0.0.1", 7119))


class SatelliteGraceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._directory.name)

    def tearDown(self) -> None:
        self._directory.cleanup()

    def test_recover_grace_only_while_committing(self) -> None:
        import time

        from dagrunner.install import write_state
        from dagrunner.satellite_update import mark_update_done, recover_grace

        self.assertEqual(recover_grace(self.root, 10), 0.0)
        write_state({"state": "committing", "version": "0.2.0"}, self.root)
        self.assertGreater(recover_grace(self.root, 10), time.monotonic())
        mark_update_done(self.root)
        self.assertEqual(recover_grace(self.root, 10), 0.0)


if __name__ == "__main__":
    unittest.main()
