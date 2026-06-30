"""Tests for the live turn-event bus + emitter (chainlink #583 slice 1)."""

from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage, ToolMessage

from mimir.turn_event_bus import TurnEventBus, TurnEventEmitter
from mimir.models import AgentEvent


def _drain(queue: "asyncio.Queue[dict]") -> list[dict]:
    out: list[dict] = []
    while True:
        try:
            out.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            return out


def test_publish_fans_out_to_channel_and_wildcard():
    bus = TurnEventBus()
    chan = bus.subscribe("web-default")
    other = bus.subscribe("web-other")
    wild = bus.subscribe()  # "*"

    bus.publish({"type": "turn", "phase": "start", "channel_id": "web-default"})

    assert len(_drain(chan)) == 1
    assert len(_drain(wild)) == 1
    assert len(_drain(other)) == 0  # different channel, not wildcard


def test_unsubscribe_stops_delivery():
    bus = TurnEventBus()
    q = bus.subscribe("web-default")
    bus.unsubscribe("web-default", q)
    bus.publish({"type": "turn", "phase": "start", "channel_id": "web-default"})
    assert _drain(q) == []


def test_full_queue_drops_oldest_not_newest():
    bus = TurnEventBus(queue_max=2)
    q = bus.subscribe("c")
    for i in range(4):
        bus.publish({"type": "turn", "phase": "chunk", "channel_id": "c", "n": i})
    drained = _drain(q)
    # Only the two NEWEST survive; the producer is never blocked.
    assert [e["n"] for e in drained] == [2, 3]


def test_publish_never_raises_on_bad_state():
    bus = TurnEventBus()
    # No subscribers, missing channel_id — must not raise.
    bus.publish({"type": "turn", "phase": "end"})


def test_emitter_noop_when_bus_none():
    emitter = TurnEventEmitter(None, turn_id="t1", channel_id="web-default")
    assert emitter.enabled is False
    # None of these should raise.
    emitter.turn_started()
    emitter.blocks_from_messages([AIMessage(content="hi")])
    emitter.turn_ended()


def test_emitter_turn_bracket_envelope():
    bus = TurnEventBus()
    q = bus.subscribe("web-default")
    emitter = TurnEventEmitter(bus, turn_id="t1", channel_id="web-default")
    emitter.turn_started()
    emitter.turn_ended(
        status="error",
        error="boom",
        outbound_message_sent=True,
        injected_input_count=2,
    )

    events = _drain(q)
    assert [(e["type"], e["phase"]) for e in events] == [("turn", "start"), ("turn", "end")]
    start, end = events
    for e in events:
        assert e["turn_id"] == "t1"
        assert e["channel_id"] == "web-default"
        assert isinstance(e["seq"], int) and e["ts"]
    assert start["seq"] < end["seq"]  # monotonic
    assert end["status"] == "error" and end["error"] == "boom"
    assert end["outbound_message_sent"] is True
    assert end["injected_input_count"] == 2


def test_turn_start_includes_sanitized_trigger_metadata_for_slack_threading():
    bus = TurnEventBus()
    q = bus.subscribe("slack-C01")
    emitter = TurnEventEmitter(bus, turn_id="t1", channel_id="slack-C01")
    event = AgentEvent(
        trigger="user_message",
        channel_id="slack-C01",
        content="secret inbound body",
        author="slack-U1",
        author_display="Alice",
        source_id="111.222",
        source="slack",
        attachment_names=["/tmp/secret.txt"],
        extra={"thread_ts": "999.000", "raw": {"text": "nope"}},
    )

    emitter.turn_started(event)

    start = _drain(q)[0]
    assert start["trigger"] == "user_message"
    assert start["source"] == "slack"
    assert start["source_id"] == "111.222"
    assert start["author"] == "slack-U1"
    assert start["author_display"] == "Alice"
    assert start["thread_ts"] == "999.000"
    assert start["reply_to_message_id"] == "999.000"
    assert "secret inbound body" not in str(start)
    assert "/tmp/secret.txt" not in str(start)


def test_turn_start_slack_thread_falls_back_to_source_ts():
    bus = TurnEventBus()
    q = bus.subscribe("slack-C01")
    emitter = TurnEventEmitter(bus, turn_id="t1", channel_id="slack-C01")
    event = AgentEvent(
        trigger="user_message",
        channel_id="slack-C01",
        content="body",
        source_id="111.222",
        source="slack",
        extra={},
    )

    emitter.turn_started(event)

    start = _drain(q)[0]
    assert start["thread_ts"] == "111.222"
    assert start["reply_to_message_id"] == "111.222"


def test_emitter_brackets_tool_call_and_result_sharing_id():
    bus = TurnEventBus()
    q = bus.subscribe("web-default")
    emitter = TurnEventEmitter(bus, turn_id="t1", channel_id="web-default")

    messages = [
        AIMessage(
            content="thinking about it",
            tool_calls=[{"id": "call_abc", "name": "send_message", "args": {"content": "hello"}}],
        ),
        ToolMessage(content="delivered", tool_call_id="call_abc"),
    ]
    emitter.blocks_from_messages(messages)

    events = _drain(q)
    seq = [(e["type"], e["phase"]) for e in events]
    # reasoning bracket, then tool_call bracket, then tool_result bracket.
    assert ("reasoning", "start") in seq
    assert ("tool_call", "start") in seq and ("tool_call", "end") in seq
    assert ("tool_result", "start") in seq and ("tool_result", "end") in seq

    tool_call_end = next(e for e in events if e["type"] == "tool_call" and e["phase"] == "end")
    tool_result_end = next(e for e in events if e["type"] == "tool_result" and e["phase"] == "end")
    # The reply text rides on the send_message tool-call args (Q5: adapter policy).
    assert tool_call_end["tool_name"] == "send_message"
    assert tool_call_end["args"] == {"content": "hello"}
    # tool_call and tool_result share the LangChain tool id → consumers join them.
    assert tool_call_end["id"] == tool_result_end["id"] == "call_abc"
    assert tool_result_end["status"] == "ok"


def test_emitter_only_brackets_new_blocks_across_snapshots():
    bus = TurnEventBus()
    q = bus.subscribe("web-default")
    emitter = TurnEventEmitter(bus, turn_id="t1", channel_id="web-default")

    snap1 = [AIMessage(content="step one", tool_calls=[{"id": "c1", "name": "noop", "args": {}}])]
    emitter.blocks_from_messages(snap1)
    first = _drain(q)
    assert any(e["type"] == "tool_call" for e in first)

    # Same prefix + a new message: only the NEW block is re-bracketed.
    snap2 = snap1 + [ToolMessage(content="done", tool_call_id="c1")]
    emitter.blocks_from_messages(snap2)
    second = _drain(q)
    assert all(e["type"] != "tool_call" for e in second)  # not re-emitted
    assert any(e["type"] == "tool_result" for e in second)  # the new block only


# ─── slice 2: token-level tool-call arg streaming + dedup ──────────────


def test_token_chunk_streams_tool_call_arg_deltas():
    from types import SimpleNamespace

    bus = TurnEventBus()
    q = bus.subscribe("web-default")
    em = TurnEventEmitter(bus, turn_id="t1", channel_id="web-default")

    # First fragment carries id + name; later fragments carry args by index.
    em.token_chunk(SimpleNamespace(tool_call_chunks=[
        {"index": 0, "id": "call_x", "name": "send_message", "args": ""}]))
    em.token_chunk(SimpleNamespace(tool_call_chunks=[
        {"index": 0, "id": None, "name": None, "args": '{"text":"hel'}]))
    em.token_chunk(SimpleNamespace(tool_call_chunks=[
        {"index": 0, "id": None, "name": None, "args": 'lo"}'}]))

    events = _drain(q)
    assert (events[0]["type"], events[0]["phase"]) == ("tool_call", "start")
    assert events[0]["tool_name"] == "send_message" and events[0]["id"] == "call_x"
    chunks = [e for e in events if e["phase"] == "chunk"]
    # The reply text reassembles from the streamed arg fragments.
    assert "".join(c["args_delta"] for c in chunks) == '{"text":"hello"}'


def test_token_streamed_span_gets_only_end_from_blocks():
    from types import SimpleNamespace

    bus = TurnEventBus()
    q = bus.subscribe("web-default")
    em = TurnEventEmitter(bus, turn_id="t1", channel_id="web-default")

    em.token_chunk(SimpleNamespace(tool_call_chunks=[
        {"index": 0, "id": "call_x", "name": "send_message", "args": '{"content":"hi"}'}]))
    _drain(q)  # discard the live start + chunk

    # The value snapshot later surfaces the completed tool call.
    em.blocks_from_messages([
        AIMessage(content="", tool_calls=[
            {"id": "call_x", "name": "send_message", "args": {"content": "hi"}}])
    ])
    events = _drain(q)
    kinds = [(e["type"], e["phase"]) for e in events]
    assert ("tool_call", "end") in kinds      # closed once
    assert ("tool_call", "start") not in kinds  # NOT double-bracketed


def test_block_bracketed_span_skips_late_token_chunk():
    from types import SimpleNamespace

    bus = TurnEventBus()
    q = bus.subscribe("web-default")
    em = TurnEventEmitter(bus, turn_id="t1", channel_id="web-default")

    # Block path brackets the tool call first (non-streaming order).
    em.blocks_from_messages([
        AIMessage(content="", tool_calls=[{"id": "call_y", "name": "noop", "args": {}}])
    ])
    _drain(q)  # full start+chunk+end already emitted
    # A late token chunk for the same id must not re-emit anything.
    em.token_chunk(SimpleNamespace(tool_call_chunks=[
        {"index": 0, "id": "call_y", "name": "noop", "args": "{}"}]))
    assert _drain(q) == []


def test_token_chunk_sequential_calls_reusing_index_dont_leak():
    """#802 review: a second tool call reusing index 0 (after the first closed)
    must open its OWN span, not leak chunks into the first call's id."""
    from types import SimpleNamespace

    bus = TurnEventBus()
    q = bus.subscribe("web-default")
    em = TurnEventEmitter(bus, turn_id="t1", channel_id="web-default")

    # Call A streams at index 0, then closes via a value snapshot.
    em.token_chunk(SimpleNamespace(tool_call_chunks=[
        {"index": 0, "id": "call_a", "name": "send_message", "args": '{"text":"a"}'}]))
    em.blocks_from_messages([
        AIMessage(content="", tool_calls=[
            {"id": "call_a", "name": "send_message", "args": {"text": "a"}}])
    ])
    _drain(q)

    # Call B reuses index 0 with a NEW id — it must get its own start.
    em.token_chunk(SimpleNamespace(tool_call_chunks=[
        {"index": 0, "id": "call_b", "name": "send_message", "args": ""}]))
    em.token_chunk(SimpleNamespace(tool_call_chunks=[
        {"index": 0, "id": None, "name": None, "args": '{"text":"b"}'}]))

    events = _drain(q)
    starts = [e for e in events if e["type"] == "tool_call" and e["phase"] == "start"]
    assert len(starts) == 1 and starts[0]["id"] == "call_b"  # NOT call_a
    chunks = [e for e in events if e["type"] == "tool_call" and e["phase"] == "chunk"]
    assert chunks and all(c["id"] == "call_b" for c in chunks)


def test_tool_result_keeps_name_when_parsed_in_a_later_snapshot():
    """chainlink #587: with incremental parsing the ToolMessage is parsed in a
    later snapshot than its tool_call, so the tool_result name must come from the
    cross-snapshot tool-name map rather than the (name-less) standalone parse."""
    bus = TurnEventBus()
    q = bus.subscribe("web-default")
    em = TurnEventEmitter(bus, turn_id="t1", channel_id="web-default")

    call = AIMessage(content="", tool_calls=[{"id": "c1", "name": "saga_query", "args": {"q": "x"}}])
    em.blocks_from_messages([call])  # snapshot 1: the call
    _drain(q)
    em.blocks_from_messages([call, ToolMessage(content="ok", tool_call_id="c1")])  # snapshot 2: + result

    events = _drain(q)
    # tool_name rides the tool_result START (existing contract); it must be the
    # name carried from snapshot 1's tool_call, not "" from the standalone parse.
    tr_start = next(e for e in events if e["type"] == "tool_result" and e["phase"] == "start")
    assert tr_start["id"] == "c1"
    assert tr_start["tool_name"] == "saga_query"
