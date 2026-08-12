from __future__ import annotations

import asyncio
import os
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from .transport import close_writer, pump_bidirectional

CONNECT_TIMEOUT = 5.0

class RelayError(RuntimeError):
    pass

class _Output:
    def __init__(self, stream: BinaryIO) -> None: self.stream, self.closed = stream, False
    def write(self, data: bytes) -> None:
        remaining = memoryview(data)
        while remaining:
            written = self.stream.write(remaining)
            if written is None:
                written = len(remaining)
            if written <= 0:
                raise BrokenPipeError
            remaining = remaining[written:]
        self.stream.flush()
    async def drain(self) -> None: self.stream.flush()
    def close(self) -> None: self.closed = True
    def is_closing(self) -> bool: return self.closed
    async def wait_closed(self) -> None: return None

async def _stdio(output: BinaryIO) -> tuple[asyncio.StreamReader, _Output, asyncio.BaseTransport]:
    loop = asyncio.get_running_loop(); reader = asyncio.StreamReader(); protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await loop.connect_read_pipe(lambda: protocol, sys.stdin.buffer)
    return reader, _Output(output), transport


def _socket(home: Path) -> Path:
    text = str(home)
    if os.name != "posix" or not PurePosixPath(text).is_absolute() or "\x00" in text or "\n" in text or len(text) > 4096:
        raise RelayError("invalid home")
    path = home / ".mimir" / "acp" / "daemon.sock"
    try: value = path.lstat(); parent = path.parent.lstat()
    except OSError as exc: raise RelayError("connection failed") from exc
    uid = os.getuid()
    if (not stat.S_ISSOCK(value.st_mode) or stat.S_ISLNK(value.st_mode) or value.st_uid != uid or
            not stat.S_ISDIR(parent.st_mode) or stat.S_ISLNK(parent.st_mode) or
            parent.st_uid != uid or parent.st_mode & 0o077):
        raise RelayError("connection failed")
    return path

async def run_relay(home: Path | str, output: BinaryIO) -> None:
    upstream_reader, upstream_writer = await asyncio.wait_for(asyncio.open_unix_connection(str(_socket(Path(home)))), CONNECT_TIMEOUT)
    try:
        reader, writer, transport = await _stdio(output)
    except BaseException:
        await close_writer(upstream_writer)
        raise
    try: await pump_bidirectional(reader, writer, upstream_reader, upstream_writer)
    finally: transport.close()
