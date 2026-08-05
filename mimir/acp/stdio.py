from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from typing import BinaryIO, cast


class _DrainProtocol(asyncio.BaseProtocol):
    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._paused = False
        self._drain_waiter: asyncio.Future[None] | None = None
        self._close_waiter: asyncio.Future[None] = self._loop.create_future()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport

    def pause_writing(self) -> None:
        self._paused = True
        if self._drain_waiter is None:
            self._drain_waiter = self._loop.create_future()

    def resume_writing(self) -> None:
        self._paused = False
        waiter = self._drain_waiter
        self._drain_waiter = None
        if waiter is not None and not waiter.done():
            waiter.set_result(None)

    def connection_lost(self, exc: Exception | None) -> None:
        self._paused = False
        drain_waiter = self._drain_waiter
        self._drain_waiter = None
        if drain_waiter is not None and not drain_waiter.done():
            if exc is None:
                drain_waiter.set_result(None)
            else:
                drain_waiter.set_exception(exc)
        if not self._close_waiter.done():
            if exc is None:
                self._close_waiter.set_result(None)
            else:
                self._close_waiter.set_exception(exc)

    async def _drain_helper(self) -> None:
        if self._paused and self._drain_waiter is not None:
            await self._drain_waiter

    def _get_close_waiter(self, stream: object) -> asyncio.Future[None]:
        return self._close_waiter


class _ReservedFrameTransport(asyncio.WriteTransport):
    def __init__(self, frame_file: BinaryIO, protocol: _DrainProtocol) -> None:
        super().__init__()
        self._frame_file = frame_file
        self._protocol = protocol
        self._closing = False
        protocol.connection_made(self)

    def write(self, data: bytes) -> None:
        if self._closing:
            raise ConnectionResetError("protocol response writer is closed")
        remaining = memoryview(data)
        while remaining:
            written = self._frame_file.write(remaining)
            if written is None:
                written = len(remaining)
            if written <= 0:
                raise BrokenPipeError("protocol response write made no progress")
            remaining = remaining[written:]
        self._frame_file.flush()

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._protocol.connection_lost(None)

    def abort(self) -> None:
        self.close()

    def can_write_eof(self) -> bool:
        return False

    def get_write_buffer_size(self) -> int:
        return 0

    def get_write_buffer_limits(self) -> tuple[int, int]:
        return (0, 0)

    def set_write_buffer_limits(self, high: int | None = None, low: int | None = None) -> None:
        return None

    def get_extra_info(self, name: str, default: object = None) -> object:
        if name == "pipe":
            return self._frame_file
        return default


@dataclass(slots=True)
class ProtocolStreams:
    request_reader: asyncio.StreamReader
    response_writer: asyncio.StreamWriter
    stdin_read_transport: asyncio.ReadTransport
    _response_transport: _ReservedFrameTransport
    _intake_stopped: bool = False
    _writer_failed: bool = False
    _drain_task: asyncio.Task[None] | None = field(default=None, init=False)
    _close_task: asyncio.Task[None] | None = field(default=None, init=False)

    @property
    def writer_failed(self) -> bool:
        return self._writer_failed

    def writer_helper_tasks(self) -> tuple[asyncio.Task[None], ...]:
        return tuple(task for task in (self._drain_task, self._close_task) if task is not None)

    def stop_request_intake(self) -> None:
        if self._intake_stopped:
            return
        self._intake_stopped = True
        self.stdin_read_transport.close()
        self.request_reader.feed_eof()

    async def drain_response_writer(self, timeout: float = 2.0) -> bool:
        if self._drain_task is None:
            self._drain_task = asyncio.create_task(
                self.response_writer.drain(),
                name="acp-response-writer-drain",
            )
        task = self._drain_task
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if task not in done:
            self._writer_failed = True
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._response_transport.abort()
            return False
        try:
            task.result()
        except (BrokenPipeError, ConnectionResetError):
            self._writer_failed = True
            return False
        except Exception:
            self._writer_failed = True
            return False
        return True

    async def close_response_writer(self, timeout: float = 1.0) -> bool:
        self.response_writer.close()
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self.response_writer.wait_closed(),
                name="acp-response-writer-close",
            )
        task = self._close_task
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if task not in done:
            self._writer_failed = True
            self._response_transport.abort()
            done, _ = await asyncio.wait({task}, timeout=0)
            if task not in done:
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return False
        try:
            task.result()
        except Exception:
            self._writer_failed = True
            return False
        return True


async def open_protocol_streams(frame_file: BinaryIO) -> ProtocolStreams:
    loop = asyncio.get_running_loop()
    request_reader = asyncio.StreamReader()
    reader_protocol = asyncio.StreamReaderProtocol(request_reader)
    read_transport, _ = await loop.connect_read_pipe(lambda: reader_protocol, sys.stdin)
    response_protocol = _DrainProtocol()
    response_transport = _ReservedFrameTransport(frame_file, response_protocol)
    response_writer = asyncio.StreamWriter(
        cast(asyncio.WriteTransport, response_transport),
        response_protocol,
        None,
        loop,
    )
    return ProtocolStreams(
        request_reader=request_reader,
        response_writer=response_writer,
        stdin_read_transport=cast(asyncio.ReadTransport, read_transport),
        _response_transport=response_transport,
    )
