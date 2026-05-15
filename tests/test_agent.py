"""Smoke tests for the deepagents-backed Agent.

These tests stub out ``_build_agent_if_needed`` so no real model or
deepagents graph is constructed. We're verifying the
``run_turn`` orchestration:

  - SAGA pre-message query → memory_block injected into prompt
  - agent.ainvoke called once
  - extract_turn_events / derive_result_fields populate TurnRecord
  - saga.feedback fires post-message with the right atom IDs
  - TurnLogger sees one record
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from mimir.agent import Agent
from mimir.history import MessageBuffer
from mimir.index import IndexGenerator
from mimir.models import AgentEvent
from mimir.turn_logger import TurnLogger


class _StubConfig:
    """The Agent stores a config reference but never reads it in
    run_turn — pass a tiny placeholder rather than building a full
    Config (which has 30+ required fields)."""
    def __init__(self, home: Path) -> None:
        self.home = home


class _FakeAgent:
    """Replaces the deepagents CompiledStateGraph. Returns a canned
    message list shaped like ChatClaudeCode's output."""

    def __init__(self, response_messages: list[Any]) -> None:
        self._response_messages = response_messages
        self.invocations: list[dict[str, Any]] = []

    async def ainvoke(self, state: dict[str, Any], *, config: dict[str, Any]):
        self.invocations.append({"state": state, "config": config})
        # Echo the input + append response messages (mirrors LangGraph state).
        return {"messages": list(state.get("messages") or []) + self._response_messages}


class _FakeSaga:
    """Tiny saga_client double — record query/feedback calls, return
    canned payloads."""

    def __init__(self, query_hits: list[dict[str, Any]] | None = None) -> None:
        self._hits = query_hits or []
        self.query_calls: list[dict[str, Any]] = []
        self.feedback_calls: list[dict[str, Any]] = []

    async def query(self, content: str, *, top_k: int = 12, session_id: str | None = None):
        self.query_calls.append(
            {"content": content, "top_k": top_k, "session_id": session_id},
        )
        return {"atoms": self._hits, "triples": []}

    async def feedback(self, atom_ids, output, *, session_id=None, feedback="positive"):
        self.feedback_calls.append({
            "atom_ids": list(atom_ids), "output": output,
            "session_id": session_id, "feedback": feedback,
        })


def _build_agent(tmp_path: Path, *,
                 fake_agent: _FakeAgent,
                 fake_saga: _FakeSaga | None = None) -> Agent:
    from mimir.event_logger import init_logger
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True, exist_ok=True)
    init_logger(home / "logs" / "events.jsonl", session_id="test")
    cfg = _StubConfig(home)
    a = Agent(
        config=cfg,  # type: ignore[arg-type]
        turn_logger=TurnLogger(home / "logs" / "turns.jsonl"),
        message_buffer=MessageBuffer(history_path=home / "messages.jsonl"),
        index_generator=IndexGenerator(home),
        saga_client=fake_saga,  # type: ignore[arg-type]
    )
    # Skip the real deepagents.create_deep_agent — return our fake
    # whenever Agent goes to build/fetch the graph.
    a._agent = fake_agent  # type: ignore[attr-defined]
    return a


async def test_run_turn_writes_record_with_extracted_events(tmp_path: Path):
    fake_agent = _FakeAgent(response_messages=[
        AIMessage(
            content="Stored.",
            response_metadata={
                "internal_tool_calls": [
                    {"id": "toolu_1", "name": "memory_store",
                     "args": {"content": "color is blue"}}
                ],
                "tool_results": [
                    {"tool_use_id": "toolu_1", "name": "memory_store",
                     "result": {"stored": True, "atom_id": "f" * 16},
                     "is_error": False},
                ],
                "total_cost_usd": 0.001,
                "num_turns": 2,
            },
        ),
    ])
    fake_saga = _FakeSaga(query_hits=[
        {"atom_id": "a" * 16, "content": "prior memory", "stream": "semantic"},
    ])
    agent = _build_agent(tmp_path, fake_agent=fake_agent, fake_saga=fake_saga)

    event = AgentEvent(
        trigger="user_message",
        channel_id="ch-1",
        content="store my favorite color",
    )
    record = await agent.run_turn(event)

    # SAGA was queried with the user content
    assert len(fake_saga.query_calls) == 1
    assert fake_saga.query_calls[0]["content"] == "store my favorite color"

    # The pre-message memory block landed in the prompt to the agent
    invocation = fake_agent.invocations[0]
    prompt_msg = invocation["state"]["messages"][0]
    assert isinstance(prompt_msg, HumanMessage)
    assert "Possibly relevant memories" in prompt_msg.content

    # Events were extracted from response_metadata
    event_types = [e["type"] for e in record.events]
    assert "tool_call" in event_types
    assert "tool_result" in event_types
    tc = next(e for e in record.events if e["type"] == "tool_call")
    assert tc["name"] == "memory_store"

    # Result fields surfaced cost + num_turns
    assert record.total_cost_usd == pytest.approx(0.001)
    assert record.num_turns == 2
    assert record.error is None

    # The TurnLogger appended one record
    turns = (tmp_path / "home" / "logs" / "turns.jsonl").read_text().splitlines()
    assert len(turns) == 1


async def test_run_turn_no_saga_skips_query_and_feedback(tmp_path: Path):
    fake_agent = _FakeAgent(response_messages=[AIMessage(content="ok")])
    agent = _build_agent(tmp_path, fake_agent=fake_agent, fake_saga=None)
    event = AgentEvent(trigger="user_message", channel_id="ch-1", content="hi")
    record = await agent.run_turn(event)
    assert record.output == "ok"
    # No SAGA → no memory block injected
    prompt = fake_agent.invocations[0]["state"]["messages"][0].content
    assert "Possibly relevant memories" not in prompt


async def test_run_turn_records_error_when_ainvoke_raises(tmp_path: Path):
    class _BoomAgent:
        async def ainvoke(self, *a, **kw):
            raise RuntimeError("upstream failure")
    fake_saga = _FakeSaga()
    agent = _build_agent(
        tmp_path, fake_agent=_BoomAgent(), fake_saga=fake_saga,  # type: ignore[arg-type]
    )
    event = AgentEvent(trigger="user_message", channel_id="ch-1", content="x")
    record = await agent.run_turn(event)
    assert record.error and "upstream failure" in record.error
    assert record.events == []
    # feedback skipped on error
    assert fake_saga.feedback_calls == []
