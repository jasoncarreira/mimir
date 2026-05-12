"""Tests for ``RecordingSagaClient`` — the transparent wrapper that
appends ``SagaCallRecord`` entries to the active ``TurnContext.saga_calls``
on every saga method invocation.

Coverage:
- Wrapper passes args + result through unchanged.
- Each recorded method type produces an entry with the right
  ``call_type``, args summary, result summary, latency_ms.
- Errored calls still produce records (with ``error`` set) and
  re-raise.
- No TurnContext → silent skip (no append, no crash).
- Unrecorded methods (e.g. ``recent_session_boundaries``) pass
  through without producing records.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from mimir._context import set_current_turn, reset_current_turn
from mimir.models import TurnContext, make_turn_id
from mimir.saga_client import RecordingSagaClient


class _FakeSaga:
    """Bare-minimum saga client stub that records calls and returns
    canned results. Tests against this so we exercise the recording
    layer without spinning up real saga."""

    def __init__(self, query_result=None, store_result=None):
        self.calls: list[tuple[str, tuple, dict]] = []
        self._query_result = query_result or {
            "atoms": [{"id": "a1"}, {"id": "a2"}],
            "rewritten_query": "the rewritten one",
        }
        self._store_result = store_result or {"atom_id": "stored-id"}

    async def query(self, query: str, **kwargs):
        self.calls.append(("query", (query,), kwargs))
        return self._query_result

    async def store(self, content: str, **kwargs):
        self.calls.append(("store", (content,), kwargs))
        return self._store_result

    async def feedback(self, atom_ids, response_text, **kwargs):
        self.calls.append(("feedback", (atom_ids, response_text), kwargs))
        return {"marked": len(atom_ids), "total": 5}

    async def end_session(self, session_id, summary, **kwargs):
        self.calls.append(("end_session", (session_id, summary), kwargs))
        return {"ok": True}

    async def mark_contributions(self, *args, **kwargs):
        self.calls.append(("mark_contributions", args, kwargs))
        return {"ok": True}

    async def recent_session_boundaries(self, **kwargs):
        """Not in _RECORDED_METHODS — should pass through unrecorded."""
        self.calls.append(("recent_session_boundaries", (), kwargs))
        return [{"session_id": "s1"}]


@pytest.fixture
def ctx_for_test():
    """Register a TurnContext for the duration of the test."""
    ctx = TurnContext(
        turn_id=make_turn_id(),
        session_id="ch-test",
        trigger="user_message",
        channel_id="ch-test",
        started_at=0.0,
    )
    tok = set_current_turn(ctx)
    yield ctx
    reset_current_turn(tok)


@pytest.mark.asyncio
async def test_query_call_records_args_and_result(ctx_for_test):
    inner = _FakeSaga()
    wrapped = RecordingSagaClient(inner)
    result = await wrapped.query("what does Alice like", top_k=5)
    # Result passes through unchanged.
    assert result == inner._query_result
    # One record appended.
    assert len(ctx_for_test.saga_calls) == 1
    rec = ctx_for_test.saga_calls[0]
    assert rec.call_type == "query"
    assert rec.args["query"] == "what does Alice like"
    assert rec.args["top_k"] == 5
    assert rec.args["context_present"] is False
    assert rec.result["ok"] is True
    assert rec.result["atom_ids"] == ["a1", "a2"]
    assert rec.result["atom_count"] == 2
    assert rec.result["rewritten_query"] == "the rewritten one"
    assert rec.latency_ms > 0
    assert rec.error is None


@pytest.mark.asyncio
async def test_store_call_records_content_and_atom_id(ctx_for_test):
    inner = _FakeSaga(store_result={"atom_id": "abc-123"})
    wrapped = RecordingSagaClient(inner)
    await wrapped.store("Alice prefers dark mode", stream="semantic")
    rec = ctx_for_test.saga_calls[0]
    assert rec.call_type == "store"
    assert rec.args["content"] == "Alice prefers dark mode"
    assert rec.args["stream"] == "semantic"
    assert rec.result["atom_id"] == "abc-123"


@pytest.mark.asyncio
async def test_feedback_call_records(ctx_for_test):
    inner = _FakeSaga()
    wrapped = RecordingSagaClient(inner)
    await wrapped.feedback(["a1", "a2"], "the reply text", feedback="useful")
    rec = ctx_for_test.saga_calls[0]
    assert rec.call_type == "feedback"
    assert rec.args["atom_ids"] == ["a1", "a2"]
    assert rec.args["response_text"] == "the reply text"
    assert rec.args["feedback"] == "useful"
    assert rec.result["marked"] == 2
    assert rec.result["total"] == 5


@pytest.mark.asyncio
async def test_end_session_call_records(ctx_for_test):
    inner = _FakeSaga()
    wrapped = RecordingSagaClient(inner)
    await wrapped.end_session(
        "saga-sess-1", "we did stuff",
        topics_discussed=["t1", "t2"],
        unfinished=["u1"],
    )
    rec = ctx_for_test.saga_calls[0]
    assert rec.call_type == "end_session"
    assert rec.args["session_id"] == "saga-sess-1"
    assert rec.args["summary"] == "we did stuff"
    assert rec.args["topics_discussed"] == ["t1", "t2"]
    assert rec.args["unfinished_count"] == 1


@pytest.mark.asyncio
async def test_args_truncated_at_200_chars(ctx_for_test):
    """Strings >200 chars get truncated in args summary to bound
    turns.jsonl row size."""
    inner = _FakeSaga()
    wrapped = RecordingSagaClient(inner)
    huge = "x" * 500
    await wrapped.query(huge)
    rec = ctx_for_test.saga_calls[0]
    # Args are truncated to 200 chars (with an ellipsis at the end).
    assert len(rec.args["query"]) <= 200
    assert rec.args["query"].endswith("…")


@pytest.mark.asyncio
async def test_error_records_and_reraises(ctx_for_test):
    """A failed saga call appends a record with ``error`` set and
    re-raises the original exception."""
    class _FailingSaga:
        async def query(self, *args, **kwargs):
            raise RuntimeError("simulated saga failure")

    wrapped = RecordingSagaClient(_FailingSaga())
    with pytest.raises(RuntimeError, match="simulated"):
        await wrapped.query("anything")

    assert len(ctx_for_test.saga_calls) == 1
    rec = ctx_for_test.saga_calls[0]
    assert rec.call_type == "query"
    assert rec.error is not None
    assert "RuntimeError" in rec.error
    assert "simulated" in rec.error
    assert rec.result == {"ok": False}


@pytest.mark.asyncio
async def test_no_turn_context_silently_skips():
    """saga calls outside a turn (consolidation cron, etc.) don't crash
    — the record append is skipped, but the call still goes through."""
    # Note: no fixture, no set_current_turn → no active context.
    inner = _FakeSaga()
    wrapped = RecordingSagaClient(inner)
    result = await wrapped.query("outside-turn")
    # Result passes through normally.
    assert result == inner._query_result


@pytest.mark.asyncio
async def test_unrecorded_methods_passthrough(ctx_for_test):
    """``recent_session_boundaries`` isn't in _RECORDED_METHODS — should
    pass through to the inner client and NOT produce a record."""
    inner = _FakeSaga()
    wrapped = RecordingSagaClient(inner)
    result = await wrapped.recent_session_boundaries(channel_id="ch-1")
    assert result == [{"session_id": "s1"}]
    assert len(ctx_for_test.saga_calls) == 0


def test_to_dict_shape():
    """SagaCallRecord.to_dict produces a JSON-friendly dict for the
    turn rollup. Latency rounded to 2 decimals; error key elided when
    None."""
    from mimir.models import SagaCallRecord
    r = SagaCallRecord(
        call_type="query",
        args={"query": "x"},
        result={"ok": True, "atom_count": 3},
        latency_ms=123.4567,
    )
    d = r.to_dict()
    assert d == {
        "call_type": "query",
        "args": {"query": "x"},
        "result": {"ok": True, "atom_count": 3},
        "latency_ms": 123.46,
    }
    # Error variant.
    r2 = SagaCallRecord(
        call_type="query",
        args={"query": "x"},
        result={"ok": False},
        latency_ms=5.0,
        error="RuntimeError: nope",
    )
    assert r2.to_dict()["error"] == "RuntimeError: nope"


@pytest.mark.asyncio
async def test_records_mcp_dispatched_calls_via_saga_session_id():
    """Reproduces the SDK-forked-task scenario: an MCP tool handler
    runs without an active contextvar (forked at SDK connect, captured
    None) but the TurnContext IS registered in ``_active_turns`` and
    can be found via ``saga_session_id`` passed in kwargs.

    This is the PR #147 review blocker — the prior implementation
    only consulted ``get_current_turn()`` and would silently drop
    every model-driven saga call (the most interesting ones).
    """
    from mimir._context import _active_turns
    saga_sid = "saga-test-sid-1234"
    ctx = TurnContext(
        turn_id=make_turn_id(),
        session_id="ch-test",
        trigger="user_message",
        channel_id="ch-test",
        started_at=0.0,
        saga_session_id=saga_sid,
    )
    # Register the turn WITHOUT setting the contextvar — simulates a
    # forked task that captured contextvar=None at fork time.
    _active_turns[ctx.turn_id] = ctx
    try:
        inner = _FakeSaga()
        wrapped = RecordingSagaClient(inner)
        # MCP-dispatched-style call: session_id flows through kwargs.
        await wrapped.query("model-driven query", session_id=saga_sid)
        assert len(ctx.saga_calls) == 1
        assert ctx.saga_calls[0].call_type == "query"
        assert ctx.saga_calls[0].args["session_id"] == saga_sid
    finally:
        _active_turns.pop(ctx.turn_id, None)


@pytest.mark.asyncio
async def test_records_via_only_active_fallback(ctx_for_test):
    """Single-channel deployments: exactly one active turn is registered
    in ``_active_turns`` (the ctx_for_test fixture does this). Even
    without a saga_session_id in kwargs, the resolver should pick up
    the active ctx via ``get_only_active_turn``."""
    inner = _FakeSaga()
    wrapped = RecordingSagaClient(inner)
    # Call without session_id — falls through to single-active heuristic.
    await wrapped.query("no session id passed")
    assert len(ctx_for_test.saga_calls) == 1
