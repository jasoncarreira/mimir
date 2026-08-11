from __future__ import annotations

import asyncio

import pytest

from mimir.acp.transport import pump_bidirectional, pump_stream


class Writer:
    def __init__(self) -> None:
        self.data = bytearray()
        self.eof = False
        self.closed = False
        self.transport = self

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def write_eof(self) -> None:
        self.eof = True

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass

    def abort(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_pump_stream_copies_bytes_and_half_closes() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(b"raw\x00bytes")
    reader.feed_eof()
    writer = Writer()
    await pump_stream(reader, writer)
    assert bytes(writer.data) == b"raw\x00bytes"
    assert writer.eof is True


@pytest.mark.asyncio
async def test_bidirectional_pump_awaits_both_directions() -> None:
    left = asyncio.StreamReader()
    right = asyncio.StreamReader()
    left.feed_data(b"left")
    left.feed_eof()
    right.feed_data(b"right")
    right.feed_eof()
    left_writer = Writer()
    right_writer = Writer()
    await pump_bidirectional(left, left_writer, right, right_writer)
    assert bytes(right_writer.data) == b"left"
    assert bytes(left_writer.data) == b"right"
    assert left_writer.closed and right_writer.closed
