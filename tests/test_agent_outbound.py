"""Auto-dispatch of the agent's final assistant text.

Covers the path where the agent emits text without explicitly calling
the ``send_message`` tool — for chat bridges (Discord/Slack/web) the
runtime auto-dispatches via the channel registry. For ``scheduled_tick``
events and bench bridges, the text is recorded to chat_history only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from mimir.agent import Agent
from mimir.bridges.base import Bridge, SendResult
from mimir.channel_registry import ChannelRegistry
from mimir.config import Config
from mimir.event_logger import init_logger
from mimir.history import MessageBuffer
from mimir.index import IndexGenerator
from mimir.models import AgentEvent, TurnContext, make_process_session_id
from mimir.turn_logger import TurnLogger


# ─── Test fixtures ──────────────────────────────────────────────────


@dataclass
class _RecordingBridge(Bridge):
    name: str = "discord"
    prefixes: tuple = ("discord-", "dm-discord-")
    sent: list[tuple[str, str]] = field(default_factory=list)
    reacted: list[tuple[str, str, str]] = field(default_factory=list)

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...

    async def send(
        self,
        channel_id: str,
        text: str,
        attachment_paths: list | None = None,
        *,
        final: bool = True,
    ) -> SendResult:
        self.sent.append((channel_id, text))
        return SendResult(sent=True, message_id=f"m{len(self.sent)}", chunks=1)

    async def react(self, channel_id: str, message_id: str, emoji: str) -> bool:
        self.reacted.append((channel_id, message_id, emoji))
        return True


@dataclass
class _BenchBridge(Bridge):
    """Bench bridge stand-in — auto-dispatch should skip this."""
    name: str = "bench"
    prefixes: tuple = ("bench-",)
    sent: list[tuple[str, str]] = field(default_factory=list)

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def send(self, channel_id, text, attachment_paths=None, *, final=True):
        self.sent.append((channel_id, text))
        return SendResult(sent=True, message_id="m1", chunks=1)
    async def react(self, channel_id, message_id, emoji):
        return True


def _make_agent(tmp_path: Path, registry: ChannelRegistry) -> Agent:
    """Build a minimal Agent. Most fields aren't exercised by the
    auto-dispatch path; we only need config.home, message_buffer,
    and the channel registry. Other agent components are None /
    omitted."""
    from dataclasses import replace
    cfg = replace(Config.from_env(), home=tmp_path)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    init_logger(cfg.events_log, make_process_session_id())

    buffer = MessageBuffer(history_path=tmp_path / "messages" / "history.jsonl")
    indexes = IndexGenerator(home=tmp_path)
    turn_logger = TurnLogger(cfg.turns_log)
    return Agent(
        cfg, turn_logger, buffer, indexes,
        channel_registry=registry,
    )


def _ctx(channel_id: str = "discord-987") -> TurnContext:
    return TurnContext(
        turn_id="t1",
        session_id="s1",
        trigger="user_message",
        channel_id=channel_id,
        started_at=0.0,
    )


def _evt(channel_id: str = "discord-987", trigger: str = "user_message") -> AgentEvent:
    return AgentEvent(
        trigger=trigger, channel_id=channel_id, content="hi",
        author="discord-99", source="discord",
    )


# ─── Tests ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_user_message_auto_dispatches_via_bridge(tmp_path: Path):
    bridge = _RecordingBridge()
    reg = ChannelRegistry()
    reg.register(bridge)
    agent = _make_agent(tmp_path, reg)

    ctx = _ctx()
    evt = _evt()
    await agent._auto_dispatch_or_record(ctx, evt, "Hello, Jason.")

    assert bridge.sent == [("discord-987", "Hello, Jason.")]
    assert ctx.last_assistant_message_id == "m1"

    # auto_dispatch_ok event landed in events.jsonl so the audit log
    # captures successful auto-deliveries (parity with how the
    # send_message tool logs explicit calls).
    import json
    events_log = tmp_path / "logs" / "events.jsonl"
    events = [json.loads(l) for l in events_log.read_text().splitlines()]
    ok_events = [e for e in events if e.get("type") == "auto_dispatch_ok"]
    assert len(ok_events) == 1
    assert ok_events[0]["channel_id"] == "discord-987"
    assert ok_events[0]["text"] == "Hello, Jason."
    assert ok_events[0]["bridge"] == "discord"


@pytest.mark.asyncio
async def test_scheduled_tick_does_not_auto_dispatch(tmp_path: Path):
    """Heartbeats / cron-fired turns are explicitly silent. Final text
    goes to chat_history only — never the bridge."""
    bridge = _RecordingBridge()
    reg = ChannelRegistry()
    reg.register(bridge)
    agent = _make_agent(tmp_path, reg)

    ctx = _ctx(channel_id="scheduler:heartbeat")
    evt = _evt(channel_id="scheduler:heartbeat", trigger="scheduled_tick")
    await agent._auto_dispatch_or_record(
        ctx, evt, "I did some maintenance work.",
    )
    assert bridge.sent == []  # no auto-dispatch on scheduled_tick


@pytest.mark.asyncio
async def test_bench_bridge_skips_auto_dispatch(tmp_path: Path):
    """Bench harness reads the SDK's final text directly via stdout
    or capture; auto-dispatch would double-send."""
    bridge = _BenchBridge()
    reg = ChannelRegistry()
    reg.register(bridge)
    agent = _make_agent(tmp_path, reg)

    ctx = _ctx(channel_id="bench-q42")
    evt = _evt(channel_id="bench-q42")
    await agent._auto_dispatch_or_record(ctx, evt, "Answer is 42.")
    assert bridge.sent == []  # bench bridge skipped


@pytest.mark.asyncio
async def test_unknown_channel_falls_back_to_record_only(tmp_path: Path):
    """Channel that doesn't match any bridge → just records to
    chat_history (Recent activity will still show the agent's reply
    even though it didn't dispatch anywhere)."""
    bridge = _RecordingBridge()
    reg = ChannelRegistry()
    reg.register(bridge)
    agent = _make_agent(tmp_path, reg)

    ctx = _ctx(channel_id="orphan-channel-1")
    evt = _evt(channel_id="orphan-channel-1")
    await agent._auto_dispatch_or_record(ctx, evt, "hello")
    assert bridge.sent == []


@pytest.mark.asyncio
async def test_actions_directives_dispatch_alongside_main_send(tmp_path: Path):
    """When the natural-text reply contains an <actions> block, the
    cleaned text goes through the bridge and the directives fire
    after — same path send_message uses internally."""
    bridge = _RecordingBridge()
    reg = ChannelRegistry()
    reg.register(bridge)
    agent = _make_agent(tmp_path, reg)

    ctx = _ctx()
    evt = _evt()
    text_with_actions = (
        "Got it.\n<actions><react emoji=\"thumbsup\" /></actions>"
    )
    await agent._auto_dispatch_or_record(ctx, evt, text_with_actions)

    # Cleaned text on the wire (no <actions> block).
    assert bridge.sent == [("discord-987", "Got it.")]
    # React fired against the just-sent message id (m1).
    assert bridge.reacted == [("discord-987", "m1", "thumbsup")]


@pytest.mark.asyncio
async def test_directives_only_text_skips_main_send(tmp_path: Path):
    """If the reply is JUST an <actions> block (no prose), no main
    text send fires; directives still dispatch using ctx's
    last_assistant_message_id as the react fallback."""
    bridge = _RecordingBridge()
    reg = ChannelRegistry()
    reg.register(bridge)
    agent = _make_agent(tmp_path, reg)

    ctx = _ctx()
    ctx.last_assistant_message_id = "prev-msg"
    evt = _evt()
    await agent._auto_dispatch_or_record(
        ctx, evt, "<actions><react emoji=\"eyes\" /></actions>",
    )

    assert bridge.sent == []  # no main text to send
    assert bridge.reacted == [("discord-987", "prev-msg", "eyes")]


@pytest.mark.asyncio
async def test_react_received_trigger_also_auto_dispatches(tmp_path: Path):
    """Inbound reactions can drive a turn (algedonic feedback). The
    agent's reply still auto-dispatches the same way as user_message."""
    bridge = _RecordingBridge()
    reg = ChannelRegistry()
    reg.register(bridge)
    agent = _make_agent(tmp_path, reg)

    ctx = _ctx()
    evt = _evt(trigger="react_received")
    await agent._auto_dispatch_or_record(ctx, evt, "Thanks for the 👍.")
    assert bridge.sent == [("discord-987", "Thanks for the 👍.")]


# ─── Streaming plan-flush directive dispatch (chainlink #5 follow-up) ──


@pytest.mark.asyncio
async def test_streaming_plan_dispatched_callback_fires_directives(tmp_path: Path):
    """Plan with text + ``<actions>``: the callback dispatches the
    directive (against the just-sent plan flush's message_id) so an
    inline ack-react actually fires. Previously these were dropped."""
    from mimir.bridges._directives import ReactDirective
    bridge = _RecordingBridge()
    reg = ChannelRegistry()
    reg.register(bridge)
    agent = _make_agent(tmp_path, reg)

    ctx = _ctx()
    evt = _evt()
    cb = agent._on_streaming_plan_dispatched(ctx, evt, bridge)

    # Simulate the dispatcher having sent the plan flush at msg-7.
    plan_send_result = SendResult(sent=True, message_id="m-plan", chunks=1)
    directives = (ReactDirective(emoji="👍", message_id=None),)
    await cb("plan body", plan_send_result, directives)

    # React landed on the plan flush message id (default fallback).
    assert bridge.reacted == [("discord-987", "m-plan", "👍")]
    assert ctx.last_assistant_message_id == "m-plan"


@pytest.mark.asyncio
async def test_streaming_plan_dispatched_explicit_message_id_wins(tmp_path: Path):
    """When the directive carries an explicit ``message`` (the
    inline ack-react pattern: ``<react message="<inbound-id>" />``),
    the explicit id wins over the plan flush's own message id."""
    from mimir.bridges._directives import ReactDirective
    bridge = _RecordingBridge()
    reg = ChannelRegistry()
    reg.register(bridge)
    agent = _make_agent(tmp_path, reg)

    ctx = _ctx()
    evt = _evt()
    cb = agent._on_streaming_plan_dispatched(ctx, evt, bridge)
    plan_send = SendResult(sent=True, message_id="m-plan", chunks=1)
    directives = (
        ReactDirective(emoji="👍", message_id="inbound-42"),
    )
    await cb("On it.", plan_send, directives)
    assert bridge.reacted == [("discord-987", "inbound-42", "👍")]


@pytest.mark.asyncio
async def test_streaming_plan_dispatched_directives_only(tmp_path: Path):
    """A plan that's ONLY an actions block: the dispatcher passes
    ``result=None`` to the callback. The directives still dispatch,
    using ``ctx.last_assistant_message_id`` as the react fallback."""
    from mimir.bridges._directives import ReactDirective
    bridge = _RecordingBridge()
    reg = ChannelRegistry()
    reg.register(bridge)
    agent = _make_agent(tmp_path, reg)

    ctx = _ctx()
    ctx.last_assistant_message_id = "prev-7"
    evt = _evt()
    cb = agent._on_streaming_plan_dispatched(ctx, evt, bridge)
    directives = (ReactDirective(emoji="eyes", message_id=None),)
    await cb("", None, directives)

    # No message was sent on this callback — no record_outbound call,
    # no last_assistant_message_id mutation. React used the prev id.
    assert bridge.sent == []  # callback didn't call bridge.send itself
    assert bridge.reacted == [("discord-987", "prev-7", "eyes")]
    assert ctx.last_assistant_message_id == "prev-7"


@pytest.mark.asyncio
async def test_streaming_plan_dispatched_no_directives_only_text(tmp_path: Path):
    """No directives, plain plan text → callback records outbound and
    logs the streamed_plan event; no directive dispatch attempted."""
    bridge = _RecordingBridge()
    reg = ChannelRegistry()
    reg.register(bridge)
    agent = _make_agent(tmp_path, reg)

    ctx = _ctx()
    evt = _evt()
    cb = agent._on_streaming_plan_dispatched(ctx, evt, bridge)
    plan_send = SendResult(sent=True, message_id="m-plan", chunks=1)
    await cb("just plan text", plan_send, ())

    assert bridge.reacted == []
    assert ctx.last_assistant_message_id == "m-plan"
    # auto_dispatch_streamed_plan event landed.
    import json
    events_log = tmp_path / "logs" / "events.jsonl"
    events = [json.loads(l) for l in events_log.read_text().splitlines()]
    plan_events = [
        e for e in events if e.get("type") == "auto_dispatch_streamed_plan"
    ]
    assert len(plan_events) == 1
    assert plan_events[0]["actions_in_plan"] == 0
