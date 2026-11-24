"""Control files for the watchdog: runtime state, intent, singleton lock.

Three files coordinate the supervisor, the supervised service and the update
flow:

``var/watchdog-<role>.json``
    Runtime status written by the watchdog (pid, child pid, counters) plus the
    ``intent`` flag written by the service before it exits on purpose.

``var/locks/watchdog-<role>.lock``
    Singleton lock. Only one watchdog per role may run at a time.

``var/updates/handoff.json``
    Optional update handoff request. When present while the service exits with
    ``intent=stop``, the watchdog switches to the requested version instead of
    restarting the old one.
"""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

INTENT_RUN = "run"
INTENT_STOP = "stop"
INTENT_UPDATE = "update"

HANDOFF_TTL_SECONDS = 600.0


class WatchdogError(RuntimeError):
    pass


class WatchdogLocked(WatchdogError):
    """Another watchdog for this role already holds the lock."""


def var_dir(root: str | Path) -> Path:
    return Path(root) / "var"


def state_path(root: str | Path, role: str) -> Path:
    return var_dir(root) / f"watchdog-{role}.json"


def lock_path(root: str | Path, role: str) -> Path:
    return var_dir(root) / "locks" / f"watchdog-{role}.lock"


def handoff_path(root: str | Path) -> Path:
    return var_dir(root) / "updates" / "handoff.json"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", text=True
    )
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def read_state(root: str | Path, role: str) -> dict:
    try:
        data = json.loads(state_path(root, role).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(root: str | Path, role: str, state: dict) -> None:
    payload = dict(state)
    payload["role"] = role
    payload["updated_at"] = _now()
    path = state_path(root, role)
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def update_state(root: str | Path, role: str, **fields: object) -> dict:
    """Merge ``fields`` into the state file and persist it atomically."""
    state = read_state(root, role)
    state.update(fields)
    write_state(root, role, state)
    return state


def get_intent(root: str | Path, role: str) -> str:
    value = str(read_state(root, role).get("intent") or INTENT_RUN)
    return value if value in (INTENT_RUN, INTENT_STOP, INTENT_UPDATE) else INTENT_RUN


def set_intent(root: str | Path, role: str, intent: str, *, pid: int | None = None) -> None:
    if intent not in (INTENT_RUN, INTENT_STOP, INTENT_UPDATE):
        raise WatchdogError(f"invalid watchdog intent: {intent!r}")
    fields: dict[str, object] = {"intent": intent}
    if pid is not None:
        fields["intent_pid"] = int(pid)
    update_state(root, role, **fields)


def request_stop(root: str | Path, role: str, *, pid: int | None = None) -> None:
    """Tell the watchdog that the next child exit is intentional."""
    set_intent(root, role, INTENT_STOP, pid=pid)


def heartbeat(root: str | Path, role: str, **fields: object) -> dict:
    return update_state(root, role, last_heartbeat=_now(), **fields)


def _pid_alive(pid: int) -> bool:
    from .launch import process_alive

    return process_alive(pid)


def is_active(
    root: str | Path,
    role: str,
    *,
    interval: float = 5.0,
    stale_after: float | None = None,
) -> bool:
    """Whether a live watchdog owns this role.

    A leftover state file from a dead watchdog must not be mistaken for a live
    one, so both the recorded pid and the heartbeat freshness are checked.
    """
    state = read_state(root, role)
    try:
        pid = int(state.get("watchdog_pid") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0 or not _pid_alive(pid):
        return False
    raw = state.get("last_heartbeat")
    if not raw:
        return False
    try:
        stamp = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    limit = stale_after if stale_after is not None else max(interval * 3.0 + 5.0, interval + 5.0)
    age = (datetime.now().astimezone() - stamp).total_seconds()
    # Small negative ages come from clock skew between writers; tolerate them.
    return -60.0 <= age <= limit


@contextmanager
def watchdog_lock(root: str | Path, role: str) -> Iterator[None]:
    """Hold the per-role singleton lock for the duration of the block."""
    path = lock_path(root, role)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            raise WatchdogLocked(f"看门狗已在运行（锁: {path}）") from exc
        yield
    finally:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


# -- update handoff ---------------------------------------------------------


def write_handoff(
    root: str | Path,
    *,
    action: str = "switch",
    version: str = "",
    previous: str = "",
    ref: str = "",
    remote: str = "",
    expected_sha: str = "",
    requested_by: int = 0,
) -> dict:
    payload = {
        "action": str(action or "switch"),
        "version": str(version or ""),
        "previous": str(previous or ""),
        "ref": str(ref or ""),
        "remote": str(remote or ""),
        "expected_sha": str(expected_sha or ""),
        "requested_by": int(requested_by or 0),
        "requested_at": _now(),
    }
    path = handoff_path(root)
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return payload


def read_handoff(root: str | Path, *, ttl: float = HANDOFF_TTL_SECONDS) -> dict | None:
    """Return a fresh handoff request, or ``None`` when absent or expired."""
    path = handoff_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("action") == "sync_branch":
        if not data.get("ref"):
            return None
    elif not data.get("version"):
        return None
    try:
        stamp = datetime.fromisoformat(str(data.get("requested_at") or ""))
    except ValueError:
        return None
    age = (datetime.now().astimezone() - stamp).total_seconds()
    if age > ttl or age < -60.0:
        return None
    return data


def clear_handoff(root: str | Path) -> None:
    try:
        handoff_path(root).unlink()
    except OSError:
        pass


def record_update_result(
    root: str | Path,
    *,
    status: str,
    version: str = "",
    error: str = "",
) -> None:
    """Persist the watchdog outcome into ``var/updates/state.json``.

    The server's :meth:`UpdateService.recover` reads this on startup to move a
    pending ``committing`` state to done / failed / rolled_back.
    """
    from ..install import read_state as read_install_state
    from ..install import write_state as write_install_state

    state = read_install_state(root)
    if not isinstance(state, dict):
        state = {}
    state["restart"] = {
        "role": "master",
        "status": str(status),
        "version": str(version or ""),
        "error": str(error or ""),
        "updated": _now(),
    }
    try:
        write_install_state(state, root)
    except OSError:
        pass
