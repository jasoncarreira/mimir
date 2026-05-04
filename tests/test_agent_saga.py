"""Phase 4 integration: pre/post SAGA hooks, session manager, synthesis turn."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    project_key_for_directory,
)

from mimir.agent import Agent
from mimir.config import Config
from mimir.event_logger import init_logger
from mimir.history import MessageBuffer
from mimir.index import IndexGenerator
from mimir.models import AgentEvent, make_process_session_id
from mimir.session_manager import ChannelSession, SessionManager
from mimir.turn_logger import TurnLogger

from ._fake_saga import FakeSaga


def _cfg(tmp_path: Path, **overrides) -> Config:
    cfg = Config.from_env()
    return replace(
        cfg,
        home=tmp_path,
        saga_session_idle_minutes=overrides.get("saga_session_idle_minutes", 60),
        recent_per_channel=overrides.get("recent_per_channel", 5),
        recent_author_cross=overrides.get("recent_author_cross", 5),
    )


def _build_agent(tmp_path: Path, saga, sessions=None) -> Agent:
    cfg = _cfg(tmp_path)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    init_logger(cfg.events_log, make_process_session_id())
    turn_logger = TurnLogger(cfg.turns_log)
    buf = MessageBuffer(history_path=cfg.home / "messages" / "chat_history.jsonl")
    indexes = IndexGenerator(cfg.home)
    sessions = sessions or SessionManager(idle_minutes=60)
    return Agent(
        cfg,
        turn_logger,
        buf,
        indexes,
        indexer=None,  # search index not under test here
        saga_client=saga,
        session_manager=sessions,
    )


def _fake_query_yielding(text: str):
    async def fake_query(*, prompt, options, session_id="default", transport=None):
        yield AssistantMessage(content=[TextBlock(text=text)], model="claude-opus-4-7")

    return fake_query


def _fake_query_calling_saga_query():
    """Simulate the agent calling saga_query mid-turn so the post-hook
    sees both the pre-injected and the mid-turn-queried atoms."""
    async def fake_query(*, prompt, options, session_id="default", transport=None):
        # The contextvar is set by run_turn; emit a tool call referencing the
        # MCP tool name so events.jsonl looks right.
        yield AssistantMessage(
            content=[
                TextBlock(text="checking memories"),
                ToolUseBlock(id="t1", name="mcp__mimir__saga_query", input={"query": "x"}),
            ],
            model="claude-opus-4-7",
        )
        yield UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="[]", is_error=False)])
        yield AssistantMessage(content=[TextBlock(text="done")], model="claude-opus-4-7")

    return fake_query


@pytest.mark.asyncio
async def test_pre_message_hook_injects_atoms_and_post_credits_them(tmp_path: Path):
    saga = FakeSaga(
        query_response={
            "_raw_atoms": [
                {"id": "atom-1", "stream": "semantic", "content": "alice prefers terse"},
                {"id": "atom-2", "stream": "episodic", "content": "discussed quantum yesterday"},
            ]
        }
    )
    agent = _build_agent(tmp_path, saga)

    fake_q = _fake_query_yielding("hello back")
    with patch("mimir.agent.query", new=fake_q):
        record = await agent.run_turn(
            AgentEvent(
                trigger="user_message",
                channel_id="bench-1",
                content="hi alice",
                author="alice",
            )
        )

    # Pre-message hook ran:
    assert "query" in saga.methods()
    q_payload = saga.last("query")
    assert q_payload["query"] == "hi alice"
    assert q_payload["session_id"] is not None  # session manager attached one

    # Post-message hook credited the atoms with the response_text:
    fb = saga.last("feedback")
    assert fb is not None
    assert sorted(fb["atom_ids"]) == ["atom-1", "atom-2"]
    assert fb["response_text"] == "hello back"
    assert fb["session_id"] == q_payload["session_id"]

    # TurnRecord captured the union of atom_ids:
    assert sorted(record.saga_atom_ids) == ["atom-1", "atom-2"]


@pytest.mark.asyncio
async def test_pre_message_hook_credits_triple_source_atoms(tmp_path: Path):
    """P42: when saga returns triples in the response, their
    source_atom_id values should also flow into ctx.saga_atom_ids so
    the post-message hook credits the originating atoms — same
    contribution-credit path as for raw atom hits."""
    saga = FakeSaga(
        query_response={
            "_raw_atoms": [
                {"id": "atom-from-prose", "stream": "semantic",
                 "content": "alice prefers terse"},
            ],
            "triples": [
                {"subject": "alice", "predicate": "prefers", "object": "terse",
                 "source_atom_id": "atom-from-triple",
                 "valid_from": None, "valid_until": None, "confidence": 1.0},
                {"subject": "alice", "predicate": "lives_in", "object": "Oakland",
                 "source_atom_id": "atom-from-triple-2",
                 "valid_from": None, "valid_until": None, "confidence": 1.0},
            ],
        }
    )
    agent = _build_agent(tmp_path, saga)

    fake_q = _fake_query_yielding("hello")
    with patch("mimir.agent.query", new=fake_q):
        record = await agent.run_turn(
            AgentEvent(
                trigger="user_message",
                channel_id="bench-1",
                content="hi alice",
                author="alice",
            )
        )

    # Both prose-atom and triple-source atoms got credited.
    fb = saga.last("feedback")
    assert fb is not None
    assert set(fb["atom_ids"]) == {
        "atom-from-prose", "atom-from-triple", "atom-from-triple-2",
    }
    assert set(record.saga_atom_ids) == {
        "atom-from-prose", "atom-from-triple", "atom-from-triple-2",
    }


@pytest.mark.asyncio
async def test_synthesis_turn_skips_pre_post_hooks_and_uses_extra_session_id(tmp_path: Path):
    saga = FakeSaga(query_response={"_raw_atoms": []})
    agent = _build_agent(tmp_path, saga)

    fake_q = _fake_query_yielding("noop bookkeeping")
    with patch("mimir.agent.query", new=fake_q):
        record = await agent.run_turn(
            AgentEvent(
                trigger="saga_session_end",
                channel_id="bench-1",
                content="",
                extra={"saga_session_id": "saga-c1-frozen-id"},
            )
        )

    # No query, no feedback — synthesis turns skip both hooks.
    assert "query" not in saga.methods()
    assert "feedback" not in saga.methods()

    # The closed session id flows through onto the turn record.
    assert record.saga_session_id == "saga-c1-frozen-id"
    assert record.trigger == "saga_session_end"


@pytest.mark.asyncio
async def test_saga_failure_does_not_break_turn(tmp_path: Path):
    saga = FakeSaga(fail_on={"query"})
    agent = _build_agent(tmp_path, saga)

    fake_q = _fake_query_yielding("still here")
    with patch("mimir.agent.query", new=fake_q):
        record = await agent.run_turn(
            AgentEvent(trigger="user_message", channel_id="c", content="hi", author="x")
        )
    assert record.error is None  # turn ran fine despite SAGA error
    assert record.output == "still here"


@pytest.mark.asyncio
async def test_synthesis_turn_filters_turns_jsonl_by_session_id(tmp_path: Path):
    """The synthesis turn embeds turns from turns.jsonl filtered by
    saga_session_id. Pre-seed the file with two sessions and verify only the
    target session's turns end up in the prompt window."""
    cfg = _cfg(tmp_path)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    init_logger(cfg.events_log, make_process_session_id())

    # Seed turns.jsonl by hand — two sessions interleaved.
    turns_path = cfg.turns_log
    rows = [
        {"turn_id": "t1", "saga_session_id": "saga-S1", "channel_id": "c1", "trigger": "user_message", "input": "hello", "events": [], "output": "hi", "session_id": "c1", "ts": "2026-04-25T10:00:00Z", "duration_ms": 100, "error": None, "saga_atom_ids": ["a1"]},
        {"turn_id": "t2", "saga_session_id": "saga-S2", "channel_id": "c1", "trigger": "user_message", "input": "later", "events": [], "output": "ok", "session_id": "c1", "ts": "2026-04-25T11:00:00Z", "duration_ms": 50, "error": None, "saga_atom_ids": []},
        {"turn_id": "t3", "saga_session_id": "saga-S1", "channel_id": "c1", "trigger": "user_message", "input": "more S1", "events": [], "output": "yep", "session_id": "c1", "ts": "2026-04-25T10:05:00Z", "duration_ms": 50, "error": None, "saga_atom_ids": ["a2"]},
    ]
    turns_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    captured: dict = {}

    async def capture_query(*, prompt, options, session_id="default", transport=None):
        captured["prompt"] = prompt
        yield AssistantMessage(content=[TextBlock(text="done")], model="claude-opus-4-7")

    with patch("mimir.agent.query", new=capture_query):
        agent = _build_agent(tmp_path, FakeSaga())
        await agent.run_turn(
            AgentEvent(
                trigger="saga_session_end",
                channel_id="c1",
                content="",
                extra={"saga_session_id": "saga-S1"},
            )
        )

    body = captured["prompt"]
    # Both S1 turns embedded; S2's turn must NOT appear.
    assert '"turn_id": "t1"' in body
    assert '"turn_id": "t3"' in body
    assert '"turn_id": "t2"' not in body


@pytest.mark.asyncio
async def test_agent_loop_passes_turn_id_as_session_id(tmp_path: Path):
    """Stage 2 of CLAUDE_SDK_CLIENT_MIGRATION.md: each turn drives the
    shared ClaudeSDKClient with ``session_id=ctx.turn_id`` so per-turn
    history is isolated. Confirm the call-site forwards it."""
    captured: dict = {}

    async def capture_session_id(*, prompt, options, session_id="default", transport=None):
        captured["session_id"] = session_id
        yield AssistantMessage(content=[TextBlock(text="ack")], model="claude-opus-4-7")

    agent = _build_agent(tmp_path, FakeSaga())
    with patch("mimir.agent.query", new=capture_session_id):
        record = await agent.run_turn(
            AgentEvent(
                trigger="user_message",
                channel_id="bench-1",
                content="hello",
                author="alice",
            )
        )

    # The session_id seen by the wrapper is the same turn_id recorded
    # on the TurnRecord — every turn gets its own SDK session.
    assert captured["session_id"] == record.turn_id
    assert captured["session_id"] != "default"


@pytest.mark.asyncio
async def test_consecutive_turns_get_distinct_session_ids(tmp_path: Path):
    """Two back-to-back turns must hand the wrapper distinct session
    ids — without that, prior-turn history would bleed into the next
    turn via the SDK's session store."""
    seen: list[str] = []

    async def record_session_id(*, prompt, options, session_id="default", transport=None):
        seen.append(session_id)
        yield AssistantMessage(content=[TextBlock(text="ack")], model="claude-opus-4-7")

    agent = _build_agent(tmp_path, FakeSaga())
    with patch("mimir.agent.query", new=record_session_id):
        r1 = await agent.run_turn(
            AgentEvent(trigger="user_message", channel_id="c1", content="a", author="alice")
        )
        r2 = await agent.run_turn(
            AgentEvent(trigger="user_message", channel_id="c1", content="b", author="alice")
        )

    assert seen == [r1.turn_id, r2.turn_id]
    assert r1.turn_id != r2.turn_id


# ─── Stage 3: explicit SessionStore + per-turn delete ────────────────


@pytest.mark.asyncio
async def test_build_options_attaches_session_store(tmp_path: Path):
    """Stage 3: the agent's per-turn options carry the same
    InMemorySessionStore instance every call. The shared store is
    what the wrapper hands to ClaudeSDKClient at connect time."""
    agent = _build_agent(tmp_path, FakeSaga())
    opts1 = agent._build_options("system prompt v1")
    opts2 = agent._build_options("system prompt v2")
    assert opts1.session_store is agent._session_store
    assert opts2.session_store is agent._session_store


@pytest.mark.asyncio
async def test_run_turn_deletes_session_after_completion(tmp_path: Path):
    """Stage 3: after a turn completes, run_turn calls
    ``store.delete()`` for that turn's session so the store doesn't
    accumulate per-turn entries. We pre-seed the store with an entry
    keyed to the turn's id (the fake query() path never appends, so
    seeding is the only way to verify the delete actually fired)."""
    seeded_ids: list[str] = []

    async def seeding_query(*, prompt, options, session_id="default", transport=None):
        # Mimic the SDK appending an entry for this session_id, the
        # way the real client+store integration would.
        seeded_ids.append(session_id)
        await options.session_store.append(
            {
                "project_key": project_key_for_directory(options.cwd),
                "session_id": session_id,
            },
            [{"type": "user", "uuid": "u1", "timestamp": "2026-05-04T00:00:00Z"}],
        )
        yield AssistantMessage(content=[TextBlock(text="ok")], model="claude-opus-4-7")

    agent = _build_agent(tmp_path, FakeSaga())
    with patch("mimir.agent.query", new=seeding_query):
        record = await agent.run_turn(
            AgentEvent(trigger="user_message", channel_id="c1", content="hi", author="alice")
        )

    assert seeded_ids == [record.turn_id]
    # After the turn, the entry for turn_id has been deleted.
    project_key = project_key_for_directory(str(tmp_path))
    assert agent._session_store.get_entries(
        {"project_key": project_key, "session_id": record.turn_id}
    ) == []
    # And the store reports no main-transcript sessions for our project key.
    assert agent._session_store.size == 0


@pytest.mark.asyncio
async def test_session_store_size_stays_flat_across_many_turns(tmp_path: Path):
    """Stage 3 success metric (scaled): the store size doesn't grow
    over N sequential turns. Spec asks for 1000 turns; we run 50 in
    unit tests and check that count stays at 0 after each — the
    invariant is identical and a higher N just costs CI time."""

    async def seeding_query(*, prompt, options, session_id="default", transport=None):
        await options.session_store.append(
            {
                "project_key": project_key_for_directory(options.cwd),
                "session_id": session_id,
            },
            [{"type": "user", "uuid": f"u-{session_id[-6:]}", "timestamp": "2026-05-04T00:00:00Z"}],
        )
        yield AssistantMessage(content=[TextBlock(text="ok")], model="claude-opus-4-7")

    agent = _build_agent(tmp_path, FakeSaga())
    with patch("mimir.agent.query", new=seeding_query):
        for i in range(50):
            await agent.run_turn(
                AgentEvent(
                    trigger="user_message",
                    channel_id="c1",
                    content=f"turn {i}",
                    author="alice",
                )
            )
            # Per-turn delete fired in finally — store stays empty.
            assert agent._session_store.size == 0


@pytest.mark.asyncio
async def test_session_store_delete_runs_even_when_query_crashes(tmp_path: Path):
    """The delete is in a ``finally`` block, so a query() crash mid-
    turn must still drop that turn's session entries — otherwise a
    flapping subprocess would leak one session per failure."""

    async def crashing_query(*, prompt, options, session_id="default", transport=None):
        await options.session_store.append(
            {
                "project_key": project_key_for_directory(options.cwd),
                "session_id": session_id,
            },
            [{"type": "user", "uuid": "u1", "timestamp": "2026-05-04T00:00:00Z"}],
        )
        raise RuntimeError("simulated SDK crash mid-turn")
        yield  # pragma: no cover — make this an async generator

    agent = _build_agent(tmp_path, FakeSaga())
    with patch("mimir.agent.query", new=crashing_query):
        record = await agent.run_turn(
            AgentEvent(trigger="user_message", channel_id="c1", content="hi", author="alice")
        )

    # The TurnRecord captured the error and the store is still empty.
    assert record.error is not None
    assert "simulated SDK crash" in record.error
    assert agent._session_store.size == 0


@pytest.mark.asyncio
async def test_session_store_delete_failure_does_not_break_turn(tmp_path: Path):
    """If the store's delete() raises (e.g., a backend hiccup), the
    turn must still complete cleanly. We swap in a store whose
    delete always raises; the run_turn finally must swallow it."""

    class _ExplodingDeleteStore:
        # Minimal duck-typed SessionStore — only the methods the
        # agent path actually uses.
        def __init__(self):
            self.deletes: list[dict] = []
            self.appends: list[tuple] = []

        async def append(self, key, entries):
            self.appends.append((dict(key), list(entries)))

        async def load(self, key):
            return None

        async def delete(self, key):
            self.deletes.append(dict(key))
            raise RuntimeError("backend down")

    agent = _build_agent(tmp_path, FakeSaga())
    exploder = _ExplodingDeleteStore()
    agent._session_store = exploder  # type: ignore[assignment]

    async def fake_q(*, prompt, options, session_id="default", transport=None):
        yield AssistantMessage(content=[TextBlock(text="ok")], model="claude-opus-4-7")

    with patch("mimir.agent.query", new=fake_q):
        record = await agent.run_turn(
            AgentEvent(trigger="user_message", channel_id="c1", content="hi", author="alice")
        )

    # Turn succeeded, delete was attempted exactly once with the right key.
    assert record.error is None
    assert len(exploder.deletes) == 1
    assert exploder.deletes[0]["session_id"] == record.turn_id
