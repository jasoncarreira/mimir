"""ChannelRegistry prefix dispatch (SPEC §7.2.3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from mimir.bridges.base import Bridge, SendResult
from mimir.channel_registry import ChannelRegistry, UnknownChannelError


@dataclass
class _RecordingBridge(Bridge):
    name: str = "rec"
    prefixes: tuple = ("rec-",)
    sent: list[tuple[str, str]] = field(default_factory=list)
    reacted: list[tuple[str, str, str]] = field(default_factory=list)

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...

    async def send(self, channel_id, text, attachment_paths=None, *, final=True):
        self.sent.append((channel_id, text))
        return SendResult(sent=True, message_id="m1", chunks=1)

    async def react(self, channel_id, message_id, emoji):
        self.reacted.append((channel_id, message_id, emoji))
        return True


def _bridge(name: str, prefixes: tuple) -> _RecordingBridge:
    b = _RecordingBridge()
    b.name = name
    b.prefixes = prefixes
    return b


def test_register_then_find():
    reg = ChannelRegistry()
    slack = _bridge("slack", ("slack-", "dm-slack-"))
    reg.register(slack)
    assert reg.find("slack-eng") is slack
    assert reg.find("dm-slack-alice") is slack
    assert reg.find("discord-foo") is None


def test_longest_prefix_wins():
    """Two bridges with overlapping prefixes — the more specific prefix
    takes precedence."""
    reg = ChannelRegistry()
    generic = _bridge("g", ("dm-",))
    specific = _bridge("s", ("dm-slack-",))
    reg.register(generic)
    reg.register(specific)
    assert reg.find("dm-slack-alice") is specific
    assert reg.find("dm-other") is generic


def test_find_or_raise_message():
    reg = ChannelRegistry()
    with pytest.raises(UnknownChannelError) as exc_info:
        reg.find_or_raise("unknown-1")
    assert "unknown-1" in str(exc_info.value)


@pytest.mark.asyncio
async def test_send_dispatches_to_matched_bridge():
    reg = ChannelRegistry()
    a = _bridge("a", ("a-",))
    b = _bridge("b", ("b-",))
    reg.register(a)
    reg.register(b)
    await reg.send("b-x", "hi")
    assert b.sent == [("b-x", "hi")]
    assert a.sent == []


@pytest.mark.asyncio
async def test_react_dispatches_to_matched_bridge():
    reg = ChannelRegistry()
    a = _bridge("a", ("a-",))
    reg.register(a)
    ok = await reg.react("a-1", "msg-1", "👍")
    assert ok is True
    assert a.reacted == [("a-1", "msg-1", "👍")]


@pytest.mark.asyncio
async def test_send_unknown_channel_raises():
    reg = ChannelRegistry()
    with pytest.raises(UnknownChannelError):
        await reg.send("nope-1", "hi")


@pytest.mark.asyncio
async def test_connect_disconnect_iterate_all():
    reg = ChannelRegistry()

    @dataclass
    class Lifecycle(_RecordingBridge):
        connected: int = 0
        disconnected: int = 0

        async def connect(self): self.connected += 1
        async def disconnect(self): self.disconnected += 1

    a = Lifecycle()
    a.name = "a"; a.prefixes = ("a-",)
    b = Lifecycle()
    b.name = "b"; b.prefixes = ("b-",)
    reg.register(a)
    reg.register(b)
    await reg.connect_all()
    await reg.disconnect_all()
    assert a.connected == 1 and b.connected == 1
    assert a.disconnected == 1 and b.disconnected == 1
