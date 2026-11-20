#!/usr/bin/env python3
"""Run a command, mirroring its output to the terminal and to log files.

Usage::

    python run_tee.py <stdout-log> <stderr-log> <command> [args...]

Each stream is appended to its log file and echoed to the matching console
stream, so a double-clicked launcher shows the program output live while still
keeping a persistent log. The command's exit code is preserved.
"""
from __future__ import annotations

import subprocess
import sys
import threading


def _pump(stream, log_path: str, tee) -> None:
    with open(log_path, "ab") as log:
        while True:
            line = stream.readline()
            if not line:
                break
            log.write(line)
            log.flush()
            tee.buffer.write(line)
            tee.buffer.flush()


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        print(
            "usage: run_tee.py <stdout-log> <stderr-log> <command> [args...]",
            file=sys.stderr,
        )
        return 2
    out_log, err_log, command = argv[1], argv[2], argv[3:]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    assert process.stdout is not None and process.stderr is not None
    workers = [
        threading.Thread(
            target=_pump, args=(process.stdout, out_log, sys.stdout), daemon=True
        ),
        threading.Thread(
            target=_pump, args=(process.stderr, err_log, sys.stderr), daemon=True
        ),
    ]
    for worker in workers:
        worker.start()
    process.wait()
    for worker in workers:
        worker.join(timeout=5)
    return process.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
