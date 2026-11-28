from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from dagrunner.package import (
    UpdateError,
    git_branch_capability,
    git_branch_status,
    git_branch_sync,
)


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
    _git(path, "-c", "user.email=test@example.com", "-c", "user.name=Test",
         "commit", "-m", f"commit {name}")
    result = _git(path, "rev-parse", "HEAD")
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


class BranchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.remote = self.base / "remote.git"
        self.seed = self.base / "seed"
        self.clone = self.base / "clone"
        self._init_remotes()

    def _init_remotes(self) -> None:
        self.remote.mkdir()
        _git(self.remote, "init", "--bare", "-b", "main")
        self.seed.mkdir()
        _git(self.seed, "init", "-b", "main")
        _git(self.seed, "remote", "add", "origin", str(self.remote))
        _commit(self.seed, "a.txt", "one")
        _git(self.seed, "push", "-u", "origin", "main")
        result = _git(self.base, "clone", str(self.remote), str(self.clone))
        self.assertEqual(result.returncode, 0, result.stderr)
        _git(self.clone, "checkout", "-b", "main")

    def test_capability_and_not_git(self) -> None:
        self.assertTrue(git_branch_capability(self.clone)["available"])
        plain = self.base / "plain"
        plain.mkdir()
        info = git_branch_capability(plain)
        self.assertFalse(info["available"])
        self.assertEqual(info["reason"], "not_git")

    def test_up_to_date_status(self) -> None:
        info = git_branch_status(self.clone)
        self.assertTrue(info["available"])
        self.assertEqual(info["behind"], 0)
        self.assertEqual(info["ahead"], 0)
        self.assertFalse(info["dirty"])
        self.assertEqual(info["local_sha"], info["remote_sha"])

    def test_behind_then_fast_forward(self) -> None:
        _commit(self.seed, "b.txt", "two")
        _git(self.seed, "push", "origin", "main")
        info = git_branch_status(self.clone)
        self.assertEqual(info["behind"], 1)
        result = git_branch_sync(self.clone)
        self.assertTrue(result["updated"])
        self.assertEqual(git_branch_status(self.clone)["local_sha"], info["remote_sha"])
        again = git_branch_sync(self.clone)
        self.assertFalse(again["updated"])

    def test_dirty_tracked_file_refuses(self) -> None:
        _commit(self.seed, "c.txt", "three")
        _git(self.seed, "push", "origin", "main")
        (self.clone / "a.txt").write_text("modified locally", encoding="utf-8")
        self.assertTrue(git_branch_status(self.clone)["dirty"])
        with self.assertRaises(UpdateError):
            git_branch_sync(self.clone)

    def test_untracked_file_does_not_block(self) -> None:
        _commit(self.seed, "c2.txt", "three-two")
        _git(self.seed, "push", "origin", "main")
        (self.clone / "untracked.txt").write_text("local note", encoding="utf-8")
        info = git_branch_status(self.clone)
        self.assertFalse(info["dirty"])
        result = git_branch_sync(self.clone)
        self.assertTrue(result["updated"])

    def test_local_ahead_refuses(self) -> None:
        _commit(self.seed, "d.txt", "four")
        _git(self.seed, "push", "origin", "main")
        _commit(self.clone, "local.txt", "local")
        self.assertEqual(git_branch_status(self.clone)["ahead"], 1)
        with self.assertRaises(UpdateError):
            git_branch_sync(self.clone)

    def test_expected_sha_mismatch(self) -> None:
        _commit(self.seed, "e.txt", "five")
        _git(self.seed, "push", "origin", "main")
        with self.assertRaises(UpdateError):
            git_branch_sync(self.clone, expected_sha="0" * 40)


if __name__ == "__main__":
    unittest.main()
