"""TLS and shared-secret authentication for the satellite protocol.

The master and every satellite generate a self-signed certificate on first
startup. The certificate only protects the transport; the peer is authenticated
with a pre-shared token using a one-time challenge (nonce) and an HMAC, so the
token itself never travels on the wire and cannot be replayed.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Callable

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


AUTH_VERSION = "dagrunner-satellite-v1"
AUTH_HEADER = "X-DAGRunner-Auth"
ALIAS_HEADER = "X-DAGRunner-Alias"
TIMESTAMP_HEADER = "X-DAGRunner-Timestamp"
NONCE_HEADER = "X-DAGRunner-Nonce"
NONCE_TTL_SECONDS = 60
TIMESTAMP_TOLERANCE_SECONDS = 300
CERT_DAYS = 3650
RSA_KEY_SIZE = 2048


class SatelliteAuthError(RuntimeError):
    """Raised when a satellite peer cannot be authenticated."""


# ---------------------------------------------------------------------------
# Certificates
# ---------------------------------------------------------------------------


def ensure_certificate(
    cert_path: str | Path,
    key_path: str | Path,
    *,
    common_name: str = "dagrunner-satellite",
) -> tuple[Path, Path]:
    """Return an existing self-signed certificate, generating one when absent."""
    cert_path = Path(cert_path)
    key_path = Path(key_path)
    if cert_path.is_file() and key_path.is_file():
        try:
            x509.load_pem_x509_certificate(cert_path.read_bytes())
            serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        except (ValueError, TypeError):
            pass
        else:
            return cert_path, key_path

    key = rsa.generate_private_key(public_exponent=65537, key_size=RSA_KEY_SIZE)
    now = datetime.now(timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=CERT_DAYS))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def certificate_fingerprint(cert_path: str | Path) -> str:
    """Return the SHA-256 fingerprint (lowercase hex) of a PEM certificate."""
    certificate = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
    return certificate.fingerprint(hashes.SHA256()).hex()


def peer_fingerprint(sock: ssl.SSLSocket) -> str:
    """Return the SHA-256 fingerprint of the peer certificate of a TLS socket."""
    der = sock.getpeercert(binary_form=True)
    if not der:
        raise SatelliteAuthError("peer did not present a certificate")
    return hashlib.sha256(der).hexdigest()


def server_ssl_context(cert_path: str | Path, key_path: str | Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(cert_path), str(key_path))
    return context


def client_ssl_context() -> ssl.SSLContext:
    """A client context that skips built-in verification.

    The peer certificate is verified manually against a pinned or trust-on-first
    use fingerprint, so the first connection pins it and later connections fail
    closed on any change.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


class FileTrustStore:
    """Trust-on-first-use certificate fingerprint store backed by a JSON file."""

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self._fingerprint: str | None = None
        if self.path is not None and self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                data = {}
            value = data.get("fingerprint")
            if isinstance(value, str) and value:
                self._fingerprint = value.lower()

    @property
    def fingerprint(self) -> str | None:
        return self._fingerprint

    def remember(self, fingerprint: str) -> None:
        """Pin the given fingerprint, persisting it for the next connection."""
        self._fingerprint = fingerprint.lower()
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fingerprint": self._fingerprint,
            "updated_time": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def check(self, fingerprint: str) -> bool:
        """Accept the fingerprint, remembering it on first sight (TOFU)."""
        fingerprint = fingerprint.lower()
        if self._fingerprint is None:
            self.remember(fingerprint)
            return True
        return hmac.compare_digest(self._fingerprint, fingerprint)


# ---------------------------------------------------------------------------
# Challenge-response authentication
# ---------------------------------------------------------------------------


def token_key(token: str) -> bytes:
    return hashlib.sha256(f"{AUTH_VERSION}:token:{token}".encode("utf-8")).digest()


def sign_auth(
    token: str,
    *,
    nonce: str,
    alias: str,
    timestamp: str,
    method: str,
    path: str,
) -> str:
    message = "\n".join(
        (AUTH_VERSION, nonce, alias, timestamp, method.upper(), path)
    )
    digest = hmac.new(token_key(token), message.encode("utf-8"), hashlib.sha256)
    return base64.b64encode(digest.digest()).decode("ascii")


def verify_auth(
    token: str,
    *,
    nonce: str,
    alias: str,
    timestamp: str,
    method: str,
    path: str,
    provided: str,
) -> bool:
    expected = sign_auth(
        token, nonce=nonce, alias=alias, timestamp=timestamp, method=method, path=path
    )
    return hmac.compare_digest(expected, provided)


def timestamp_valid(timestamp: str, *, now: float | None = None) -> bool:
    try:
        value = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = now if now is not None else datetime.now().timestamp()
    return abs(current - value) <= TIMESTAMP_TOLERANCE_SECONDS


class NonceStore:
    """Single-use, short-lived challenge nonces."""

    def __init__(
        self, ttl_seconds: int = NONCE_TTL_SECONDS, max_entries: int = 4096
    ):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._entries: dict[str, float] = {}
        self._lock = Lock()

    def issue(self) -> str:
        nonce = secrets.token_hex(16)
        with self._lock:
            self._cleanup()
            self._entries[nonce] = datetime.now().timestamp()
        return nonce

    def consume(self, nonce: str) -> bool:
        with self._lock:
            self._cleanup()
            if nonce in self._entries:
                del self._entries[nonce]
                return True
            return False

    def _cleanup(self) -> None:
        now = datetime.now().timestamp()
        expired = [
            nonce
            for nonce, issued in self._entries.items()
            if now - issued > self.ttl_seconds
        ]
        for nonce in expired:
            del self._entries[nonce]
        while len(self._entries) > self.max_entries:
            oldest = min(self._entries, key=self._entries.__getitem__)
            del self._entries[oldest]


class SessionStore:
    """Bearer sessions issued after a successful challenge-response registration.

    Registration authenticates with a one-time nonce; the returned session token
    then authorizes the rest of the connection (polling, log streaming, heartbeats)
    so the nonce endpoint is not hit on every request.
    """

    def __init__(self, ttl_seconds: int = 3600, max_entries: int = 4096):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._sessions: dict[str, tuple[str, float]] = {}
        self._lock = Lock()

    def create(self, alias: str) -> str:
        token = secrets.token_urlsafe(32)
        now = datetime.now().timestamp()
        with self._lock:
            self._cleanup(now)
            self._sessions[token] = (alias, now)
        return token

    def resolve(self, token: str) -> str | None:
        now = datetime.now().timestamp()
        with self._lock:
            self._cleanup(now)
            entry = self._sessions.get(token)
            if entry is None:
                return None
            alias, _ = entry
            self._sessions[token] = (alias, now)
            return alias

    def revoke(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def revoke_alias(self, alias: str) -> None:
        with self._lock:
            stale = [
                token
                for token, (name, _) in self._sessions.items()
                if name == alias
            ]
            for token in stale:
                del self._sessions[token]

    def _cleanup(self, now: float) -> None:
        expired = [
            token
            for token, (_, used) in self._sessions.items()
            if now - used > self.ttl_seconds
        ]
        for token in expired:
            del self._sessions[token]
        while len(self._sessions) > self.max_entries:
            oldest = min(self._sessions, key=lambda item: self._sessions[item][1])
            del self._sessions[oldest]


class RateLimiter:
    """Fixed-window per-key rate limiter used to protect the auth endpoints."""

    def __init__(self, limit: int = 10, window_seconds: int = 60):
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[str, tuple[float, int]] = {}
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        now = datetime.now().timestamp()
        with self._lock:
            started, count = self._hits.get(key, (now, 0))
            if now - started > self.window_seconds:
                started, count = now, 0
            count += 1
            self._hits[key] = (started, count)
            return count <= self.limit


def authenticate_request(
    *,
    token: str,
    nonce_store: NonceStore,
    nonce: str,
    alias: str,
    timestamp: str,
    method: str,
    path: str,
    provided: str,
    session_store: SessionStore | None = None,
    authorization: str = "",
) -> None:
    """Raise :class:`SatelliteAuthError` unless the request is authenticated.

    A bearer session token (issued by ``register``) authorizes the connection;
    otherwise the one-time nonce challenge-response is verified.
    """
    if authorization:
        scheme, _, credential = authorization.partition(" ")
        if scheme.lower() != "bearer" or not credential:
            raise SatelliteAuthError("unsupported authorization scheme")
        if session_store is None:
            raise SatelliteAuthError("sessions are not supported")
        session_alias = session_store.resolve(credential)
        if session_alias is None:
            raise SatelliteAuthError("unknown or expired session")
        if session_alias != alias:
            raise SatelliteAuthError("session does not match the declared alias")
        return
    if not alias or not provided:
        raise SatelliteAuthError("missing satellite credentials")
    if not timestamp_valid(timestamp):
        raise SatelliteAuthError("stale or invalid timestamp")
    if not nonce_store.consume(nonce):
        raise SatelliteAuthError("unknown or reused nonce")
    if not verify_auth(
        token,
        nonce=nonce,
        alias=alias,
        timestamp=timestamp,
        method=method,
        path=path,
        provided=provided,
    ):
        raise SatelliteAuthError("invalid satellite signature")


def build_auth_headers(
    token: str,
    *,
    nonce: str,
    alias: str,
    method: str,
    path: str,
    timestamp: float | None = None,
) -> dict[str, str]:
    """Build the authenticated request headers for an outbound satellite call."""
    now = int(timestamp if timestamp is not None else datetime.now().timestamp())
    stamp = str(now)
    return {
        ALIAS_HEADER: alias,
        TIMESTAMP_HEADER: stamp,
        NONCE_HEADER: nonce,
        AUTH_HEADER: sign_auth(
            token, nonce=nonce, alias=alias, timestamp=stamp, method=method, path=path
        ),
    }
