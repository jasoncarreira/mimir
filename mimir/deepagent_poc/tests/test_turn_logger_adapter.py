"""Concrete pytest tests for the turn_logger adapter.

Mirrors the shape of mimir's existing SDK-side
``tests/test_turn_logger.py`` but goes against LangChain message
types instead of claude_agent_sdk. Proves the test migration is
mechanical — same assertion targets, different message-type imports.

This is the new pattern Phase D would propagate across the 3
SDK-specific test files (test_agent_sdk_client.py,
test_agent_saga.py, test_turn_hooks.py).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from mimir.deepagent_poc.turn_logger import (
    TurnLogger,
    TurnRecord,
    derive_result_fields,
    extract_turn_events,
    truncate_input,
)


# ─── extract_turn_events ─────────────────────────────────────────


def test_extract_turn_events_plain_assistant_reply():
    """AIMessage with text content, no tool calls → appended to output."""
    messages = [
        HumanMessage(content="What's 2+2?"),
        AIMessage(content="4"),
    ]
    events, output = extract_turn_events(messages)
    assert events == []
    assert output == "4"


def test_extract_turn_events_reasoning_then_tool_call():
    """AIMessage with both content AND tool_calls → emits a reasoning
    event followed by tool_call event. SDK-side path uses ThinkingBlock
    + ToolUseBlock; LangChain uses content + .tool_calls. Same end state."""
    ai = AIMessage(
        content="Let me look that up.",
        tool_calls=[{
            "id": "call_123",
            "name": "memory_query",
            "args": {"query": "user's favorite color"},
        }],
    )
    events, output = extract_turn_events([HumanMessage(content="?"), ai])
    assert output == ""
    assert events[0] == {"type": "reasoning", "content": "Let me look that up."}
    assert events[1] == {
        "type": "tool_call",
        "id": "call_123",
        "name": "memory_query",
        "args": {"query": "user's favorite color"},
    }


def test_extract_turn_events_tool_result():
    """ToolMessage → tool_result event, with content truncation."""
    tool_msg = ToolMessage(
        content="The user's favorite color is blue.",
        tool_call_id="call_123",
        name="memory_query",
    )
    events, output = extract_turn_events([tool_msg])
    assert output == ""
    assert events == [{
        "type": "tool_result",
        "id": "call_123",
        "name": "memory_query",
        "content": "The user's favorite color is blue.",
        "is_error": False,
    }]


def test_extract_turn_events_truncates_long_tool_result():
    """Tool results over MAX_TOOL_RESULT_BYTES are clipped."""
    big = "x" * 10_000
    tool_msg = ToolMessage(
        content=big, tool_call_id="c", name="memory_query",
    )
    events, _ = extract_turn_events([tool_msg])
    assert events[0]["content"].endswith("…[truncated]")
    assert len(events[0]["content"]) < 10_000


def test_extract_turn_events_full_loop_shape():
    """Full multi-step turn: human → ai-with-tool-call → tool result →
    final ai. Output reflects ONLY the last AI's text reply."""
    messages = [
        HumanMessage(content="What's my favorite color?"),
        AIMessage(
            content="I'll check.",
            tool_calls=[{"id": "c1", "name": "memory_query",
                         "args": {"query": "favorite color"}}],
        ),
        ToolMessage(content="(User, prefers_color, blue)",
                    tool_call_id="c1", name="memory_query"),
        AIMessage(content="Your favorite color is blue."),
    ]
    events, output = extract_turn_events(messages)
    assert output == "Your favorite color is blue."
    types = [e["type"] for e in events]
    assert types == ["reasoning", "tool_call", "tool_result"]


# ─── derive_result_fields ────────────────────────────────────────


def test_derive_result_fields_aggregates_usage():
    """usage_metadata across multiple AI messages adds up."""
    messages = [
        HumanMessage(content="?"),
        AIMessage(content="thinking",
                  usage_metadata={"input_tokens": 100, "output_tokens": 5, "total_tokens": 105}),
        AIMessage(content="answer",
                  usage_metadata={"input_tokens": 200, "output_tokens": 10, "total_tokens": 210}),
    ]
    fields = derive_result_fields(messages)
    assert fields["usage"]["input_tokens"] == 300
    assert fields["usage"]["output_tokens"] == 15
    assert fields["num_turns"] == 2  # two AI messages


def test_derive_result_fields_empty_message_list_safe():
    """No AI messages → fields are None (matches mimir's nullable contract)."""
    fields = derive_result_fields([])
    assert fields["result_subtype"] is None
    assert fields["usage"] is None
    assert fields["num_turns"] is None


# ─── truncate_input ──────────────────────────────────────────────


def test_truncate_input_short_passthrough():
    """Short prompts pass through unchanged."""
    assert truncate_input("hi") == "hi"


def test_truncate_input_long_truncated():
    """Prompts over MAX_INPUT_BYTES get an ellipsis suffix."""
    big = "x" * 10_000
    out = truncate_input(big)
    assert out.endswith("…[truncated]")
    assert len(out) < 10_000


# ─── TurnLogger ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_turn_logger_writes_record(tmp_path: Path):
    """TurnLogger.write appends one JSON line per record."""
    logger = TurnLogger(tmp_path / "turns.jsonl")
    record = TurnRecord(
        ts="2026-05-14T20:00:00Z",
        turn_id="abc123",
        session_id="sess-1",
        saga_session_id=None,
        trigger="user_message",
        channel_id="bench-test",
        input="hello",
        events=[],
        output="hi back",
    )
    await logger.write(record)
    lines = (tmp_path / "turns.jsonl").read_text().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["turn_id"] == "abc123"
    assert parsed["output"] == "hi back"


@pytest.mark.asyncio
async def test_turn_logger_trims_when_over_max(tmp_path: Path):
    """When line count exceeds max_turns, file trims to the most recent."""
    logger = TurnLogger(tmp_path / "turns.jsonl", max_turns=3)
    for i in range(5):
        await logger.write(TurnRecord(
            ts=f"t{i}", turn_id=f"id{i}",
            session_id="s", saga_session_id=None,
            trigger="user_message", channel_id=None, input="",
        ))
    lines = (tmp_path / "turns.jsonl").read_text().splitlines()
    assert len(lines) == 3
    ids = [json.loads(l)["turn_id"] for l in lines]
    # Most recent 3 retained
    assert ids == ["id2", "id3", "id4"]
