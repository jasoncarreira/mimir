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

import subprocess
import sys
import textwrap
import time
from unittest.mock import AsyncMock, Mock

import pytest

from mimir._context import reset_current_turn, set_current_turn
from mimir import _context
from langchain.tools import ToolRuntime

from mimir.models import (
    AuthContext,
    InformationFlowLabels,
    InformationFlowState,
    Integrity,
    SourceLabel,
    TurnContext,
)
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
        auth_context=None,
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
        integrity=None,
        origin_trigger=None,
        origin_ref=None,
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
                "integrity": integrity,
                "origin_trigger": origin_trigger,
                "origin_ref": origin_ref,
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
    labels = InformationFlowLabels(sources=(SourceLabel(
        principal="test-user",
        domain="channel",
        resource_id="ch-1",
        bridge_instance="test",
        sensitivity="private",
        authorized_principals=frozenset({"test-user"}),
        integrity=Integrity.TRUSTED,
    ),))
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
        saga_session_id="sess-abc",
        ifc_labels=labels,
        ifc_state=InformationFlowState(labels=labels),
    )
    ctx = TurnContext(
        turn_id="t-1",
        session_id="ch-1",
        trigger="user_message",
        channel_id="ch-1",
        started_at=time.monotonic(),
        saga_session_id="sess-abc",
        auth_context=auth_ctx,
        ifc_labels=labels,
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

    @pytest.mark.parametrize("active_count", [0, 1, 2], ids=["none", "single", "ambiguous"])
    def test_none_resolves_without_contextvar_in_owned_process(
        self, active_count: int
    ) -> None:
        # The single-active heuristic reads the whole process registry. A turn
        # registered by this test is not necessarily the only turn in a full
        # suite worker. Exercise the real registration/resolution chain in a
        # child rather than clearing globals or guessing among multiple turns.
        script = textwrap.dedent("""
            import sys
            import time
            from mimir import _context
            from mimir.models import TurnContext
            from mimir.tools.saga_ops import _resolve_session_id

            count = int(sys.argv[1])
            tokens = []
            try:
                for index in range(count):
                    ctx = TurnContext(
                        turn_id=f"turn-{index}",
                        session_id=f"channel-{index}",
                        trigger="user_message",
                        channel_id=f"channel-{index}",
                        started_at=time.monotonic(),
                        saga_session_id=f"sess-{index}",
                    )
                    tokens.append(_context.set_current_turn(ctx))
                assert len(_context._active_turns) == count
                token = _context._current_turn.set(None)
                try:
                    expected = "sess-0" if count == 1 else None
                    assert _resolve_session_id(None) == expected
                finally:
                    _context._current_turn.reset(token)
            finally:
                for token in reversed(tokens):
                    _context.reset_current_turn(token)
            assert not _context._active_turns
        """)
        result = subprocess.run(
            [sys.executable, "-c", script, str(active_count)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr


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


def test_end_session_description_requires_capability_for_explicit_call() -> None:
    description = " ".join(saga_ops.saga_end_session.description.split())

    assert "has the ``saga_end_session`` capability" in description
    assert "call explicitly when" in description
    assert "session is wrapping" in description
    assert "Without that capability, do not attempt the call" in description
    assert "synthesis turn closes the session" in description


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
    assert "tainted" in out
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
    assert "tainted" in out
    assert store.end_session_calls == []


@pytest.mark.asyncio
async def test_end_session_wrong_existing_and_nonexistent_ids_have_same_denial(
    store: _StubStore, turn_with_session: TurnContext,
) -> None:
    runtime = _runtime(turn_with_session)
    denials = [
        await saga_ops.saga_end_session.ainvoke({
            "runtime": runtime, "session_id": session_id, "summary": "done",
        })
        for session_id in ("sess-existing-but-not-bound", "sess-nonexistent")
    ]

    assert denials[0] == denials[1]
    assert denials[0] == "saga_end_session failed: session write denied"
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
    assert "tainted" in out
    assert store.store_calls == []


@pytest.mark.asyncio
async def test_record_skill_learning_non_admin_non_service_denied(
    store: _StubStore,
) -> None:
    """A regular user without admin or trusted-service role must be denied."""
    labels = InformationFlowLabels(sources=(SourceLabel(
        principal="regular-user",
        domain="channel",
        resource_id="test-channel",
        bridge_instance="test",
        sensitivity="private",
        authorized_principals=frozenset({"regular-user"}),
        integrity=Integrity.TRUSTED,
    ),))
    user_auth = AuthContext(
        principal="regular-user",
        canonical_principal="regular-user",
        roles=(),
        event_ingress="test",
        trigger="user_message",
        channel_id="test-channel",
        interactivity=None,
        ifc_labels=labels,
        ifc_state=InformationFlowState(labels=labels),
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
        ifc_labels=labels,
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


@pytest.mark.asyncio
@pytest.mark.parametrize("threshold", ["contribution_threshold", "contradiction_threshold"])
@pytest.mark.parametrize("dry_run", [True, False], ids=["preview", "destructive"])
@pytest.mark.parametrize("supported", [False, True], ids=["alone", "mixed"])
async def test_forget_rejects_unsupported_in_process_thresholds(
    monkeypatch, turn_with_session, threshold, dry_run, supported,
):
    from mimir.saga.client import SagaStore

    client = Mock(spec=SagaStore)
    client.forget = AsyncMock(return_value={"tombstoned_count": 1})
    monkeypatch.setitem(_MEMORY_STATE, "client", client)
    args = {"dry_run": dry_run, threshold: 0.0}
    if supported:
        args["min_retrievals"] = 1
    result = await saga_ops.saga_forget.coroutine(
        **args, runtime=_runtime(turn_with_session),
    )
    client.forget.assert_not_awaited()
    assert result.startswith("saga_forget failed:")
    assert threshold in result
    assert "unsupported" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("in_process", [True, False])
async def test_forget_preserves_supported_backend_arguments(
    monkeypatch, turn_with_session, in_process,
):
    from mimir.saga.client import SagaStore

    client = Mock(spec=SagaStore) if in_process else _StubStore()
    client.forget = AsyncMock(return_value={"tombstoned_count": 0})
    monkeypatch.setitem(_MEMORY_STATE, "client", client)
    args = {"dry_run": False, "min_retrievals": 1, "confidence_floor": 0.0, "grace_days": 0}
    if not in_process:
        args.update(contribution_threshold=0.0, contradiction_threshold=0.0)
    result = await saga_ops.saga_forget.coroutine(
        **args, runtime=_runtime(turn_with_session),
    )
    client.forget.assert_awaited_once_with(
        **args, auth_context=turn_with_session.auth_context,
    )
    assert '"tombstoned_count": 0' in result
