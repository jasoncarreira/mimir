from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from mimir.acp.bridge import ACPBridge
from mimir.bridges.base import Bridge, SendResult


class Publisher:
    def __init__(self) -> None:
        self.updates = []
        self.error: BaseException | None = None

    async def publish_live(self, update):
        if self.error is not None:
            raise self.error
        self.updates.append(update)


@pytest.mark.asyncio
async def test_plain_text_is_authoritative_update_and_exact_result() -> None:
    publisher = Publisher()
    bridge = ACPBridge(publisher)
    await bridge.connect()
    result = await bridge.send("acp:session", "hello")
    assert result == SendResult(sent=True, chunks=1)
    assert publisher.updates[0].content.text == "hello"
    assert publisher.updates[0].message_id is None
    assert publisher.updates[0].session_update == "agent_message_chunk"
    assert ACPBridge.prefixes == ("acp:",)
    assert ACPBridge.name == "acp"


def test_send_signature_exactly_matches_bridge_contract() -> None:
    assert inspect.signature(ACPBridge.send) == inspect.signature(Bridge.send)


@pytest.mark.asyncio
async def test_connected_unbound_and_channel_bound_routing() -> None:
    bridge = ACPBridge()
    await bridge.connect()
    assert await bridge.send("acp:unbound", "hello") == SendResult(sent=False, error="unbound ACP channel")
    first = Publisher()
    second = Publisher()
    bridge.bind("acp:first", first)
    bridge.bind("acp:second", second)
    assert (await bridge.send("acp:first", "one")).sent
    assert (await bridge.send("acp:second", "two")).sent
    assert [item.content.text for item in first.updates] == ["one"]
    assert [item.content.text for item in second.updates] == ["two"]
    bridge.unbind("acp:first", second)
    assert (await bridge.send("acp:first", "still-bound")).sent
    bridge.unbind("acp:first", first)
    assert not (await bridge.send("acp:first", "unbound")).sent


@pytest.mark.asyncio
async def test_ordinary_delivery_failure_maps_to_soft_failure() -> None:
    publisher = Publisher()
    publisher.error = RuntimeError("private transport detail")
    bridge = ACPBridge(publisher)
    await bridge.connect()
    assert await bridge.send("acp:session", "hello") == SendResult(sent=False, error="ACP delivery failed")


@pytest.mark.asyncio
async def test_cancellation_propagates() -> None:
    publisher = Publisher()
    publisher.error = asyncio.CancelledError()
    bridge = ACPBridge(publisher)
    await bridge.connect()
    with pytest.raises(asyncio.CancelledError):
        await bridge.send("acp:session", "hello")


@pytest.mark.asyncio
async def test_rich_invalid_disconnected_and_reaction_fail_safely(tmp_path: Path) -> None:
    bridge = ACPBridge(Publisher())
    assert not (await bridge.send("acp:session", "hello")).sent
    await bridge.connect()
    assert not (await bridge.send("other", "hello")).sent
    assert not (await bridge.send("acp:session", "hello", [tmp_path / "x"])).sent
    assert not (await bridge.send("acp:session", "hello", blocks=[{"type": "rich"}])).sent
    assert not (await bridge.send("acp:session", "hello", embed=object())).sent
    assert await bridge.react("acp:session", "m", "+1") is False
    await bridge.disconnect()
    await bridge.disconnect()
