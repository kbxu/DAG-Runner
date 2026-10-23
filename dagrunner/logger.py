from __future__ import annotations

import codecs
import locale
import os
import re
import shutil
from pathlib import Path


_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
)


def _fallback_encodings() -> tuple[str, ...]:
    encodings: list[str] = []
    if os.name == "nt":
        encodings.extend(("mbcs", "cp936", "gbk"))
    try:
        preferred = locale.getpreferredencoding(False)
    except Exception:
        preferred = None
    if preferred:
        encodings.append(preferred)
    encodings.extend(("cp1252", "latin-1"))
    seen: set[str] = set()
    unique: list[str] = []
    for encoding in encodings:
        if encoding and encoding not in seen:
            seen.add(encoding)
            unique.append(encoding)
    return tuple(unique)


def _decode_bytes(data: bytes) -> str:
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            return data.decode(encoding, errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    fallback = _fallback_encodings()
    lines: list[str] = []
    for raw_line in data.split(b"\n"):
        try:
            lines.append(raw_line.decode("utf-8"))
        except UnicodeDecodeError:
            for encoding in fallback:
                try:
                    lines.append(raw_line.decode(encoding))
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
            else:
                lines.append(raw_line.decode("utf-8", errors="replace"))
    return "\n".join(lines)


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
        return _decode_bytes(Path(path).read_bytes())

    @staticmethod
    def read_lines(path: str | Path) -> list[str]:
        text = _decode_bytes(Path(path).read_bytes())
        if not text:
            return []
        lines = text.split("\n")
        if text.endswith("\n"):
            lines.pop()
        return [line[:-1] if line.endswith("\r") else line for line in lines]

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
