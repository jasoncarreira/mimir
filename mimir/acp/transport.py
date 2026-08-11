from __future__ import annotations

import asyncio
from typing import Any

WRITER_DRAIN_TIMEOUT = 2.0
WRITER_CLOSE_TIMEOUT = 1.0
WRITER_ABORT_TIMEOUT = 1.0
FORCE_CLOSE_TIMEOUT = 5.0
_COPY_CHUNK_BYTES = 64 * 1024


async def close_writer(writer: Any) -> None:
    try:
        await asyncio.wait_for(writer.drain(), WRITER_DRAIN_TIMEOUT)
    except (TimeoutError, ConnectionError, OSError):
        pass
    writer.close()
    wait_closed = getattr(writer, "wait_closed", None)
    if wait_closed is not None:
        try:
            await asyncio.wait_for(wait_closed(), WRITER_CLOSE_TIMEOUT)
            return
        except (TimeoutError, ConnectionError, OSError):
            pass
    transport = getattr(writer, "transport", None)
    abort = getattr(transport, "abort", None)
    if abort is not None:
        abort()
        if wait_closed is not None:
            try:
                await asyncio.wait_for(wait_closed(), WRITER_ABORT_TIMEOUT)
            except (TimeoutError, ConnectionError, OSError):
                pass


async def pump_stream(reader: Any, writer: Any) -> None:
    while True:
        data = await reader.read(_COPY_CHUNK_BYTES)
        if not data:
            write_eof = getattr(writer, "write_eof", None)
            if write_eof is not None:
                try:
                    write_eof()
                    await asyncio.wait_for(writer.drain(), WRITER_DRAIN_TIMEOUT)
                except (NotImplementedError, TimeoutError, ConnectionError, OSError):
                    pass
            return
        writer.write(data)
        await asyncio.wait_for(writer.drain(), WRITER_DRAIN_TIMEOUT)


async def pump_bidirectional(
    left_reader: Any,
    left_writer: Any,
    right_reader: Any,
    right_writer: Any,
) -> None:
    tasks = {
        asyncio.create_task(pump_stream(left_reader, right_writer)),
        asyncio.create_task(pump_stream(right_reader, left_writer)),
    }
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    finally:
        closing = asyncio.gather(
            close_writer(left_writer), close_writer(right_writer),
            return_exceptions=True,
        )
        try:
            await asyncio.wait_for(closing, FORCE_CLOSE_TIMEOUT)
        except TimeoutError:
            pass


__all__ = [
    "FORCE_CLOSE_TIMEOUT",
    "WRITER_ABORT_TIMEOUT",
    "WRITER_CLOSE_TIMEOUT",
    "WRITER_DRAIN_TIMEOUT",
    "close_writer",
    "pump_bidirectional",
    "pump_stream",
]
