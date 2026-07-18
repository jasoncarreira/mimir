"""181-L regression: SAGA agent-callable ops as langchain @tools.

The deepagents cutover kept ``memory_query`` (≈ saga_query) and
``memory_store`` (≈ saga_store) but dropped the other four agent-
facing SAGA verbs:

  - saga_feedback           (outcome marker)
  - saga_mark_contributions (manual credit pass)
  - saga_end_session        (write boundary atom)
  - saga_forget             (intentional-forgetting engine)

Mimir flagged: the SAGA internals survive byte-identical so the
runtime can still invoke them, but the model can no longer issue
these calls explicitly. 181-L re-adds them as langchain @tools in
``mimir/tools/saga_ops.py`` routing to the same SagaStore instance
``memory_query`` uses.

Tests stub the SagaStore to capture arg-routing without touching
disk or running real LLM calls.
"""

from __future__ import annotations

import time

import pytest

from mimir._context import reset_current_turn, set_current_turn
from langchain.tools import ToolRuntime

from mimir.models import AuthContext, TurnContext
from mimir.tools import saga_ops
from mimir.tools.memory import _MEMORY_STATE


class _StubStore:
    """Minimal SagaStore stub recording arg routing for saga_feedback
    / saga_mark_contributions / saga_end_session / saga_forget."""

    def __init__(self) -> None:
        self.outcome_calls: list[dict] = []
        self.feedback_calls: list[dict] = []
        self.end_session_calls: list[dict] = []
        self.forget_calls: list[dict] = []
        self.mark_contributions_calls: list[dict] = []
        self.raise_on: str | None = None

    async def outcome(self, atom_ids, *, feedback, session_id, auth_context=None):
        if self.raise_on == "outcome":
            raise RuntimeError("outcome boom")
        self.outcome_calls.append(
            {
                "atom_ids": atom_ids,
                "feedback": feedback,
                "session_id": session_id,
            }
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
                "topics_discussed": topics_discussed,
                "decisions_made": decisions_made,
                "unfinished": unfinished,
                "emotional_state": emotional_state,
                "closed_since": closed_since,
                "channel_id": channel_id,
            }
        )
        return {
            "session_id": session_id,
            "session_summary_written": True,
        }

    async def forget(
        self,
        *,
        dry_run=True,
        min_retrievals=None,
        contribution_threshold=None,
        contradiction_threshold=None,
        confidence_floor=None,
        grace_days=None,
        auth_context=None,
    ):
        if self.raise_on == "forget":
            raise RuntimeError("forget boom")
        self.forget_calls.append(
            {
                "dry_run": dry_run,
                "min_retrievals": min_retrievals,
                "contribution_threshold": contribution_threshold,
                "contradiction_threshold": contradiction_threshold,
                "confidence_floor": confidence_floor,
                "grace_days": grace_days,
                "auth_context": auth_context,
            }
        )
        return {"dry_run": dry_run, "actions_taken": 0, "total_candidates": 7}


@pytest.fixture
def store() -> _StubStore:
    """Install a stub SagaStore on _MEMORY_STATE and yield it."""
    stub = _StubStore()
    prev = _MEMORY_STATE.get("client")
    _MEMORY_STATE["client"] = stub
    yield stub
    _MEMORY_STATE["client"] = prev


@pytest.fixture
def turn_with_session() -> TurnContext:
    """Register a TurnContext so ``session_id`` defaults to the turn's
    ``saga_session_id``."""
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


# ─── saga_feedback ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_feedback_useful_maps_to_positive(
    store: _StubStore,
    turn_with_session: TurnContext,
) -> None:
    out = await saga_ops.saga_feedback.ainvoke(
        {
            "atom_id": "atom-1",
            "signal": "useful",
            "runtime": _runtime(turn_with_session),
        }
    )
    assert "ok" in out.lower()
    assert store.outcome_calls == [
        {
            "atom_ids": ["atom-1"],
            "feedback": "positive",
            "session_id": "sess-abc",
        }
    ]


@pytest.mark.asyncio
async def test_feedback_incorrect_maps_to_negative(
    store: _StubStore,
    turn_with_session: TurnContext,
) -> None:
    await saga_ops.saga_feedback.ainvoke(
        {
            "atom_id": "atom-2",
            "signal": "incorrect",
            "runtime": _runtime(turn_with_session),
        }
    )
    assert store.outcome_calls[0]["feedback"] == "negative"


@pytest.mark.asyncio
async def test_feedback_stale_maps_to_negative(
    store: _StubStore,
    turn_with_session: TurnContext,
) -> None:
    await saga_ops.saga_feedback.ainvoke(
        {"atom_id": "atom-3", "signal": "stale", "runtime": _runtime(turn_with_session)}
    )
    assert store.outcome_calls[0]["feedback"] == "negative"


@pytest.mark.asyncio
async def test_feedback_bad_signal_returns_error(
    store: _StubStore,
    turn_with_session: TurnContext,
) -> None:
    out = await saga_ops.saga_feedback.ainvoke(
        {"atom_id": "atom-4", "signal": "ambivalent"}
    )
    assert "must be useful|incorrect|stale" in out
    assert store.outcome_calls == []


@pytest.mark.asyncio
async def test_feedback_no_store_returns_error(
    turn_with_session: TurnContext,
) -> None:
    prev = _MEMORY_STATE.get("client")
    _MEMORY_STATE["client"] = None
    try:
        out = await saga_ops.saga_feedback.ainvoke(
            {"atom_id": "atom-1", "signal": "useful"}
        )
        assert "no SagaStore configured" in out
    finally:
        _MEMORY_STATE["client"] = prev


# ─── saga_mark_contributions ──────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_contributions_routes_to_feedback(
    store: _StubStore,
    turn_with_session: TurnContext,
) -> None:
    out = await saga_ops.saga_mark_contributions.ainvoke(
        {
            "atom_ids": ["a1", "a2"],
            "response_text": "thanks",
            "runtime": _runtime(turn_with_session),
        }
    )
    assert "credited 2 atoms" in out
    assert store.mark_contributions_calls == [
        {
            "retrieved_atoms": [{"id": "a1"}, {"id": "a2"}],
            "response_text": "thanks",
            "session_id": "sess-abc",
        }
    ]


@pytest.mark.asyncio
async def test_mark_contributions_empty_list_is_a_no_op(
    store: _StubStore,
    turn_with_session: TurnContext,
) -> None:
    """An empty atom_ids list still calls mark_contributions (caller may want a
    no-op feedback ping for protocol reasons), reporting 0 credited."""
    out = await saga_ops.saga_mark_contributions.ainvoke(
        {
            "atom_ids": [],
            "response_text": "noop",
            "runtime": _runtime(turn_with_session),
        }
    )
    assert "credited 0 atoms" in out
    assert store.mark_contributions_calls[0]["retrieved_atoms"] == []


# ─── saga_end_session ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_end_session_threads_server_runtime_provenance(
    store: _StubStore,
    turn_with_session: TurnContext,
) -> None:
    out = await saga_ops.saga_end_session.ainvoke(
        {
            "runtime": _runtime(turn_with_session),
            "session_id": "sess-abc",
            "summary": "wrapped up the auth work",
            "topics_discussed": ["auth", "tokens"],
            "decisions_made": ["use JWT"],
            "unfinished": ["refresh-token rotation"],
            "emotional_state": "relieved",
            "closed_since": ["#41"],
        }
    )
    assert "ok" in out.lower()
    assert "summary_written=True" in out
    assert store.end_session_calls[0]["session_id"] == "sess-abc"
    assert store.end_session_calls[0]["topics_discussed"] == ["auth", "tokens"]
    assert store.end_session_calls[0]["channel_id"] == "ch-1"


@pytest.mark.asyncio
async def test_end_session_strips_empty_optionals(
    store: _StubStore,
    turn_with_session: TurnContext,
) -> None:
    await saga_ops.saga_end_session.ainvoke(
        {
            "runtime": _runtime(turn_with_session),
            "session_id": "sess-abc",
            "summary": "minimal close",
            "topics_discussed": [],
            "decisions_made": ["", "  "],
            "unfinished": None,
        }
    )
    call = store.end_session_calls[0]
    assert call["topics_discussed"] is None
    assert call["decisions_made"] is None
    assert call["unfinished"] is None


@pytest.mark.asyncio
async def test_end_session_requires_summary(
    store: _StubStore,
    turn_with_session: TurnContext,
) -> None:
    out = await saga_ops.saga_end_session.ainvoke(
        {
            "runtime": _runtime(turn_with_session),
            "session_id": "sess-abc",
            "summary": "",
        }
    )
    assert "summary is required" in out
    assert store.end_session_calls == []


# ─── saga_forget ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_forget_defaults_to_dry_run(
    store: _StubStore,
    turn_with_session: TurnContext,
) -> None:
    out = await saga_ops.saga_forget.ainvoke({"runtime": _runtime(turn_with_session)})
    assert store.forget_calls == [
        {
            "dry_run": True,
            "min_retrievals": None,
            "contribution_threshold": None,
            "contradiction_threshold": None,
            "confidence_floor": None,
            "grace_days": None,
            "auth_context": turn_with_session.auth_context,
        }
    ]
    # Payload comes back as JSON.
    import json

    parsed = json.loads(out)
    assert parsed["dry_run"] is True


@pytest.mark.asyncio
async def test_forget_threads_optional_args(
    store: _StubStore,
    turn_with_session: TurnContext,
) -> None:
    await saga_ops.saga_forget.ainvoke(
        {
            "dry_run": False,
            "min_retrievals": 3,
            "contribution_threshold": -0.5,
            "confidence_floor": 0.2,
            "grace_days": 30,
            "runtime": _runtime(turn_with_session),
        }
    )
    call = store.forget_calls[0]
    assert call["dry_run"] is False
    assert call["min_retrievals"] == 3
    assert call["confidence_floor"] == 0.2
    assert call["grace_days"] == 30


@pytest.mark.asyncio
async def test_forget_raise_surfaces_in_message(
    store: _StubStore,
    turn_with_session: TurnContext,
) -> None:
    store.raise_on = "forget"
    out = await saga_ops.saga_forget.ainvoke(
        {"dry_run": True, "runtime": _runtime(turn_with_session)}
    )
    assert "saga_forget failed" in out
    assert "boom" in out


# ─── Registry inclusion ──────────────────────────────────────────


def test_all_mimir_tools_includes_saga_ops_quartet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The four SAGA ops are unconditional members of all_mimir_tools()."""
    from mimir.tools import all_mimir_tools

    monkeypatch.setenv("MIMIR_MODEL_SPEC", "claude-code:foo")
    names = {t.name for t in all_mimir_tools()}
    assert {
        "saga_feedback",
        "saga_mark_contributions",
        "saga_end_session",
        "saga_forget",
    }.issubset(names)
