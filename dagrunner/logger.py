from __future__ import annotations

import codecs
import locale
import os
import re
import shutil
from pathlib import Path


_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")
# UTF-32 BOMs must be checked before UTF-16 BOMs: the UTF-32LE BOM starts
# with the UTF-16LE BOM bytes.
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def _candidate_encodings() -> tuple[str, ...]:
    """Encodings worth trying, in tie-break order.

    Chinese ANSI text (GBK/GB18030) is included on every platform because a
    Windows satellite may produce such logs while the master runs elsewhere.
    UTF-16 is deliberately absent: on ASCII-heavy files its byte pairs look
    like CJK code points, which outscores the correct decoding. UTF-16 is
    handled separately by BOM detection and the NUL-pattern sniff below.
    """
    encodings: list[str] = []
    if os.name == "nt":
        encodings.append("mbcs")
    encodings.extend(("gb18030", "cp936", "big5"))
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


def _plausibility(text: str) -> int:
    """Score decoded text so real prose beats byte-shifted misdecoding."""
    score = 0
    for character in text:
        code = ord(character)
        if code in (0x09, 0x0A, 0x0D) or 0x20 <= code < 0x7F:
            score += 1
        elif (
            0x4E00 <= code <= 0x9FFF
            or 0x3400 <= code <= 0x4DBF
            or 0xF900 <= code <= 0xFAFF
        ):
            score += 4
        elif 0x3000 <= code <= 0x303F or 0xFF00 <= code <= 0xFFEF:
            score += 3
        elif 0x00C0 <= code <= 0x024F:
            score += 1
        elif code < 0x20 or 0x7F <= code <= 0x9F:
            score -= 10
        elif 0xE000 <= code <= 0xF8FF:
            score -= 2
    return score


def _looks_like_text(text: str) -> bool:
    if not text:
        return True
    bad = sum(
        1
        for character in text
        if ord(character) == 0
        or (ord(character) < 0x20 and character not in "\t\r\n")
    )
    return bad <= len(text) * 0.02


def _sniff_utf16(raw: bytes) -> str | None:
    """Detect UTF-16 without BOM from the NUL-byte pattern of ASCII text."""
    length = len(raw)
    if length < 4 or length % 2:
        return None
    zeros_even = raw[0::2].count(0)
    zeros_odd = raw[1::2].count(0)
    half = length / 2
    # ASCII inside UTF-16 leaves zero high bytes at one parity only. The 1%
    # floor keeps a stray NUL in an ordinary text file from triggering this.
    minimum = max(2, int(half * 0.01))
    if zeros_odd >= minimum and zeros_even == 0:
        return "utf-16-le"
    if zeros_even >= minimum and zeros_odd == 0:
        return "utf-16-be"
    # Fallback for content that mixes ASCII and CJK: require a clear majority
    # of NULs on one side so plain text is never mistaken for UTF-16.
    if zeros_odd >= max(2, half * 0.3) and zeros_even <= zeros_odd * 0.2:
        return "utf-16-le"
    if zeros_even >= max(2, half * 0.3) and zeros_odd <= zeros_even * 0.2:
        return "utf-16-be"
    return None


def decode_bytes(data: bytes, *, strict: bool = False) -> str:
    """Decode log or upload bytes by BOM, then NUL-sniffed UTF-16, then plausibility.

    With ``strict`` set, payloads that do not look like text raise
    UnicodeDecodeError instead of being replaced character by character.
    """
    raw = bytes(data)
    for bom, encoding in _BOMS:
        if raw.startswith(bom):
            return raw.decode(encoding, errors="replace")
    sniffed = _sniff_utf16(raw)
    if sniffed is not None:
        sniffed_text = raw.decode(sniffed, errors="replace")
        if strict and not _looks_like_text(sniffed_text):
            raise UnicodeDecodeError(
                "unknown", raw, 0, 1, "cannot detect text encoding"
            )
        return sniffed_text
    # Plain UTF-8 is the common case. A clean decode wins immediately, which
    # keeps ASCII text from being reinterpreted as UTF-16 by the scoring below.
    try:
        utf8_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    else:
        if _looks_like_text(utf8_text):
            return utf8_text
    best_key: tuple[int, int] | None = None
    best_text: str | None = None
    for order, encoding in enumerate(_candidate_encodings()):
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        key = (-_plausibility(text), order)
        if best_key is None or key < best_key:
            best_key = key
            best_text = text
    if best_text is None:
        if strict:
            raise UnicodeDecodeError(
                "unknown", raw, 0, 1, "cannot detect text encoding"
            )
        return raw.decode("utf-8", errors="replace")
    if strict and not _looks_like_text(best_text):
        raise UnicodeDecodeError(
            "unknown", raw, 0, 1, "cannot detect text encoding"
        )
    return best_text


def _decode_bytes(data: bytes) -> str:
    return decode_bytes(data)


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
