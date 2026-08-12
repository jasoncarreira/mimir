from __future__ import annotations

import asyncio
import io

import pytest

from mimir.acp.host import _FrameDelivery, close_protocol_writer


@pytest.mark.asyncio
async def test_frame_delivery_is_bounded_and_terminal() -> None:
    stream = io.BytesIO()
    errors: list[BaseException] = []
    delivery = _FrameDelivery(stream, 16, errors.append)
    assert delivery.write(b"frame") == 5
    delivery.finish()
    await delivery.wait_terminal()
    delivery.join()
    assert stream.getvalue() == b"frame"
    assert not errors


@pytest.mark.asyncio
async def test_frame_delivery_rejects_capacity() -> None:
    delivery = _FrameDelivery(io.BytesIO(), 2, lambda error: None)
    with pytest.raises(BufferError):
        delivery.write(b"long")
    with pytest.raises(BufferError):
        await delivery.wait_terminal()
    delivery.join()


class Partial(io.BytesIO):
    def write(self, data: bytes) -> int:
        return super().write(bytes(data[:2]))


@pytest.mark.asyncio
async def test_frame_delivery_handles_partial_writes() -> None:
    stream = Partial()
    delivery = _FrameDelivery(stream, 32, lambda error: None)
    delivery.write(b"complete")
    delivery.finish()
    await delivery.wait_terminal()
    delivery.join()
    assert stream.getvalue() == b"complete"


@pytest.mark.asyncio
async def test_protocol_writer_uses_bounded_transport_close(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    async def close(writer: object) -> None:
        calls.append(writer)
    monkeypatch.setattr("mimir.acp.host.close_writer", close)
    writer = object()
    await close_protocol_writer(writer)
    assert calls == [writer]
