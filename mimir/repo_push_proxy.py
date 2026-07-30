"""One-use, scope-bound GitHub receive-pack proxy for typed repository pushes."""

from __future__ import annotations

import base64
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import re
import threading
from types import TracebackType
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .models import RepoPRActionScope


_OID_RE = re.compile(rb"[0-9a-f]{40}")
_MAX_REQUEST_BYTES = 128 * 1024 * 1024
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class PushProxyError(RuntimeError):
    """A safe push-proxy failure that never includes credential material."""


def _github_token() -> str:
    return os.environ.get("GITHUB_TOKEN", "").strip()


def _receive_pack_update(body: bytes) -> tuple[str, str, str]:
    """Parse and return the sole command from a protocol-v0 receive-pack request."""
    commands: list[tuple[str, str, str]] = []
    offset = 0
    while offset + 4 <= len(body):
        try:
            length = int(body[offset:offset + 4], 16)
        except ValueError as exc:
            raise PushProxyError("push request used an invalid packet line") from exc
        offset += 4
        if length == 0:
            break
        if length < 4 or offset + length - 4 > len(body):
            raise PushProxyError("push request used an invalid packet length")
        packet = body[offset:offset + length - 4]
        offset += length - 4
        command = packet.split(b"\0", 1)[0].rstrip(b"\n")
        fields = command.split(b" ")
        if len(fields) != 3 or not _OID_RE.fullmatch(fields[0]) or not _OID_RE.fullmatch(fields[1]):
            raise PushProxyError("push request used an invalid ref update")
        try:
            commands.append((fields[0].decode(), fields[1].decode(), fields[2].decode("ascii")))
        except UnicodeDecodeError as exc:
            raise PushProxyError("push request used an invalid ref name") from exc
    if len(commands) != 1:
        raise PushProxyError("push request must update exactly one ref")
    return commands[0]


class ScopedGitHubPushProxy:
    """Expose only receive-pack for one immutable repository/ref/SHA update.

    The upstream token remains in this server process. Git receives neither the
    token nor a general credential helper; its only network authority is this
    short-lived endpoint, which revalidates the receive-pack command itself.
    """

    def __init__(
        self,
        scope: RepoPRActionScope,
        expected_head: str,
        *,
        token_provider: Callable[[], str] = _github_token,
    ) -> None:
        parsed = urlsplit(scope.canonical_origin)
        expected_paths = {f"/{scope.canonical_repo}", f"/{scope.canonical_repo}.git"}
        if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.path not in expected_paths:
            raise PushProxyError("push credential is unavailable for the scoped origin")
        if parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment:
            raise PushProxyError("scoped origin contains unsupported URL components")
        self._scope = scope
        self._expected_head = expected_head.lower()
        self._upstream = f"https://github.com/{scope.canonical_repo}.git"
        self._token_provider = token_provider
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> str:
        token = self._token_provider().strip()
        if not token:
            raise PushProxyError("push credential is unavailable")
        scope = self._scope
        expected_head = self._expected_head
        upstream = self._upstream
        authorization = "Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _deny(self, status: int, message: str) -> None:
                body = (message + "\n").encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def _forward(self, body: bytes | None = None) -> None:
                split = urlsplit(self.path)
                expected_path = f"/{scope.canonical_repo}.git"
                is_advertisement = (
                    self.command == "GET"
                    and split.path == f"{expected_path}/info/refs"
                    and split.query == "service=git-receive-pack"
                )
                is_push = self.command == "POST" and split.path == f"{expected_path}/git-receive-pack" and not split.query
                if not (is_advertisement or is_push):
                    self._deny(403, "scoped push proxy refused the requested operation")
                    return
                if is_push:
                    try:
                        old, new, ref = _receive_pack_update(body or b"")
                    except PushProxyError as exc:
                        self._deny(403, str(exc))
                        return
                    if (
                        old.lower() != scope.observed_head_sha.lower()
                        or new.lower() != expected_head
                        or ref != scope.destination_ref
                        or new == "0" * 40
                    ):
                        self._deny(403, "push request does not match the immutable scope")
                        return
                url = upstream + split.path[len(expected_path):]
                if split.query:
                    url += "?" + split.query
                headers = {
                    "Authorization": authorization,
                    "User-Agent": "mimir-scoped-repo-push",
                    "Accept": self.headers.get("Accept", "*/*"),
                }
                content_type = self.headers.get("Content-Type")
                if content_type:
                    headers["Content-Type"] = content_type
                request = Request(url, data=body, headers=headers, method=self.command)
                try:
                    response = urlopen(request, timeout=20)
                    status = response.status
                    response_headers = response.headers
                    payload = response.read(_MAX_RESPONSE_BYTES + 1)
                except HTTPError as exc:
                    status = exc.code
                    response_headers = exc.headers
                    payload = exc.read(_MAX_RESPONSE_BYTES + 1)
                except (OSError, URLError):
                    self._deny(502, "scoped push upstream transport failed")
                    return
                if len(payload) > _MAX_RESPONSE_BYTES:
                    self._deny(502, "scoped push upstream response exceeded its limit")
                    return
                self.send_response(status)
                for name in ("Content-Type", "Content-Encoding"):
                    value = response_headers.get(name)
                    if value:
                        self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                self._forward()

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                raw_length = self.headers.get("Content-Length", "")
                try:
                    length = int(raw_length)
                except ValueError:
                    self._deny(411, "scoped push requires a bounded request body")
                    return
                if length <= 0 or length > _MAX_REQUEST_BYTES:
                    self._deny(413, "scoped push request exceeded its limit")
                    return
                self._forward(self.rfile.read(length))

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        port = self._server.server_address[1]
        return f"http://127.0.0.1:{port}/{scope.canonical_repo}.git"

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)


__all__ = ["PushProxyError", "ScopedGitHubPushProxy"]
