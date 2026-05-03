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


# ─── <actions> directive integration ────────────────────────────────


@pytest.mark.asyncio
async def test_send_message_with_react_directive(tmp_path: Path):
    """Text + <actions><react/></actions> dispatches the main send,
    then reacts on the just-sent message id (no explicit message attr)."""
    bridge = _RecordingBridge()
    reg = ChannelRegistry()
    reg.register(bridge)
    tools = {t.name: t for t in build_channel_tools(reg, home=tmp_path)}

    ctx = _ctx_with(LoopDetector(), channel="c-1")
    token = _context.set_current_turn(ctx)
    try:
        out = await tools["send_message"].handler({
            "text": "Got it.\n<actions><react emoji=\"thumbsup\" /></actions>",
        })
    finally:
        _context.reset_current_turn(token)

    # Main send delivered cleaned text; <actions> stripped.
    assert bridge.sent == [("c-1", "Got it.")]
    # Single reaction on the just-sent message id (m1 from _RecordingBridge).
    assert bridge.reacted == [("c-1", "m1", "thumbsup")]
    body = out["content"][0]["text"]
    assert "send_message complete" in body
    assert "react [ok]" in body


@pytest.mark.asyncio
async def test_send_message_directives_only_no_main_send(tmp_path: Path):
    """When <actions> is the only content (clean_text empty), the main
    send is skipped and only directives fire."""
    bridge = _RecordingBridge()
    reg = ChannelRegistry()
    reg.register(bridge)
    tools = {t.name: t for t in build_channel_tools(reg, home=tmp_path)}

    ctx = _ctx_with(LoopDetector(), channel="c-1")
    ctx.last_assistant_message_id = "prev-msg"
    token = _context.set_current_turn(ctx)
    try:
        await tools["send_message"].handler({
            "text": "<actions><react emoji=\"eyes\" /></actions>",
        })
    finally:
        _context.reset_current_turn(token)

    assert bridge.sent == []  # no main send
    assert bridge.reacted == [("c-1", "prev-msg", "eyes")]


@pytest.mark.asyncio
async def test_send_message_directives_only_rejects_when_no_recent_msg(tmp_path: Path):
    """Directives-only with a react that has no message attr AND no
    last_assistant_message_id → recorded as a per-directive failure."""
    bridge = _RecordingBridge()
    reg = ChannelRegistry()
    reg.register(bridge)
    tools = {t.name: t for t in build_channel_tools(reg, home=tmp_path)}

    ctx = _ctx_with(LoopDetector(), channel="c-1")
    # ctx.last_assistant_message_id stays None
    token = _context.set_current_turn(ctx)
    try:
        out = await tools["send_message"].handler({
            "text": "<actions><react emoji=\"eyes\" /></actions>",
        })
    finally:
        _context.reset_current_turn(token)

    body = out["content"][0]["text"]
    assert "react [FAIL]" in body
    assert "no message_id" in body
    assert bridge.reacted == []


@pytest.mark.asyncio
async def test_send_message_with_send_file_directive_resolves_under_outbound(
    tmp_path: Path,
):
    """<send-file path="..."> resolves against home/attachments/outbound/
    and is delivered as an attachment_paths argument to bridge.send."""
    outbound = tmp_path / "attachments" / "outbound"
    outbound.mkdir(parents=True)
    target = outbound / "report.pdf"
    target.write_bytes(b"%PDF")

    sent_with_attach: list[tuple[str, str, list]] = []

    @dataclass
    class _AttachBridge(Bridge):
        name: str = "att"
        prefixes: tuple = ("c-",)
        async def connect(self): ...
        async def disconnect(self): ...
        async def send(self, channel_id, text, attachment_paths=None):
            sent_with_attach.append((channel_id, text, list(attachment_paths or [])))
            return SendResult(sent=True, message_id="m1", chunks=1)
        async def react(self, channel_id, message_id, emoji):
            return True

    bridge = _AttachBridge()
    reg = ChannelRegistry()
    reg.register(bridge)
    tools = {t.name: t for t in build_channel_tools(reg, home=tmp_path)}

    ctx = _ctx_with(LoopDetector(), channel="c-1")
    token = _context.set_current_turn(ctx)
    try:
        out = await tools["send_message"].handler({
            "text": "Here it is.\n<actions><send-file path=\"report.pdf\" caption=\"Q3\" /></actions>",
        })
    finally:
        _context.reset_current_turn(token)

    # Two sends: main (cleaned text), then send-file (caption + attachment).
    assert len(sent_with_attach) == 2
    assert sent_with_attach[0][1] == "Here it is."
    assert sent_with_attach[0][2] == []
    assert sent_with_attach[1][1] == "Q3"
    assert sent_with_attach[1][2] == [target.resolve()]
    body = out["content"][0]["text"]
    assert "send-file [ok]" in body


@pytest.mark.asyncio
async def test_send_message_send_file_path_escape_logged_not_raised(tmp_path: Path):
    """Path escape in <send-file path=".."/> is per-directive failure;
    the main send still succeeds."""
    bridge = _RecordingBridge()
    reg = ChannelRegistry()
    reg.register(bridge)
    tools = {t.name: t for t in build_channel_tools(reg, home=tmp_path)}

    # No outbound dir created — resolve_outbound_path will reject as
    # non-existent path; either way the directive fails cleanly.
    ctx = _ctx_with(LoopDetector(), channel="c-1")
    token = _context.set_current_turn(ctx)
    try:
        out = await tools["send_message"].handler({
            "text": "see\n<actions><send-file path=\"../etc/passwd\" /></actions>",
        })
    finally:
        _context.reset_current_turn(token)

    body = out["content"][0]["text"]
    assert "send_message complete" in body
    assert "send-file [FAIL]" in body
    # Main text still delivered.
    assert bridge.sent == [("c-1", "see")]


@pytest.mark.asyncio
async def test_send_message_send_file_without_home_is_failure(tmp_path: Path):
    """If home wasn't passed to build_channel_tools, <send-file>
    fails with a clear message."""
    bridge = _RecordingBridge()
    reg = ChannelRegistry()
    reg.register(bridge)
    tools = {t.name: t for t in build_channel_tools(reg)}  # no home

    ctx = _ctx_with(LoopDetector(), channel="c-1")
    token = _context.set_current_turn(ctx)
    try:
        out = await tools["send_message"].handler({
            "text": "x\n<actions><send-file path=\"y.pdf\" /></actions>",
        })
    finally:
        _context.reset_current_turn(token)

    body = out["content"][0]["text"]
    assert "send-file [FAIL]" in body
    assert "outbound attachments dir not configured" in body


@pytest.mark.asyncio
async def test_send_message_plain_text_unchanged(tmp_path: Path):
    """No <actions> in text → behavior identical to pre-stage-4 path."""
    bridge = _RecordingBridge()
    reg = ChannelRegistry()
    reg.register(bridge)
    tools = {t.name: t for t in build_channel_tools(reg, home=tmp_path)}

    ctx = _ctx_with(LoopDetector(), channel="c-1")
    token = _context.set_current_turn(ctx)
    try:
        out = await tools["send_message"].handler({"text": "hello"})
    finally:
        _context.reset_current_turn(token)

    assert bridge.sent == [("c-1", "hello")]
    assert bridge.reacted == []
    body = out["content"][0]["text"]
    assert "send_message complete" in body
    # No directive bullets when there were no directives.
    assert "directives:" not in body


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
