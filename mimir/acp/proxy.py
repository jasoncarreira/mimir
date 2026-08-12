from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, BinaryIO

from .credentials import CredentialError, NativeCredentialStore
from .profiles import Profile, ProfileError, ProfileStore, selected_profile
from .transport import pump_bidirectional

CONNECT_TIMEOUT = 5.0
MAX_FRAME_BYTES = 1024 * 1024

class ProxyError(RuntimeError):
    pass

class FrameWriter:
    def __init__(self, writer: Any, credential: str) -> None:
        self._writer = writer
        self._credential = credential
        self._buffer = bytearray()

    def write(self, data: bytes) -> None:
        self._buffer.extend(data)
        if len(self._buffer) > MAX_FRAME_BYTES and b"\n" not in self._buffer:
            raise ProxyError("invalid frame")
        while True:
            end = self._buffer.find(b"\n")
            if end < 0: return
            frame = bytes(self._buffer[:end]); del self._buffer[:end + 1]
            if len(frame) > MAX_FRAME_BYTES: raise ProxyError("invalid frame")
            try:
                message = json.loads(frame)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProxyError("invalid frame") from exc
            if isinstance(message, dict) and message.get("method") == "authenticate":
                params = message.get("params")
                if isinstance(params, dict):
                    metadata = params.get("_meta")
                    clean = {key: value for key, value in metadata.items() if isinstance(key, str) and key != "mimir" and not key.startswith("mimir.")} if isinstance(metadata, dict) else {}
                    clean["mimir.webKey"] = self._credential
                    params["_meta"] = clean
            encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
            if len(encoded) > MAX_FRAME_BYTES: raise ProxyError("invalid frame")
            self._writer.write(encoded)

    async def drain(self) -> None: await self._writer.drain()
    def write_eof(self) -> None:
        if self._buffer: raise ProxyError("invalid frame")
        method = getattr(self._writer, "write_eof", None)
        if method is not None: method()
    def close(self) -> None: self._writer.close()
    def is_closing(self) -> bool: return self._writer.is_closing()
    async def wait_closed(self) -> None:
        method = getattr(self._writer, "wait_closed", None)
        if method is not None: await method()
    @property
    def transport(self) -> Any: return getattr(self._writer, "transport", None)

ReservedMetadataWriter = FrameWriter

class _OutputWriter:
    def __init__(self, output: BinaryIO) -> None: self.output, self.closed = output, False
    def write(self, data: bytes) -> None:
        if self.closed: raise BrokenPipeError
        remaining = memoryview(data)
        while remaining:
            size = self.output.write(remaining)
            if size is None: size = len(remaining)
            if size <= 0: raise BrokenPipeError
            remaining = remaining[size:]
        self.output.flush()
    async def drain(self) -> None: self.output.flush()
    def close(self) -> None: self.closed = True
    def is_closing(self) -> bool: return self.closed
    async def wait_closed(self) -> None: return None

async def open_stdio(output: BinaryIO) -> tuple[asyncio.StreamReader, _OutputWriter, asyncio.BaseTransport]:
    loop = asyncio.get_running_loop(); reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
    return reader, _OutputWriter(output), transport


def socket_path(profile: Profile) -> Path:
    path = profile.home / ".mimir" / "acp" / "daemon.sock"
    try:
        value = path.lstat(); directory = path.parent.lstat()
    except OSError as exc: raise ProxyError("connection failed") from exc
    uid = os.getuid()
    if (not stat.S_ISSOCK(value.st_mode) or stat.S_ISLNK(value.st_mode) or value.st_uid != uid or
        not stat.S_ISDIR(directory.st_mode) or stat.S_ISLNK(directory.st_mode) or directory.st_uid != uid or directory.st_mode & 0o077):
        raise ProxyError("connection failed")
    return path

async def run_local_proxy(profile: Profile, credential: str, output: BinaryIO) -> None:
    path = socket_path(profile)
    upstream_reader, upstream_writer = await asyncio.wait_for(asyncio.open_unix_connection(str(path)), CONNECT_TIMEOUT)
    stdin_reader, stdout_writer, stdin_transport = await open_stdio(output)
    try:
        await pump_bidirectional(stdin_reader, stdout_writer, upstream_reader, FrameWriter(upstream_writer, credential))
    finally:
        stdin_transport.close()

async def run_proxy(profile_name: str | None, output: BinaryIO, *, profiles: ProfileStore | None = None, credentials: NativeCredentialStore | None = None) -> None:
    name = selected_profile(profile_name)
    profile = (profiles or ProfileStore()).get(name)
    if profile is None: raise ProfileError("profile-not-found")
    credential = (credentials or NativeCredentialStore()).get(name)
    if credential is None: raise CredentialError("credential-read-failed")
    if profile.remote is not None: raise ProxyError("remote profile requires SSH")
    await run_local_proxy(profile, credential, output)
