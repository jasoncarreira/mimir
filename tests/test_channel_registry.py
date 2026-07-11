"""ChannelRegistry prefix dispatch (SPEC §7.2.3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from mimir.bridges.base import Bridge, SendResult
from mimir.channel_registry import (
    ChannelRegistry,
    INTERACTIVE_TRIGGERS,
    OPERATOR_CHANNEL_SENTINEL,
    UnknownChannelError,
    classify_turn_interactivity,
    is_interactive_turn,
    post_job_failure_notice,
    resolve_deliver_channel,
)
from mimir.models import TurnInteractivity
from mimir.worklink.continuation import HTTP_EVENT_INGRESS_EXTRA_VALUE


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


# ── is_interactive_turn (0.3.0) ──────────────────────────────────────


def _interactive_reg() -> ChannelRegistry:
    reg = ChannelRegistry()
    reg.register(_bridge("disc", ("discord-", "dm-discord-")))
    return reg


def test_is_interactive_user_message_on_bridge():
    assert is_interactive_turn("discord-123", "user_message", _interactive_reg()) is True


def test_is_interactive_shell_job_complete_on_bridge():
    # shell_job_complete is interactive when it lands on a bridge channel
    assert is_interactive_turn("discord-9", "shell_job_complete", _interactive_reg()) is True


@pytest.mark.parametrize(
    "trigger", ["scheduled_tick", "poller", "saga_session_end", "upgrade"]
)
def test_automated_triggers_not_interactive_even_on_bridge(trigger):
    # Trigger gating applies even when the channel has a registered bridge
    # (e.g. a saga_session_end synthesis turn on a real discord channel).
    assert is_interactive_turn("discord-123", trigger, _interactive_reg()) is False


def test_no_bridge_channel_not_interactive():
    reg = _interactive_reg()
    assert is_interactive_turn("scheduler:heartbeat", "user_message", reg) is False
    assert is_interactive_turn("poller:gmail", "poller", reg) is False


def test_none_inputs_not_interactive():
    reg = _interactive_reg()
    assert is_interactive_turn(None, "user_message", reg) is False
    assert is_interactive_turn("discord-1", None, reg) is False
    assert is_interactive_turn("discord-1", "user_message", None) is False


def test_interactive_triggers_allowlist_membership():
    assert "user_message" in INTERACTIVE_TRIGGERS
    assert "shell_job_complete" in INTERACTIVE_TRIGGERS
    assert "scheduled_tick" not in INTERACTIVE_TRIGGERS
    assert "saga_session_end" not in INTERACTIVE_TRIGGERS


def test_http_event_ingress_forces_non_interactive_even_on_bridge_user_message():
    assert classify_turn_interactivity(
        "discord-123",
        "user_message",
        HTTP_EVENT_INGRESS_EXTRA_VALUE,
        _interactive_reg(),
    ) == TurnInteractivity.NON_INTERACTIVE


# ─── chainlink #508: deliver: channel resolution + failure notice ────


class TestResolveDeliverChannel:
    def test_literal_passthrough(self):
        assert resolve_deliver_channel("slack-ops", "slack-alerts") == "slack-ops"

    def test_operator_sentinel_resolves(self):
        assert resolve_deliver_channel(OPERATOR_CHANNEL_SENTINEL, "slack-alerts") == "slack-alerts"

    def test_operator_sentinel_unconfigured_is_none(self):
        # sentinel used but no operator alert channel configured → graceful None
        assert resolve_deliver_channel(OPERATOR_CHANNEL_SENTINEL, "") is None
        assert resolve_deliver_channel(OPERATOR_CHANNEL_SENTINEL, None) is None

    def test_unset_is_none(self):
        assert resolve_deliver_channel(None, "slack-alerts") is None
        assert resolve_deliver_channel("   ", "slack-alerts") is None


@pytest.mark.asyncio
async def test_post_job_failure_notice_sends():
    reg = ChannelRegistry()
    bridge = _bridge("rec", ("rec-",))
    reg.register(bridge)
    await post_job_failure_notice(reg, "rec-ops", label="github-activity", error="boom")
    assert len(bridge.sent) == 1
    cid, text = bridge.sent[0]
    assert cid == "rec-ops"
    assert "github-activity failed" in text and "boom" in text and "⚠️" in text


@pytest.mark.asyncio
async def test_post_job_failure_notice_noops_without_channel_or_registry():
    reg = ChannelRegistry()
    bridge = _bridge("rec", ("rec-",))
    reg.register(bridge)
    await post_job_failure_notice(reg, None, label="x", error="y")     # no channel
    await post_job_failure_notice(None, "rec-ops", label="x", error="y")  # no registry
    assert bridge.sent == []


@pytest.mark.asyncio
async def test_post_job_failure_notice_swallows_send_errors():
    reg = ChannelRegistry()  # no bridge registered → send raises UnknownChannelError
    # must not propagate — a failure notice can't cascade into another failure
    await post_job_failure_notice(reg, "rec-ops", label="x", error="y")
