"""Master-side satellite manager and the satellite-facing HTTPS service.

The manager keeps the runtime state for both transport modes:

* reverse mode - a satellite registered over SSE and receives jobs on that stream;
* forward mode - the master posts jobs to the satellite's own HTTPS endpoint.

It also exposes the Flask app served on the satellite port (default 7120).
"""
from __future__ import annotations

import base64
import functools
import hmac
import http.client
import json
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Event, Lock

from flask import Flask, Response, jsonify, request, send_file

from ..database import BaseStateDatabase, local_now
from ..install import tree_hash, versions_dir
from .crypto import (
    ALIAS_HEADER,
    AUTH_HEADER,
    NONCE_HEADER,
    TIMESTAMP_HEADER,
    NonceStore,
    RateLimiter,
    SatelliteAuthError,
    SessionStore,
    authenticate_request,
)
from .http import SatelliteClient, SatelliteHttpError
from .protocol import (
    AGENT_VERSION,
    JOB_CANCELLED,
    JOB_ERROR,
    JOB_FAILED,
    JOB_PENDING,
    JOB_RUNNING,
    JOB_SUCCESS,
    MASTER_ALIAS,
    PATH_HEALTH,
    PATH_HEARTBEAT,
    PATH_JOB,
    PATH_JOB_CANCEL,
    PATH_JOBS,
    PATH_NONCE,
    PATH_PING,
    PATH_REGISTER,
    PATH_STREAM,
    PATH_UPDATE_COMMIT,
    PATH_UPDATE_PACKAGE,
    PATH_UPDATE_PREPARE,
    PATH_UPDATE_REPORT,
    PATH_UPDATE_ROLLBACK,
    PATH_UPDATE_STATUS,
    ROUTE_JOB,
    SSE_EVENT_CANCEL,
    SSE_EVENT_JOB,
    SSE_EVENT_PING,
    SSE_EVENT_UPDATE_COMMIT,
    SSE_EVENT_UPDATE_PREPARE,
    SSE_EVENT_UPDATE_ROLLBACK,
    VERSION_MISMATCH,
    JobSpec,
    SatelliteConfig,
    sse_event,
)
from ..scripting import ExecutionResult
from ..package import UpdateError, archive_name


STREAM_STALE_SECONDS = 90
STREAM_CLOSE = object()
FORWARD_FAILURE_LIMIT = 600


class SatelliteError(RuntimeError):
    pass


@dataclass
class _JobRuntime:
    job_id: str
    alias: str
    spec: JobSpec
    log_file: Path
    status: str = JOB_PENDING
    exit_code: int | None = None
    error_message: str | None = None
    log_offset: int = 0
    cancel_requested: bool = False
    created: float = field(default_factory=time.monotonic)
    done: Event = field(default_factory=Event)


@dataclass
class _StreamState:
    alias: str
    queue: queue.Queue = field(default_factory=queue.Queue)
    last_seen: float = field(default_factory=time.monotonic)


_TERMINAL = {JOB_SUCCESS, JOB_FAILED, JOB_CANCELLED, JOB_ERROR}


def _error_agent_version(exc: SatelliteHttpError) -> str:
    try:
        data = json.loads(exc.payload or b"{}")
    except ValueError:
        return ""
    return str(data.get("agent_version", "") or "")


class SatelliteManager:
    def __init__(
        self,
        database: BaseStateDatabase,
        *,
        agent_version: str = AGENT_VERSION,
        timeout: float = 15,
        poll_interval: float = 1.0,
        update_timeout: float = 300,
        root=None,
    ):
        self.database = database
        self.agent_version = agent_version
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.update_timeout = update_timeout
        self.root = Path(root) if root is not None else Path.cwd().resolve()
        self.nonce_store = NonceStore()
        self.session_store = SessionStore()
        self.rate_limiter = RateLimiter(limit=1200, window_seconds=60)
        self._clients: dict[str, SatelliteClient] = {}
        self._jobs: dict[str, _JobRuntime] = {}
        self._jobs_lock = Lock()
        self._log_lock = Lock()
        self._streams: dict[str, _StreamState] = {}
        self._streams_lock = Lock()
        self._update_reports: dict[str, dict] = {}
        self._update_events: dict[str, Event] = {}
        self._package_meta: dict[str, dict] = {}
        self._stop = Event()
        self._monitor: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Version compatibility
    # ------------------------------------------------------------------

    def version_mismatch_message(self, agent_version: str) -> str:
        return (
            f"版本不一致: master {self.agent_version} != "
            f"satellite {agent_version or '未知'}"
        )

    def reject_version(self, alias: str, agent_version: str) -> None:
        """Drop any live channel from a satellite whose version does not match."""
        with self._streams_lock:
            state = self._streams.pop(alias, None)
        if state is not None:
            state.queue.put(STREAM_CLOSE)
        self.database.set_satellite_status(
            alias, "offline", last_error=self.version_mismatch_message(agent_version)
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._monitor is not None:
            return
        self._monitor = threading.Thread(
            target=self._monitor_loop, daemon=True, name="satellite-monitor"
        )
        self._monitor.start()

    def shutdown(self) -> None:
        self._stop.set()
        with self._jobs_lock:
            runtimes = list(self._jobs.values())
        for runtime in runtimes:
            if runtime.status not in _TERMINAL:
                self.cancel_job(runtime.job_id)
        with self._streams_lock:
            states = list(self._streams.values())
        for state in states:
            state.queue.put(STREAM_CLOSE)

    # ------------------------------------------------------------------
    # Job execution (used by SatelliteTaskExecutor)
    # ------------------------------------------------------------------

    def execute(
        self,
        alias: str,
        spec: JobSpec,
        log_file: Path | str,
        *,
        task_timeout: int | None = None,
        cancel_event: Event | None = None,
        workflow_timeout_event: Event | None = None,
        workflow_timeout: int | None = None,
    ) -> ExecutionResult:
        runtime = self._create_job(alias, spec, Path(log_file))
        try:
            self._dispatch(runtime)
        except Exception as exc:
            self._fail_job(runtime, str(exc))
            return ExecutionResult(127, str(exc))

        started = time.monotonic()
        while True:
            if runtime.done.wait(0.2):
                break
            cancelled = cancel_event.is_set() if cancel_event is not None else False
            if cancelled:
                workflow_timed_out = bool(
                    workflow_timeout_event and workflow_timeout_event.is_set()
                )
                message = (
                    f"workflow timed out after {workflow_timeout} seconds"
                    if workflow_timed_out
                    else "task stopped by user"
                )
                self.cancel_job(runtime.job_id)
                return ExecutionResult(124 if workflow_timed_out else 130, message)
            if task_timeout and time.monotonic() - started >= task_timeout:
                message = f"task timed out after {task_timeout} seconds"
                self.cancel_job(runtime.job_id)
                return ExecutionResult(124, message)

        exit_code = runtime.exit_code if runtime.exit_code is not None else 1
        return ExecutionResult(exit_code, runtime.error_message)

    def refresh_satellite_platform(self, alias: str) -> str:
        """Ask a forward-reachable satellite for its platform and remember it."""
        config = self.database.get_satellite(alias)
        if config is None or not config.url:
            return ""
        try:
            client = self._forward_client(config)
            payload = client.request("GET", PATH_HEALTH, auth=False, timeout=self.timeout)
        except Exception:
            return ""
        platform = str(payload.get("platform", "") or "")
        if platform:
            self.database.set_satellite_status(alias, "online", platform=platform)
        return platform

    def test_connection(self, alias: str) -> dict:
        """Probe a satellite and report reachability, transport mode and latency."""
        config = self.database.get_satellite(alias)
        if config is None:
            raise SatelliteError(f"satellite {alias!r} not found")
        started = time.monotonic()
        if self._get_stream(alias) is not None:
            return {
                "reachable": True,
                "mode": "reverse",
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        if not config.url:
            return {
                "reachable": False,
                "mode": "forward",
                "error_message": "未配置 URL，且没有反向连接",
            }
        try:
            client = self._forward_client(config)
            client.request("GET", PATH_PING, timeout=10)
        except Exception as exc:
            return {
                "reachable": False,
                "mode": "forward",
                "error_message": str(exc)[:500],
            }
        try:
            payload = client.request(
                "GET", PATH_HEALTH, auth=False, timeout=self.timeout
            )
        except Exception:
            payload = {}
        remote_version = str(payload.get("version", "") or "")
        if remote_version and remote_version != self.agent_version:
            return {
                "reachable": False,
                "mode": "forward",
                "error_message": self.version_mismatch_message(remote_version),
            }
        return {
            "reachable": True,
            "mode": "forward",
            "latency_ms": int((time.monotonic() - started) * 1000),
        }

    # ------------------------------------------------------------------
    # Update distribution
    # ------------------------------------------------------------------

    def _package_metadata(self, version: str) -> dict:
        meta = self._package_meta.get(version)
        if meta is None:
            staged = versions_dir(self.root) / version
            archive = self.root / "var" / "updates" / "archives" / archive_name(version)
            if not staged.is_dir():
                raise UpdateError(f"版本 {version} 尚未暂存")
            meta = {
                "version": version,
                "sha256": tree_hash(staged),
                "size": archive.stat().st_size if archive.is_file() else 0,
                "package_path": archive_name(version),
                "url": "",
            }
            self._package_meta[version] = meta
        return meta

    def prepare_update(self, alias: str, package: dict) -> dict:
        """Ask a satellite to download and stage ``package``."""
        payload = dict(package)
        payload.setdefault("package_path", archive_name(str(payload.get("version", ""))))
        stream = self._get_stream(alias)
        if stream is not None:
            self._update_reports.pop(alias, None)
            event = Event()
            self._update_events[alias] = event
            stream.queue.put(sse_event(SSE_EVENT_UPDATE_PREPARE, payload))
            if not event.wait(self.update_timeout):
                raise SatelliteError("卫星更新准备超时")
            report = self._update_reports.get(alias) or {}
            state = str(report.get("state", ""))
            if state not in ("ready", "preparing"):
                raise SatelliteError(f"卫星更新准备失败: {report.get('message', '') or state}")
            return report
        config = self.database.get_satellite(alias)
        if config is None:
            raise SatelliteError(f"卫星不存在: {alias}")
        client = self._forward_client(config)
        return client.request("POST", PATH_UPDATE_PREPARE, json_body=payload)

    def commit_update(self, alias: str, version: str) -> dict:
        stream = self._get_stream(alias)
        if stream is not None:
            stream.queue.put(sse_event(SSE_EVENT_UPDATE_COMMIT, {"version": version}))
            return {"ok": True}
        config = self.database.get_satellite(alias)
        if config is None:
            raise SatelliteError(f"卫星不存在: {alias}")
        client = self._forward_client(config)
        return client.request("POST", PATH_UPDATE_COMMIT, json_body={"version": version})

    def rollback_update(self, alias: str, version: str) -> dict:
        stream = self._get_stream(alias)
        if stream is not None:
            stream.queue.put(sse_event(SSE_EVENT_UPDATE_ROLLBACK, {"version": version}))
            return {"ok": True}
        config = self.database.get_satellite(alias)
        if config is None:
            raise SatelliteError(f"卫星不存在: {alias}")
        client = self._forward_client(config)
        return client.request("POST", PATH_UPDATE_ROLLBACK, json_body={"version": version})

    def report_update(self, alias: str, payload: dict) -> dict:
        self._update_reports[alias] = dict(payload)
        event = self._update_events.get(alias)
        if event is not None:
            event.set()
        return {"ok": True}

    def satellite_update_status(self, alias: str) -> dict:
        stream = self._get_stream(alias)
        if stream is not None:
            return self._update_reports.get(alias, {})
        config = self.database.get_satellite(alias)
        if config is None:
            raise SatelliteError(f"卫星不存在: {alias}")
        client = self._forward_client(config)
        return client.request("GET", PATH_UPDATE_STATUS)

    def cancel_job(self, job_id: str) -> None:
        with self._jobs_lock:
            runtime = self._jobs.get(job_id)
        if runtime is None or runtime.status in _TERMINAL:
            return
        runtime.cancel_requested = True
        stream = self._get_stream(runtime.alias)
        if stream is not None:
            stream.queue.put(sse_event(SSE_EVENT_CANCEL, {"job_id": job_id}))
            return
        threading.Thread(
            target=self._cancel_forward, args=(runtime,), daemon=True
        ).start()

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _create_job(self, alias: str, spec: JobSpec, log_file: Path) -> _JobRuntime:
        runtime = _JobRuntime(
            job_id=spec.job_id, alias=alias, spec=spec, log_file=log_file
        )
        with self._jobs_lock:
            self._jobs[spec.job_id] = runtime
        self.database.create_satellite_job(
            spec.job_id,
            alias,
            spec.workflow_name,
            spec.run_id,
            spec.task_name,
            str(log_file),
        )
        return runtime

    def _dispatch(self, runtime: _JobRuntime) -> None:
        stream = self._get_stream(runtime.alias)
        if stream is not None:
            stream.queue.put(
                sse_event(
                    SSE_EVENT_JOB,
                    {"job_id": runtime.job_id, "spec": runtime.spec.to_json()},
                )
            )
            self._mark_running(runtime)
            return
        self._submit_forward(runtime)

    def _submit_forward(self, runtime: _JobRuntime) -> None:
        config = self.database.get_satellite(runtime.alias)
        if config is None:
            raise SatelliteError(f"satellite {runtime.alias!r} not found")
        if not config.url:
            raise SatelliteError(
                f"satellite {runtime.alias!r} has no active channel or URL"
            )
        client = self._forward_client(config)
        try:
            response = client.register(json_body={"role": "master"})
        except SatelliteHttpError as exc:
            if exc.error_code == VERSION_MISMATCH:
                message = self.version_mismatch_message(_error_agent_version(exc))
                self.database.set_satellite_status(
                    config.alias, "offline", last_error=message
                )
            raise
        remote_version = str(response.get("agent_version", "") or "")
        if remote_version != self.agent_version:
            message = self.version_mismatch_message(remote_version)
            self.database.set_satellite_status(
                config.alias, "offline", last_error=message
            )
            raise SatelliteError(message)
        platform = str(response.get("platform", "") or "")
        if platform:
            self.database.set_satellite_status(
                config.alias,
                "online",
                platform=platform,
                hostname=str(response.get("hostname", "") or ""),
                agent_version=str(response.get("agent_version", "") or ""),
                branch_info=json.dumps(
                    response.get("branch") or {}, ensure_ascii=False
                ),
            )
        client.request("POST", PATH_JOBS, json_body=runtime.spec.to_json())
        self._mark_running(runtime)
        threading.Thread(
            target=self._forward_poll_loop,
            args=(runtime, client),
            daemon=True,
            name=f"satellite-poll-{runtime.job_id}",
        ).start()

    def _forward_poll_loop(self, runtime: _JobRuntime, client: SatelliteClient) -> None:
        offset = runtime.log_offset
        failures = 0
        while not runtime.done.is_set():
            try:
                payload = client.request(
                    "GET",
                    PATH_JOB.format(job_id=runtime.job_id),
                    timeout=self.timeout,
                )
            except (SatelliteHttpError, OSError, ValueError, http.client.HTTPException) as exc:
                failures += 1
                if failures >= FORWARD_FAILURE_LIMIT:
                    self._fail_job(runtime, f"satellite unreachable: {exc}")
                    return
                time.sleep(min(5.0, failures))
                continue
            failures = 0
            status = str(payload.get("status", ""))
            if status == JOB_RUNNING:
                self._mark_running(runtime)
            data = payload.get("log_data", "")
            if data:
                self._append_log(
                    runtime,
                    int(payload.get("log_offset", offset)),
                    base64.b64decode(data),
                )
                offset = runtime.log_offset
            if status in _TERMINAL:
                self._finish_job(
                    runtime,
                    status,
                    payload.get("exit_code"),
                    payload.get("error_message"),
                )
                return
            time.sleep(self.poll_interval)

    def _cancel_forward(self, runtime: _JobRuntime) -> None:
        try:
            config = self.database.get_satellite(runtime.alias)
            if config is None or not config.url:
                return
            client = self._forward_client(config)
            client.request(
                "POST", PATH_JOB_CANCEL.format(job_id=runtime.job_id)
            )
        except Exception:
            pass

    def _forward_client(self, config: SatelliteConfig) -> SatelliteClient:
        client = self._clients.get(config.alias)
        if client is not None:
            return client

        current = self.database.get_satellite(config.alias)
        expected = ((current.cert_fingerprint if current else "") or "").lower()

        def trust(fingerprint: str) -> bool:
            current = self.database.get_satellite(config.alias)
            known = (current.cert_fingerprint if current else "") or ""
            if config.host_key_policy == "pinned" and not known:
                return False
            if known:
                return hmac.compare_digest(known.lower(), fingerprint.lower())
            self.database.remember_satellite_fingerprint(config.alias, fingerprint)
            return True

        client = SatelliteClient(
            config.url,
            token=config.token,
            alias=MASTER_ALIAS,
            timeout=self.timeout,
            trust=trust,
            version=self.agent_version,
            expected_fingerprint=expected,
        )
        self._clients[config.alias] = client
        return client

    # ------------------------------------------------------------------
    # Job state
    # ------------------------------------------------------------------

    def _mark_running(self, runtime: _JobRuntime) -> None:
        if runtime.status != JOB_PENDING:
            return
        runtime.status = JOB_RUNNING
        self.database.update_satellite_job(
            runtime.job_id, status=JOB_RUNNING, started_time=local_now()
        )

    def _finish_job(
        self,
        runtime: _JobRuntime,
        status: str,
        exit_code: int | None,
        error_message: str | None,
    ) -> None:
        with self._jobs_lock:
            if runtime.status in _TERMINAL:
                return
            runtime.status = status
            runtime.exit_code = exit_code
            runtime.error_message = error_message
        self.database.update_satellite_job(
            runtime.job_id,
            status=status,
            exit_code=exit_code,
            error_message=error_message,
        )
        runtime.done.set()

    def _fail_job(self, runtime: _JobRuntime, message: str) -> None:
        self._finish_job(runtime, JOB_FAILED, 127, message)

    def _append_log(self, runtime: _JobRuntime, offset: int, data: bytes) -> None:
        if not data:
            return
        with self._log_lock:
            if offset < runtime.log_offset:
                data = data[runtime.log_offset - offset :]
                offset = runtime.log_offset
                if not data:
                    return
            runtime.log_file.parent.mkdir(parents=True, exist_ok=True)
            with runtime.log_file.open("ab") as handle:
                if offset > runtime.log_offset:
                    handle.write(b"\n" * (offset - runtime.log_offset))
                handle.write(data)
            runtime.log_offset = offset + len(data)

    # ------------------------------------------------------------------
    # Reverse channel state
    # ------------------------------------------------------------------

    def _get_stream(self, alias: str) -> _StreamState | None:
        with self._streams_lock:
            state = self._streams.get(alias)
            if state is None:
                return None
            if time.monotonic() - state.last_seen > STREAM_STALE_SECONDS:
                return None
            return state

    def _register_stream(self, alias: str) -> _StreamState:
        state = _StreamState(alias=alias)
        with self._streams_lock:
            old = self._streams.get(alias)
            self._streams[alias] = state
        if old is not None:
            old.queue.put(STREAM_CLOSE)
        return state

    def _drop_stream(self, alias: str, state: _StreamState) -> None:
        with self._streams_lock:
            current = self._streams.get(alias) is state
            if current:
                del self._streams[alias]
        # A superseded stream must not mark a newer connection as offline.
        if current:
            self.database.set_satellite_status(
                alias, "offline", last_error="stream disconnected"
            )

    def _heartbeat(self, alias: str) -> None:
        with self._streams_lock:
            state = self._streams.get(alias)
            if state is not None:
                state.last_seen = time.monotonic()
        self.database.set_satellite_status(alias, "online", last_seen=local_now())

    # ------------------------------------------------------------------
    # Monitoring
    # ------------------------------------------------------------------

    def _monitor_loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(10)
            if self._stop.is_set():
                break
            self._check_streams()
            self._check_forward_satellites()
            self._cleanup_jobs()

    def _check_streams(self) -> None:
        now = time.monotonic()
        stale: list[_StreamState] = []
        with self._streams_lock:
            for state in self._streams.values():
                if now - state.last_seen > STREAM_STALE_SECONDS:
                    stale.append(state)
        for state in stale:
            state.queue.put(STREAM_CLOSE)
            self.database.set_satellite_status(
                state.alias, "offline", last_error="heartbeat timeout"
            )

    def _check_forward_satellites(self) -> None:
        for config in self.database.list_satellites():
            if config.mode == "reverse":
                continue
            if config.mode == "auto" and self._get_stream(config.alias) is not None:
                continue
            if not config.url:
                continue
            self._probe_forward(config)

    def _probe_forward(self, config: SatelliteConfig) -> None:
        try:
            client = self._forward_client(config)
            payload = client.request("GET", PATH_HEALTH, auth=False, timeout=10)
        except Exception as exc:
            self.database.set_satellite_status(
                config.alias, "offline", last_error=str(exc)[:500]
            )
        else:
            remote_version = str(payload.get("version", "") or "")
            if remote_version and remote_version != self.agent_version:
                self.database.set_satellite_status(
                    config.alias,
                    "offline",
                    last_error=self.version_mismatch_message(remote_version),
                )
            else:
                self.database.set_satellite_status(
                    config.alias,
                    "online",
                    platform=str(payload.get("platform", "") or ""),
                )

    def _cleanup_jobs(self) -> None:
        cutoff = time.monotonic() - 3600
        with self._jobs_lock:
            stale = [
                job_id
                for job_id, runtime in self._jobs.items()
                if runtime.status in _TERMINAL and runtime.created < cutoff
            ]
            for job_id in stale:
                del self._jobs[job_id]


# ---------------------------------------------------------------------------
# Flask application (satellite-facing service)
# ---------------------------------------------------------------------------


def create_satellite_master_app(manager: SatelliteManager) -> Flask:
    app = Flask(__name__, template_folder=None, static_folder=None)

    def authenticate() -> None:
        alias = request.headers.get(ALIAS_HEADER, "")
        config = manager.database.get_satellite(alias)
        token = config.token if config is not None else ""
        authenticate_request(
            token=token,
            nonce_store=manager.nonce_store,
            nonce=request.headers.get(NONCE_HEADER, ""),
            alias=alias,
            timestamp=request.headers.get(TIMESTAMP_HEADER, ""),
            method=request.method,
            path=request.path,
            provided=request.headers.get(AUTH_HEADER, ""),
            session_store=manager.session_store,
            authorization=request.headers.get("Authorization", ""),
        )

    def require_auth(handler):
        @functools.wraps(handler)
        def wrapper(*args, **kwargs):
            try:
                authenticate()
            except SatelliteAuthError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 401
            return handler(*args, **kwargs)

        return wrapper

    def rate_limit() -> bool:
        return manager.rate_limiter.allow(request.remote_addr or "unknown")

    @app.get(PATH_HEALTH)
    def health():
        return jsonify(
            {
                "ok": True,
                "service": "dagrunner-satellite-master",
                "version": manager.agent_version,
                "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
        )

    @app.get(PATH_NONCE)
    def nonce():
        if not rate_limit():
            return jsonify({"ok": False, "error": "rate limited"}), 429
        return jsonify({"ok": True, "nonce": manager.nonce_store.issue()})

    @app.post(PATH_REGISTER)
    @require_auth
    def register():
        payload = request.get_json(silent=True) or {}
        alias = request.headers.get(ALIAS_HEADER, "")
        config = manager.database.get_satellite(alias)
        if config is None:
            return jsonify({"ok": False, "error": "unknown satellite"}), 403
        agent_version = str(payload.get("agent_version", "") or "")
        if agent_version != manager.agent_version:
            manager.reject_version(alias, agent_version)
            return jsonify(
                {
                    "ok": False,
                    "error_code": VERSION_MISMATCH,
                    "error": manager.version_mismatch_message(agent_version),
                    "server_version": manager.agent_version,
                    "agent_version": agent_version,
                }
            ), 409
        manager.database.set_satellite_status(
            alias,
            "online",
            last_seen=local_now(),
            platform=str(payload.get("platform", "")),
            hostname=str(payload.get("hostname", "")),
            agent_version=str(payload.get("agent_version", "")),
            branch_info=json.dumps(payload.get("branch") or {}, ensure_ascii=False),
        )
        manager.session_store.revoke_alias(alias)
        return jsonify(
            {
                "ok": True,
                "session_token": manager.session_store.create(alias),
                "server_time": datetime.now().astimezone().isoformat(
                    timespec="seconds"
                ),
                "stream_path": PATH_STREAM,
            }
        )

    @app.get(PATH_STREAM)
    @require_auth
    def stream():
        alias = request.headers.get(ALIAS_HEADER, "")
        state = manager._register_stream(alias)
        manager.database.set_satellite_status(alias, "online", last_seen=local_now())

        def generate():
            try:
                # Flush the response status line immediately; otherwise the
                # client waits for the first event before it can even read the
                # headers, which looks like a connection timeout.
                yield sse_event(SSE_EVENT_PING, {"ts": int(time.time())})
                while True:
                    try:
                        frame = state.queue.get(timeout=5)
                    except queue.Empty:
                        frame = sse_event(
                            SSE_EVENT_PING, {"ts": int(time.time())}
                        )
                    else:
                        if frame is STREAM_CLOSE:
                            return
                    yield frame
            finally:
                manager._drop_stream(alias, state)

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post(PATH_HEARTBEAT)
    @require_auth
    def heartbeat():
        manager._heartbeat(request.headers.get(ALIAS_HEADER, ""))
        return jsonify({"ok": True})

    @app.post(f"{ROUTE_JOB}/chunks")
    @require_auth
    def job_chunks(job_id: str):
        payload = request.get_json(silent=True) or {}
        with manager._jobs_lock:
            runtime = manager._jobs.get(job_id)
        if runtime is None:
            return jsonify({"ok": False, "error": "unknown job"}), 404
        data = payload.get("data", "")
        if data:
            manager._append_log(
                runtime, int(payload.get("offset", runtime.log_offset)),
                base64.b64decode(data),
            )
        return jsonify({"ok": True, "offset": runtime.log_offset})

    @app.post(f"{ROUTE_JOB}/result")
    @require_auth
    def job_result(job_id: str):
        payload = request.get_json(silent=True) or {}
        with manager._jobs_lock:
            runtime = manager._jobs.get(job_id)
        if runtime is None:
            return jsonify({"ok": False, "error": "unknown job"}), 404
        status = str(payload.get("status", JOB_FAILED))
        if status not in _TERMINAL:
            status = JOB_FAILED
        manager._finish_job(
            runtime,
            status,
            payload.get("exit_code"),
            payload.get("error_message"),
        )
        return jsonify({"ok": True})

    @app.post(f"{ROUTE_JOB}/cancel_ack")
    @require_auth
    def job_cancel_ack(job_id: str):
        with manager._jobs_lock:
            runtime = manager._jobs.get(job_id)
        if runtime is not None:
            runtime.cancel_requested = True
        return jsonify({"ok": True})

    @app.get(PATH_UPDATE_PACKAGE)
    @require_auth
    def update_package():
        version = str(request.args.get("version", "")).strip()
        if not version:
            return jsonify({"ok": False, "error": "missing version"}), 400
        archive = manager.root / "var" / "updates" / "archives" / archive_name(version)
        if not archive.is_file():
            return jsonify({"ok": False, "error": "package not staged"}), 404
        return send_file(
            archive,
            mimetype="application/gzip",
            as_attachment=True,
            download_name=archive.name,
        )

    @app.get(PATH_UPDATE_STATUS)
    @require_auth
    def update_status():
        version = str(request.args.get("version", "")).strip()
        if not version:
            return jsonify({"ok": False, "error": "missing version"}), 400
        try:
            return jsonify({"ok": True, **manager._package_metadata(version)})
        except UpdateError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404

    @app.post(PATH_UPDATE_REPORT)
    @require_auth
    def update_report():
        alias = request.headers.get(ALIAS_HEADER, "")
        payload = request.get_json(silent=True) or {}
        return jsonify(manager.report_update(alias, payload))

    return app
