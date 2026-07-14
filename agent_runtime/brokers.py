"""Trusted host brokers for provider-only egress and read-only GitHub search."""

from __future__ import annotations

import json
import os
import selectors
import socket
import socketserver
import subprocess
import sys
import threading
import urllib.parse
from pathlib import Path
from typing import Any

from .redaction import sanitize_message

MAX_HEADER = 65536


class BrokerError(ValueError):
    pass


def _recv_header(connection: socket.socket) -> tuple[bytes, bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > MAX_HEADER:
            raise BrokerError("proxy header exceeded its byte bound")
    head, marker, tail = data.partition(b"\r\n\r\n")
    if not marker:
        raise BrokerError("proxy request header is incomplete")
    return head + marker, tail


def _allowed_host(host: str, allowlist: tuple[str, ...]) -> bool:
    candidate = host.rstrip(".").casefold()
    for allowed in allowlist:
        rule = allowed.rstrip(".").casefold()
        if rule.startswith("*."):
            suffix = rule[1:]
            if candidate.endswith(suffix) and candidate != suffix[1:]:
                return True
        elif candidate == rule:
            return True
    return False


def _relay(left: socket.socket, right: socket.socket, initial: bytes = b"") -> None:
    if initial:
        right.sendall(initial)
    selector = selectors.DefaultSelector()
    selector.register(left, selectors.EVENT_READ, right)
    selector.register(right, selectors.EVENT_READ, left)
    try:
        while selector.get_map():
            for key, _ in selector.select(timeout=60):
                source = key.fileobj
                destination = key.data
                try:
                    chunk = source.recv(65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    try:
                        destination.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    selector.unregister(source)
                    continue
                destination.sendall(chunk)
    finally:
        selector.close()


class _ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


class ProviderProxy:
    """A CONNECT proxy reachable only through a mounted Unix socket."""

    def __init__(self, socket_path: str, allowed_hosts: list[str]) -> None:
        self.socket_path = socket_path
        self.allowed_hosts = tuple(allowed_hosts)
        proxy = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                try:
                    header, tail = _recv_header(self.request)
                    lines = header.split(b"\r\n")
                    method, target, _ = lines[0].decode("ascii", "strict").split(" ", 2)
                    if method.upper() == "CONNECT":
                        host, separator, port_text = target.rpartition(":")
                        if not separator or not port_text.isdigit():
                            raise BrokerError("proxy CONNECT target is invalid")
                        port = int(port_text)
                        if port != 443:
                            raise BrokerError("provider proxy permits TLS port 443 only")
                    else:
                        parsed = urllib.parse.urlsplit(target)
                        host = parsed.hostname or ""
                        port = parsed.port or (443 if parsed.scheme == "https" else 80)
                        if parsed.scheme not in ("http", "https"):
                            raise BrokerError("provider proxy request scheme is invalid")
                    if not _allowed_host(host, proxy.allowed_hosts):
                        raise BrokerError("provider endpoint is outside the selected auth profile")
                    upstream = socket.create_connection((host, port), timeout=20)
                    try:
                        if method.upper() == "CONNECT":
                            self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                            _relay(self.request, upstream, tail)
                        else:
                            _relay(self.request, upstream, header + tail)
                    finally:
                        upstream.close()
                except Exception:
                    try:
                        self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                    except OSError:
                        pass

        Path(socket_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass
        self.server = _ThreadingUnixServer(socket_path, Handler)
        os.chmod(socket_path, 0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass


class SearchBroker:
    """Owner-scoped read-only GitHub tool broker.

    Only this trusted host process sees READONLY_TOKEN. Child ``gh`` receives a
    scrubbed environment containing that token and fixed command shapes from the
    existing Wheelhouse search implementation.
    """

    def __init__(self, socket_path: str, owner: str, target_repo: str, token: str, config: dict[str, Any]) -> None:
        if not token:
            raise BrokerError("read-only GitHub search broker credential is missing")
        scripts = str(Path(__file__).resolve().parents[1] / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import nl_readonly_search as search

        self.search = search
        self.token = token
        self.allowed = search.allowed_repos(owner, target_repo, config=config)
        broker = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                data = b""
                while not data.endswith(b"\n"):
                    chunk = self.request.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                    if len(data) > search.MAX_REQUEST_BYTES:
                        break
                response: dict[str, Any]
                try:
                    if len(data) > search.MAX_REQUEST_BYTES:
                        raise ValueError("search request exceeded its bound")
                    request = json.loads(data.decode("utf-8"))

                    def runner(arguments: list[str]) -> str:
                        environment = {
                            "GH_TOKEN": broker.token,
                            "GH_PROMPT_DISABLED": "1",
                            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                            "HOME": os.environ.get("RUNNER_TEMP", "/tmp"),
                            "LC_ALL": "C.UTF-8",
                            "TZ": "UTC",
                        }
                        completed = subprocess.run(["gh"] + list(arguments), capture_output=True, text=True, env=environment, timeout=30)
                        output = completed.stdout
                        if completed.returncode:
                            output += "\n[gh read failed]\n"
                        return search._cap(output)

                    text = search.handle_request(request, broker.allowed, runner=runner)
                    response = {"ok": True, "text": text, "truncated": text.endswith("[output truncated]\n")}
                except Exception as error:
                    response = {"ok": False, "error": sanitize_message(str(error), fallback="search request rejected")}
                self.request.sendall(json.dumps(response, separators=(",", ":")).encode("utf-8"))

        Path(socket_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass
        self.socket_path = socket_path
        self.server = _ThreadingUnixServer(socket_path, Handler)
        os.chmod(socket_path, 0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
