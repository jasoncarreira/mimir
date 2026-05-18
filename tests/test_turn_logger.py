"""Tests for the deepagents-era turn_logger.

Covers ``extract_turn_events`` and ``derive_result_fields`` against the
LangChain message shapes mimir actually sees in production:

  - ``AIMessage.tool_calls``                       (langchain-anthropic / -openai)
  - ``response_metadata["internal_tool_calls"]``   (ChatClaudeCode / OAuth path)
  - ``response_metadata["tool_results"]``          (ChatClaudeCode / OAuth path)
  - ``ToolMessage``                                (standard LangGraph tool roundtrip)
  - ``response_metadata`` fields surfaced from claude_agent_sdk
    ResultMessage: ``total_cost_usd``, ``num_turns``, ``usage``, ``is_error``
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from mimir.models import TurnRecord
from mimir.turn_logger import (
    TurnLogger,
    derive_result_fields,
    extract_turn_events,
    make_turn_id,
    truncate_input,
)


# ── extract_turn_events ──────────────────────────────────────────────


def test_empty_message_list():
    events, output = extract_turn_events([])
    assert events == []
    assert output == ""


def test_plain_ai_message_with_no_tools_becomes_output():
    msg = AIMessage(content="Hello there.")
    events, output = extract_turn_events([msg])
    assert events == []
    assert output == "Hello there."


def test_intermediate_aimessage_with_tools_is_reasoning_only():
    """AIMessages with content + tool_calls that are NOT the final
    AIMessage in the turn are pure reasoning — the model is thinking
    aloud before a tool call, with more turns expected after."""
    msgs = [
        AIMessage(
            content="I'll look that up.",
            tool_calls=[
                {"id": "tc_1", "name": "memory_query", "args": {"query": "x"}},
            ],
        ),
        # A second AIMessage makes the first one intermediate.
        AIMessage(content="The answer is 42."),
    ]
    events, output = extract_turn_events(msgs)
    # Intermediate AIMessage's text goes to reasoning, not output;
    # the final AIMessage's text is the output.
    assert output == "The answer is 42."
    types = [e["type"] for e in events]
    assert types == ["reasoning", "tool_call"]
    assert events[0]["content"] == "I'll look that up."


def test_final_aimessage_with_tool_calls_promotes_content_to_output():
    """Regression for the 50-q bluesky bench finding: when the model
    runs through ChatClaudeCode and emits ONE AIMessage with content
    (the answer) AND internal_tool_calls (the tools it used to find
    it), the content must land in ``output`` so bench adapters that
    poll ``turn.output`` for the canonical reply can read it."""
    msg = AIMessage(
        content="I'll look that up.",
        tool_calls=[
            {"id": "tc_1", "name": "memory_query", "args": {"query": "x"}},
        ],
    )
    events, output = extract_turn_events([msg])
    # Single AIMessage = it IS the final → content is output.
    assert output == "I'll look that up."
    types = [e["type"] for e in events]
    # Reasoning event still fires alongside output so turn_viewer can
    # show the pre-tool commentary.
    assert types == ["reasoning", "tool_call"]
    assert events[1]["name"] == "memory_query"


def test_chat_claude_code_internal_tool_calls_are_captured():
    # ChatClaudeCode stashes them under response_metadata instead of
    # tool_calls (deliberate — keeps LangGraph from re-executing).
    msg = AIMessage(
        content="Stored.",
        response_metadata={
            "internal_tool_calls": [
                {"id": "toolu_1", "name": "memory_store",
                 "args": {"content": "blue", "stream": "semantic"}},
            ],
        },
    )
    events, _ = extract_turn_events([msg])
    types = [e["type"] for e in events]
    assert "tool_call" in types
    tc = next(e for e in events if e["type"] == "tool_call")
    assert tc["name"] == "memory_store"
    assert tc["args"] == {"content": "blue", "stream": "semantic"}


def test_chat_claude_code_tool_results_are_captured():
    msg = AIMessage(
        content="Done.",
        response_metadata={
            "internal_tool_calls": [
                {"id": "toolu_1", "name": "memory_store", "args": {"x": 1}}
            ],
            "tool_results": [
                {"tool_use_id": "toolu_1", "name": "memory_store",
                 "result": {"stored": True, "atom_id": "deadbeef"},
                 "is_error": False},
            ],
        },
    )
    events, _ = extract_turn_events([msg])
    tr = next(e for e in events if e["type"] == "tool_result")
    assert tr["name"] == "memory_store"
    assert tr["id"] == "toolu_1"
    assert not tr["is_error"]
    # Result dict gets coerced to a string-ish body
    assert "deadbeef" in tr["content"]


def test_streaming_path_tool_results_captured_under_internal_key():
    """``ChatClaudeCode.astream`` (the path mimir actually uses)
    surfaces tool results under ``response_metadata["internal_tool_results"]``
    rather than ``"tool_results"``. Without reading both keys, every
    streaming-path tool_result silently dropped — most damaging for
    claude-code built-in tools (Bash/Read/Edit/Grep/Glob) whose
    results only ever come back this way.

    mimirbot turn 24a1a8858209 (2026-05-17) captured 63 tool_calls but
    only 2 tool_results because of this — every Bash/Read/Edit/Grep
    result was lost from the audit trail.
    """
    msg = AIMessage(
        content="Done.",
        response_metadata={
            "internal_tool_calls": [
                {"id": "toolu_42", "name": "Bash",
                 "args": {"command": "ls /tmp"}},
            ],
            # NOTE: key is ``internal_tool_results`` (streaming shape),
            # records do NOT carry ``name`` — that has to be reverse-
            # looked-up from the matching tool_call's ``id``.
            "internal_tool_results": [
                {"tool_use_id": "toolu_42",
                 "content": "foo\nbar\nbaz\n",
                 "is_error": False},
            ],
        },
    )
    events, _ = extract_turn_events([msg])
    trs = [e for e in events if e["type"] == "tool_result"]
    assert len(trs) == 1
    tr = trs[0]
    assert tr["id"] == "toolu_42"
    # Reverse-lookup populates the name from the matching tool_call.
    assert tr["name"] == "Bash"
    assert "bar" in tr["content"]
    assert tr["is_error"] is False


def test_tool_result_name_reverse_looked_up_from_id():
    """``langchain-claude-code._parse_assistant_message`` produces
    ToolResult records with ``tool_use_id`` + ``content`` + ``is_error``
    but NO ``name`` field — the name has to be cross-referenced from
    the matching tool_call's ``id``. Verify the lookup populates the
    name on the emitted event."""
    msg = AIMessage(
        content="",
        response_metadata={
            "internal_tool_calls": [
                {"id": "toolu_a", "name": "Read", "args": {"file_path": "/x"}},
                {"id": "toolu_b", "name": "Edit", "args": {"file_path": "/y"}},
            ],
            "internal_tool_results": [
                {"tool_use_id": "toolu_b", "content": "edited", "is_error": False},
                {"tool_use_id": "toolu_a", "content": "file body", "is_error": False},
            ],
        },
    )
    events, _ = extract_turn_events([msg])
    trs = [e for e in events if e["type"] == "tool_result"]
    by_id = {e["id"]: e for e in trs}
    assert by_id["toolu_a"]["name"] == "Read"
    assert by_id["toolu_b"]["name"] == "Edit"


def test_tool_result_falls_back_to_record_name_when_lookup_misses():
    """Defensive: if the tool_use_id doesn't match any known tool_call
    (e.g. truncation, out-of-order delivery), fall back to whatever
    ``name`` field the record itself carries (non-streaming shape).
    Worst case ``name=""`` rather than raising."""
    msg = AIMessage(
        content="",
        response_metadata={
            "internal_tool_calls": [
                {"id": "toolu_1", "name": "memory_store", "args": {}},
            ],
            "tool_results": [
                {"tool_use_id": "orphan_id", "name": "fallback_name",
                 "content": "x", "is_error": False},
            ],
        },
    )
    events, _ = extract_turn_events([msg])
    tr = next(e for e in events if e["type"] == "tool_result")
    assert tr["id"] == "orphan_id"
    assert tr["name"] == "fallback_name"


def test_tool_message_emits_tool_result():
    msgs = [
        AIMessage(
            content="checking",
            tool_calls=[{"id": "tc_1", "name": "memory_query", "args": {}}],
        ),
        ToolMessage(content="hit_count=3", tool_call_id="tc_1", name="memory_query"),
    ]
    events, _ = extract_turn_events(msgs)
    tr = next(e for e in events if e["type"] == "tool_result")
    assert tr["id"] == "tc_1"
    assert tr["name"] == "memory_query"
    assert tr["content"] == "hit_count=3"
    assert tr["is_error"] is False


def test_tool_message_error_status_flagged():
    msg = ToolMessage(content="boom", tool_call_id="tc_x", name="bad", status="error")
    events, _ = extract_turn_events([msg])
    assert events[0]["is_error"] is True


def test_oversized_tool_result_truncated():
    body = "x" * 100_000
    msg = ToolMessage(content=body, tool_call_id="tc_x", name="big")
    events, _ = extract_turn_events([msg])
    assert events[0]["content"].endswith("…[truncated]")
    assert len(events[0]["content"]) < len(body)


# ── derive_result_fields ─────────────────────────────────────────────


def test_derive_with_no_messages_returns_all_none():
    rf = derive_result_fields([])
    for k in (
        "result_subtype", "result_is_error", "stop_reason",
        "num_turns", "total_cost_usd", "usage",
    ):
        assert rf[k] is None


def test_derive_aggregates_usage_metadata_across_ai_messages():
    msg1 = AIMessage(
        content="a",
        usage_metadata={
            "input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
            "input_token_details": {"cache_read": 30, "cache_creation": 0},
        },
    )
    msg2 = AIMessage(
        content="b",
        usage_metadata={
            "input_tokens": 200, "output_tokens": 40, "total_tokens": 240,
            "input_token_details": {"cache_read": 10, "cache_creation": 5},
        },
    )
    rf = derive_result_fields([msg1, msg2])
    assert rf["usage"]["input_tokens"] == 300
    assert rf["usage"]["output_tokens"] == 60
    assert rf["usage"]["cache_read_input_tokens"] == 40
    assert rf["usage"]["cache_creation_input_tokens"] == 5
    assert rf["num_turns"] == 2


def test_derive_picks_up_chat_claude_code_result_metadata():
    # ChatClaudeCode mirrors claude_agent_sdk's ResultMessage into
    # response_metadata. Make sure we surface what's there.
    msg = AIMessage(
        content="done",
        response_metadata={
            "total_cost_usd": 0.0123,
            "num_turns": 4,
            "is_error": False,
            "usage": {"input_tokens": 5000, "output_tokens": 80},
        },
    )
    rf = derive_result_fields([msg])
    assert rf["total_cost_usd"] == pytest.approx(0.0123)
    assert rf["num_turns"] == 4
    assert rf["result_is_error"] is False
    assert rf["usage"]["input_tokens"] == 5000


def test_derive_marks_max_turns_as_error_subtype():
    msg = AIMessage(content="halted", response_metadata={"stop_reason": "max_turns"})
    rf = derive_result_fields([msg])
    assert rf["result_subtype"] == "error_max_turns"
    assert rf["result_is_error"] is True
    assert rf["stop_reason"] == "max_turns"


# ── TurnLogger / helpers ─────────────────────────────────────────────


def test_truncate_input_returns_string():
    long = "x" * 50_000
    out = truncate_input(long)
    assert isinstance(out, str)
    assert len(out) < len(long) or len(out) == len(long)  # length policy lives in module


def test_make_turn_id_unique_and_shaped():
    ids = {make_turn_id() for _ in range(100)}
    assert len(ids) == 100  # collision-free
    assert all(isinstance(t, str) and len(t) >= 8 for t in ids)


async def test_turn_logger_writes_appendable_jsonl(tmp_path: Path):
    log = TurnLogger(tmp_path / "turns.jsonl")
    record = TurnRecord(
        ts="2026-05-15T12:00:00Z",
        turn_id="t1",
        session_id="ch-1",
        saga_session_id=None,
        trigger="user_message",
        channel_id="ch-1",
        input="hi",
        output="hello",
        events=[{"type": "reasoning", "content": "thinking"}],
        duration_ms=42,
    )
    await log.write(record)
    await log.write(record)
    lines = (tmp_path / "turns.jsonl").read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["turn_id"] == "t1"
    assert first["events"][0]["type"] == "reasoning"
