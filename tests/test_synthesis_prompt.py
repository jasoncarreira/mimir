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
    _session_has_atoms,
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
    # Synthesis prompt for two trivial turns must be small. Threshold
    # bumped from 5000 → 6500 when Step 1b (saga_store discipline) added
    # ~900 chars to both templates 2026-05-10 — still bounded against
    # the cubic-blowup failure mode this test is guarding.
    assert len(out) < 6500


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


def test_render_mentions_learnings_pending_buffer():
    """Synthesis prompt step #1 must point at memory/learnings-pending.md
    as the capture path for candidate learned behaviors, with an explicit
    do-not-write-to-core-40 safety note. Reflection (weekly) is the only
    autonomous writer of memory/core/40-learned-behaviors.md per
    memory/core/30-reflection-policy.md — synthesis turns capture
    candidates here for reflection to promote."""
    out = render_saga_session_end(
        channel_id="c",
        saga_session_id="s",
        idle_minutes=10,
        turns_window=[_turn("t1")],
        prompts_dir=None,
    )
    assert "memory/learnings-pending.md" in out
    # Safety: prompt must steer synthesis turns away from direct core writes.
    # (Match in flattened-whitespace form so the assertion survives template
    # line wraps around "Do NOT write directly to ...".)
    assert "40-learned-behaviors.md" in out
    flat = " ".join(out.split())
    assert "Do NOT write directly to" in flat
    assert "memory/core/40-learned-behaviors.md" in flat


def test_render_full_template_includes_saga_store_step_1b():
    """Full synthesis template must contain Step 1b (SAGA atom storage
    discipline) with the streams taxonomy + do-NOT list. Without this
    section the synthesis turn never reaches for saga_store; the empirical
    data showed 4 non-boundary raw atoms across a 6-day window before
    this prompt landed."""
    turns = [_turn("t1", saga_atom_ids=["atom-1"])]  # force full template
    out = render_saga_session_end(
        channel_id="c", saga_session_id="s", idle_minutes=10,
        turns_window=turns, prompts_dir=None,
    )
    assert "1b. Store SAGA atoms" in out
    # Streams taxonomy present.
    assert "semantic" in out
    assert "episodic" in out
    assert "procedural" in out
    # Do-NOT list present (the load-bearing protection against
    # workflow-state / session-retell pollution of the atom layer).
    flat = " ".join(out.split())
    assert "Do NOT store" in flat
    assert "Meta-observations" in flat
    assert "Self-state claims" in flat
    # saga_store actually mentioned (the original gap was that the
    # synthesis prompt never named the tool).
    assert "saga_store" in out


def test_render_lean_template_includes_saga_store_step_1b():
    """Lean variant must also have Step 1b — sessions with zero atom
    citations can still surface durable semantic facts worth storing."""
    turns = [_turn("t1", saga_atom_ids=[]), _turn("t2", saga_atom_ids=[])]
    out = render_saga_session_end(
        channel_id="c", saga_session_id="s", idle_minutes=10,
        turns_window=turns, prompts_dir=None,
    )
    # Confirm we're hitting the lean template (Step 2 is the boundary,
    # not Step 3 as in the full variant — chainlink #7 lean shape).
    assert "Do two things" in out
    assert "1b. Store SAGA atoms" in out
    assert "saga_store" in out
    # Lean variant still drops the dedicated atom-scoring step.
    assert "Score SAGA atoms" not in out


def test_render_handles_empty_turns_window():
    """Empty turns → no atoms cited → chainlink #7 lean template wins.
    The lean template doesn't have the atoms-cited section at all
    (that's the cost-saving point), so we assert on the lean shape."""
    out = render_saga_session_end(
        channel_id="c",
        saga_session_id="s",
        idle_minutes=10,
        turns_window=[],
        prompts_dir=None,
    )
    assert "(no turns recorded for this session)" in out
    # Lean template: no Atoms-cited section.
    assert "Atoms cited across the session" not in out
    # Lean template: step 2 is the boundary record (renumbered from 3).
    assert "Score SAGA atoms" not in out
    assert "Record the session boundary" in out
    assert "Do two things" in out


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


# ── _filter_session_turns (CR#4) ─────────────────────────────────────


def test_filter_session_turns_returns_matching_rows(tmp_path: Path):
    from mimir.agent import _filter_session_turns

    path = tmp_path / "turns.jsonl"
    path.write_text(
        json.dumps({"turn_id": "a", "saga_session_id": "S1", "output": "x"}) + "\n"
        + json.dumps({"turn_id": "b", "saga_session_id": "S2", "output": "y"}) + "\n"
        + json.dumps({"turn_id": "c", "saga_session_id": "S1", "output": "z"}) + "\n",
        encoding="utf-8",
    )

    rows = _filter_session_turns(path, "S1")
    assert [r["turn_id"] for r in rows] == ["a", "c"]


def test_filter_session_turns_handles_missing_file(tmp_path: Path):
    from mimir.agent import _filter_session_turns

    assert _filter_session_turns(tmp_path / "nope.jsonl", "S1") == []


def test_filter_session_turns_handles_interleaved_sessions(tmp_path: Path):
    """Regression for PR #105 review (mimir-carreira): the previous
    200-record streak heuristic could silently drop older session
    turns when many other-session turns interleaved between matches.
    The replacement time-based break uses ``idle_minutes`` as the
    cap — sessions are bounded by saga's idle policy, so any record
    older than ``newest_match - 2*idle_minutes`` cannot belong to
    the target session.

    Constructs a fixture where the target session's turns straddle
    300 other-session turns (well over the old 200 streak threshold)
    BUT all within the 2*idle_minutes time window. The new logic
    must return both target turns; the old logic would have dropped
    the older one.
    """
    from datetime import datetime, timedelta, timezone
    from mimir.agent import _filter_session_turns

    base = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    path = tmp_path / "turns.jsonl"
    lines: list[str] = []
    # Oldest target turn — 90 minutes ago. Inside 2 * 60min = 120min window.
    lines.append(json.dumps({
        "turn_id": "target_old",
        "saga_session_id": "S1",
        "timestamp": (base - timedelta(minutes=90)).isoformat(),
    }))
    # 300 interleaved other-session turns at 1-minute intervals,
    # spanning the 90-min-to-5-min-ago range.
    for i in range(300):
        offset_min = 90 - (i + 1) * (85 / 300)  # 89.7 → 5.0 min ago
        lines.append(json.dumps({
            "turn_id": f"other_{i}",
            "saga_session_id": f"S_other_{i}",
            "timestamp": (base - timedelta(minutes=offset_min)).isoformat(),
        }))
    # Newest target turn — 1 min ago.
    lines.append(json.dumps({
        "turn_id": "target_new",
        "saga_session_id": "S1",
        "timestamp": (base - timedelta(minutes=1)).isoformat(),
    }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rows = _filter_session_turns(path, "S1", idle_minutes=60)
    # Both target turns survive — chronological order. The old streak
    # heuristic would have stopped after 200 non-matches and missed
    # ``target_old``.
    assert [r["turn_id"] for r in rows] == ["target_old", "target_new"]


def test_filter_session_turns_break_respects_idle_minutes_margin(tmp_path: Path):
    """Time-based break bounds the walk to ``2 * idle_minutes`` past
    the latest match. A non-target record older than that bound IS
    safely past the session boundary — we stop there to avoid
    unbounded walks on a hot file.

    Fixture: target session at the tail; an unrelated old record
    well outside the margin. The walk must NOT include the old
    record (proves the break fires) AND must include all target
    records (proves the margin is wide enough)."""
    from datetime import datetime, timedelta, timezone
    from mimir.agent import _filter_session_turns

    base = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    path = tmp_path / "turns.jsonl"
    lines: list[str] = []
    # Far-old non-match (3 hours ago). Beyond 2*60min margin.
    lines.append(json.dumps({
        "turn_id": "old_other",
        "saga_session_id": "S_other",
        "timestamp": (base - timedelta(hours=3)).isoformat(),
    }))
    # Target session — last 30 minutes.
    for i in range(3):
        lines.append(json.dumps({
            "turn_id": f"target_{i}",
            "saga_session_id": "S1",
            "timestamp": (base - timedelta(minutes=30 - i * 10)).isoformat(),
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rows = _filter_session_turns(path, "S1", idle_minutes=60)
    assert [r["turn_id"] for r in rows] == ["target_0", "target_1", "target_2"]


def test_filter_session_turns_skips_malformed_lines(tmp_path: Path):
    """Defensive: bad JSON lines are skipped, not crashed on."""
    from mimir.agent import _filter_session_turns

    path = tmp_path / "turns.jsonl"
    path.write_text(
        "not json\n"
        + json.dumps({"turn_id": "a", "saga_session_id": "S1"}) + "\n"
        + "{bad\n"
        + json.dumps({"turn_id": "b", "saga_session_id": "S1"}) + "\n",
        encoding="utf-8",
    )

    rows = _filter_session_turns(path, "S1")
    assert [r["turn_id"] for r in rows] == ["a", "b"]


@pytest.mark.asyncio
async def test_build_synthesis_prompt_is_async():
    """CR#4: ``_build_synthesis_prompt`` must be a coroutine function so
    the synchronous turns.jsonl read inside it can be awaited via
    ``asyncio.to_thread`` — keeping the event loop unblocked during
    session-end synthesis under MIMIR_MAX_TURNS=1000 / 50MB worst case."""
    import inspect
    from mimir.agent import Agent

    assert inspect.iscoroutinefunction(Agent._build_synthesis_prompt), (
        "_build_synthesis_prompt must be async so its turns.jsonl read "
        "runs off the event loop (CR#4)."
    )


# ── chainlink #7: lean vs full template selection ────────────────────


def test_session_has_atoms_detects_any_cite():
    assert _session_has_atoms([_turn("a", saga_atom_ids=["atom-1"])]) is True
    # Mixed: some atoms, some none → True (any).
    assert _session_has_atoms([
        _turn("a", saga_atom_ids=[]),
        _turn("b", saga_atom_ids=["atom-1"]),
    ]) is True
    # All empty.
    assert _session_has_atoms([_turn("a", saga_atom_ids=[])]) is False
    # Missing field entirely (treat as empty).
    assert _session_has_atoms([{"turn_id": "a"}]) is False
    # Empty window.
    assert _session_has_atoms([]) is False


def test_render_uses_lean_template_when_no_atoms_cited():
    """Sessions with zero atom citations across all turns → lean
    template (drops step 2 + the trailing atoms-cited block) per
    chainlink #7. Saves $1-3 per bookkeeping turn."""
    turns = [
        _turn("t1", saga_atom_ids=[]),
        _turn("t2", saga_atom_ids=[]),
    ]
    out = render_saga_session_end(
        channel_id="c", saga_session_id="s", idle_minutes=10,
        turns_window=turns, prompts_dir=None,
    )
    # Lean shape: 2 steps total (memory capture + boundary record).
    assert "Do two things" in out
    assert "Score SAGA atoms" not in out
    assert "Atoms cited across the session" not in out
    # Memory capture (step 1) still present — the valuable part.
    assert "Capture memories worth keeping" in out
    # Boundary record (step 2) still present.
    assert "Record the session boundary" in out
    assert "saga_end_session" in out


def test_render_uses_full_template_when_any_atoms_cited():
    """Even one atom citation across the session keeps the full
    template — the contribution-credit pass is valuable when there's
    something to credit."""
    turns = [
        _turn("t1", saga_atom_ids=[]),
        _turn("t2", saga_atom_ids=["atom-1"]),
    ]
    out = render_saga_session_end(
        channel_id="c", saga_session_id="s", idle_minutes=10,
        turns_window=turns, prompts_dir=None,
    )
    assert "Do three things" in out
    assert "Score SAGA atoms" in out
    assert "Atoms cited across the session" in out


def test_render_lean_template_overridable_via_prompts_dir(tmp_path: Path):
    """An operator override at ``<prompts_dir>/saga_session_end_lean.md``
    is respected. Confirms the lean template uses ``saga_session_end_lean``
    as its load_template name (parallel to ``saga_session_end``)."""
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "saga_session_end_lean.md").write_text(
        "lean override for {channel_id}: {saga_session_id} "
        "({idle_minutes}m)\n{turn_summary_block}",
        encoding="utf-8",
    )
    turns = [_turn("t1", saga_atom_ids=[])]
    out = render_saga_session_end(
        channel_id="c", saga_session_id="s", idle_minutes=10,
        turns_window=turns, prompts_dir=prompts,
    )
    assert out.startswith("lean override for c: s (10m)")
    # Confirm: the override doesn't have the lean-default's "Do two
    # things" header — proves we're rendering the override.
    assert "Do two things" not in out


def test_render_full_template_override_preserved_when_atoms_present(tmp_path: Path):
    """Existing ``<prompts_dir>/saga_session_end.md`` overrides
    behavior is unchanged when atoms ARE present — chainlink #7 is
    purely additive (a new lean code path), not a rename of the
    full path."""
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "saga_session_end.md").write_text(
        "full override for {channel_id}: {saga_session_id} "
        "({idle_minutes}m)\n{turn_summary_block}\n{atom_feedback_block}",
        encoding="utf-8",
    )
    turns = [_turn("t1", saga_atom_ids=["atom-1"])]
    out = render_saga_session_end(
        channel_id="c", saga_session_id="s", idle_minutes=10,
        turns_window=turns, prompts_dir=prompts,
    )
    assert out.startswith("full override for c: s (10m)")
