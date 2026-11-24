"""HTTPS serving helpers for the satellite protocol.

waitress has no built-in TLS support and does not complete a handshake on the
non-blocking sockets it uses internally. This module terminates TLS in a small
in-process TCP proxy and forwards the plaintext stream to a waitress server
bound to loopback, which keeps the production WSGI server unchanged.
"""
from __future__ import annotations

import socket
import ssl
import threading

from waitress.server import create_server


class TlsTerminator:
    """Accept TLS connections and pump bytes to a plaintext backend."""

    def __init__(
        self,
        *,
        listen_host: str,
        listen_port: int,
        backend_host: str,
        backend_port: int,
        ssl_context: ssl.SSLContext,
        timeout: float = 30,
    ):
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.backend_host = backend_host
        self.backend_port = backend_port
        self.ssl_context = ssl_context
        self.timeout = timeout
        self._socket: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.listen_host, self.listen_port))
        sock.listen(128)
        self._socket = sock
        self._thread = threading.Thread(
            target=self._accept_loop, daemon=True, name="tls-terminator"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass

    def _accept_loop(self) -> None:
        assert self._socket is not None
        while not self._stop.is_set():
            try:
                client, _ = self._socket.accept()
            except OSError:
                break
            threading.Thread(
                target=self._handle, args=(client,), daemon=True
            ).start()

    def _handle(self, client: socket.socket) -> None:
        try:
            tls = self.ssl_context.wrap_socket(client, server_side=True)
        except (ssl.SSLError, OSError):
            try:
                client.close()
            except OSError:
                pass
            return
        try:
            backend = socket.create_connection(
                (self.backend_host, self.backend_port), timeout=self.timeout
            )
        except OSError:
            try:
                tls.close()
            except OSError:
                pass
            return
        self._pump(tls, backend)

    def _pump(self, first: socket.socket, second: socket.socket) -> None:
        def copy(source: socket.socket, destination: socket.socket) -> None:
            try:
                while True:
                    data = source.recv(65536)
                    if not data:
                        break
                    destination.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    destination.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        worker = threading.Thread(
            target=copy, args=(first, second), daemon=True
        )
        worker.start()
        copy(second, first)
        worker.join(timeout=5)
        for sock in (first, second):
            try:
                sock.close()
            except OSError:
                pass


def serve_ssl(
    app,
    *,
    host: str,
    port: int,
    threads: int,
    ssl_context: ssl.SSLContext,
    backend_host: str = "127.0.0.1",
):
    """Serve a WSGI app over HTTPS; return (waitress_server, terminator)."""
    server = create_server(
        app,
        listen=f"{backend_host}:0",
        threads=threads,
        _start=False,
        url_scheme="https",
    )
    # _start=False skips accept_connections(), which enables the accept loop;
    # call it explicitly before the event loop starts.
    server.accept_connections()
    backend_port = int(server.effective_port)
    thread = threading.Thread(
        target=server.run, daemon=True, name="waitress-backend"
    )
    thread.start()
    terminator = TlsTerminator(
        listen_host=host,
        listen_port=port,
        backend_host=backend_host,
        backend_port=backend_port,
        ssl_context=ssl_context,
    )
    terminator.start()
    return server, terminator
