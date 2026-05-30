"""Tests for SubconsciousQueryHook (mimir/hooks/subconscious.py).

Coverage (8 async tests):
1. test_env_gate_off — MIMIR_SUBCONSCIOUS_QUERY=false suppresses everything
2. test_trigger_guard_scheduled_tick — synthetic trigger skipped
3. test_trigger_guard_poller — poller trigger skipped
4. test_query_fires_on_user_message — normal path: saga called, block written
5. test_dedup_filters_already_seen_ids — all returned IDs already in ctx → None
6. test_exception_isolation — RuntimeError from saga doesn't propagate
7. test_empty_result_no_block — empty saga payload → ctx.subconscious_block is None
8. test_nonempty_result_block_written — atoms returned, no prior IDs → block is non-empty string
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest

from mimir.hooks import SubconsciousQueryHook


# ── Minimal fakes ────────────────────────────────────────────────────


@dataclass
class _FakeTurnContext:
    turn_id: str = "test-turn-1"
    saga_atom_ids: list[str] = field(default_factory=list)
    subconscious_block: str | None = None


@dataclass
class _FakeAgentEvent:
    trigger: str = "user_message"
    content: str = "What did we discuss last week?"


def _make_payload(*atom_ids: str) -> dict[str, Any]:
    """Return a minimal real-shaped saga payload with the given atom IDs."""
    atoms = [
        {
            "id": aid,
            "content": f"remembered content for {aid}",
            "stream": "episodic",
            "score": 0.9,
        }
        for aid in atom_ids
    ]
    return {"atoms": atoms}


def _empty_payload() -> dict[str, Any]:
    return {"atoms": []}


# ── Tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_env_gate_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """MIMIR_SUBCONSCIOUS_QUERY=false → saga never called, block stays None."""
    monkeypatch.setenv("MIMIR_SUBCONSCIOUS_QUERY", "false")

    saga = AsyncMock()
    hook = SubconsciousQueryHook(saga)
    ctx = _FakeTurnContext()
    event = _FakeAgentEvent()

    await hook.pre_query(ctx, event)

    saga.query.assert_not_called()
    assert ctx.subconscious_block is None


@pytest.mark.asyncio
async def test_trigger_guard_scheduled_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """scheduled_tick trigger → saga never called, block stays None."""
    monkeypatch.delenv("MIMIR_SUBCONSCIOUS_QUERY", raising=False)

    saga = AsyncMock()
    hook = SubconsciousQueryHook(saga)
    ctx = _FakeTurnContext()
    event = _FakeAgentEvent(trigger="scheduled_tick")

    await hook.pre_query(ctx, event)

    saga.query.assert_not_called()
    assert ctx.subconscious_block is None


@pytest.mark.asyncio
async def test_trigger_guard_poller(monkeypatch: pytest.MonkeyPatch) -> None:
    """poller trigger → saga never called, block stays None."""
    monkeypatch.delenv("MIMIR_SUBCONSCIOUS_QUERY", raising=False)

    saga = AsyncMock()
    hook = SubconsciousQueryHook(saga)
    ctx = _FakeTurnContext()
    event = _FakeAgentEvent(trigger="poller")

    await hook.pre_query(ctx, event)

    saga.query.assert_not_called()
    assert ctx.subconscious_block is None


@pytest.mark.asyncio
async def test_query_fires_on_user_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """user_message trigger → saga.query called with framed query; block non-None."""
    monkeypatch.delenv("MIMIR_SUBCONSCIOUS_QUERY", raising=False)

    payload = _make_payload("atom-1", "atom-2")
    saga = AsyncMock()
    saga.query = AsyncMock(return_value=payload)

    hook = SubconsciousQueryHook(saga)
    ctx = _FakeTurnContext()
    content = "What did we discuss last week?"
    event = _FakeAgentEvent(trigger="user_message", content=content)

    await hook.pre_query(ctx, event)

    expected_query = "Background context and relevant history: " + content
    saga.query.assert_called_once_with(expected_query, top_k=5)
    assert ctx.subconscious_block is not None


@pytest.mark.asyncio
async def test_dedup_filters_already_seen_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """All returned atom IDs already in ctx.saga_atom_ids → block stays None."""
    monkeypatch.delenv("MIMIR_SUBCONSCIOUS_QUERY", raising=False)

    atom_ids = ["atom-a", "atom-b"]
    payload = _make_payload(*atom_ids)
    saga = AsyncMock()
    saga.query = AsyncMock(return_value=payload)

    hook = SubconsciousQueryHook(saga)
    ctx = _FakeTurnContext(saga_atom_ids=list(atom_ids))
    event = _FakeAgentEvent(trigger="user_message")

    await hook.pre_query(ctx, event)

    # saga was called (dedup happens after retrieval, not before)
    saga.query.assert_called_once()
    # block suppressed because all IDs are dupes
    assert ctx.subconscious_block is None


@pytest.mark.asyncio
async def test_exception_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """RuntimeError from saga.query does not propagate; block stays None."""
    monkeypatch.delenv("MIMIR_SUBCONSCIOUS_QUERY", raising=False)

    saga = AsyncMock()
    saga.query = AsyncMock(side_effect=RuntimeError("saga exploded"))

    hook = SubconsciousQueryHook(saga)
    ctx = _FakeTurnContext()
    event = _FakeAgentEvent(trigger="user_message")

    # Must not raise
    await hook.pre_query(ctx, event)

    assert ctx.subconscious_block is None


@pytest.mark.asyncio
async def test_empty_result_no_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty saga payload (no atoms) → ctx.subconscious_block stays None."""
    monkeypatch.delenv("MIMIR_SUBCONSCIOUS_QUERY", raising=False)

    payload = _empty_payload()
    saga = AsyncMock()
    saga.query = AsyncMock(return_value=payload)

    hook = SubconsciousQueryHook(saga)
    ctx = _FakeTurnContext()
    event = _FakeAgentEvent(trigger="user_message")

    await hook.pre_query(ctx, event)

    saga.query.assert_called_once()
    assert ctx.subconscious_block is None


@pytest.mark.asyncio
async def test_nonempty_result_block_written(monkeypatch: pytest.MonkeyPatch) -> None:
    """Atoms returned, no prior saga_atom_ids → subconscious_block is non-empty string."""
    monkeypatch.delenv("MIMIR_SUBCONSCIOUS_QUERY", raising=False)

    payload = _make_payload("atom-x", "atom-y")
    saga = AsyncMock()
    saga.query = AsyncMock(return_value=payload)

    hook = SubconsciousQueryHook(saga)
    ctx = _FakeTurnContext(saga_atom_ids=[])
    event = _FakeAgentEvent(trigger="user_message")

    await hook.pre_query(ctx, event)

    assert isinstance(ctx.subconscious_block, str)
    assert len(ctx.subconscious_block) > 0
