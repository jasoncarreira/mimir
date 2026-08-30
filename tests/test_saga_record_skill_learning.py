"""Tests for the saga_record_skill_learning tool (chainlink #266, slice 5).

The write counterpart to skill-memory recall: the agent calls this (per
the synthesis-turn prompt, or mid-turn when a skill misleads it) to
persist a per-skill learning. Coverage:
  - happy path: validated metadata + skill_learning source_type +
    procedural stream + content stripped + session_id from ctx
  - valence is a CLOSED enum: unknown kind / empty skill rejected with
    NO write attempted (build_metadata guards the convention)
  - empty content rejected
  - no-client / dedup-hit / store-exception surface as strings
  - explicit session_id override
"""
from __future__ import annotations

import time
from dataclasses import replace

import pytest

from langchain.tools import ToolRuntime

from mimir._context import reset_current_turn, set_current_turn
from mimir.models import (
    AuthContext,
    InformationFlowLabels,
    InformationFlowState,
    Integrity,
    SourceLabel,
    TurnContext,
)
from mimir.skill_memory import SKILL_LEARNING_SOURCE_TYPE
from mimir.tools.memory import _MEMORY_STATE
from mimir.tools.saga_ops import saga_record_skill_learning


TRUSTED_LABELS = InformationFlowLabels().with_source(SourceLabel(
    principal="test-admin", domain="channel", resource_id="test-channel",
    bridge_instance="test", sensitivity="private",
    authorized_principals=frozenset({"test-admin"}),
    integrity=Integrity.TRUSTED,
))


ADMIN_AUTH = AuthContext(
    principal="test-admin",
    canonical_principal="test-admin",
    roles=("admin",),
    event_ingress="test",
    trigger="user_message",
    channel_id="test-channel",
    interactivity=None,
    ifc_labels=TRUSTED_LABELS,
    ifc_state=InformationFlowState(labels=TRUSTED_LABELS),
)


class _StubStore:
    def __init__(self, *, stored: bool = True, raise_: bool = False) -> None:
        self.calls: list[dict] = []
        self._stored = stored
        self._raise = raise_

    async def store(
        self, content, *, stream=None, source_type="api",
        metadata=None, session_id=None, **kwargs,
    ):
        if self._raise:
            raise RuntimeError("store boom")
        self.calls.append({
            "content": content, "stream": stream,
            "source_type": source_type, "metadata": metadata,
            "session_id": session_id,
            **kwargs,
        })
        return {"stored": self._stored, "atom_id": "atom-xyz"}


@pytest.fixture
def store():
    stub = _StubStore()
    prev = _MEMORY_STATE.get("client")
    _MEMORY_STATE["client"] = stub
    yield stub
    _MEMORY_STATE["client"] = prev


@pytest.fixture
def turn_with_session():
    ctx = TurnContext(
        turn_id="t-1", session_id="ch-1", trigger="user_message",
        channel_id="ch-1", started_at=time.monotonic(),
        saga_session_id="sess-abc",
        auth_context=ADMIN_AUTH,
        ifc_labels=None,
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
        tool_call_id="saga-record-skill-learning-test",
        store=None,
    )


async def _call(turn_with_session=None, **kwargs):
    if turn_with_session is not None:
        kwargs["runtime"] = _runtime(turn_with_session)
    return await saga_record_skill_learning.ainvoke(kwargs)


@pytest.mark.asyncio
async def test_records_learning_with_validated_metadata(store, turn_with_session):
    msg = await _call(
        turn_with_session,
        skill="memory", kind="failure-mode",
        content="  circuit-breaker trips on empty input  ",
    )
    assert "ok" in msg and "atom-xyz" in msg and "memory/failure-mode" in msg
    assert len(store.calls) == 1
    call = store.calls[0]
    assert call["source_type"] == SKILL_LEARNING_SOURCE_TYPE
    assert call["metadata"] == {"skill": "memory", "kind": "failure-mode"}
    assert call["content"] == "circuit-breaker trips on empty input"  # stripped
    assert call["stream"] == "procedural"
    assert call["session_id"] == "sess-abc"  # resolved from ctx


@pytest.mark.asyncio
async def test_trusted_turn_records_trusted_learning_with_origin(
    store, turn_with_session,
):
    labels = InformationFlowLabels().with_source(SourceLabel(
        principal="test-admin", domain="channel", resource_id="message:1",
        bridge_instance="test", sensitivity="private",
        authorized_principals=frozenset({"test-admin"}),
        integrity=Integrity.TRUSTED,
    ))
    auth = replace(
        turn_with_session.auth_context,
        origin_trigger="poller:skill-review",
        origin_ref="event:123",
        ifc_labels=labels,
        ifc_state=InformationFlowState(labels=labels),
    )
    turn = replace(turn_with_session, auth_context=auth, ifc_labels=labels)

    await _call(turn, skill="memory", kind="tip", content="trusted learning")

    call = store.calls[0]
    assert call["integrity"] == Integrity.TRUSTED
    assert call["origin_trigger"] == "poller:skill-review"
    assert call["origin_ref"] == "event:123"


@pytest.mark.asyncio
async def test_turn_with_any_untrusted_source_refuses_learning_as_tainted(
    store, turn_with_session,
):
    labels = InformationFlowLabels(sources=(
        SourceLabel(
            principal="test-admin", domain="channel", resource_id="message:1",
            bridge_instance="test", sensitivity="private",
            authorized_principals=frozenset({"test-admin"}),
            integrity=Integrity.TRUSTED,
        ),
        SourceLabel(
            principal="web", domain="internet", resource_id="https://example.com",
            bridge_instance="fetch", sensitivity="public",
            authorized_principals=frozenset({"test-admin"}),
            integrity=Integrity.UNTRUSTED,
        ),
    ))
    auth = replace(
        turn_with_session.auth_context,
        ifc_labels=labels,
        ifc_state=InformationFlowState(labels=labels),
    )
    turn = replace(turn_with_session, auth_context=auth, ifc_labels=labels)

    result = await _call(
        turn, skill="memory", kind="tip", content="tainted learning",
    )

    assert "tainted" in result
    assert store.calls == []


@pytest.mark.asyncio
async def test_positive_kind_also_records(store, turn_with_session):
    msg = await _call(turn_with_session, skill="github-poller", kind="tip", content="x")
    assert "ok" in msg
    assert store.calls[0]["metadata"] == {"skill": "github-poller", "kind": "tip"}


@pytest.mark.asyncio
async def test_rejects_unknown_kind_without_writing(store, turn_with_session):
    msg = await _call(turn_with_session, skill="memory", kind="gotcha", content="x")
    assert "failed" in msg and "unknown skill-learning kind" in msg
    assert store.calls == []  # convention guarded BEFORE any write


@pytest.mark.asyncio
async def test_rejects_empty_skill_without_writing(store, turn_with_session):
    msg = await _call(turn_with_session, skill="   ", kind="tip", content="x")
    assert "failed" in msg
    assert store.calls == []


@pytest.mark.asyncio
async def test_rejects_empty_content_without_writing(store, turn_with_session):
    msg = await _call(turn_with_session, skill="memory", kind="tip", content="   ")
    assert "failed" in msg and "content is required" in msg
    assert store.calls == []


@pytest.mark.asyncio
async def test_no_client_configured():
    prev = _MEMORY_STATE.get("client")
    _MEMORY_STATE["client"] = None
    try:
        msg = await _call(skill="memory", kind="tip", content="x")
        assert "no SagaStore configured" in msg
    finally:
        _MEMORY_STATE["client"] = prev


@pytest.mark.asyncio
async def test_explicit_session_id_override(store, turn_with_session):
    await _call(turn_with_session, skill="memory", kind="tip", content="x", session_id="override-1")
    assert store.calls[0]["session_id"] == "override-1"


@pytest.mark.asyncio
async def test_dedup_hit_reports_already_present(turn_with_session):
    stub = _StubStore(stored=False)
    prev = _MEMORY_STATE.get("client")
    _MEMORY_STATE["client"] = stub
    try:
        msg = await _call(turn_with_session, skill="memory", kind="tip", content="dup")
        assert "already present" in msg
    finally:
        _MEMORY_STATE["client"] = prev


@pytest.mark.asyncio
async def test_store_exception_surfaced(turn_with_session):
    stub = _StubStore(raise_=True)
    prev = _MEMORY_STATE.get("client")
    _MEMORY_STATE["client"] = stub
    try:
        msg = await _call(turn_with_session, skill="memory", kind="tip", content="x")
        assert "failed" in msg and "store boom" in msg
    finally:
        _MEMORY_STATE["client"] = prev
