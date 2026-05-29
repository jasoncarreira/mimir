"""Additional coverage for mimir/tools/saga_ops.py (chainlink #247, slice 5/5).

Fills gaps NOT already in test_saga_ops_wiring.py:
  - _resolve_session_id unit tests
  - saga_feedback: missing atom_id, explicit session_id override
  - saga_mark_contributions: non-list atom_ids, store raises
  - saga_end_session: no store, missing session_id, no active turn,
    store raises, saga_end_session_called not set on error
  - saga_forget: no store path
"""
from __future__ import annotations

import time
from typing import Any

import pytest

from mimir._context import reset_current_turn, set_current_turn
from mimir.models import TurnContext
from mimir.tools import saga_ops
from mimir.tools.memory import _MEMORY_STATE
from mimir.tools.saga_ops import _resolve_session_id


# ────────────────────────────────────────────────────────────────────
# Stub store (replicates test_saga_ops_wiring.py pattern; not imported
# from that file so each test module is independently runnable)
# ────────────────────────────────────────────────────────────────────


class _StubStore:
    def __init__(self) -> None:
        self.outcome_calls: list[dict] = []
        self.feedback_calls: list[dict] = []
        self.end_session_calls: list[dict] = []
        self.forget_calls: list[dict] = []
        self.raise_on: str | None = None

    async def outcome(self, atom_ids, *, feedback, session_id):
        if self.raise_on == "outcome":
            raise RuntimeError("outcome boom")
        self.outcome_calls.append(
            {"atom_ids": atom_ids, "feedback": feedback, "session_id": session_id}
        )

    async def feedback(self, atom_ids, response_text, *, session_id):
        if self.raise_on == "feedback":
            raise RuntimeError("feedback boom")
        self.feedback_calls.append(
            {"atom_ids": atom_ids, "response_text": response_text, "session_id": session_id}
        )

    async def end_session(
        self,
        *,
        session_id,
        summary,
        topics_discussed,
        decisions_made,
        unfinished,
        emotional_state,
        closed_since,
        channel_id,
    ):
        if self.raise_on == "end_session":
            raise RuntimeError("end_session boom")
        self.end_session_calls.append(
            {
                "session_id": session_id,
                "summary": summary,
                "channel_id": channel_id,
            }
        )
        return {"session_id": session_id, "session_summary_written": True}

    async def forget(self, **kwargs):
        if self.raise_on == "forget":
            raise RuntimeError("forget boom")
        self.forget_calls.append(kwargs)
        return {"dry_run": kwargs.get("dry_run", True), "actions_taken": 0}


@pytest.fixture
def store() -> _StubStore:
    stub = _StubStore()
    prev = _MEMORY_STATE.get("client")
    _MEMORY_STATE["client"] = stub
    yield stub
    _MEMORY_STATE["client"] = prev


@pytest.fixture
def turn_with_session() -> TurnContext:
    ctx = TurnContext(
        turn_id="t-1",
        session_id="ch-1",
        trigger="user_message",
        channel_id="ch-1",
        started_at=time.monotonic(),
        saga_session_id="sess-abc",
    )
    token = set_current_turn(ctx)
    yield ctx
    reset_current_turn(token)


# ────────────────────────────────────────────────────────────────────
# _resolve_session_id unit tests
# ────────────────────────────────────────────────────────────────────


class TestResolveSessionId:
    def test_explicit_string_returned_directly(self) -> None:
        result = _resolve_session_id("explicit-123")
        assert result == "explicit-123"

    def test_explicit_string_does_not_consult_context(
        self, turn_with_session: TurnContext
    ) -> None:
        # Even with an active TurnContext the explicit value wins.
        result = _resolve_session_id("explicit-123")
        assert result == "explicit-123"

    def test_whitespace_falls_back_to_ctx(
        self, turn_with_session: TurnContext
    ) -> None:
        # Whitespace-only explicit is treated as empty → fall back to ctx.
        result = _resolve_session_id("  ")
        assert result == "sess-abc"

    def test_none_with_no_active_turn_returns_none(self) -> None:
        # No active TurnContext registered → returns None.
        result = _resolve_session_id(None)
        assert result is None


# ────────────────────────────────────────────────────────────────────
# saga_feedback additional coverage
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_feedback_missing_atom_id_returns_error(
    store: _StubStore, turn_with_session: TurnContext
) -> None:
    out = await saga_ops.saga_feedback.ainvoke(
        {"atom_id": "", "signal": "useful"}
    )
    assert "atom_id is required" in out
    assert store.outcome_calls == []


@pytest.mark.asyncio
async def test_feedback_explicit_session_id_overrides_turn(
    store: _StubStore, turn_with_session: TurnContext
) -> None:
    out = await saga_ops.saga_feedback.ainvoke(
        {"atom_id": "atom-x", "signal": "useful", "session_id": "override-sess"}
    )
    assert "ok" in out.lower()
    assert store.outcome_calls[0]["session_id"] == "override-sess"


# ────────────────────────────────────────────────────────────────────
# saga_mark_contributions additional coverage
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_contributions_non_list_atom_ids_returns_error(
    store: _StubStore, turn_with_session: TurnContext
) -> None:
    # Passing a plain string instead of a list should be rejected.
    # LangChain's Pydantic schema enforces list[str] at ainvoke time, so we
    # call the coroutine directly to reach the tool body's isinstance guard.
    assert saga_ops.saga_mark_contributions.coroutine is not None
    out = await saga_ops.saga_mark_contributions.coroutine(
        atom_ids="a1", response_text="hello"
    )
    assert "atom_ids must be a list of strings" in out
    assert store.feedback_calls == []


@pytest.mark.asyncio
async def test_mark_contributions_store_raises_surfaces_error(
    store: _StubStore, turn_with_session: TurnContext
) -> None:
    store.raise_on = "feedback"
    out = await saga_ops.saga_mark_contributions.ainvoke(
        {"atom_ids": ["a1"], "response_text": "context"}
    )
    assert "saga_mark_contributions failed" in out
    assert "boom" in out


# ────────────────────────────────────────────────────────────────────
# saga_end_session additional coverage
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_end_session_no_store_returns_error(
    turn_with_session: TurnContext,
) -> None:
    prev = _MEMORY_STATE.get("client")
    _MEMORY_STATE["client"] = None
    try:
        out = await saga_ops.saga_end_session.ainvoke(
            {"session_id": "sess-abc", "summary": "done"}
        )
        assert "no SagaStore configured" in out
    finally:
        _MEMORY_STATE["client"] = prev


@pytest.mark.asyncio
async def test_end_session_missing_session_id_returns_error(
    store: _StubStore, turn_with_session: TurnContext
) -> None:
    out = await saga_ops.saga_end_session.ainvoke(
        {"session_id": "", "summary": "done"}
    )
    assert "session_id is required" in out
    assert store.end_session_calls == []


@pytest.mark.asyncio
async def test_end_session_no_active_turn_channel_id_is_none(
    store: _StubStore,
) -> None:
    # No TurnContext registered → channel_id passed to store should be None.
    out = await saga_ops.saga_end_session.ainvoke(
        {"session_id": "sess-xyz", "summary": "wrapping up"}
    )
    assert "ok" in out.lower()
    assert store.end_session_calls[0]["channel_id"] is None


@pytest.mark.asyncio
async def test_end_session_store_raises_surfaces_error(
    store: _StubStore, turn_with_session: TurnContext
) -> None:
    store.raise_on = "end_session"
    out = await saga_ops.saga_end_session.ainvoke(
        {"session_id": "sess-abc", "summary": "done"}
    )
    assert "saga_end_session failed" in out
    assert "boom" in out


@pytest.mark.asyncio
async def test_end_session_ctx_flag_not_set_on_store_error(
    store: _StubStore, turn_with_session: TurnContext
) -> None:
    # saga_end_session_called should remain False when the store raises.
    store.raise_on = "end_session"
    await saga_ops.saga_end_session.ainvoke(
        {"session_id": "sess-abc", "summary": "done"}
    )
    assert getattr(turn_with_session, "saga_end_session_called", False) is False


# ────────────────────────────────────────────────────────────────────
# saga_forget additional coverage
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_forget_no_store_returns_error(
    turn_with_session: TurnContext,
) -> None:
    prev = _MEMORY_STATE.get("client")
    _MEMORY_STATE["client"] = None
    try:
        out = await saga_ops.saga_forget.ainvoke({})
        assert "no SagaStore configured" in out
    finally:
        _MEMORY_STATE["client"] = prev
