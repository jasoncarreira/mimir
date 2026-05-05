"""Tests for SYNTHESIS_AND_BUDGET_FIXES.md change 1 — synthesis turn
passes turn-id summaries instead of JSON-dumped transcripts, with a
``mimir_get_turn`` MCP tool for selective lookup."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mimir.templates import (
    _atom_feedback_lines,
    _output_preview,
    _turn_summary_lines,
    render_saga_session_end,
)
from mimir.turntools import build_turn_tools


# ── render_saga_session_end ──────────────────────────────────────────


def _turn(turn_id: str, **kw) -> dict:
    base = {
        "turn_id": turn_id,
        "trigger": "user_message",
        "input": "",
        "output": "",
        "events": [],
        "saga_atom_ids": [],
        "total_cost_usd": None,
    }
    base.update(kw)
    return base


def test_render_saga_session_end_excludes_inputs():
    """Even a turns_window with massive `input` fields renders to a
    bounded prompt — the input field is the source of the cubic blowup
    and must NOT appear in the synthesis prompt."""
    huge_input = "x" * 30000
    turns = [_turn("t1", input=huge_input, output="reply 1"),
             _turn("t2", input=huge_input, output="reply 2")]
    out = render_saga_session_end(
        channel_id="c",
        saga_session_id="s",
        idle_minutes=10,
        turns_window=turns,
        prompts_dir=None,
    )
    assert "x" * 30000 not in out
    # Synthesis prompt for two trivial turns must be small (well under
    # 5k chars even on a long template).
    assert len(out) < 5000


def test_render_saga_session_end_lists_turn_ids():
    turns = [_turn("abc123"), _turn("def456")]
    out = render_saga_session_end(
        channel_id="c",
        saga_session_id="s",
        idle_minutes=10,
        turns_window=turns,
        prompts_dir=None,
    )
    assert "abc123" in out
    assert "def456" in out


def test_atom_feedback_groups_citations():
    """Atoms cited in multiple turns appear once with all citing
    turn_ids listed; per-turn dedup is preserved."""
    turns = [
        _turn("t1", saga_atom_ids=["a1", "a2"]),
        _turn("t2", saga_atom_ids=["a2", "a3"]),
    ]
    rendered = _atom_feedback_lines(turns)
    # a1 cited once
    assert "a1: cited in turn(s) t1" in rendered
    # a2 cited in both — single line lists both turn_ids
    assert "a2: cited in turn(s) t1, t2" in rendered
    # a3 cited once
    assert "a3: cited in turn(s) t2" in rendered


def test_atom_feedback_handles_empty():
    assert "no atoms" in _atom_feedback_lines([])
    assert "no atoms" in _atom_feedback_lines([_turn("t1", saga_atom_ids=[])])


def test_turn_summary_emits_cost_and_tool_call_count():
    turns = [
        _turn(
            "t1",
            trigger="user_message",
            total_cost_usd=0.073,
            events=[
                {"type": "tool_call", "name": "Read"},
                {"type": "tool_result"},
                {"type": "tool_call", "name": "Write"},
            ],
            output="hello",
        )
    ]
    line = _turn_summary_lines(turns)
    assert "$0.073" in line
    # Two tool_call events; tool_result doesn't count.
    assert "2 tool calls" in line
    assert "user_message" in line


def test_turn_summary_handles_missing_cost():
    turns = [_turn("t1")]
    line = _turn_summary_lines(turns)
    assert "$?" in line


def test_output_preview_truncates_long_text():
    out = _output_preview("x" * 5000)
    # Cap is 200 chars + ellipsis suffix mentioning the original length
    assert "5000 chars total" in out
    assert len(out) < 300


def test_output_preview_collapses_whitespace():
    assert _output_preview("foo\n\n\tbar   baz") == "foo bar baz"


def test_output_preview_empty():
    assert _output_preview("") == "(empty)"


def test_synthesis_prompt_under_50k_for_long_session():
    """20-turn session with realistic-sized turn fields should render
    well under the 50k-char cap (i.e. a few k tokens) — proves the
    prompt no longer scales with the embedded transcripts."""
    turns = [
        _turn(
            f"turn-{i}",
            input="x" * 20000,  # 20k-char rendered prompts (typical mimir size)
            output=f"reply {i} " * 50,
            events=[
                {"type": "tool_call", "name": "Read"},
                {"type": "tool_result"},
            ] * 3,
            saga_atom_ids=[f"a{i}-1", f"a{i}-2"],
            total_cost_usd=0.05 + i * 0.01,
        )
        for i in range(20)
    ]
    out = render_saga_session_end(
        channel_id="c",
        saga_session_id="s",
        idle_minutes=10,
        turns_window=turns,
        prompts_dir=None,
    )
    assert len(out) < 50000


def test_render_handles_empty_turns_window():
    out = render_saga_session_end(
        channel_id="c",
        saga_session_id="s",
        idle_minutes=10,
        turns_window=[],
        prompts_dir=None,
    )
    assert "(no turns recorded for this session)" in out
    assert "(no atoms cited in this session)" in out


# ── mimir_get_turn ───────────────────────────────────────────────────


@pytest.fixture
def turns_log(tmp_path: Path) -> Path:
    path = tmp_path / "turns.jsonl"
    rows = [
        {
            "turn_id": "t1",
            "trigger": "user_message",
            "input": "the rendered prompt for t1",
            "output": "t1 output",
            "events": [{"type": "tool_call", "name": "Read"}],
            "saga_atom_ids": ["a1"],
            "usage": {"input_tokens": 100},
        },
        {
            "turn_id": "t2",
            "trigger": "scheduled_tick",
            "input": "the rendered prompt for t2",
            "output": "t2 output",
            "events": [],
            "saga_atom_ids": [],
            "usage": {"input_tokens": 50},
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_get_turn_returns_only_output_and_events(turns_log: Path):
    [tool_obj] = build_turn_tools(turns_log)
    result = await tool_obj.handler({"turn_id": "t1"})
    text = result["content"][0]["text"]
    payload = json.loads(text)
    assert payload["turn_id"] == "t1"
    assert payload["output"] == "t1 output"
    assert payload["events"] == [{"type": "tool_call", "name": "Read"}]
    assert payload["trigger"] == "user_message"
    # `input` MUST be stripped — that's the cubic-blowup field.
    assert "input" not in payload
    assert "usage" not in payload
    assert "saga_atom_ids" not in payload


@pytest.mark.asyncio
async def test_get_turn_unknown_id_returns_error(turns_log: Path):
    [tool_obj] = build_turn_tools(turns_log)
    result = await tool_obj.handler({"turn_id": "nope"})
    assert result.get("is_error") is True
    text = result["content"][0]["text"]
    assert "no turn found" in text
    assert "'nope'" in text


@pytest.mark.asyncio
async def test_get_turn_missing_log_returns_error(tmp_path: Path):
    [tool_obj] = build_turn_tools(tmp_path / "nonexistent.jsonl")
    result = await tool_obj.handler({"turn_id": "t1"})
    assert result.get("is_error") is True
    assert "turns log not found" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_get_turn_requires_turn_id(turns_log: Path):
    [tool_obj] = build_turn_tools(turns_log)
    result = await tool_obj.handler({})
    assert result.get("is_error") is True
    assert "required" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_get_turn_skips_malformed_lines(tmp_path: Path):
    """Malformed JSON lines in turns.jsonl don't blow up the lookup —
    we just skip them and keep scanning."""
    path = tmp_path / "turns.jsonl"
    path.write_text(
        "not json\n"
        + json.dumps({"turn_id": "t1", "output": "found", "events": []}) + "\n"
        + "{bad}\n",
        encoding="utf-8",
    )
    [tool_obj] = build_turn_tools(path)
    result = await tool_obj.handler({"turn_id": "t1"})
    assert "is_error" not in result
    text = result["content"][0]["text"]
    assert "found" in text
