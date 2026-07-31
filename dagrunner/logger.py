from __future__ import annotations

import re
import shutil
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

    def delete_run(self, workflow_name: str, run_id: str) -> None:
        for value, label in (
            (workflow_name, "workflow name"),
            (run_id, "run id"),
        ):
            if not _SAFE_COMPONENT.fullmatch(value):
                raise ValueError(f"unsafe {label} for log path: {value!r}")
        directory = (self.root / workflow_name / run_id).resolve()
        if not directory.is_relative_to(self.root):
            raise ValueError(f"unsafe run log directory: {directory}")
        if directory.exists():
            shutil.rmtree(directory)

    def delete_workflow(self, workflow_name: str) -> None:
        if not _SAFE_COMPONENT.fullmatch(workflow_name):
            raise ValueError(f"unsafe workflow name for log path: {workflow_name!r}")
        directory = (self.root / workflow_name).resolve()
        if not directory.is_relative_to(self.root):
            raise ValueError(f"unsafe workflow log directory: {directory}")
        if directory.exists():
            shutil.rmtree(directory)

    def copy_for_run(
        self,
        source: str | Path,
        workflow_name: str,
        run_id: str,
        task_name: str,
    ) -> Path:
        source_path = Path(source).resolve()
        if not source_path.is_relative_to(self.root):
            raise ValueError(f"refusing to copy log outside log root: {source_path}")
        destination = self.path_for(workflow_name, run_id, task_name)
        if source_path != destination:
            shutil.copy2(source_path, destination)
        return destination
