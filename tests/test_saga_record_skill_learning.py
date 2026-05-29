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

import pytest

from mimir._context import reset_current_turn, set_current_turn
from mimir.models import TurnContext
from mimir.skill_memory import SKILL_LEARNING_SOURCE_TYPE
from mimir.tools.memory import _MEMORY_STATE
from mimir.tools.saga_ops import saga_record_skill_learning


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
    )
    token = set_current_turn(ctx)
    yield ctx
    reset_current_turn(token)


async def _call(**kwargs):
    return await saga_record_skill_learning.ainvoke(kwargs)


@pytest.mark.asyncio
async def test_records_learning_with_validated_metadata(store, turn_with_session):
    msg = await _call(
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
async def test_positive_kind_also_records(store, turn_with_session):
    msg = await _call(skill="github-poller", kind="tip", content="x")
    assert "ok" in msg
    assert store.calls[0]["metadata"] == {"skill": "github-poller", "kind": "tip"}


@pytest.mark.asyncio
async def test_rejects_unknown_kind_without_writing(store):
    msg = await _call(skill="memory", kind="gotcha", content="x")
    assert "failed" in msg and "unknown skill-learning kind" in msg
    assert store.calls == []  # convention guarded BEFORE any write


@pytest.mark.asyncio
async def test_rejects_empty_skill_without_writing(store):
    msg = await _call(skill="   ", kind="tip", content="x")
    assert "failed" in msg
    assert store.calls == []


@pytest.mark.asyncio
async def test_rejects_empty_content_without_writing(store):
    msg = await _call(skill="memory", kind="tip", content="   ")
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
    await _call(skill="memory", kind="tip", content="x", session_id="override-1")
    assert store.calls[0]["session_id"] == "override-1"


@pytest.mark.asyncio
async def test_dedup_hit_reports_already_present(turn_with_session):
    stub = _StubStore(stored=False)
    prev = _MEMORY_STATE.get("client")
    _MEMORY_STATE["client"] = stub
    try:
        msg = await _call(skill="memory", kind="tip", content="dup")
        assert "already present" in msg
    finally:
        _MEMORY_STATE["client"] = prev


@pytest.mark.asyncio
async def test_store_exception_surfaced(turn_with_session):
    stub = _StubStore(raise_=True)
    prev = _MEMORY_STATE.get("client")
    _MEMORY_STATE["client"] = stub
    try:
        msg = await _call(skill="memory", kind="tip", content="x")
        assert "failed" in msg and "store boom" in msg
    finally:
        _MEMORY_STATE["client"] = prev
