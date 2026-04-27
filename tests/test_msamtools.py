"""MSAM MCP tools (SPEC §8.2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mimir import _context
from mimir.models import TurnContext, make_turn_id
from mimir.msamtools import build_msam_tools

from ._fake_msam import FakeMsam


def _by_name(tools, name):
    for t in tools:
        if t.name == name:
            return t
    raise AssertionError(f"tool {name!r} not registered")


def _ctx(channel_id: str = "c1", msam_session_id: str = "msam-c1-1") -> TurnContext:
    return TurnContext(
        turn_id=make_turn_id(),
        session_id=channel_id,
        trigger="user_message",
        channel_id=channel_id,
        started_at=0.0,
        msam_session_id=msam_session_id,
    )


@pytest.mark.asyncio
async def test_msam_query_passes_session_id_and_appends_atom_ids():
    fake = FakeMsam(
        query_response={
            "_raw_atoms": [
                {"id": "a1", "stream": "semantic", "content": "alpha"},
                {"id": "a2", "stream": "episodic", "content": "beta"},
            ]
        }
    )
    tools = build_msam_tools(fake)  # type: ignore[arg-type]
    msam_query = _by_name(tools, "msam_query")

    ctx = _ctx()
    token = _context.set_current_turn(ctx)
    try:
        out = await msam_query.handler({"query": "anything", "top_k": 5})
    finally:
        _context.reset_current_turn(token)

    assert out.get("is_error") is not True
    assert ctx.msam_atom_ids == ["a1", "a2"]
    payload = fake.last("query")
    assert payload["session_id"] == "msam-c1-1"
    assert payload["top_k"] == 5


@pytest.mark.asyncio
async def test_msam_query_extracts_from_live_atoms_key():
    """Real MSAM (msam-hindsight-ideas server.py:api_query) returns atoms
    under the ``atoms`` key, not ``_raw_atoms``. Regression for the bug
    where contributions never marked because the extractor only looked
    at the legacy/never-shipped key."""
    fake = FakeMsam(
        query_response={
            "atoms": [
                {"id": "live-1", "stream": "semantic", "content": "x"},
                {"id": "live-2", "stream": "semantic", "content": "y"},
            ]
        }
    )
    tools = build_msam_tools(fake)  # type: ignore[arg-type]
    msam_query = _by_name(tools, "msam_query")

    ctx = _ctx()
    token = _context.set_current_turn(ctx)
    try:
        await msam_query.handler({"query": "hi"})
    finally:
        _context.reset_current_turn(token)

    assert ctx.msam_atom_ids == ["live-1", "live-2"]


@pytest.mark.asyncio
async def test_msam_query_extracts_from_two_tier_observations_and_raws():
    """When two_tier_enabled = true, MSAM returns observations and raws as
    separate lists (msam-hindsight-ideas core.py:_two_tier_split). Both
    contribute atom IDs to the contribution-tracking set, with observations
    surfacing first since they're the higher-level consolidated atoms."""
    fake = FakeMsam(
        query_response={
            "observations": [
                {"id": "obs-1", "memory_type": "observation",
                 "stream": "semantic", "content": "synthesized inference"},
            ],
            "raws": [
                {"id": "raw-1", "memory_type": "raw",
                 "stream": "semantic", "content": "evidence atom 1"},
                {"id": "raw-2", "memory_type": "raw",
                 "stream": "semantic", "content": "evidence atom 2"},
            ],
        }
    )
    tools = build_msam_tools(fake)  # type: ignore[arg-type]
    msam_query = _by_name(tools, "msam_query")

    ctx = _ctx()
    token = _context.set_current_turn(ctx)
    try:
        result = await msam_query.handler({"query": "hi"})
    finally:
        _context.reset_current_turn(token)

    # Observations come before raws, both feed the contribution-tracking set.
    assert ctx.msam_atom_ids == ["obs-1", "raw-1", "raw-2"]
    # The rendered hits list reflects memory_type so the agent can tell
    # observations apart from raw evidence.
    text = result["content"][0]["text"]
    assert "obs-1" in text  # atom id
    assert "observation" in text  # the memory_type label leaked through


@pytest.mark.asyncio
async def test_msam_query_renders_per_atom_confidence_tier():
    """Per-atom confidence_tier (post MSAM commit with per-atom gating)
    surfaces in both the slim hits dict and downstream label rendering, so
    the agent can prefer observation/high atoms over raw/low ones."""
    from mimir.msamtools import _atom_label, _hits_summary
    payload = {
        "two_tier": True,
        "observations": [
            {"id": "obs-h", "memory_type": "observation",
             "confidence_tier": "high", "similarity": 0.7,
             "score": 0.05, "evidence_count": 4, "stream": "semantic",
             "content": "high-confidence observation"},
        ],
        "raws": [
            {"id": "raw-m", "memory_type": "raw",
             "confidence_tier": "medium", "similarity": 0.32,
             "score": 0.04, "stream": "semantic",
             "content": "medium raw"},
            {"id": "raw-l", "memory_type": "raw",
             "confidence_tier": "low", "similarity": 0.18,
             "score": 0.02, "stream": "semantic",
             "content": "low raw"},
        ],
    }
    hits = _hits_summary(payload)
    assert [h["atom_id"] for h in hits] == ["obs-h", "raw-m", "raw-l"]
    assert hits[0]["memory_type"] == "observation"
    assert hits[0]["confidence_tier"] == "high"
    assert hits[0]["evidence_count"] == 4
    assert hits[1]["confidence_tier"] == "medium"
    assert hits[2]["confidence_tier"] == "low"

    assert _atom_label(payload["observations"][0]) == "observation/high"
    assert _atom_label(payload["raws"][0]) == "semantic/medium"
    assert _atom_label(payload["raws"][1]) == "semantic/low"
    # Legacy single-tier (no tier field) — fall back to base label.
    assert _atom_label({"stream": "semantic"}) == "semantic"


@pytest.mark.asyncio
async def test_msam_client_passes_min_confidence_tier_when_set():
    """MsamClient.query forwards min_confidence_tier into the request body
    only when explicitly set; omitting it lets MSAM use its config default."""
    from mimir.msam_client import MsamClient
    captured = {}

    class _StubResp:
        status = 200
        async def text(self): return "{}"
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class _StubSess:
        @property
        def closed(self): return False
        async def close(self): pass
        def post(self, url, json=None):
            captured["url"] = url
            captured["body"] = json
            return _StubResp()

    client = MsamClient("http://stub:3002")
    client._session = _StubSess()  # type: ignore[assignment]

    await client.query("q1", top_k=5)
    assert "min_confidence_tier" not in captured["body"]

    await client.query("q2", top_k=5, min_confidence_tier="medium")
    assert captured["body"]["min_confidence_tier"] == "medium"


@pytest.mark.asyncio
async def test_msam_query_handles_msam_error_gracefully():
    fake = FakeMsam(fail_on={"query"})
    tools = build_msam_tools(fake)  # type: ignore[arg-type]
    msam_query = _by_name(tools, "msam_query")
    ctx = _ctx()
    token = _context.set_current_turn(ctx)
    try:
        out = await msam_query.handler({"query": "x"})
    finally:
        _context.reset_current_turn(token)
    assert out.get("is_error") is True
    assert ctx.msam_atom_ids == []


@pytest.mark.asyncio
async def test_msam_feedback_maps_signal_to_outcome_vocab():
    fake = FakeMsam()
    tools = build_msam_tools(fake)  # type: ignore[arg-type]
    msam_feedback = _by_name(tools, "msam_feedback")

    ctx = _ctx()
    token = _context.set_current_turn(ctx)
    try:
        out = await msam_feedback.handler({"atom_id": "a1", "signal": "useful"})
    finally:
        _context.reset_current_turn(token)
    assert out.get("is_error") is not True

    payload = fake.last("outcome")
    assert payload["atom_ids"] == ["a1"]
    assert payload["feedback"] == "positive"
    assert payload["session_id"] == "msam-c1-1"


@pytest.mark.asyncio
async def test_msam_feedback_rejects_unknown_signal():
    fake = FakeMsam()
    tools = build_msam_tools(fake)  # type: ignore[arg-type]
    msam_feedback = _by_name(tools, "msam_feedback")
    out = await msam_feedback.handler({"atom_id": "a1", "signal": "fancy"})
    assert out.get("is_error") is True


@pytest.mark.asyncio
async def test_msam_mark_contributions_passes_session_id():
    fake = FakeMsam()
    tools = build_msam_tools(fake)  # type: ignore[arg-type]
    mark = _by_name(tools, "msam_mark_contributions")

    ctx = _ctx()
    token = _context.set_current_turn(ctx)
    try:
        out = await mark.handler({
            "atom_ids": ["a1", "a2"],
            "response_text": "ok",
        })
    finally:
        _context.reset_current_turn(token)
    assert out.get("is_error") is not True
    payload = fake.last("feedback")
    assert payload["atom_ids"] == ["a1", "a2"]
    assert payload["session_id"] == "msam-c1-1"


@pytest.mark.asyncio
async def test_msam_end_session_drops_empty_optionals():
    fake = FakeMsam()
    tools = build_msam_tools(fake)  # type: ignore[arg-type]
    end = _by_name(tools, "msam_end_session")

    out = await end.handler({
        "session_id": "msam-c1-1",
        "summary": "we discussed quantum",
        "topics_discussed": ["quantum"],
        "decisions_made": [],
        "unfinished": [],
        "emotional_state": "",
    })
    assert out.get("is_error") is not True
    payload = fake.last("end_session")
    assert payload["topics_discussed"] == ["quantum"]
    # Empty lists/strings drop on the wire.
    assert payload["decisions_made"] is None
    assert payload["unfinished"] is None
    assert payload["emotional_state"] is None


@pytest.mark.asyncio
async def test_msam_store_passes_through():
    fake = FakeMsam()
    tools = build_msam_tools(fake)  # type: ignore[arg-type]
    store = _by_name(tools, "msam_store")
    out = await store.handler({"content": "x", "stream": "semantic"})
    assert out.get("is_error") is not True
    payload = fake.last("store")
    assert payload["content"] == "x"
    assert payload["stream"] == "semantic"


@pytest.mark.asyncio
async def test_subagent_isolation_does_not_leak_atom_ids():
    """SPEC §9.3: subagents run in distinct asyncio tasks. ContextVars copy
    the parent's value at task creation, so when the *child* mutates its own
    TurnContext, the parent's list stays clean.

    We approximate the subagent boundary with ``asyncio.create_task`` — the
    child task's contextvars are a copy, not a shared reference."""
    fake = FakeMsam(
        query_response={"_raw_atoms": [{"id": "child-atom", "content": "x"}]}
    )
    tools = build_msam_tools(fake)  # type: ignore[arg-type]
    msam_query = _by_name(tools, "msam_query")

    parent_ctx = _ctx("parent")
    parent_token = _context.set_current_turn(parent_ctx)
    try:
        async def subagent_run():
            child_ctx = _ctx("child", msam_session_id="msam-child-1")
            child_token = _context.set_current_turn(child_ctx)
            try:
                await msam_query.handler({"query": "x", "top_k": 1})
            finally:
                _context.reset_current_turn(child_token)
            return child_ctx

        child = await asyncio.create_task(subagent_run())  # type: ignore[name-defined]
    finally:
        _context.reset_current_turn(parent_token)

    assert child.msam_atom_ids == ["child-atom"]
    assert parent_ctx.msam_atom_ids == []  # parent untouched


# Late import to keep the file's main imports compact.
import asyncio  # noqa: E402
