from __future__ import annotations

import re
from pathlib import Path


_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")


class TaskLogManager:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def path_for(self, workflow_name: str, run_id: str, task_name: str) -> Path:
        for value, label in (
            (workflow_name, "workflow name"),
            (run_id, "run id"),
            (task_name, "task name"),
        ):
            if not _SAFE_COMPONENT.fullmatch(value):
                raise ValueError(f"unsafe {label} for log path: {value!r}")
        directory = self.root / workflow_name / run_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{task_name}.log"

    @staticmethod
    def read(path: str | Path) -> str:
        return Path(path).read_text(encoding="utf-8", errors="replace")
