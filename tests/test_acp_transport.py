from __future__ import annotations

import asyncio

import pytest

from mimir.acp.transport import close_writer, pump_bidirectional, pump_stream


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


class StagedWriter(Writer):
    def __init__(
        self,
        *,
        drain_gate: asyncio.Event | None = None,
        close_gate: asyncio.Event | None = None,
    ) -> None:
        super().__init__()
        self.drain_gate = drain_gate
        self.close_gate = close_gate
        self.aborted = False
        self.drain_calls = 0
        self.wait_calls = 0

    async def drain(self) -> None:
        self.drain_calls += 1
        if self.drain_gate is not None:
            await self.drain_gate.wait()

    async def wait_closed(self) -> None:
        self.wait_calls += 1
        if self.close_gate is not None:
            await self.close_gate.wait()

    def abort(self) -> None:
        self.aborted = True
        self.closed = True


@pytest.mark.asyncio
async def test_close_writer_drain_timeout_escalates_to_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mimir.acp.transport.WRITER_DRAIN_TIMEOUT", 0.01)
    writer = StagedWriter(drain_gate=asyncio.Event())
    await close_writer(writer)
    assert writer.closed
    assert not writer.aborted
    assert writer.drain_calls == 1


@pytest.mark.asyncio
async def test_close_writer_close_timeout_escalates_to_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mimir.acp.transport.WRITER_CLOSE_TIMEOUT", 0.01)
    monkeypatch.setattr("mimir.acp.transport.WRITER_ABORT_TIMEOUT", 0.01)
    writer = StagedWriter(close_gate=asyncio.Event())
    await asyncio.wait_for(close_writer(writer), 0.1)
    assert writer.closed
    assert writer.aborted
    assert writer.wait_calls == 2


@pytest.mark.asyncio
async def test_pump_drain_timeout_is_connection_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mimir.acp.transport.WRITER_DRAIN_TIMEOUT", 0.01)
    left = asyncio.StreamReader()
    right = asyncio.StreamReader()
    left.feed_data(b"blocked")
    left.feed_eof()
    right.feed_eof()
    left_writer = Writer()
    right_writer = StagedWriter(drain_gate=asyncio.Event())
    with pytest.raises(TimeoutError):
        await pump_bidirectional(left, left_writer, right, right_writer)
    assert left_writer.closed
    assert right_writer.closed


@pytest.mark.asyncio
async def test_force_close_deadline_bounds_writer_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mimir.acp.transport.FORCE_CLOSE_TIMEOUT", 0.01)
    monkeypatch.setattr("mimir.acp.transport.WRITER_DRAIN_TIMEOUT", 10.0)
    left = asyncio.StreamReader()
    right = asyncio.StreamReader()
    left.feed_eof()
    right.feed_eof()
    class BlocksOnCloseDrain(StagedWriter):
        async def drain(self) -> None:
            self.drain_calls += 1
            if self.drain_calls > 1:
                await asyncio.Event().wait()

    left_writer = BlocksOnCloseDrain()
    right_writer = BlocksOnCloseDrain()
    await asyncio.wait_for(
        pump_bidirectional(left, left_writer, right, right_writer), 0.1
    )


async def _release_after(event: asyncio.Event, delay: float) -> None:
    await asyncio.sleep(delay)
    event.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("release_delay,before", [(0.005, True), (0.03, False)])
async def test_drain_deadline_before_and_after_witnesses(
    release_delay: float,
    before: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mimir.acp.transport.WRITER_DRAIN_TIMEOUT", 0.02)
    gate = asyncio.Event()
    writer = StagedWriter(drain_gate=gate)
    release = asyncio.create_task(_release_after(gate, release_delay))
    await close_writer(writer)
    assert writer.closed
    assert writer.aborted is False
    assert gate.is_set() is before
    await release


@pytest.mark.asyncio
@pytest.mark.parametrize("release_delay,before", [(0.005, True), (0.03, False)])
async def test_close_deadline_before_and_after_witnesses(
    release_delay: float,
    before: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mimir.acp.transport.WRITER_CLOSE_TIMEOUT", 0.02)
    monkeypatch.setattr("mimir.acp.transport.WRITER_ABORT_TIMEOUT", 0.05)
    gate = asyncio.Event()
    writer = StagedWriter(close_gate=gate)
    release = asyncio.create_task(_release_after(gate, release_delay))
    await close_writer(writer)
    assert writer.aborted is not before
    assert writer.wait_calls == (1 if before else 2)
    await release


@pytest.mark.asyncio
@pytest.mark.parametrize("release_delay,before", [(0.02, True), (0.06, False)])
async def test_abort_wait_deadline_before_and_after_witnesses(
    release_delay: float,
    before: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mimir.acp.transport.WRITER_CLOSE_TIMEOUT", 0.01)
    monkeypatch.setattr("mimir.acp.transport.WRITER_ABORT_TIMEOUT", 0.03)
    gate = asyncio.Event()
    writer = StagedWriter(close_gate=gate)
    release = asyncio.create_task(_release_after(gate, release_delay))
    await close_writer(writer)
    assert writer.aborted
    assert gate.is_set() is before
    assert writer.wait_calls == 2
    await release


@pytest.mark.asyncio
@pytest.mark.parametrize("release_delay,before", [(0.005, True), (0.04, False)])
async def test_force_close_deadline_before_and_after_witnesses(
    release_delay: float,
    before: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mimir.acp.transport.FORCE_CLOSE_TIMEOUT", 0.02)
    monkeypatch.setattr("mimir.acp.transport.WRITER_DRAIN_TIMEOUT", 1.0)

    class ClosingGateWriter(StagedWriter):
        async def drain(self) -> None:
            self.drain_calls += 1
            if self.drain_calls > 1:
                await gate.wait()

    gate = asyncio.Event()
    left = asyncio.StreamReader()
    right = asyncio.StreamReader()
    left.feed_eof()
    right.feed_eof()
    left_writer = ClosingGateWriter()
    right_writer = ClosingGateWriter()
    release = asyncio.create_task(_release_after(gate, release_delay))
    await pump_bidirectional(left, left_writer, right, right_writer)
    assert gate.is_set() is before
    if before:
        assert left_writer.closed and right_writer.closed
    await release
