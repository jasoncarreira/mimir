from __future__ import annotations

import asyncio
import queue
import threading
from typing import BinaryIO, Callable, cast

from .transport import close_writer

FRAME_DELIVERY_CAPACITY = 8 * 1024 * 1024
_END = object()

class _FrameDelivery:
    def __init__(self, frame_file: BinaryIO, capacity: int, error_callback: Callable[[BaseException], None]) -> None:
        if capacity <= 0: raise ValueError("frame delivery capacity must be positive")
        self._file, self._capacity, self._callback = frame_file, capacity, error_callback
        self._queue: queue.SimpleQueue[bytes | object] = queue.SimpleQueue(); self._lock = threading.Lock()
        self._reserved = 0; self._peak = 0; self._error: BaseException | None = None; self._accepting = True
        self._loop = asyncio.get_running_loop(); self._terminal = self._loop.create_future()
        self._thread = threading.Thread(target=self._deliver, name="acp-frame-delivery", daemon=True); self._thread.start()
    @property
    def closed(self) -> bool: return not self._accepting
    @property
    def terminal(self) -> bool: return not self._thread.is_alive()
    @property
    def reserved_bytes(self) -> int:
        with self._lock: return self._reserved
    @property
    def peak_reserved_bytes(self) -> int:
        with self._lock: return self._peak
    def write(self, data: bytes | bytearray | memoryview) -> int:
        payload = bytes(data)
        with self._lock:
            if not self._accepting: raise ConnectionResetError("frame delivery closed")
            if len(payload) > self._capacity - self._reserved:
                error = BufferError("frame delivery capacity exceeded"); self._error = error; self._accepting = False; self._queue.put(_END)
            else:
                self._reserved += len(payload); self._peak = max(self._peak, self._reserved); self._queue.put(payload); return len(payload)
        self._callback(error); raise error
    def flush(self) -> None:
        if self._error is not None: raise self._error
    def finish(self) -> None:
        with self._lock:
            if self._accepting: self._accepting = False; self._queue.put(_END)
    async def wait_terminal(self) -> None:
        await self._terminal
        if self._error is not None: raise self._error
    def join(self) -> None: self._thread.join()
    def _deliver(self) -> None:
        try:
            while True:
                item = self._queue.get()
                if item is _END: break
                payload = cast(bytes, item)
                remaining = memoryview(payload)
                while remaining:
                    written = self._file.write(remaining)
                    if written is None:
                        written = len(remaining)
                    if written <= 0:
                        raise BrokenPipeError
                    remaining = remaining[written:]
                self._file.flush()
                with self._lock: self._reserved -= len(payload)
        except BaseException as exc:
            self._error = exc; self._accepting = False; self._loop.call_soon_threadsafe(self._callback, exc)
        finally: self._loop.call_soon_threadsafe(self._terminal.set_result, None)

async def close_protocol_writer(writer: object) -> None:
    await close_writer(writer)
