"""The satellite agent: a standalone process running on a target machine.

It exposes an HTTPS endpoint for forward-mode dispatch and, when it can reach
the master, registers itself and receives jobs over an SSE stream (reverse
mode). Jobs run locally with bash on Linux and cmd on Windows by default.
"""
from __future__ import annotations

import base64
import functools
import hmac
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Event, Lock

from flask import Flask, jsonify, request

from .satellite_crypto import (
    ALIAS_HEADER,
    AUTH_HEADER,
    NONCE_HEADER,
    TIMESTAMP_HEADER,
    FileTrustStore,
    NonceStore,
    RateLimiter,
    SatelliteAuthError,
    SessionStore,
    authenticate_request,
    ensure_certificate,
    server_ssl_context,
)
from .satellite_server import serve_ssl
from .satellite_http import SatelliteClient
from .satellite_protocol import (
    AGENT_VERSION,
    JOB_CANCELLED,
    JOB_ERROR,
    JOB_FAILED,
    JOB_PENDING,
    JOB_RUNNING,
    JOB_SUCCESS,
    PATH_HEARTBEAT,
    PATH_JOB_CHUNKS,
    PATH_JOB_RESULT,
    PATH_HEALTH,
    PATH_JOBS,
    PATH_NONCE,
    PATH_PING,
    PATH_REGISTER,
    PATH_STREAM,
    ROUTE_JOB,
    ROUTE_JOB_CANCEL,
    SSE_EVENT_CANCEL,
    SSE_EVENT_JOB,
    JobSpec,
    ProtocolError,
    build_job_plan,
)
from .scripting import (
    append_error,
    remove_temp_script,
    terminate_process,
    write_log_header,
)


TERMINAL = {JOB_SUCCESS, JOB_FAILED, JOB_CANCELLED, JOB_ERROR}


def _is_timeout_error(exc: BaseException) -> bool:
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    text = str(exc).lower()
    return "timed out" in text or "timeout" in text


@dataclass
class SatelliteAgentConfig:
    alias: str
    master_url: str
    token: str
    mode: str = "auto"
    listen_host: str = "0.0.0.0"
    listen_port: int = 7121
    cert: str = "var/satellite-certs/cert.pem"
    key: str = "var/satellite-certs/key.pem"
    host_key_policy: str = "accept-new"
    master_cert_fingerprint: str = ""
    workdir: str = ""
    log_dir: str = "var/satellite-logs"
    heartbeat_interval: int = 30
    probe_interval: int = 60
    timeout: float = 15


@dataclass
class _AgentJob:
    job_id: str
    spec: JobSpec
    transport: str
    log_file: Path
    status: str = JOB_PENDING
    exit_code: int | None = None
    error_message: str | None = None
    pushed_offset: int = 0
    process: subprocess.Popen | None = None
    cancel_event: Event = field(default_factory=Event)
    done: Event = field(default_factory=Event)


class SatelliteAgent:
    def __init__(self, config: SatelliteAgentConfig):
        self.config = config
        self.nonce_store = NonceStore()
        self.session_store = SessionStore()
        self.rate_limiter = RateLimiter(limit=1200, window_seconds=60)
        self.jobs: dict[str, _AgentJob] = {}
        self._client: SatelliteClient | None = None
        self.jobs_lock = Lock()
        self._stop = Event()
        self._trust_store = FileTrustStore(
            Path(config.cert).resolve().parent / "known_master.json"
        )
        self._last_error: str | None = None

    # ------------------------------------------------------------------
    # Trust
    # ------------------------------------------------------------------

    def _trust(self, fingerprint: str) -> bool:
        if self.config.host_key_policy == "pinned":
            if not self.config.master_cert_fingerprint:
                return False
            return hmac.compare_digest(
                self.config.master_cert_fingerprint.lower(), fingerprint.lower()
            )
        return self._trust_store.check(fingerprint)

    def _master_client(self) -> SatelliteClient:
        if self._client is None:
            self._client = SatelliteClient(
                self.config.master_url,
                token=self.config.token,
                alias=self.config.alias,
                timeout=self.config.timeout,
                stream_timeout=60,
                trust=self._trust,
            )
        return self._client

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(self) -> int:
        self._install_signal_handlers()
        ensure_certificate(
            self.config.cert,
            self.config.key,
            common_name=f"dagrunner-satellite-{self.config.alias}",
        )
        app = create_satellite_agent_app(self)
        context = server_ssl_context(self.config.cert, self.config.key)
        try:
            self._server, self._terminator = serve_ssl(
                app,
                host=self.config.listen_host,
                port=self.config.listen_port,
                threads=16,
                ssl_context=context,
            )
        except OSError as exc:
            raise SystemExit(
                f"卫星服务端口绑定失败 "
                f"{self.config.listen_host}:{self.config.listen_port}: {exc}"
            ) from exc
        agent_thread = threading.Thread(
            target=self._agent_loop, daemon=True, name="satellite-agent"
        )
        agent_thread.start()
        print(
            f"dagrunner satellite {self.config.alias!r} listening on "
            f"https://{self.config.listen_host}:{self.config.listen_port}"
        )
        print(f"mode: {self.config.mode}, master: {self.config.master_url}")
        try:
            while not self._stop.is_set():
                self._stop.wait(1)
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()
        return 0

    def _install_signal_handlers(self) -> None:
        def handle(signum, frame):
            self._stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handle)
            except (ValueError, OSError, AttributeError):
                pass

    def shutdown(self) -> None:
        self._stop.set()
        with self.jobs_lock:
            jobs = list(self.jobs.values())
        for job in jobs:
            job.cancel_event.set()
            if job.process is not None:
                terminate_process(job.process)

    # ------------------------------------------------------------------
    # Reverse mode
    # ------------------------------------------------------------------

    def _agent_loop(self) -> None:
        while not self._stop.is_set():
            if self.config.mode == "forward":
                self._stop.wait(self.config.probe_interval)
                continue
            try:
                self._connect_reverse()
                self._last_error = None
            except Exception as exc:
                self._last_error = str(exc)
                print(f"satellite reverse channel unavailable: {exc}", file=sys.stderr)
                self._stop.wait(
                    5 if self.config.mode == "reverse" else self.config.probe_interval
                )
            else:
                self._stop.wait(1)

    def _connect_reverse(self) -> None:
        client = self._master_client()
        client.register(
            json_body={
                "alias": self.config.alias,
                "agent_version": AGENT_VERSION,
                "platform": "windows" if os.name == "nt" else "linux",
                "hostname": socket.gethostname(),
                "listener_addr": f"{self.config.listen_host}:{self.config.listen_port}",
            }
        )
        connection, response = client.open_stream(PATH_STREAM)
        stop_heartbeat = Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(stop_heartbeat,),
            daemon=True,
            name="satellite-heartbeat",
        )
        heartbeat.start()
        try:
            for event, data in self._read_sse(response):
                if event == SSE_EVENT_JOB:
                    self._handle_job_event(data)
                elif event == SSE_EVENT_CANCEL:
                    self._handle_cancel_event(data)
        finally:
            stop_heartbeat.set()
            try:
                connection.close()
            except OSError:
                pass

    def _heartbeat_loop(self, stop_event: Event) -> None:
        while not stop_event.wait(self.config.heartbeat_interval):
            try:
                client = self._master_client()
                client.request("POST", PATH_HEARTBEAT, json_body={})
            except Exception:
                pass

    def _read_sse(self, response):
        event = "message"
        data_lines: list[str] = []
        while not self._stop.is_set():
            try:
                line = response.readline()
            except OSError as exc:
                # A read timeout is not a dead stream: the master sends pings
                # every few seconds. Anything else means the stream is gone.
                if _is_timeout_error(exc):
                    continue
                break
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            if text == "":
                if data_lines:
                    yield event, "\n".join(data_lines)
                event, data_lines = "message", []
                continue
            if text.startswith(":"):
                continue
            field, _, value = text.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field == "event":
                event = value
            elif field == "data":
                data_lines.append(value)

    def _handle_job_event(self, data: str) -> None:
        try:
            payload = json.loads(data)
            spec = JobSpec.from_json(payload.get("spec"))
        except (ValueError, ProtocolError) as exc:
            print(f"satellite ignored invalid job: {exc}", file=sys.stderr)
            return
        self._start_job(spec, transport="reverse")

    def _handle_cancel_event(self, data: str) -> None:
        try:
            payload = json.loads(data)
        except ValueError:
            return
        job_id = str(payload.get("job_id", ""))
        with self.jobs_lock:
            job = self.jobs.get(job_id)
        if job is not None:
            job.cancel_event.set()

    # ------------------------------------------------------------------
    # Job execution
    # ------------------------------------------------------------------

    def _start_job(self, spec: JobSpec, *, transport: str) -> _AgentJob:
        job = _AgentJob(
            job_id=spec.job_id,
            spec=spec,
            transport=transport,
            log_file=Path(self.config.log_dir).resolve() / f"{spec.job_id}.log",
        )
        with self.jobs_lock:
            self.jobs[spec.job_id] = job
        threading.Thread(
            target=self._run_job_thread,
            args=(job,),
            daemon=True,
            name=f"satellite-job-{spec.job_id}",
        ).start()
        if transport == "reverse":
            threading.Thread(
                target=self._log_pusher,
                args=(job,),
                daemon=True,
                name=f"satellite-push-{spec.job_id}",
            ).start()
        return job

    def _run_job_thread(self, job: _AgentJob) -> None:
        try:
            self._execute_job(job)
        except Exception as exc:
            self._finish_job(job, JOB_FAILED, 127, f"task executor crashed: {exc}")

    def _execute_job(self, job: _AgentJob) -> None:
        spec = job.spec
        try:
            plan = build_job_plan(spec, self.config.workdir)
        except Exception as exc:
            self._finish_job(job, JOB_FAILED, 127, f"could not build command: {exc}")
            return
        job.status = JOB_RUNNING
        job.log_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            with job.log_file.open("wb") as output:
                write_log_header(output, plan.script)
        except OSError as exc:
            self._finish_job(job, JOB_FAILED, 127, f"could not write log: {exc}")
            return
        try:
            with job.log_file.open("ab") as output:
                process = subprocess.Popen(
                    plan.argv,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=os.name != "nt",
                    creationflags=(
                        subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                    ),
                )
                job.process = process
                started = time.monotonic()
                while process.poll() is None:
                    if job.cancel_event.is_set():
                        terminate_process(process)
                        self._finish_job(job, JOB_CANCELLED, 130, "task stopped by user")
                        return
                    if spec.timeout and time.monotonic() - started >= spec.timeout:
                        terminate_process(process)
                        message = f"task timed out after {spec.timeout} seconds"
                        append_error(job.log_file, message)
                        self._finish_job(job, JOB_FAILED, 124, message)
                        return
                    time.sleep(0.2)
            exit_code = process.returncode if process.returncode is not None else 1
            if exit_code == 0:
                self._finish_job(job, JOB_SUCCESS, 0, None)
            else:
                message = f"command exited with code {exit_code}"
                append_error(job.log_file, message)
                self._finish_job(job, JOB_FAILED, exit_code, message)
        except OSError as exc:
            message = f"could not start command: {exc}"
            append_error(job.log_file, message)
            self._finish_job(job, JOB_FAILED, 127, message)
        finally:
            # The shell reads the script file, so it must outlive the process.
            if plan.temp_script is not None:
                remove_temp_script(plan.temp_script)

    def _finish_job(
        self,
        job: _AgentJob,
        status: str,
        exit_code: int | None,
        error_message: str | None,
    ) -> None:
        with self.jobs_lock:
            if job.status in TERMINAL:
                return
            job.status = status
            job.exit_code = exit_code
            job.error_message = error_message
        self._push_log(job)
        job.done.set()
        if job.transport == "reverse":
            self._post_result(job)

    def _post_result(self, job: _AgentJob) -> None:
        payload = {
            "status": job.status,
            "exit_code": job.exit_code,
            "error_message": job.error_message,
        }
        for attempt in range(3):
            try:
                client = self._master_client()
                client.request(
                    "POST",
                    PATH_JOB_RESULT.format(job_id=job.job_id),
                    json_body=payload,
                )
                return
            except Exception:
                if attempt < 2:
                    time.sleep(1 + attempt)

    def _log_pusher(self, job: _AgentJob) -> None:
        while not job.done.is_set():
            self._push_log(job)
            job.done.wait(1.0)
        self._push_log(job)

    def _push_log(self, job: _AgentJob) -> None:
        data, new_offset = self._read_log_delta(job, job.pushed_offset)
        if not data:
            return
        if job.transport != "reverse":
            return
        try:
            client = self._master_client()
            client.request(
                "POST",
                PATH_JOB_CHUNKS.format(job_id=job.job_id),
                json_body={
                    "offset": job.pushed_offset,
                    "data": base64.b64encode(data).decode("ascii"),
                },
            )
            job.pushed_offset = new_offset
        except Exception:
            pass

    @staticmethod
    def _read_log_delta(job: _AgentJob, offset: int) -> tuple[bytes, int]:
        try:
            data = job.log_file.read_bytes()
        except OSError:
            return b"", offset
        if offset < 0:
            offset = 0
        if offset > len(data):
            offset = len(data)
        return data[offset:], len(data)


# ---------------------------------------------------------------------------
# Flask application (master-facing service)
# ---------------------------------------------------------------------------


def create_satellite_agent_app(agent: SatelliteAgent) -> Flask:
    app = Flask(__name__, template_folder=None, static_folder=None)

    def authenticate() -> None:
        alias = request.headers.get(ALIAS_HEADER, "")
        authenticate_request(
            token=agent.config.token,
            nonce_store=agent.nonce_store,
            nonce=request.headers.get(NONCE_HEADER, ""),
            alias=alias,
            timestamp=request.headers.get(TIMESTAMP_HEADER, ""),
            method=request.method,
            path=request.path,
            provided=request.headers.get(AUTH_HEADER, ""),
            session_store=agent.session_store,
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
        return agent.rate_limiter.allow(request.remote_addr or "unknown")

    @app.get(PATH_HEALTH)
    def health():
        return jsonify(
            {
                "ok": True,
                "service": "dagrunner-satellite-agent",
                "alias": agent.config.alias,
                "version": AGENT_VERSION,
                "platform": "windows" if os.name == "nt" else "linux",
                "hostname": socket.gethostname(),
                "mode": agent.config.mode,
                "last_error": agent._last_error,
                "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
        )

    @app.get(PATH_PING)
    @require_auth
    def ping():
        return jsonify({"ok": True, "alias": agent.config.alias})

    @app.get(PATH_NONCE)
    def nonce():
        if not rate_limit():
            return jsonify({"ok": False, "error": "rate limited"}), 429
        return jsonify({"ok": True, "nonce": agent.nonce_store.issue()})

    @app.post(PATH_REGISTER)
    @require_auth
    def register():
        alias = request.headers.get(ALIAS_HEADER, "")
        agent.session_store.revoke_alias(alias)
        return jsonify(
            {
                "ok": True,
                "session_token": agent.session_store.create(alias),
                "agent": agent.config.alias,
                "platform": "windows" if os.name == "nt" else "linux",
                "hostname": socket.gethostname(),
                "agent_version": AGENT_VERSION,
            }
        )

    @app.post(PATH_JOBS)
    @require_auth
    def create_job():
        payload = request.get_json(silent=True) or {}
        try:
            spec = JobSpec.from_json(payload)
        except ProtocolError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        with agent.jobs_lock:
            if spec.job_id in agent.jobs:
                return jsonify({"ok": False, "error": "duplicate job"}), 409
        job = agent._start_job(spec, transport="forward")
        return jsonify({"ok": True, "job_id": job.job_id})

    @app.get(ROUTE_JOB)
    @require_auth
    def job_status(job_id: str):
        with agent.jobs_lock:
            job = agent.jobs.get(job_id)
        if job is None:
            return jsonify({"ok": False, "error": "unknown job"}), 404
        offset = max(0, int(request.args.get("offset", job.pushed_offset)))
        data, _ = agent._read_log_delta(job, offset)
        return jsonify(
            {
                "ok": True,
                "job_id": job_id,
                "status": job.status,
                "exit_code": job.exit_code,
                "error_message": job.error_message,
                "log_offset": offset,
                "log_data": base64.b64encode(data).decode("ascii"),
                "finished": job.status in TERMINAL,
            }
        )

    @app.post(ROUTE_JOB_CANCEL)
    @require_auth
    def cancel_job(job_id: str):
        with agent.jobs_lock:
            job = agent.jobs.get(job_id)
        if job is None:
            return jsonify({"ok": False, "error": "unknown job"}), 404
        job.cancel_event.set()
        if job.process is not None:
            terminate_process(job.process)
        return jsonify({"ok": True})

    return app
