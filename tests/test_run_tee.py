from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class RunTeeTests(unittest.TestCase):
    def test_mirrors_streams_and_preserves_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "out.log"
            err = Path(directory) / "err.log"
            code = (
                "import sys; print('out-line'); "
                "print('err-line', file=sys.stderr); sys.exit(7)"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "run_tee.py"),
                    str(out),
                    str(err),
                    sys.executable,
                    "-c",
                    code,
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 7)
            self.assertIn("out-line", result.stdout)
            self.assertIn("err-line", result.stderr)
            self.assertIn("out-line", out.read_text(encoding="utf-8"))
            self.assertIn("err-line", err.read_text(encoding="utf-8"))

    def test_usage_error(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "run_tee.py")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage", result.stderr)


if __name__ == "__main__":
    unittest.main()
