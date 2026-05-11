"""Phase 2c — commitment lifecycle MCP tools.

Direct-handler tests over ``build_commitment_tools``:
``commitment_complete`` / ``commitment_snooze`` / ``commitment_dismiss``
/ ``commitment_list`` round-trip through a real ``CommitmentsStore``
on a tmp jsonl file. Same shape as
``tests/test_scheduletools.py`` — invokes ``tool.handler({...})``
synchronously via pytest-asyncio.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mimir.commitments import (
    CommitmentRecord,
    CommitmentStatus,
    CommitmentsStore,
)
from mimir.committools import build_commitment_tools, commitment_tool_names


@pytest.fixture
def store(tmp_path: Path) -> CommitmentsStore:
    return CommitmentsStore(path=tmp_path / "commitments.jsonl")


def _tools(store: CommitmentsStore) -> dict:
    return {t.name: t for t in build_commitment_tools(store)}


def _text(result: dict) -> str:
    return result["content"][0]["text"]


@pytest.mark.asyncio
async def test_complete_happy_path(store):
    rec = await store.add(CommitmentRecord(
        id="c-aaa", channel_id="ch-1", text="Do thing",
    ))
    tools = _tools(store)
    out = await tools["commitment_complete"].handler({"id": rec.id})
    assert out.get("is_error") is not True
    assert "completed c-aaa" in _text(out)
    assert store.current_state()[rec.id].status == CommitmentStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_complete_unknown_returns_error(store):
    tools = _tools(store)
    out = await tools["commitment_complete"].handler({"id": "c-missing"})
    assert out.get("is_error") is True
    assert "not found" in _text(out)


@pytest.mark.asyncio
async def test_complete_already_terminal_returns_error(store):
    rec = await store.add(CommitmentRecord(
        id="c-bbb", channel_id="ch-1", text="X",
    ))
    await store.complete(rec.id)
    tools = _tools(store)
    out = await tools["commitment_complete"].handler({"id": rec.id})
    assert out.get("is_error") is True
    assert "already" in _text(out)
    assert "completed" in _text(out)


@pytest.mark.asyncio
async def test_complete_with_message_id(store):
    rec = await store.add(CommitmentRecord(
        id="c-ccc", channel_id="ch-1", text="X",
    ))
    tools = _tools(store)
    out = await tools["commitment_complete"].handler({
        "id": rec.id, "message_id": "msg-9999",
    })
    assert out.get("is_error") is not True
    assert store.current_state()[rec.id].completion_message_id == "msg-9999"


@pytest.mark.asyncio
async def test_snooze_for_days_happy_path(store):
    rec = await store.add(CommitmentRecord(
        id="c-ddd", channel_id="ch-1", text="X",
    ))
    tools = _tools(store)
    out = await tools["commitment_snooze"].handler({
        "id": rec.id, "for_days": 7,
    })
    assert out.get("is_error") is not True
    state = store.current_state()[rec.id]
    assert state.status == CommitmentStatus.SNOOZED.value
    assert state.snooze_count == 1


@pytest.mark.asyncio
async def test_snooze_until_unix_happy_path(store):
    rec = await store.add(CommitmentRecord(
        id="c-eee", channel_id="ch-1", text="X",
    ))
    target = 1_800_000_000.0
    tools = _tools(store)
    out = await tools["commitment_snooze"].handler({
        "id": rec.id, "until_unix": target,
    })
    assert out.get("is_error") is not True
    state = store.current_state()[rec.id]
    assert state.snoozed_until_unix == target
    # snooze() slides due_window_start to the new target
    assert state.due_window_start_unix == target


@pytest.mark.asyncio
async def test_snooze_requires_exactly_one_of_for_days_or_until_unix(store):
    rec = await store.add(CommitmentRecord(
        id="c-fff", channel_id="ch-1", text="X",
    ))
    tools = _tools(store)

    # Neither
    out_none = await tools["commitment_snooze"].handler({"id": rec.id})
    assert out_none.get("is_error") is True
    assert "exactly one" in _text(out_none)

    # Both
    out_both = await tools["commitment_snooze"].handler({
        "id": rec.id, "for_days": 1, "until_unix": 1_800_000_000.0,
    })
    assert out_both.get("is_error") is True
    assert "exactly one" in _text(out_both)


@pytest.mark.asyncio
async def test_snooze_stores_reason(store):
    rec = await store.add(CommitmentRecord(
        id="c-ggg", channel_id="ch-1", text="X",
    ))
    tools = _tools(store)
    await tools["commitment_snooze"].handler({
        "id": rec.id, "for_days": 3, "reason": "blocked on review",
    })
    assert store.current_state()[rec.id].snooze_reason == "blocked on review"


@pytest.mark.asyncio
async def test_dismiss_happy_path(store):
    rec = await store.add(CommitmentRecord(
        id="c-hhh", channel_id="ch-1", text="X",
    ))
    tools = _tools(store)
    out = await tools["commitment_dismiss"].handler({
        "id": rec.id, "reason": "no longer relevant",
    })
    assert out.get("is_error") is not True
    state = store.current_state()[rec.id]
    assert state.status == CommitmentStatus.DISMISSED.value
    assert state.dismiss_reason == "no longer relevant"


@pytest.mark.asyncio
async def test_dismiss_already_terminal_returns_error(store):
    rec = await store.add(CommitmentRecord(
        id="c-iii", channel_id="ch-1", text="X",
    ))
    await store.dismiss(rec.id)
    tools = _tools(store)
    out = await tools["commitment_dismiss"].handler({"id": rec.id})
    assert out.get("is_error") is True
    assert "already" in _text(out)


@pytest.mark.asyncio
async def test_list_returns_active_by_default(store):
    p = await store.add(CommitmentRecord(
        id="c-p1", channel_id="ch-1", text="pending",
    ))
    s = await store.add(CommitmentRecord(
        id="c-s1", channel_id="ch-1", text="snoozed",
    ))
    await store.snooze(s.id, until_unix=2_000_000_000.0)
    done = await store.add(CommitmentRecord(
        id="c-d1", channel_id="ch-1", text="done",
    ))
    await store.complete(done.id)

    tools = _tools(store)
    out = await tools["commitment_list"].handler({})
    rows = json.loads(_text(out))
    ids = {r["id"] for r in rows}
    assert "c-p1" in ids
    assert "c-s1" in ids
    assert "c-d1" not in ids  # completed excluded by default


@pytest.mark.asyncio
async def test_list_status_filter(store):
    p = await store.add(CommitmentRecord(
        id="c-p2", channel_id="ch-1", text="pending",
    ))
    done = await store.add(CommitmentRecord(
        id="c-d2", channel_id="ch-1", text="done",
    ))
    await store.complete(done.id)
    tools = _tools(store)

    out_done = await tools["commitment_list"].handler({"status": "completed"})
    rows = json.loads(_text(out_done))
    assert len(rows) == 1
    assert rows[0]["id"] == "c-d2"


@pytest.mark.asyncio
async def test_list_channel_filter_with_unbound(store):
    await store.add(CommitmentRecord(
        id="c-bound", channel_id="ch-A", text="A",
    ))
    await store.add(CommitmentRecord(
        id="c-other", channel_id="ch-B", text="B",
    ))
    await store.add(CommitmentRecord(
        id="c-unbound", channel_id=None, text="U",
    ))

    tools = _tools(store)
    out = await tools["commitment_list"].handler({"channel_id": "ch-A"})
    rows = json.loads(_text(out))
    ids = {r["id"] for r in rows}
    # Bound to channel A → included; unbound → included; B → excluded
    assert ids == {"c-bound", "c-unbound"}


@pytest.mark.asyncio
async def test_list_payload_fields(store):
    rec = await store.add(CommitmentRecord(
        id="c-payload", channel_id="ch-1", text="check it",
        recipient_identity="alice", due_window_hint="Thursday",
        due_window_start_unix=1_800_000_000.0,
    ))
    tools = _tools(store)
    out = await tools["commitment_list"].handler({})
    rows = json.loads(_text(out))
    row = next(r for r in rows if r["id"] == "c-payload")
    assert row["recipient_identity"] == "alice"
    assert row["due_window_hint"] == "Thursday"
    assert row["due_window_start_unix"] == 1_800_000_000.0
    assert row["status"] == "pending"


def test_tool_names_match_builder_output(store):
    """``commitment_tool_names`` must list every tool that
    ``build_commitment_tools`` actually registers — drift between them
    means allowed_tools doesn't allow what the server exposes."""
    declared = set(commitment_tool_names())
    built = {
        f"mcp__mimir__{t.name}" for t in build_commitment_tools(store)
    }
    assert declared == built
