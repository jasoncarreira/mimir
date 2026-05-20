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
    _coerce_content,
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


# ── hooks-based tool_events path (preferred when present) ───────────


def test_tool_events_path_preserves_interleaved_order():
    """``response_metadata["tool_events"]`` (populated by the hooks
    patch in ``_langchain_claude_code_patches.py``) carries an ALREADY-
    interleaved list of call→result→call→result events. ``extract_turn_events``
    must walk it in order and produce matching events — not bunch
    calls before results like the legacy ``internal_tool_calls`` /
    ``internal_tool_results`` split path does."""
    msg = AIMessage(
        content="Did two things.",
        response_metadata={
            "tool_events": [
                {"type": "tool_call", "tool_use_id": "toolu_a",
                 "name": "Read", "input": {"file_path": "/x"}},
                {"type": "tool_result", "tool_use_id": "toolu_a",
                 "name": "Read", "result": "contents-of-x",
                 "is_error": False},
                {"type": "tool_call", "tool_use_id": "toolu_b",
                 "name": "Bash", "input": {"command": "ls"}},
                {"type": "tool_result", "tool_use_id": "toolu_b",
                 "name": "Bash", "result": {"output": "a\nb"},
                 "is_error": False},
            ],
        },
    )
    events, output = extract_turn_events([msg])
    tool_event_types = [e["type"] for e in events if e["type"] != "reasoning"]
    # Interleaved order preserved, NOT bunched.
    assert tool_event_types == [
        "tool_call", "tool_result", "tool_call", "tool_result",
    ]
    # IDs/names paired correctly.
    by_id = {e["id"]: e for e in events if e["type"] == "tool_result"}
    assert by_id["toolu_a"]["name"] == "Read"
    assert "contents-of-x" in by_id["toolu_a"]["content"]
    assert by_id["toolu_b"]["name"] == "Bash"
    assert by_id["toolu_b"]["is_error"] is False
    # Final AIMessage's content goes to output.
    assert "Did two things" in output


def test_tool_events_path_captures_built_in_tool_results():
    """The hooks path is the only one that surfaces built-in tool
    results (Bash/Read/Edit/Write/Glob/ToolSearch). Pre-fix mimirbot
    turns showed 60 Bash/Read calls with 0 results because UserMessage
    ToolResultBlocks were dropped by langchain-claude-code. The
    tool_events list — populated by ``PostToolUse`` hook regardless of
    tool origin — is the audit-trail fix."""
    msg = AIMessage(
        content="",
        response_metadata={
            "tool_events": [
                {"type": "tool_call", "tool_use_id": "toolu_bash1",
                 "name": "Bash", "input": {"command": "echo hi"}},
                {"type": "tool_result", "tool_use_id": "toolu_bash1",
                 "name": "Bash", "result": {"output": "hi\n"},
                 "is_error": False},
            ],
        },
    )
    events, _ = extract_turn_events([msg])
    tcs = [e for e in events if e["type"] == "tool_call"]
    trs = [e for e in events if e["type"] == "tool_result"]
    assert len(tcs) == 1
    assert len(trs) == 1  # ← this is what was 0 before the hooks patch
    assert tcs[0]["name"] == "Bash"
    assert trs[0]["name"] == "Bash"
    assert tcs[0]["id"] == trs[0]["id"] == "toolu_bash1"


def test_tool_events_path_preserves_is_error_for_failures():
    """``PostToolUseFailure`` hook records produce events with
    ``is_error=True`` and an ``error`` string instead of ``result``.
    ``extract_turn_events`` must surface that as a tool_result with
    is_error=True; the body content comes from the error string."""
    msg = AIMessage(
        content="",
        response_metadata={
            "tool_events": [
                {"type": "tool_call", "tool_use_id": "toolu_fail",
                 "name": "Bash", "input": {"command": "/bin/false"}},
                {"type": "tool_result", "tool_use_id": "toolu_fail",
                 "name": "Bash", "error": "exited with status 1",
                 "is_error": True},
            ],
        },
    )
    events, _ = extract_turn_events([msg])
    trs = [e for e in events if e["type"] == "tool_result"]
    assert len(trs) == 1
    assert trs[0]["is_error"] is True
    assert "status 1" in trs[0]["content"]


def test_tool_events_path_skips_legacy_internal_tool_calls():
    """When ``tool_events`` is present, the legacy ``internal_tool_calls`` /
    ``internal_tool_results`` keys MUST NOT be walked — otherwise we'd
    double-count every event. The hooks path is authoritative."""
    msg = AIMessage(
        content="Done.",
        response_metadata={
            "tool_events": [
                {"type": "tool_call", "tool_use_id": "toolu_x",
                 "name": "Read", "input": {"file_path": "/y"}},
                {"type": "tool_result", "tool_use_id": "toolu_x",
                 "name": "Read", "result": "y-content", "is_error": False},
            ],
            # Legacy keys also populated — patch must IGNORE these when
            # tool_events is present (they'd duplicate the events).
            "internal_tool_calls": [
                {"id": "toolu_LEGACY", "name": "ShouldNotAppear",
                 "args": {}},
            ],
            "internal_tool_results": [
                {"tool_use_id": "toolu_LEGACY",
                 "content": "should-not-appear", "is_error": False},
            ],
        },
    )
    events, _ = extract_turn_events([msg])
    names = {e.get("name") for e in events if e["type"] != "reasoning"}
    assert "ShouldNotAppear" not in names
    assert names == {"Read"}


def test_legacy_path_still_works_without_tool_events_key():
    """Backward compat: when ``tool_events`` is absent (operator on the
    anthropic-only path, or hooks patch not loaded), the legacy
    ``internal_tool_calls`` + ``internal_tool_results`` parsing remains
    the path. This locks in the fallback so the hooks patch can be
    introduced without breaking anyone."""
    msg = AIMessage(
        content="",
        response_metadata={
            "internal_tool_calls": [
                {"id": "toolu_legacy", "name": "Bash",
                 "args": {"command": "ls"}},
            ],
            "internal_tool_results": [
                {"tool_use_id": "toolu_legacy",
                 "content": "a\nb", "is_error": False},
            ],
        },
    )
    events, _ = extract_turn_events([msg])
    tcs = [e for e in events if e["type"] == "tool_call"]
    trs = [e for e in events if e["type"] == "tool_result"]
    assert len(tcs) == 1 and len(trs) == 1
    assert tcs[0]["name"] == "Bash"
    # #193's reverse-lookup still populates the result name from
    # the matching call.
    assert trs[0]["name"] == "Bash"


def test_tool_events_path_empty_string_result_not_treated_as_falsy():
    """Empty-string tool results (e.g. Bash with no output) must be
    preserved as ``content=""`` rather than falling through to the
    ``error`` field. Regression guard for the ``result or error``
    truthiness trap fixed in chainlink #149 — ``or`` evaluates ``""``
    as falsy and would silently substitute the error text."""
    msg = AIMessage(
        content="",
        response_metadata={
            "tool_events": [
                {"type": "tool_call", "tool_use_id": "toolu_silent",
                 "name": "Bash", "input": {"command": ": # no-op"}},
                {"type": "tool_result", "tool_use_id": "toolu_silent",
                 "name": "Bash",
                 "result": "",           # empty-string — the truthiness trap target
                 "error": "should not appear",
                 "is_error": False},
            ],
        },
    )
    events, _ = extract_turn_events([msg])
    trs = [e for e in events if e["type"] == "tool_result"]
    assert len(trs) == 1
    assert trs[0]["content"] == ""        # empty preserved, not "should not appear"
    assert trs[0]["is_error"] is False


def test_legacy_path_empty_string_content_not_treated_as_falsy():
    """Same truthiness-trap regression guard for the legacy path
    (``internal_tool_results`` with a ``content`` key). An empty-string
    ``content`` must be preserved rather than falling through to
    ``result``."""
    msg = AIMessage(
        content="",
        response_metadata={
            "internal_tool_calls": [
                {"id": "toolu_empty", "name": "Bash",
                 "args": {"command": ": # no-op"}},
            ],
            "internal_tool_results": [
                {"tool_use_id": "toolu_empty",
                 "content": "",            # empty — truthiness trap target
                 "result": "should not appear",
                 "is_error": False},
            ],
        },
    )
    events, _ = extract_turn_events([msg])
    trs = [e for e in events if e["type"] == "tool_result"]
    assert len(trs) == 1
    assert trs[0]["content"] == ""        # empty preserved, not "should not appear"
    assert trs[0]["is_error"] is False


# ── End-to-end native-provider paths ────────────────────────────────
# Coverage for non-claude-code providers (langchain-anthropic /
# langchain-openai / etc., resolved via ``init_chat_model`` in
# ``_resolve_model``). These providers populate ``msg.tool_calls``
# directly and emit ``ToolMessage`` records for results — the
# claude-code-specific ``response_metadata["internal_tool_calls"]``
# / ``["internal_tool_results"]`` paths are no-ops for them. These
# tests verify the audit trail captures everything correctly for
# native shapes too, so the recent claude-code-specific additions
# (PR #193) don't drop coverage for Anthropic / OpenAI deployments.


def test_anthropic_native_full_turn_shape():
    """End-to-end LangGraph-native turn shape (langchain-anthropic
    via ``init_chat_model("anthropic:claude-...")``). The audit trail
    should populate reasoning + tool_call + tool_result + output
    cleanly with no claude-code-specific keys in play."""
    msgs = [
        HumanMessage(content="what was alice's last post"),
        AIMessage(
            content="Let me look that up.",
            tool_calls=[
                {"id": "call_anthropic_1", "name": "memory_query",
                 "args": {"query": "alice last post"}},
            ],
            response_metadata={
                # Anthropic populates ``stop_reason`` for tool_use turns.
                "stop_reason": "tool_use",
                "model_name": "claude-sonnet-4-6",
            },
            usage_metadata={"input_tokens": 120, "output_tokens": 18, "total_tokens": 138},
        ),
        ToolMessage(
            content='{"posts": ["alice posted about boids"]}',
            tool_call_id="call_anthropic_1",
            name="memory_query",
        ),
        AIMessage(
            content="Alice posted about boids.",
            response_metadata={"stop_reason": "end_turn"},
            usage_metadata={"input_tokens": 180, "output_tokens": 8, "total_tokens": 188},
        ),
    ]
    events, output = extract_turn_events(msgs)

    # Final AIMessage is the canonical reply.
    assert output == "Alice posted about boids."

    types = [e["type"] for e in events]
    # Intermediate AIMessage with tool_calls → reasoning + tool_call.
    assert types.count("reasoning") == 1
    assert types.count("tool_call") == 1
    assert types.count("tool_result") == 1

    tc = next(e for e in events if e["type"] == "tool_call")
    assert tc["id"] == "call_anthropic_1"
    assert tc["name"] == "memory_query"

    tr = next(e for e in events if e["type"] == "tool_result")
    # ToolMessage carries name directly — no reverse-lookup needed.
    assert tr["name"] == "memory_query"
    assert tr["id"] == "call_anthropic_1"
    assert "boids" in tr["content"]
    assert tr["is_error"] is False


def test_openai_native_full_turn_shape():
    """OpenAI shape (langchain-openai): ``response_metadata`` carries
    ``finish_reason`` rather than ``stop_reason``; tool_calls again
    live on ``msg.tool_calls`` and results come as ``ToolMessage``.
    The existing ``stop_reason or finish_reason`` fallback handles
    the stop_reason gap."""
    msgs = [
        HumanMessage(content="hi"),
        AIMessage(
            content="checking",
            tool_calls=[
                {"id": "call_openai_1", "name": "memory_store",
                 "args": {"content": "hello", "stream": "semantic"}},
            ],
            response_metadata={
                # OpenAI uses ``finish_reason="tool_calls"`` for tool-use turns.
                "finish_reason": "tool_calls",
                "model_name": "gpt-4o",
            },
            usage_metadata={"input_tokens": 80, "output_tokens": 12, "total_tokens": 92},
        ),
        ToolMessage(
            content='{"stored": true, "atom_id": "abc123"}',
            tool_call_id="call_openai_1",
            name="memory_store",
        ),
        AIMessage(
            content="Stored.",
            response_metadata={"finish_reason": "stop"},
            usage_metadata={"input_tokens": 100, "output_tokens": 1, "total_tokens": 101},
        ),
    ]
    events, output = extract_turn_events(msgs)

    assert output == "Stored."
    tcs = [e for e in events if e["type"] == "tool_call"]
    trs = [e for e in events if e["type"] == "tool_result"]
    assert len(tcs) == 1
    assert len(trs) == 1
    assert tcs[0]["name"] == "memory_store"
    assert trs[0]["name"] == "memory_store"
    assert "abc123" in trs[0]["content"]


def test_native_anthropic_derive_result_fields():
    """Anthropic-shape AIMessage: ``stop_reason`` populated, no
    ``finish_reason`` / ``is_error`` / ``num_turns`` /
    ``total_cost_usd``. derive_result_fields should produce sensible
    defaults: stop_reason captured directly, usage from
    ``usage_metadata``, num_turns falls back to AIMessage count,
    total_cost_usd None, result_is_error False."""
    msg = AIMessage(
        content="done",
        response_metadata={"stop_reason": "end_turn"},
        usage_metadata={
            "input_tokens": 200, "output_tokens": 10, "total_tokens": 210,
            "input_token_details": {"cache_read": 50, "cache_creation": 5},
        },
    )
    rf = derive_result_fields([msg])
    assert rf["stop_reason"] == "end_turn"
    assert rf["result_subtype"] == "success"
    assert rf["result_is_error"] is False
    assert rf["num_turns"] == 1  # falls back to AIMessage count
    assert rf["total_cost_usd"] is None
    assert rf["usage"]["input_tokens"] == 200
    assert rf["usage"]["output_tokens"] == 10
    assert rf["usage"]["cache_read_input_tokens"] == 50
    assert rf["usage"]["cache_creation_input_tokens"] == 5


def test_native_openai_derive_result_fields():
    """OpenAI-shape AIMessage: ``finish_reason`` (not ``stop_reason``).
    The existing ``stop_reason or finish_reason`` or-chain in
    derive_result_fields picks up ``finish_reason`` as the effective
    stop_reason; result_is_error falls back from finish_reason as
    well via PR #193's defense-in-depth path."""
    msg = AIMessage(
        content="done",
        response_metadata={"finish_reason": "stop"},
        usage_metadata={"input_tokens": 50, "output_tokens": 5, "total_tokens": 55},
    )
    rf = derive_result_fields([msg])
    # stop_reason resolved from finish_reason fallback.
    assert rf["stop_reason"] == "stop"
    # finish_reason="stop" → result_is_error=False via PR #193 fallback.
    assert rf["result_is_error"] is False
    assert rf["result_subtype"] == "success"
    assert rf["num_turns"] == 1
    assert rf["usage"]["input_tokens"] == 50


def test_native_provider_path_ignores_missing_internal_tool_results():
    """Defensive: the new ``internal_tool_results`` lookup (PR #193's
    streaming-path fix) is a no-op for native providers — they don't
    populate that key. Verify extract_turn_events doesn't crash and
    doesn't synthesize phantom tool_result events for them."""
    msg = AIMessage(
        content="just text, no tools",
        response_metadata={"stop_reason": "end_turn"},
        usage_metadata={"input_tokens": 10, "output_tokens": 3, "total_tokens": 13},
    )
    events, output = extract_turn_events([msg])
    assert output == "just text, no tools"
    # No tool_result events — neither claude-code nor LangGraph-native
    # path fired.
    assert not any(e["type"] == "tool_result" for e in events)


def test_native_provider_path_with_no_response_metadata():
    """Some langchain providers populate response_metadata sparsely
    (e.g. when streaming via ``ainvoke`` without an upstream-supplied
    metadata dict). extract_turn_events should still handle a None /
    empty response_metadata gracefully."""
    msg = AIMessage(content="hi", response_metadata={})
    events, output = extract_turn_events([msg])
    assert output == "hi"
    assert not events  # no tool calls, no results, no reasoning

    # Also no exceptions when response_metadata is missing entirely.
    msg2 = AIMessage(content="hi2")
    events2, output2 = extract_turn_events([msg2])
    assert output2 == "hi2"
    assert not events2


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


def test_derive_marks_anthropic_max_tokens_as_truncation():
    """Anthropic native: ``response_metadata["stop_reason"] = "max_tokens"``
    when the response hit the per-message token cap. Same shape as
    claude-code SDK's max_tokens — should map to error_max_turns."""
    msg = AIMessage(
        content="cut off mid-",
        response_metadata={"stop_reason": "max_tokens"},
    )
    rf = derive_result_fields([msg])
    assert rf["result_subtype"] == "error_max_turns"
    assert rf["result_is_error"] is True
    assert rf["stop_reason"] == "max_tokens"


def test_derive_marks_openai_length_as_truncation():
    """OpenAI native: ``response_metadata["finish_reason"] = "length"``
    when the model hit max_tokens and the response is truncated. The
    canonical OpenAI signal — semantically identical to Anthropic's
    ``max_tokens`` and claude-code's ``max_turns``. Should map to
    ``result_subtype="error_max_turns"`` and ``result_is_error=True``
    so operators reading bench/audit metrics can distinguish a
    successful end-of-turn reply from a length-truncated one regardless
    of which provider produced the turn."""
    # finish_reason flows through the ``stop_reason or finish_reason``
    # fallback in derive_result_fields, so stop_reason resolves to
    # "length" and the truncation check picks it up.
    msg = AIMessage(
        content="this answer was cut off because",
        response_metadata={"finish_reason": "length"},
        usage_metadata={
            "input_tokens": 500,
            "output_tokens": 4096,  # hit the cap
            "total_tokens": 4596,
        },
    )
    rf = derive_result_fields([msg])
    assert rf["stop_reason"] == "length"
    assert rf["result_subtype"] == "error_max_turns"
    assert rf["result_is_error"] is True


def test_derive_marks_openai_length_via_explicit_stop_reason_field():
    """Symmetric: if a provider happens to populate
    ``stop_reason="length"`` directly (e.g. an OpenAI-compatible
    endpoint that uses langchain-anthropic's key name), the same
    truncation classification applies."""
    msg = AIMessage(
        content="truncated",
        response_metadata={"stop_reason": "length"},
    )
    rf = derive_result_fields([msg])
    assert rf["result_subtype"] == "error_max_turns"
    assert rf["result_is_error"] is True


def test_derive_is_error_falls_back_to_finish_reason_when_streaming():
    """``ChatClaudeCode._astream`` (the path mimir uses) collapses
    ``msg.is_error`` into a binary ``finish_reason`` and never emits
    ``is_error`` as its own response_metadata field. Without a fallback,
    every streaming-mode subprocess error rendered as
    ``result_is_error=False``. The ``enrich_streaming_metadata``
    patch in ``_langchain_claude_code_patches`` is the primary fix
    (restores the original ``is_error``); this test covers the
    defense-in-depth path for deployments where the patch didn't
    apply."""
    # finish_reason="error" with no explicit is_error → recover True
    msg_err = AIMessage(
        content="oops",
        response_metadata={"finish_reason": "error"},
    )
    rf = derive_result_fields([msg_err])
    assert rf["result_is_error"] is True

    # finish_reason="stop" → recover False
    msg_ok = AIMessage(
        content="done",
        response_metadata={"finish_reason": "stop"},
    )
    rf_ok = derive_result_fields([msg_ok])
    assert rf_ok["result_is_error"] is False


def test_derive_is_error_explicit_field_wins_over_finish_reason():
    """When the patch DID apply (or upstream preserves both), the
    explicit ``is_error`` field is authoritative; the finish_reason
    fallback only kicks in when ``is_error`` is None."""
    # Explicit is_error=False overrides finish_reason="error"
    msg = AIMessage(
        content="",
        response_metadata={"is_error": False, "finish_reason": "error"},
    )
    rf = derive_result_fields([msg])
    assert rf["result_is_error"] is False


def test_derive_picks_up_streaming_enriched_metadata():
    """When the ``enrich_streaming_metadata`` patch is active (the
    expected production path for claude-code subprocess turns), the
    result chunk's response_metadata carries the full ResultMessage
    field set: stop_reason / num_turns / is_error preserved alongside
    the binary finish_reason. derive_result_fields should pick these
    up just like the non-streaming path."""
    msg = AIMessage(
        content="done",
        response_metadata={
            # Streaming-shape generation_info as enriched by the patch
            "finish_reason": "stop",
            "total_cost_usd": 0.0042,
            "session_id": "sess-abc",
            # Patched-in fields (from ResultMessage):
            "stop_reason": "end_turn",
            "num_turns": 7,
            "is_error": False,
            "usage": {"input_tokens": 2400, "output_tokens": 180},
        },
    )
    rf = derive_result_fields([msg])
    assert rf["stop_reason"] == "end_turn"
    assert rf["num_turns"] == 7
    assert rf["result_is_error"] is False
    assert rf["result_subtype"] == "success"
    assert rf["total_cost_usd"] == pytest.approx(0.0042)


def test_derive_max_turns_recoverable_via_streaming_patch():
    """End-to-end: when the patch preserves the granular ``stop_reason``
    on a max-turns truncation, derive_result_fields correctly emits
    ``result_subtype="error_max_turns"`` even though the streaming
    finish_reason was just the binary ``"error"``. Without the patch,
    that distinction would be lost (only finish_reason="error"
    survives, which maps to result_subtype=success-but-is_error path)."""
    msg = AIMessage(
        content="truncated",
        response_metadata={
            "finish_reason": "error",       # streaming binary
            "stop_reason": "max_turns",     # preserved by patch
            "is_error": True,               # preserved by patch
            "num_turns": 50,                # preserved by patch
        },
    )
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


# ─── _coerce_content content-block filtering (Anthropic extended thinking) ──


def test_coerce_content_drops_thinking_block_keeps_text():
    """Anthropic-style content list with a thinking block (Anthropic
    extended thinking + Minimax via Anthropic-compat) must produce
    only the visible-text reply. The thinking block's signature +
    reasoning text MUST NOT leak into the output.

    Regression for 2026-05-20 muninn-mimir cutover: a Discord message
    showed up with the literal JSON ``{"signature": "...",
    "thinking": "...", "type": "thinking"}`` JSON-dumped above the
    real reply text. Root cause: ``_coerce_content`` fell back to
    ``json.dumps(item)`` when an item lacked a ``text`` key. The fix
    is an explicit allowlist on the ``type`` field.
    """

    content = [
        {
            "type": "thinking",
            "thinking": "Jason says I'm up. Let me self-check.",
            "signature": "11dd7a398010bbf76252acae6ebfb6ccc01c5c4b535f73db4",
        },
        {"type": "text", "text": "Okay, cutover complete. Quick sanity check:"},
    ]
    out = _coerce_content(content)
    assert out == "Okay, cutover complete. Quick sanity check:"
    # Specifically: NONE of the thinking-block fields should appear.
    assert "thinking" not in out
    assert "signature" not in out
    assert "Jason says" not in out


def test_coerce_content_drops_redacted_thinking_block():
    """Anthropic's tamper-evident redacted-thinking shape — same
    drop semantics as visible thinking."""

    content = [
        {"type": "redacted_thinking", "data": "opaque-base64-here"},
        {"type": "text", "text": "Visible reply."},
    ]
    assert _coerce_content(content) == "Visible reply."


def test_coerce_content_drops_tool_use_and_tool_result_in_content():
    """``extract_turn_events`` walks tool_calls / ToolMessage separately
    and emits its own tool_call / tool_result events. The string we
    build here is for the AIMessage's user-visible OUTPUT only; tool
    structure in content blocks would double-count if we included it."""

    content = [
        {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"cmd": "ls"}},
        {"type": "text", "text": "I ran a command."},
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "result"},
    ]
    assert _coerce_content(content) == "I ran a command."


def test_coerce_content_legacy_no_type_falls_through_as_text():
    """Back-compat: a content block with no ``type`` key but a ``text``
    key (the pre-typed-block LangChain shape) still gets its text
    included. This is the shape some non-Anthropic providers emit."""

    content = [{"text": "legacy text block, no type field"}]
    assert _coerce_content(content) == "legacy text block, no type field"


def test_coerce_content_multiple_text_blocks_joined_by_newline():
    """When the model emits multiple text blocks (e.g., separated by
    a thinking interlude), they're concatenated with a newline — same
    as the prior behavior modulo the thinking drop."""

    content = [
        {"type": "text", "text": "First paragraph."},
        {"type": "thinking", "thinking": "let me elaborate", "signature": "s"},
        {"type": "text", "text": "Second paragraph."},
    ]
    assert _coerce_content(content) == "First paragraph.\nSecond paragraph."


def test_coerce_content_unknown_type_is_silently_dropped():
    """Pre-fix: an unknown ``type`` (e.g. a future provider's new block
    kind) would get JSON-dumped into the output. Post-fix: silently
    skipped. This is the right default — leaking raw internal state
    into a user-visible reply is a worse failure than dropping a
    block we don't know how to render."""

    content = [
        {"type": "future_unknown_block", "data": {"foo": "bar"}},
        {"type": "text", "text": "Visible part."},
    ]
    out = _coerce_content(content)
    assert out == "Visible part."
    assert "future_unknown" not in out
    assert "foo" not in out


def test_thinking_blocks_surface_as_reasoning_events_in_turns_jsonl():
    """The flip side of ``_coerce_content`` dropping thinking blocks
    from user-visible output: ``extract_turn_events`` must capture
    them as ``reasoning`` events with ``source:
    "model_thinking_block"`` so introspection / turn replay can still
    see the model's chain of thought.

    Without this, the thinking content would be DROPPED entirely
    (gone from output, gone from events) and the agent's reasoning
    would be invisible to operators reviewing turns.jsonl.
    """
    msg = AIMessage(content=[
        {
            "type": "thinking",
            "thinking": "Jason says I'm up. Let me self-check the soul doc.",
            "signature": "sig-abc-123",
        },
        {"type": "text", "text": "Okay, cutover complete. Sanity check:"},
    ])
    events, output = extract_turn_events([msg])
    # Output: only the visible text — thinking dropped.
    assert output == "Okay, cutover complete. Sanity check:"
    # Events: the thinking surfaces as a typed reasoning entry.
    assert len(events) >= 1
    thinking_events = [
        e for e in events
        if e.get("type") == "reasoning"
        and e.get("source") == "model_thinking_block"
    ]
    assert len(thinking_events) == 1
    assert thinking_events[0]["content"] == (
        "Jason says I'm up. Let me self-check the soul doc."
    )
    # The signature field MUST NOT appear in the reasoning content —
    # we extract only the ``thinking`` text, not the whole block.
    assert "sig-abc-123" not in thinking_events[0]["content"]


def test_redacted_thinking_blocks_surface_as_reasoning_placeholder():
    """Anthropic's ``redacted_thinking`` blocks are server-encrypted
    base64 — the content is opaque to us. We still emit a reasoning
    event so turns.jsonl records that the model produced reasoning,
    even when we can't see what it was. Pre-fix: dropped silently."""
    msg = AIMessage(content=[
        {"type": "redacted_thinking", "data": "opaque-base64-here"},
        {"type": "text", "text": "Visible reply."},
    ])
    events, output = extract_turn_events([msg])
    assert output == "Visible reply."
    redacted = [
        e for e in events
        if e.get("type") == "reasoning"
        and e.get("source") == "model_thinking_block"
        and "redacted" in e.get("content", "")
    ]
    assert len(redacted) == 1


def test_multiple_thinking_blocks_preserve_order():
    """The model can emit multiple thinking interludes within a single
    turn (e.g., reason → reply paragraph → reason more → reply more).
    Each becomes its own reasoning event, in document order."""
    msg = AIMessage(content=[
        {"type": "thinking", "thinking": "first thought", "signature": "s1"},
        {"type": "text", "text": "First paragraph."},
        {"type": "thinking", "thinking": "second thought", "signature": "s2"},
        {"type": "text", "text": "Second paragraph."},
    ])
    events, output = extract_turn_events([msg])
    assert output == "First paragraph.\nSecond paragraph."
    thinking_texts = [
        e["content"] for e in events
        if e.get("source") == "model_thinking_block"
    ]
    assert thinking_texts == ["first thought", "second thought"]
