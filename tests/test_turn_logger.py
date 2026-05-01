"""Verify SDK message stream → TurnRecord events extraction (SPEC §10.3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from mimir.models import TurnRecord
from mimir.turn_logger import TurnLogger, extract_turn_events, truncate_input


def _assistant(*blocks):
    return AssistantMessage(content=list(blocks), model="claude-opus-4-7")


def test_extract_text_only_appends_to_output():
    msgs = [_assistant(TextBlock(text="hello there"))]
    events, output = extract_turn_events(msgs)
    assert events == []
    assert output == "hello there"


def test_extract_tool_use_emits_reasoning_then_tool_call():
    msgs = [
        _assistant(
            TextBlock(text="thinking about it"),
            ToolUseBlock(id="t1", name="echo", input={"text": "hi"}),
        )
    ]
    events, output = extract_turn_events(msgs)
    assert output == ""
    assert events == [
        {"type": "reasoning", "content": "thinking about it"},
        {"type": "tool_call", "id": "t1", "name": "echo", "args": {"text": "hi"}},
    ]


def test_extract_thinking_block_becomes_reasoning():
    msgs = [_assistant(ThinkingBlock(thinking="deep thought", signature="sig"), TextBlock(text="ok"))]
    events, output = extract_turn_events(msgs)
    assert events == [{"type": "reasoning", "content": "deep thought"}]
    assert output == "ok"


def test_extract_tool_result_from_user_message():
    msgs = [
        UserMessage(
            content=[
                ToolResultBlock(tool_use_id="t1", content="echoed: hi", is_error=False),
            ]
        ),
    ]
    events, output = extract_turn_events(msgs)
    assert output == ""
    # Without a preceding tool_call the name correlation falls back to "".
    assert events == [
        {"type": "tool_result", "id": "t1", "name": "", "content": "echoed: hi", "is_error": False},
    ]


def test_subagent_internal_messages_are_filtered_out():
    """SPEC §10.3: subagent-internal turns (parent_tool_use_id != None) must
    not flatten into the parent's events list — only the Agent tool_call and
    its tool_result remain."""
    parent_msgs = [
        _assistant(
            ToolUseBlock(id="agent-1", name="Agent", input={"subagent_type": "researcher"}),
        ),
        # Subagent-internal AssistantMessage with parent_tool_use_id set.
        AssistantMessage(
            content=[
                TextBlock(text="subagent thinking"),
                ToolUseBlock(id="sub-1", name="WebFetch", input={"url": "x"}),
            ],
            model="claude-opus-4-7",
            parent_tool_use_id="agent-1",
        ),
        UserMessage(
            content=[ToolResultBlock(tool_use_id="sub-1", content="page body", is_error=False)],
            parent_tool_use_id="agent-1",
        ),
        # Parent's tool_result for the Agent call.
        UserMessage(
            content=[ToolResultBlock(tool_use_id="agent-1", content="done", is_error=False)],
        ),
        _assistant(TextBlock(text="researcher said done")),
    ]
    events, output = extract_turn_events(parent_msgs)
    names = [(e["type"], e.get("name")) for e in events]
    # Only the parent's Agent call and its result; subagent's WebFetch dropped.
    assert names == [("tool_call", "Agent"), ("tool_result", "Agent")]
    assert output == "researcher said done"


def test_tool_result_name_correlates_with_preceding_tool_call():
    msgs = [
        _assistant(ToolUseBlock(id="t42", name="echo", input={"text": "x"})),
        UserMessage(content=[ToolResultBlock(tool_use_id="t42", content="x", is_error=False)]),
    ]
    events, _ = extract_turn_events(msgs)
    assert events[0]["type"] == "tool_call"
    assert events[1] == {
        "type": "tool_result",
        "id": "t42",
        "name": "echo",
        "content": "x",
        "is_error": False,
    }


def test_extract_handles_full_turn_round_trip():
    msgs = [
        _assistant(
            TextBlock(text="let me echo"),
            ToolUseBlock(id="t1", name="echo", input={"text": "hello"}),
        ),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="hello", is_error=False)]),
        _assistant(TextBlock(text="echoed for you")),
    ]
    events, output = extract_turn_events(msgs)
    assert output == "echoed for you"
    assert [e["type"] for e in events] == ["reasoning", "tool_call", "tool_result"]


def test_truncate_input_caps_at_max():
    big = "x" * 4096
    out = truncate_input(big)
    assert out.endswith("…[truncated]")
    assert len(out) < len(big)


@pytest.mark.asyncio
async def test_turn_logger_appends_jsonl(tmp_path: Path):
    log_path = tmp_path / "turns.jsonl"
    logger = TurnLogger(log_path, max_turns=3)

    record = TurnRecord(
        ts="2026-04-25T10:00:00+00:00",
        turn_id="abc123",
        session_id="bench-1",
        msam_session_id=None,
        trigger="user_message",
        channel_id="bench-1",
        input="hi",
    )
    await logger.write(record)

    contents = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(contents) == 1
    parsed = json.loads(contents[0])
    assert parsed["turn_id"] == "abc123"
    assert parsed["msam_session_id"] is None
    assert parsed["msam_atom_ids"] == []


@pytest.mark.asyncio
async def test_turn_logger_trims_when_over_cap(tmp_path: Path):
    """Hysteresis: trim fires when over cap by ≥10% (rounded up to at
    least 1 line). With cap=2 the trigger is >3 lines."""
    log_path = tmp_path / "turns.jsonl"
    logger = TurnLogger(log_path, max_turns=2)

    for i in range(20):
        await logger.write(
            TurnRecord(
                ts="t", turn_id=f"id{i}", session_id="c", msam_session_id=None,
                trigger="x", channel_id="c", input=str(i),
            )
        )

    lines = [json.loads(l) for l in log_path.read_text().strip().splitlines()]
    # Bound: between cap (right after trim) and cap+10% rounded up.
    assert 2 <= len(lines) <= 3
    # Most recent turn always kept.
    assert lines[-1]["turn_id"] == "id19"
