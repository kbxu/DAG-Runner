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

from flask import Flask, Response, jsonify, request

from .database import StateDatabase, local_now
from .satellite_crypto import (
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
from .satellite_http import SatelliteClient, SatelliteHttpError
from .satellite_protocol import (
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
    ROUTE_JOB,
    SSE_EVENT_CANCEL,
    SSE_EVENT_JOB,
    SSE_EVENT_PING,
    JobSpec,
    SatelliteConfig,
    sse_event,
)
from .scripting import ExecutionResult


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


class SatelliteManager:
    def __init__(
        self,
        database: StateDatabase,
        *,
        agent_version: str = AGENT_VERSION,
        timeout: float = 15,
        poll_interval: float = 1.0,
    ):
        self.database = database
        self.agent_version = agent_version
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.nonce_store = NonceStore()
        self.session_store = SessionStore()
        self.rate_limiter = RateLimiter(limit=1200, window_seconds=60)
        self._clients: dict[str, SatelliteClient] = {}
        self._jobs: dict[str, _JobRuntime] = {}
        self._jobs_lock = Lock()
        self._log_lock = Lock()
        self._streams: dict[str, _StreamState] = {}
        self._streams_lock = Lock()
        self._stop = Event()
        self._monitor: threading.Thread | None = None

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
        return {
            "reachable": True,
            "mode": "forward",
            "latency_ms": int((time.monotonic() - started) * 1000),
        }

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
        response = client.register(json_body={"role": "master"})
        platform = str(response.get("platform", "") or "")
        if platform:
            self.database.set_satellite_status(
                config.alias,
                "online",
                platform=platform,
                hostname=str(response.get("hostname", "") or ""),
                agent_version=str(response.get("agent_version", "") or ""),
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

        def trust(fingerprint: str) -> bool:
            current = self.database.get_satellite(config.alias)
            expected = (current.cert_fingerprint if current else "") or ""
            if config.host_key_policy == "pinned" and not expected:
                return False
            if expected:
                return hmac.compare_digest(expected.lower(), fingerprint.lower())
            self.database.remember_satellite_fingerprint(config.alias, fingerprint)
            return True

        client = SatelliteClient(
            config.url,
            token=config.token,
            alias=MASTER_ALIAS,
            timeout=self.timeout,
            trust=trust,
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
        manager.database.set_satellite_status(
            alias,
            "online",
            last_seen=local_now(),
            platform=str(payload.get("platform", "")),
            hostname=str(payload.get("hostname", "")),
            agent_version=str(payload.get("agent_version", "")),
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

    return app
