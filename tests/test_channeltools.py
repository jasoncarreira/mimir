"""send_message + react MCP tools, channel-aware dispatch + breaker (SPEC §7.1, §7.2.4)."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from mimir import _context
from mimir.bridges.base import Bridge, SendResult
from mimir.channel_registry import ChannelRegistry
from mimir.channeltools import build_channel_tools
from mimir.event_logger import init_logger
from mimir.loop_detector import LoopDetector
from mimir.models import TurnContext

from tests._fake_saga import FakeSaga


@pytest.fixture(autouse=True)
def _logger(tmp_path: Path):
    (tmp_path / "logs").mkdir()
    init_logger(tmp_path / "logs" / "events.jsonl", session_id="test-proc")


@dataclass
class _RecordingBridge(Bridge):
    name: str = "rec"
    prefixes: tuple = ("c-", "bench-")
    sent: list[tuple[str, str]] = field(default_factory=list)
    reacted: list[tuple[str, str, str]] = field(default_factory=list)
    fail_send: bool = False

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...

    async def send(self, channel_id, text, attachment_paths=None):
        self.sent.append((channel_id, text))
        if self.fail_send:
            return SendResult(sent=False, error="bridge boom")
        return SendResult(sent=True, message_id=f"m{len(self.sent)}", chunks=1)

    async def react(self, channel_id, message_id, emoji):
        self.reacted.append((channel_id, message_id, emoji))
        return True


def _ctx_with(detector: LoopDetector | None = None, channel: str = "c-1") -> TurnContext:
    return TurnContext(
        turn_id="t1",
        session_id=channel,
        trigger="user_message",
        channel_id=channel,
        started_at=0.0,
        loop_detector=detector,
    )


def _by_name(tools, name):
    for t in tools:
        if t.name == name:
            return t
    raise AssertionError(f"tool {name!r} not registered")


@pytest.mark.asyncio
async def test_send_message_uses_current_channel_default():
    bridge = _RecordingBridge()
    reg = ChannelRegistry()
    reg.register(bridge)
    tools = {t.name: t for t in build_channel_tools(reg)}

    ctx = _ctx_with(LoopDetector(), channel="c-1")
    token = _context.set_current_turn(ctx)
    try:
        out = await tools["send_message"].handler({"text": "hello"})
    finally:
        _context.reset_current_turn(token)

    assert out.get("is_error") is not True
    assert bridge.sent == [("c-1", "hello")]
    assert ctx.last_assistant_message_id == "m1"


@pytest.mark.asyncio
async def test_send_message_explicit_channel_overrides_default():
    bridge = _RecordingBridge()
    reg = ChannelRegistry()
    reg.register(bridge)
    tools = {t.name: t for t in build_channel_tools(reg)}
    ctx = _ctx_with(LoopDetector(), channel="c-1")
    token = _context.set_current_turn(ctx)
    try:
        await tools["send_message"].handler({"text": "hi", "channel_id": "c-other"})
    finally:
        _context.reset_current_turn(token)
    assert bridge.sent == [("c-other", "hi")]


@pytest.mark.asyncio
async def test_send_message_unknown_prefix_returns_error():
    reg = ChannelRegistry()
    reg.register(_RecordingBridge())
    tools = {t.name: t for t in build_channel_tools(reg)}
    ctx = _ctx_with(LoopDetector(), channel="other-1")
    token = _context.set_current_turn(ctx)
    try:
        out = await tools["send_message"].handler({"text": "hi"})
    finally:
        _context.reset_current_turn(token)
    assert out.get("is_error") is True
    assert "no bridge registered" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_send_message_breaker_soft_warn_still_sends():
    """Below the hard limit, soft warn lets the send through but emits a
    ⚠️ reaction once."""
    bridge = _RecordingBridge()
    reg = ChannelRegistry()
    reg.register(bridge)
    tools = {t.name: t for t in build_channel_tools(reg)}
    detector = LoopDetector(soft_limit=2, hard_limit=10, similarity_threshold=0.9)
    ctx = _ctx_with(detector)

    token = _context.set_current_turn(ctx)
    try:
        await tools["send_message"].handler({"text": "same text"})
        await tools["send_message"].handler({"text": "same text"})  # reaches soft
    finally:
        _context.reset_current_turn(token)

    assert len(bridge.sent) == 2  # both sent through
    # ⚠️ reaction emitted once for the soft warn.
    assert any(emoji == "⚠️" for _, _, emoji in bridge.reacted)


@pytest.mark.asyncio
async def test_send_message_breaker_hard_stop_refuses():
    bridge = _RecordingBridge()
    reg = ChannelRegistry()
    reg.register(bridge)
    tools = {t.name: t for t in build_channel_tools(reg)}
    detector = LoopDetector(soft_limit=2, hard_limit=3, similarity_threshold=0.9)
    ctx = _ctx_with(detector)

    token = _context.set_current_turn(ctx)
    try:
        await tools["send_message"].handler({"text": "x"})
        await tools["send_message"].handler({"text": "x"})
        out = await tools["send_message"].handler({"text": "x"})  # hits hard limit
    finally:
        _context.reset_current_turn(token)

    assert out.get("is_error") is True
    assert "hard stop" in out["content"][0]["text"]
    assert len(bridge.sent) == 2  # third send refused
    # ❌ reaction emitted (best-effort) on the last assistant message.
    assert any(emoji == "❌" for _, _, emoji in bridge.reacted)


@pytest.mark.asyncio
async def test_send_message_empty_text_rejected():
    bridge = _RecordingBridge()
    reg = ChannelRegistry()
    reg.register(bridge)
    tools = {t.name: t for t in build_channel_tools(reg)}
    ctx = _ctx_with(LoopDetector())
    token = _context.set_current_turn(ctx)
    try:
        out = await tools["send_message"].handler({"text": "   "})
    finally:
        _context.reset_current_turn(token)
    assert out.get("is_error") is True
    assert bridge.sent == []


@pytest.mark.asyncio
async def test_send_message_bridge_failure_returns_error():
    bridge = _RecordingBridge(fail_send=True)
    reg = ChannelRegistry()
    reg.register(bridge)
    tools = {t.name: t for t in build_channel_tools(reg)}
    ctx = _ctx_with(LoopDetector())
    token = _context.set_current_turn(ctx)
    try:
        out = await tools["send_message"].handler({"text": "hi"})
    finally:
        _context.reset_current_turn(token)
    assert out.get("is_error") is True
    assert "bridge boom" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_react_uses_last_assistant_message_id():
    bridge = _RecordingBridge()
    reg = ChannelRegistry()
    reg.register(bridge)
    tools = {t.name: t for t in build_channel_tools(reg)}
    ctx = _ctx_with(LoopDetector())
    ctx.last_assistant_message_id = "msg-prev"
    token = _context.set_current_turn(ctx)
    try:
        out = await tools["react"].handler({"emoji": "👍"})
    finally:
        _context.reset_current_turn(token)
    assert out.get("is_error") is not True
    assert bridge.reacted == [("c-1", "msg-prev", "👍")]


@pytest.mark.asyncio
async def test_react_without_message_id_or_recent_fails():
    bridge = _RecordingBridge()
    reg = ChannelRegistry()
    reg.register(bridge)
    tools = {t.name: t for t in build_channel_tools(reg)}
    ctx = _ctx_with(LoopDetector())  # no last_assistant_message_id
    token = _context.set_current_turn(ctx)
    try:
        out = await tools["react"].handler({"emoji": "👍"})
    finally:
        _context.reset_current_turn(token)
    assert out.get("is_error") is True


@pytest.mark.asyncio
async def test_bench_bridge_writes_to_stream(tmp_path: Path):
    """Smoke for the BenchBridge — uses a StringIO instead of stdout."""
    from mimir.bridges.bench import BenchBridge

    buf = io.StringIO()
    bridge = BenchBridge(home=tmp_path, stream=buf)
    result = await bridge.send("bench-1", "hello world")
    assert result.sent is True
    assert result.message_id is not None
    assert "channel=bench-1" in buf.getvalue()
    assert "hello world" in buf.getvalue()


@pytest.mark.asyncio
async def test_send_message_fires_saga_feedback():
    """When saga_client is wired, every successful send_message should call
    feedback() with the sent text + ctx.saga_atom_ids and bump
    send_message_count so the agent's post_message_hook becomes a no-op."""
    bridge = _RecordingBridge()
    reg = ChannelRegistry()
    reg.register(bridge)
    saga = FakeSaga()
    tools = {t.name: t for t in build_channel_tools(reg, saga_client=saga)}

    ctx = _ctx_with(LoopDetector(), channel="c-1")
    ctx.saga_atom_ids = ["atom-A", "atom-B", "atom-A"]  # de-dup expected
    ctx.saga_session_id = "saga-c-1-x"
    token = _context.set_current_turn(ctx)
    try:
        out = await tools["send_message"].handler({"text": "the reply text"})
    finally:
        _context.reset_current_turn(token)

    assert out.get("is_error") is not True
    assert ctx.send_message_count == 1
    fb_calls = [c for c in saga.calls if c.method == "feedback"]
    assert len(fb_calls) == 1
    payload = fb_calls[0].payload
    assert payload["atom_ids"] == ["atom-A", "atom-B"]  # de-duped, order preserved
    assert payload["response_text"] == "the reply text"
    assert payload["session_id"] == "saga-c-1-x"


@pytest.mark.asyncio
async def test_send_message_skips_feedback_with_no_atoms():
    """No atoms in flight → no feedback call, but send_message_count still
    increments so the post-hook fallback knows a send happened."""
    bridge = _RecordingBridge()
    reg = ChannelRegistry()
    reg.register(bridge)
    saga = FakeSaga()
    tools = {t.name: t for t in build_channel_tools(reg, saga_client=saga)}

    ctx = _ctx_with(LoopDetector(), channel="c-1")  # saga_atom_ids stays []
    token = _context.set_current_turn(ctx)
    try:
        await tools["send_message"].handler({"text": "hi"})
    finally:
        _context.reset_current_turn(token)

    assert ctx.send_message_count == 1
    assert [c.method for c in saga.calls] == []  # no feedback call


@pytest.mark.asyncio
async def test_send_message_skips_feedback_on_synthesis_turn():
    """saga_session_end synthesis turns call saga_feedback per-atom inside
    the agent's prompt — send_message must not double-count."""
    bridge = _RecordingBridge()
    reg = ChannelRegistry()
    reg.register(bridge)
    saga = FakeSaga()
    tools = {t.name: t for t in build_channel_tools(reg, saga_client=saga)}

    ctx = _ctx_with(LoopDetector(), channel="c-1")
    ctx.trigger = "saga_session_end"
    ctx.saga_atom_ids = ["atom-A"]
    token = _context.set_current_turn(ctx)
    try:
        await tools["send_message"].handler({"text": "shouldn't actually send on synth turn but test the gate"})
    finally:
        _context.reset_current_turn(token)

    assert ctx.send_message_count == 1
    assert [c.method for c in saga.calls] == []
