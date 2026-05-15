"""Tests for the bundled run_turn pipeline.

Mocks the deepagent and the MemoryClient — tests our pre-hook /
post-hook / turn-log wrapper code in isolation, no LLM calls.

This is the migration shape for tests/test_agent_saga.py — same
integration assertions (pre-hook fires, atoms credited, turn record
written, error handling), without claude_agent_sdk dependencies.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from mimir.deepagent_poc.turn_logger import TurnLogger
from mimir.deepagent_poc.turn_runner import run_turn


def _make_fake_memory_client(query_payload: dict | None = None) -> AsyncMock:
    """Build a fake MemoryClient that returns canned responses."""
    client = AsyncMock()
    # MemoryClient.query returns the saga-payload shape
    client.query.return_value = query_payload or {
        "observations": [],
        "raws": [
            {"atom_id": "abc1234567890abc",
             "content": "[2023-05-30 user] I graduated with Business Administration.",
             "memory_type": "raw", "stream": "episodic"}
        ],
        "triples": [],
    }
    # MemoryClient.feedback for the post-hook credit pass
    client.feedback.return_value = {"marked": 1}
    return client


def _make_fake_agent(reply_text: str = "Business Administration.") -> AsyncMock:
    """Build a fake compiled-state-graph that returns a canned message list."""
    agent = AsyncMock()
    agent.ainvoke.return_value = {
        "messages": [
            HumanMessage(content="prompt"),
            AIMessage(content=reply_text),
        ],
    }
    return agent


# ─── Successful turn ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_turn_happy_path(tmp_path: Path):
    """End-to-end: pre-hook fires, agent invoked, post-hook credits
    atoms, turn record written."""
    client = _make_fake_memory_client()
    agent = _make_fake_agent("You graduated with Business Administration.")
    turn_logger = TurnLogger(tmp_path / "turns.jsonl")

    outcome = await run_turn(
        agent,
        memory_client=client,
        question="What did I graduate with?",
        session_id="sess-1",
        channel_id="bench-1",
        saga_session_id="saga-sess-1",
        turn_logger=turn_logger,
    )

    # Agent invoked with the augmented HumanMessage (memory prepended)
    assert agent.ainvoke.called
    call_kwargs = agent.ainvoke.call_args
    messages = call_kwargs.args[0]["messages"]
    assert len(messages) == 1
    assert isinstance(messages[0], HumanMessage)
    # The augmented content includes the memory block prefix
    assert "Possibly relevant memories" in messages[0].content
    assert "What did I graduate with?" in messages[0].content

    # MemoryClient.query fired (pre-hook)
    assert client.query.called

    # MemoryClient.feedback fired (post-hook credit pass)
    assert client.feedback.called
    fb_args = client.feedback.call_args
    credited_ids = fb_args.args[0]
    assert "abc1234567890abc" in credited_ids

    # TurnOutcome envelope populated
    assert outcome.error is None
    assert "Business Administration" in outcome.output
    assert outcome.pre_message is not None
    assert outcome.post_message is not None
    assert outcome.post_message.feedback_ok

    # turn record written
    records = (tmp_path / "turns.jsonl").read_text().splitlines()
    assert len(records) == 1


# ─── Failure modes ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_turn_pre_hook_query_failure(tmp_path: Path):
    """Pre-hook MemoryClient.query failure → run_turn still proceeds,
    agent gets the unaugmented question, no atoms credited."""
    client = _make_fake_memory_client()
    client.query.side_effect = RuntimeError("transport down")
    agent = _make_fake_agent("I don't have memory access.")
    turn_logger = TurnLogger(tmp_path / "turns.jsonl")

    outcome = await run_turn(
        agent, memory_client=client, question="Q?",
        session_id="s", channel_id="c", saga_session_id="ss",
        turn_logger=turn_logger,
    )

    # Agent still invoked (with the raw question, not augmented)
    assert agent.ainvoke.called
    # Empty atom IDs → no feedback call
    assert outcome.pre_message is not None
    assert outcome.pre_message.saga_atom_ids == []
    # Either no post_message or empty credited list
    assert outcome.post_message is None or outcome.post_message.atom_ids_credited == []


@pytest.mark.asyncio
async def test_run_turn_agent_failure_logs_turn(tmp_path: Path):
    """Agent failure → error captured, turn record still written
    (matches mimir's fail-soft contract)."""
    client = _make_fake_memory_client()
    agent = AsyncMock()
    agent.ainvoke.side_effect = RuntimeError("model unavailable")
    turn_logger = TurnLogger(tmp_path / "turns.jsonl")

    outcome = await run_turn(
        agent, memory_client=client, question="Q?",
        session_id="s", channel_id="c", saga_session_id="ss",
        turn_logger=turn_logger,
    )

    assert outcome.error is not None
    assert "model unavailable" in outcome.error
    # post-hook skipped on error (matches mimir: don't credit on failure)
    assert outcome.post_message is None
    # turn record still written
    records = (tmp_path / "turns.jsonl").read_text().splitlines()
    assert len(records) == 1


# ─── Configurability ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_turn_passes_thread_id_via_config(tmp_path: Path):
    """``config`` kwarg passes through to agent.ainvoke unchanged —
    LangGraph per-call isolation via thread_id."""
    client = _make_fake_memory_client()
    agent = _make_fake_agent("ok")
    turn_logger = TurnLogger(tmp_path / "turns.jsonl")

    await run_turn(
        agent, memory_client=client, question="Q?",
        session_id="s", channel_id="c", saga_session_id="ss",
        turn_logger=turn_logger,
        config={"configurable": {"thread_id": "my-thread-id"}},
    )

    call = agent.ainvoke.call_args
    cfg = call.kwargs.get("config")
    if cfg is None and len(call.args) > 1:
        cfg = call.args[1]
    assert cfg is not None
    assert cfg.get("configurable", {}).get("thread_id") == "my-thread-id"


@pytest.mark.asyncio
async def test_run_turn_credits_triple_source_atoms(tmp_path: Path):
    """When pre-hook payload contains triples with source_atom_id,
    those atoms also get credited in the post-hook."""
    payload = {
        "observations": [],
        "raws": [{"atom_id": "raw_atom_111111aa", "content": "...",
                  "memory_type": "raw", "stream": "episodic"}],
        "triples": [
            {"subject": "User", "predicate": "prefers", "object": "blue",
             "source_atom_id": "triple_src_111111ab",
             "valid_from": None, "valid_until": None, "confidence": 0.9},
        ],
    }
    client = _make_fake_memory_client(query_payload=payload)
    agent = _make_fake_agent("Your favorite color is blue.")
    turn_logger = TurnLogger(tmp_path / "turns.jsonl")

    outcome = await run_turn(
        agent, memory_client=client, question="Color?",
        session_id="s", channel_id="c", saga_session_id="ss",
        turn_logger=turn_logger,
    )

    assert outcome.post_message is not None
    credited = outcome.post_message.atom_ids_credited
    # Both raw + triple source atom credited
    assert "raw_atom_111111aa" in credited
    assert "triple_src_111111ab" in credited
