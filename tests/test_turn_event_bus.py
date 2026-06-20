"""Tests for the live turn-event bus + emitter (chainlink #583 slice 1)."""

from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage, ToolMessage

from mimir.turn_event_bus import TurnEventBus, TurnEventEmitter


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
    emitter.turn_ended(status="error", error="boom")

    events = _drain(q)
    assert [(e["type"], e["phase"]) for e in events] == [("turn", "start"), ("turn", "end")]
    start, end = events
    for e in events:
        assert e["turn_id"] == "t1"
        assert e["channel_id"] == "web-default"
        assert isinstance(e["seq"], int) and e["ts"]
    assert start["seq"] < end["seq"]  # monotonic
    assert end["status"] == "error" and end["error"] == "boom"


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
        {"index": 0, "id": None, "name": None, "args": '{"content":"hel'}]))
    em.token_chunk(SimpleNamespace(tool_call_chunks=[
        {"index": 0, "id": None, "name": None, "args": 'lo"}'}]))

    events = _drain(q)
    assert (events[0]["type"], events[0]["phase"]) == ("tool_call", "start")
    assert events[0]["tool_name"] == "send_message" and events[0]["id"] == "call_x"
    chunks = [e for e in events if e["phase"] == "chunk"]
    # The reply text reassembles from the streamed arg fragments.
    assert "".join(c["args_delta"] for c in chunks) == '{"content":"hello"}'


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
