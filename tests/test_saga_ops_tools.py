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

import pytest

from mimir._context import reset_current_turn, set_current_turn
from mimir import _context
from langchain.tools import ToolRuntime

from mimir.models import AuthContext, TurnContext
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
        self.mark_contributions_calls: list[dict] = []
        self.store_calls: list[dict] = []
        self.raise_on: str | None = None

    async def outcome(self, atom_ids, *, feedback, session_id, auth_context=None):
        if self.raise_on == "outcome":
            raise RuntimeError("outcome boom")
        self.outcome_calls.append(
            {"atom_ids": atom_ids, "feedback": feedback, "session_id": session_id}
        )

    async def feedback(self, atom_ids, response_text, *, session_id, auth_context=None):
        if self.raise_on == "feedback":
            raise RuntimeError("feedback boom")
        self.feedback_calls.append(
            {
                "atom_ids": atom_ids,
                "response_text": response_text,
                "session_id": session_id,
            }
        )

    async def mark_contributions(
        self,
        retrieved_atoms,
        response_text,
        *,
        session_id,
        threshold=None,
        auth_context=None,
    ):
        if self.raise_on == "mark_contributions":
            raise RuntimeError("mark_contributions boom")
        self.mark_contributions_calls.append(
            {
                "retrieved_atoms": retrieved_atoms,
                "response_text": response_text,
                "session_id": session_id,
            }
        )
        return {
            "contributed_atom_ids": [a.get("id") for a in retrieved_atoms],
            "contribution_rate": 1.0 if retrieved_atoms else 0.0,
            "total": len(retrieved_atoms),
        }

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
        owner_principal=None,
        origin_channel=None,
        origin_domain=None,
        visibility=None,
        provenance=None,
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

    async def store(
        self,
        content,
        *,
        stream=None,
        source_type=None,
        metadata=None,
        session_id=None,
        owner_principal=None,
        origin_channel=None,
        origin_domain=None,
        visibility=None,
        provenance=None,
    ):
        if self.raise_on == "store":
            raise RuntimeError("store boom")
        self.store_calls.append(
            {
                "content": content,
                "stream": stream,
                "source_type": source_type,
                "metadata": metadata,
                "session_id": session_id,
                "owner_principal": owner_principal,
                "origin_channel": origin_channel,
                "visibility": visibility,
            }
        )
        return {"stored": True, "atom_id": "test-atom-id"}


@pytest.fixture
def store() -> _StubStore:
    stub = _StubStore()
    prev = _MEMORY_STATE.get("client")
    _MEMORY_STATE["client"] = stub
    yield stub
    _MEMORY_STATE["client"] = prev


@pytest.fixture
def turn_with_session() -> TurnContext:
    auth_ctx = AuthContext(
        principal="test-user",
        canonical_principal="test-user",
        roles=("admin",),
        event_ingress="test",
        trigger="user_message",
        channel_id="ch-1",
        interactivity=None,
        policy_version=None,
        is_service=False,
        enforcement_enabled=False,
    )
    ctx = TurnContext(
        turn_id="t-1",
        session_id="ch-1",
        trigger="user_message",
        channel_id="ch-1",
        started_at=time.monotonic(),
        saga_session_id="sess-abc",
        auth_context=auth_ctx,
    )
    token = set_current_turn(ctx)
    yield ctx
    reset_current_turn(token)


def _runtime(ctx: TurnContext) -> ToolRuntime[AuthContext]:
    return ToolRuntime(
        state={},
        context=ctx.auth_context,
        config={},
        stream_writer=lambda _: None,
        tool_call_id="saga-write-test",
        store=None,
    )


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

    def test_whitespace_falls_back_to_ctx(self, turn_with_session: TurnContext) -> None:
        # Whitespace-only explicit is treated as empty → fall back to ctx.
        result = _resolve_session_id("  ")
        assert result == "sess-abc"

    def test_none_with_no_active_turn_returns_none(self) -> None:
        # No active TurnContext registered → returns None.
        result = _resolve_session_id(None)
        assert result is None

    def test_none_uses_single_active_turn_when_contextvar_missing(
        self, turn_with_session: TurnContext
    ) -> None:
        # MCP tool dispatch can run on a forked task where the contextvar is
        # missing even though run_turn registered the active turn.  Simulate that
        # boundary by clearing only the contextvar, not _active_turns.
        token = _context._current_turn.set(None)
        try:
            result = _resolve_session_id(None)
        finally:
            _context._current_turn.reset(token)
        assert result == "sess-abc"


# ────────────────────────────────────────────────────────────────────
# saga_feedback additional coverage
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_feedback_missing_atom_id_returns_error(
    store: _StubStore, turn_with_session: TurnContext
) -> None:
    out = await saga_ops.saga_feedback.ainvoke({"atom_id": "", "signal": "useful"})
    assert "atom_id is required" in out
    assert store.outcome_calls == []


@pytest.mark.asyncio
async def test_feedback_explicit_session_id_overrides_turn(
    store: _StubStore, turn_with_session: TurnContext
) -> None:
    out = await saga_ops.saga_feedback.ainvoke(
        {
            "atom_id": "atom-x",
            "signal": "useful",
            "session_id": "override-sess",
            "runtime": _runtime(turn_with_session),
        }
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
    store.raise_on = "mark_contributions"
    out = await saga_ops.saga_mark_contributions.ainvoke(
        {
            "atom_ids": ["a1"],
            "response_text": "context",
            "runtime": _runtime(turn_with_session),
        }
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
    out = await saga_ops.saga_end_session.ainvoke({"session_id": "", "summary": "done"})
    assert "session_id is required" in out
    assert store.end_session_calls == []


@pytest.mark.asyncio
async def test_end_session_without_server_runtime_fails_closed(
    store: _StubStore,
) -> None:
    out = await saga_ops.saga_end_session.ainvoke(
        {"session_id": "sess-xyz", "summary": "wrapping up"}
    )
    assert "write access denied" in out
    assert store.end_session_calls == []


@pytest.mark.asyncio
async def test_end_session_does_not_consult_active_registry(
    store: _StubStore,
    turn_with_session: TurnContext,
) -> None:
    token = _context._current_turn.set(None)
    try:
        out = await saga_ops.saga_end_session.ainvoke(
            {"session_id": "sess-abc", "summary": "wrapping up"}
        )
    finally:
        _context._current_turn.reset(token)
    assert "write access denied" in out
    assert store.end_session_calls == []


@pytest.mark.asyncio
async def test_end_session_store_raises_surfaces_error(
    store: _StubStore, turn_with_session: TurnContext
) -> None:
    store.raise_on = "end_session"
    out = await saga_ops.saga_end_session.ainvoke(
        {
            "runtime": _runtime(turn_with_session),
            "session_id": "sess-abc",
            "summary": "done",
        }
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
        {
            "runtime": _runtime(turn_with_session),
            "session_id": "sess-abc",
            "summary": "done",
        }
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


# ─── agent-curated feedback emits saga_feedback_sent (chainlink #266 slice 6) ───
# The per-turn auto-credit pass that used to emit saga_feedback_sent was
# removed; the deliberate feedback tools carry the emit now so viability
# loop 1.1 + the self-state feedback line stay alive off real curation.


@pytest.mark.asyncio
async def test_saga_feedback_emits_feedback_sent(
    store: _StubStore, turn_with_session: TurnContext, monkeypatch
) -> None:
    import mimir.event_logger as _ev

    captured: list[tuple] = []

    async def _fake_log_event(etype, **kw):
        captured.append((etype, kw))

    monkeypatch.setattr(_ev, "log_event", _fake_log_event)
    out = await saga_ops.saga_feedback.ainvoke(
        {
            "atom_id": "a" * 16,
            "signal": "useful",
            "runtime": _runtime(turn_with_session),
        }
    )
    assert "ok" in out
    sent = [e for e in captured if e[0] == "saga_feedback_sent"]
    assert sent, "saga_feedback must emit saga_feedback_sent on success"
    assert sent[0][1].get("feedback") == "positive"


@pytest.mark.asyncio
async def test_mark_contributions_emits_feedback_sent(
    store: _StubStore, turn_with_session: TurnContext, monkeypatch
) -> None:
    import mimir.event_logger as _ev

    captured: list[tuple] = []

    async def _fake_log_event(etype, **kw):
        captured.append((etype, kw))

    monkeypatch.setattr(_ev, "log_event", _fake_log_event)
    out = await saga_ops.saga_mark_contributions.ainvoke(
        {
            "atom_ids": ["a" * 16, "b" * 16],
            "response_text": "resp",
            "runtime": _runtime(turn_with_session),
        }
    )
    assert "credited 2" in out
    sent = [e for e in captured if e[0] == "saga_feedback_sent"]
    assert sent and sent[0][1].get("atom_count") == 2


@pytest.mark.asyncio
async def test_saga_feedback_no_event_on_failure(
    store: _StubStore, turn_with_session: TurnContext, monkeypatch
) -> None:
    """A failed outcome() must NOT emit saga_feedback_sent."""
    import mimir.event_logger as _ev

    captured: list[tuple] = []

    async def _fake_log_event(etype, **kw):
        captured.append((etype, kw))

    monkeypatch.setattr(_ev, "log_event", _fake_log_event)
    store.raise_on = "outcome"
    out = await saga_ops.saga_feedback.ainvoke(
        {"atom_id": "a" * 16, "signal": "useful"}
    )
    assert "failed" in out
    assert not any(e[0] == "saga_feedback_sent" for e in captured)


@pytest.mark.asyncio
async def test_saga_feedback_stale_emits_negative(
    store: _StubStore, turn_with_session: TurnContext, monkeypatch
) -> None:
    """#268: the stale signal (→ negative wire) must emit
    saga_feedback_sent with feedback=negative, not just the useful path."""
    import mimir.event_logger as _ev

    captured: list[tuple] = []

    async def _fake_log_event(etype, **kw):
        captured.append((etype, kw))

    monkeypatch.setattr(_ev, "log_event", _fake_log_event)
    out = await saga_ops.saga_feedback.ainvoke(
        {"atom_id": "a" * 16, "signal": "stale", "runtime": _runtime(turn_with_session)}
    )
    assert "ok" in out and "negative" in out
    sent = [e for e in captured if e[0] == "saga_feedback_sent"]
    assert sent and sent[0][1].get("feedback") == "negative"


# ────────────────────────────────────────────────────────────────────
# saga_record_skill_learning authorization tests
# ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_skill_learning_without_runtime_fails_closed(
    store: _StubStore,
) -> None:
    """Without a server-provided runtime (AuthContext), the write must be denied."""
    out = await saga_ops.saga_record_skill_learning.ainvoke(
        {"skill": "test-skill", "kind": "tip", "content": "Test learning"}
    )
    assert "write access denied" in out
    assert store.store_calls == []


@pytest.mark.asyncio
async def test_record_skill_learning_non_admin_non_service_denied(
    store: _StubStore,
) -> None:
    """A regular user without admin or trusted-service role must be denied."""
    user_auth = AuthContext(
        principal="regular-user",
        canonical_principal="regular-user",
        roles=(),
        event_ingress="test",
        trigger="user_message",
        channel_id="test-channel",
        interactivity=None,
    )
    user_ctx = TurnContext(
        turn_id="turn-1",
        session_id="sess-1",
        trigger="user_message",
        channel_id="test-channel",
        started_at="2024-01-01T00:00:00Z",
        agent_id="agent-1",
        saga_session_id="sess-1",
        auth_context=user_auth,
        ifc_labels=None,
    )
    token = set_current_turn(user_ctx)
    try:
        out = await saga_ops.saga_record_skill_learning.ainvoke(
            {
                "skill": "test-skill",
                "kind": "tip",
                "content": "Test learning",
                "runtime": _runtime(user_ctx),
            }
        )
    finally:
        reset_current_turn(token)
    assert "write access denied" in out
    assert store.store_calls == []


@pytest.mark.asyncio
async def test_record_skill_learning_admin_allowed(
    store: _StubStore, turn_with_session: TurnContext
) -> None:
    """An admin user must be allowed to write skill learnings."""
    out = await saga_ops.saga_record_skill_learning.ainvoke(
        {
            "skill": "test-skill",
            "kind": "tip",
            "content": "Test learning",
            "runtime": _runtime(turn_with_session),
        }
    )
    assert "ok" in out.lower()
    assert len(store.store_calls) == 1
    call = store.store_calls[0]
    assert call["content"] == "Test learning"
    assert call["source_type"] == "skill_learning"
    assert call["visibility"] == "private"
