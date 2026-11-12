"""HTTPS client for the satellite protocol.

Registration authenticates with a one-time nonce challenge; the returned bearer
session token then authorizes polling, log streaming, and heartbeats so the
nonce endpoint is not hit on every request.
"""
from __future__ import annotations

import http.client
import json
import urllib.parse

from .satellite_crypto import (
    ALIAS_HEADER,
    NonceStore,
    build_auth_headers,
    client_ssl_context,
    peer_fingerprint,
)
from .satellite_protocol import PATH_NONCE, PATH_REGISTER


class SatelliteHttpError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        payload: bytes | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class SatelliteClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str,
        alias: str,
        ssl_context=None,
        timeout: float = 15,
        trust=None,
        nonce_ttl: int = 120,
        register_body: dict | None = None,
    ):
        parsed = urllib.parse.urlsplit(base_url.rstrip("/"))
        if parsed.scheme != "https":
            raise ValueError("satellite URL must use https")
        if not parsed.hostname:
            raise ValueError("satellite URL must include a host")
        self.base_url = base_url.rstrip("/")
        self.host = parsed.hostname
        self.port = parsed.port or 443
        self.token = token
        self.alias = alias
        self.ssl_context = ssl_context or client_ssl_context()
        self.timeout = timeout
        self.trust = trust
        self._nonce_store = NonceStore(ttl_seconds=nonce_ttl)
        self._register_body = register_body or {}
        self._session_token: str | None = None

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    def _connect(self, timeout: float | None = None) -> http.client.HTTPSConnection:
        connection = http.client.HTTPSConnection(
            self.host,
            self.port,
            context=self.ssl_context,
            timeout=timeout if timeout is not None else self.timeout,
        )
        connection.connect()
        if self.trust is not None:
            fingerprint = peer_fingerprint(connection.sock)
            if not self.trust(fingerprint):
                connection.close()
                raise SatelliteHttpError("server certificate fingerprint mismatch")
        return connection

    def _fetch_nonce(self, timeout: float | None = None) -> str:
        connection = self._connect(timeout)
        try:
            connection.request("GET", PATH_NONCE)
            response = connection.getresponse()
            data = response.read()
            if response.status != 200:
                raise SatelliteHttpError(
                    "nonce request failed",
                    status_code=response.status,
                    payload=data,
                )
            payload = json.loads(data or b"{}")
            nonce = payload.get("nonce")
            if not nonce:
                raise SatelliteHttpError("nonce response missing nonce")
            return str(nonce)
        finally:
            connection.close()

    def _raw_request(
        self,
        method: str,
        path: str,
        *,
        headers: dict | None = None,
        json_body: dict | None = None,
        timeout: float | None = None,
        ok: tuple[int, ...] = (200,),
    ) -> dict:
        request_headers = dict(headers or {})
        body = None
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        connection = self._connect(timeout)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            data = response.read()
            if response.status not in ok:
                raise SatelliteHttpError(
                    f"{method} {path} failed with {response.status}",
                    status_code=response.status,
                    payload=data,
                )
            return json.loads(data) if data else {}
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _challenge_headers(
        self, method: str, path: str, timeout: float | None = None
    ) -> dict[str, str]:
        nonce = self._fetch_nonce(timeout)
        return build_auth_headers(
            self.token,
            nonce=nonce,
            alias=self.alias,
            method=method,
            path=path,
        )

    def _authorized_headers(
        self, method: str, path: str, timeout: float | None = None
    ) -> dict[str, str]:
        headers = {"Accept": "application/json", ALIAS_HEADER: self.alias}
        if self._session_token:
            headers["Authorization"] = f"Bearer {self._session_token}"
        else:
            headers.update(self._challenge_headers(method, path, timeout))
        return headers

    def register(
        self, *, timeout: float | None = None, json_body: dict | None = None
    ) -> dict:
        """Authenticate with the challenge-response and store a session token."""
        headers = self._challenge_headers("POST", PATH_REGISTER, timeout)
        body = json_body if json_body is not None else self._register_body
        response = self._raw_request(
            "POST", PATH_REGISTER, headers=headers, json_body=body, timeout=timeout
        )
        token = response.get("session_token")
        if token:
            self._session_token = token
        return response

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        timeout: float | None = None,
        auth: bool = True,
        ok: tuple[int, ...] = (200,),
    ) -> dict:
        headers = {"Accept": "application/json"}
        if auth:
            headers.update(self._authorized_headers(method, path, timeout))
        try:
            return self._raw_request(
                method, path, headers=headers, json_body=json_body,
                timeout=timeout, ok=ok,
            )
        except SatelliteHttpError as exc:
            if not auth or exc.status_code != 401 or not self._session_token:
                raise
            # The session expired: register again and retry once.
            self._session_token = None
            self.register(timeout=timeout)
            headers = {
                "Accept": "application/json",
                ALIAS_HEADER: self.alias,
                "Authorization": f"Bearer {self._session_token}",
            }
            return self._raw_request(
                method, path, headers=headers, json_body=json_body,
                timeout=timeout, ok=ok,
            )

    def open_stream(self, path: str, *, timeout: float | None = None):
        """Open an SSE stream and return (connection, response)."""
        headers = self._authorized_headers("GET", path, timeout)
        headers["Accept"] = "text/event-stream"
        connection = http.client.HTTPSConnection(
            self.host,
            self.port,
            context=self.ssl_context,
            timeout=timeout if timeout is not None else self.timeout,
        )
        connection.connect()
        if self.trust is not None:
            fingerprint = peer_fingerprint(connection.sock)
            if not self.trust(fingerprint):
                connection.close()
                raise SatelliteHttpError("server certificate fingerprint mismatch")
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            data = response.read()
            connection.close()
            raise SatelliteHttpError(
                f"stream failed with {response.status}",
                status_code=response.status,
                payload=data,
            )
        return connection, response
