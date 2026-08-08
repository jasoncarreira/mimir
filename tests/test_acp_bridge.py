from __future__ import annotations

from pathlib import Path

import pytest

from mimir.acp.bridge import ACPBridge


class Publisher:
    def __init__(self) -> None:
        self.updates = []

    async def publish_live(self, update):
        self.updates.append(update)


@pytest.mark.asyncio
async def test_plain_text_is_authoritative_update() -> None:
    publisher = Publisher()
    bridge = ACPBridge(publisher)
    await bridge.connect()
    result = await bridge.send("acp:session", "hello")
    assert result.sent is True and result.chunks == 1
    assert publisher.updates[0].content.text == "hello"
    assert publisher.updates[0].message_id is None
    assert ACPBridge.prefixes == ("acp:",)


@pytest.mark.asyncio
async def test_rich_unbound_and_reaction_fail_safely(tmp_path: Path) -> None:
    bridge = ACPBridge(Publisher())
    assert not (await bridge.send("acp:session", "hello")).sent
    await bridge.connect()
    assert not (await bridge.send("other", "hello")).sent
    assert not (await bridge.send("acp:session", "hello", [tmp_path / "x"])).sent
    assert await bridge.react("acp:session", "m", "+1") is False
    await bridge.disconnect()
    await bridge.disconnect()
