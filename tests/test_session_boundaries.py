"""v0.4 §3a: session boundary surfacing.

Local mirror append/read, render_session_summaries layout, and the
agent-level fallback path (MSAM empty → mirror)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from mimir.session_boundary_log import (
    SessionBoundaryLog,
    render_session_summaries,
)


# ---- SessionBoundaryLog: append + read ---------------------------------


@pytest.mark.asyncio
async def test_append_creates_file_and_writes_record(tmp_path: Path):
    path = tmp_path / ".mimir" / "session_boundaries.jsonl"
    log = SessionBoundaryLog(path=path)
    await log.append(
        {
            "channel_id": "slack-eng",
            "msam_session_id": "msam-slack-eng-1",
            "atom_id": "atom-1",
            "summary": "Helped Alice debug deploy.",
            "unfinished": ["heap config Monday"],
        }
    )
    assert path.is_file()
    body = path.read_text()
    rec = json.loads(body.splitlines()[0])
    assert rec["channel_id"] == "slack-eng"
    assert rec["summary"] == "Helped Alice debug deploy."
    # ``ts`` is auto-stamped by append at write time.
    assert "ts" in rec


@pytest.mark.asyncio
async def test_recent_returns_reverse_chronological(tmp_path: Path):
    log = SessionBoundaryLog(path=tmp_path / ".mimir" / "sb.jsonl")
    await log.append({"channel_id": "c", "summary": "first"})
    await log.append({"channel_id": "c", "summary": "second"})
    await log.append({"channel_id": "c", "summary": "third"})

    out = log.recent(count=2)
    assert [r["summary"] for r in out] == ["third", "second"]


@pytest.mark.asyncio
async def test_recent_filters_by_channel(tmp_path: Path):
    log = SessionBoundaryLog(path=tmp_path / ".mimir" / "sb.jsonl")
    await log.append({"channel_id": "slack-eng", "summary": "A"})
    await log.append({"channel_id": "discord-99", "summary": "B"})
    await log.append({"channel_id": "slack-eng", "summary": "C"})

    out = log.recent(channel_id="slack-eng", count=5)
    assert [r["summary"] for r in out] == ["C", "A"]


def test_recent_empty_when_file_missing(tmp_path: Path):
    log = SessionBoundaryLog(path=tmp_path / "no" / "such" / "file.jsonl")
    assert log.recent() == []


# ---- render_session_summaries -----------------------------------------


def test_render_returns_none_for_empty():
    assert render_session_summaries([]) is None


def test_render_basic_layout():
    boundaries = [
        {
            "ts": "2026-04-29T14:02:00+00:00",
            "channel_id": "slack-eng",
            "summary": "Helped Alice debug the deploy migration.",
            "unfinished": ["heap config Monday", "verify rollback"],
        }
    ]
    out = render_session_summaries(boundaries)
    assert out is not None
    assert "2026-04-29 14:02 (slack-eng) — Helped Alice debug" in out
    assert "Unfinished: heap config Monday; verify rollback" in out


def test_render_omits_unfinished_when_empty():
    boundaries = [
        {
            "ts": "2026-04-29T14:00:00+00:00",
            "channel_id": "slack-eng",
            "summary": "Routine sync.",
            "unfinished": [],
        }
    ]
    out = render_session_summaries(boundaries)
    assert out is not None
    assert "Unfinished" not in out


def test_render_collapses_summary_newlines():
    boundaries = [
        {
            "ts": "2026-04-29T14:00:00+00:00",
            "channel_id": "x",
            "summary": "Multi-line\nsummary\nwith breaks.",
        }
    ]
    out = render_session_summaries(boundaries)
    assert "\nMulti-line\nsummary" not in (out or "")
    assert "Multi-line summary with breaks." in (out or "")


def test_render_handles_missing_fields_gracefully():
    boundaries = [{"summary": "no metadata here"}]
    out = render_session_summaries(boundaries)
    assert out is not None
    assert "no metadata here" in out
    # Channel placeholder + no timestamp prefix.
    assert "(-)" in out


# ---- prompt assembly integration ---------------------------------------


def test_build_turn_prompt_renders_session_summaries_section():
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    block = render_session_summaries(
        [{"ts": "2026-04-29T14:02:00+00:00", "channel_id": "slack-eng",
          "summary": "Helped Alice debug deploy.",
          "unfinished": ["heap config Monday"]}]
    )
    prompt = build_turn_prompt(
        AgentEvent(trigger="user_message", channel_id="slack-eng",
                   author="alice", content="hi"),
        session_summaries_block=block,
    )
    assert "## Recent session summaries" in prompt
    assert "Helped Alice debug deploy." in prompt
    assert "Unfinished: heap config Monday" in prompt


def test_build_turn_prompt_omits_session_section_when_block_none():
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    prompt = build_turn_prompt(
        AgentEvent(trigger="user_message", channel_id="slack-eng",
                   author="alice", content="hi"),
        session_summaries_block=None,
    )
    assert "Recent session summaries" not in prompt
