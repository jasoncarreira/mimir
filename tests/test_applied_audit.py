"""Tests for §12.2 applied-proposals audit."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from textwrap import dedent

import pytest

from mimir.reflection.applied_audit import (
    AppliedProposal,
    AuditRow,
    Signal,
    audit_window,
    compute_signals,
    load_applied_proposals,
    mark_applied,
    render_audit_block,
)

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)


def _seed_proposed_changes(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(body).lstrip("\n"), encoding="utf-8")


# ─── mark_applied ───────────────────────────────────────────────────────


def test_mark_applied_moves_section_and_appends_log(tmp_path: Path):
    pc = tmp_path / "state" / "proposed-changes.md"
    _seed_proposed_changes(pc, """
        # Proposed Changes

        Pending HITL items.

        ## Pending

        ## 2026-04-12 — split persona block
        Source: reflection 2026-04-12
        Proposal: Split memory/core/00-persona.md.
        Rationale: Block grew past 30 lines.
        Affected: memory/core/00-persona.md
        Predicted effect: Drift indicators would drop.

        ## 2026-04-15 — add wiki lint skill
        Source: reflection 2026-04-15
        Proposal: New skill for wiki orphan detection.
        Rationale: Three orphans found this week.
        Affected: memory/skills/
        Predicted effect: Wiki orphan rate would drop.

        ## Applied

        ## Rejected
    """)
    log = tmp_path / "state" / "applied-proposals.jsonl"

    proposal = mark_applied(pc, log, "split persona", now=NOW)

    assert "split persona" in proposal.id
    assert proposal.applied_at == NOW.isoformat()
    assert proposal.predicted_effect == "Drift indicators would drop."

    # File is rewritten — split persona moved out of Pending into Applied.
    new_body = pc.read_text()
    pending_idx = new_body.find("## Pending")
    applied_idx = new_body.find("## Applied")
    rejected_idx = new_body.find("## Rejected")
    split_idx = new_body.find("split persona block")
    wiki_idx = new_body.find("add wiki lint skill")
    assert pending_idx < applied_idx < rejected_idx
    # split persona block heading must now live under Applied (between
    # applied_idx and rejected_idx).
    assert applied_idx < split_idx < rejected_idx
    # add wiki lint skill stays under Pending.
    assert pending_idx < wiki_idx < applied_idx

    # JSONL log got an entry.
    assert log.is_file()
    records = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    assert len(records) == 1
    assert records[0]["id"] == proposal.id
    assert records[0]["predicted_effect"] == "Drift indicators would drop."


def test_mark_applied_raises_when_no_match(tmp_path: Path):
    pc = tmp_path / "state" / "proposed-changes.md"
    _seed_proposed_changes(pc, """
        # Proposed Changes

        ## Pending

        ## 2026-04-12 — split persona block
        Source: reflection 2026-04-12
        Proposal: x

        ## Applied
    """)
    log = tmp_path / "state" / "applied-proposals.jsonl"
    with pytest.raises(LookupError):
        mark_applied(pc, log, "no-such-thing", now=NOW)


def test_mark_applied_fence_aware_ignores_inner_headings(tmp_path: Path):
    """Regression for chainlink #114.

    A proposal body that contains a fenced code block with its own ``##``
    heading must not be split mid-body. Triggered originally on
    2026-05-11 by the 2026-05-09 non-goal proposal whose proposed body
    started with ``## Don't accept the source frame uncritically`` inside
    a fenced sample.
    """
    pc = tmp_path / "state" / "proposed-changes.md"
    _seed_proposed_changes(pc, """
        # Proposed Changes

        ## Pending

        ## 2026-05-09 — add non-goal: source-frame uncritical
        Source: reflection 2026-05-09
        Proposal: Add a new entry to memory/core/05-non-goals.md.
        Predicted effect: Error rate would drop.

        **Proposed file body:**

        ```
        ## Don't accept the source frame uncritically

        When the user (or a tool, or a doc) sets up a frame, my first move
        is checking whether the frame is right, not just answering within
        it.
        ```

        ## 2026-05-10 — unrelated later entry
        Source: reflection 2026-05-10
        Proposal: Something else.
        Predicted effect: Error rate would drop.

        ## Applied

        ## Rejected
    """)
    log = tmp_path / "state" / "applied-proposals.jsonl"

    proposal = mark_applied(pc, log, "source-frame uncritical", now=NOW)

    # The matched id must be the outer entry's full heading, not the
    # inner fenced-block heading.
    assert "source-frame uncritical" in proposal.id.lower()
    assert "don't accept" not in proposal.id.lower()
    # The Pending section retained the unrelated 2026-05-10 entry;
    # the matched entry moved to Applied with its fenced body intact.
    new_body = pc.read_text()
    pending_idx = new_body.find("## Pending")
    applied_idx = new_body.find("## Applied")
    rejected_idx = new_body.find("## Rejected")
    moved_idx = new_body.find("source-frame uncritical")
    later_idx = new_body.find("unrelated later entry")
    assert pending_idx < applied_idx < rejected_idx
    # Moved entry now lives under Applied.
    assert applied_idx < moved_idx < rejected_idx
    # Unrelated entry is still under Pending.
    assert pending_idx < later_idx < applied_idx
    # Fenced inner content survived the move — the prose line from
    # inside the fenced sample landed under Applied with its parent.
    fenced_line = new_body.find("When the user (or a tool, or a doc)")
    assert applied_idx < fenced_line < rejected_idx
    # And the fenced inner ``## Don't accept`` heading is still present
    # (the body wasn't split on it).
    assert "## Don't accept the source frame uncritically" in new_body


def test_mark_applied_creates_applied_section_when_missing(tmp_path: Path):
    pc = tmp_path / "state" / "proposed-changes.md"
    _seed_proposed_changes(pc, """
        # Proposed Changes

        ## Pending

        ## 2026-04-12 — split persona block
        Source: x
        Proposal: y
    """)
    log = tmp_path / "state" / "applied-proposals.jsonl"
    mark_applied(pc, log, "split persona", now=NOW)
    body = pc.read_text()
    assert "## Applied" in body


# ─── load_applied_proposals ─────────────────────────────────────────────


def test_load_applied_proposals_returns_empty_when_missing(tmp_path: Path):
    assert load_applied_proposals(tmp_path / "missing.jsonl") == []


def test_load_applied_proposals_skips_malformed(tmp_path: Path):
    p = tmp_path / "applied.jsonl"
    p.write_text(
        json.dumps({
            "id": "ok", "applied_at": NOW.isoformat(),
            "source": "x", "proposal": "y", "rationale": "z",
            "affected": "a", "predicted_effect": "b",
        }) + "\n"
        + "not json\n"
        + json.dumps({"unknown_field": "x"}) + "\n"
    )
    out = load_applied_proposals(p)
    assert len(out) == 1
    assert out[0].id == "ok"


# ─── compute_signals ───────────────────────────────────────────────────


def _write_event(path: Path, *, ts: datetime, type: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps({"timestamp": ts.isoformat(), "type": type,
                             "session_id": "s"}) + "\n")


def _write_turn(path: Path, *, ts: datetime, tool_calls: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": ts.isoformat(), "turn_id": "t", "session_id": "s",
        "saga_session_id": None, "trigger": "user_message",
        "channel_id": "c", "input": "",
        "events": [
            {"type": "tool_call", "id": f"u{i}", "name": name, "args": {}}
            for i, name in enumerate(tool_calls)
        ],
    }
    with path.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def test_compute_signals_error_rate_drop(tmp_path: Path):
    applied_at = NOW
    proposal = AppliedProposal(
        id="2026-04-24 — fix flaky tool",
        applied_at=applied_at.isoformat(),
        predicted_effect="Error rate would drop after the change.",
    )
    events = tmp_path / "logs" / "events.jsonl"
    # 5 errors before, 1 after.
    for i in range(5):
        _write_event(events, ts=applied_at - timedelta(hours=12 + i),
                     type="tool_denied")
    _write_event(events, ts=applied_at + timedelta(hours=2),
                 type="tool_denied")

    signals = compute_signals(
        proposal,
        events_log=events,
        turns_log=tmp_path / "logs" / "turns.jsonl",
        window_days=7,
        now=applied_at + timedelta(days=7),
    )
    assert len(signals) == 1
    s = signals[0]
    assert s.name == "error_events"
    assert s.before == 5
    assert s.after == 1
    assert s.delta == -4


def test_compute_signals_tool_freq(tmp_path: Path):
    applied_at = NOW
    proposal = AppliedProposal(
        id="2026-04-24 — promote Read",
        applied_at=applied_at.isoformat(),
        predicted_effect="Read tool would be invoked more often after this.",
    )
    turns = tmp_path / "logs" / "turns.jsonl"
    _write_turn(turns, ts=applied_at - timedelta(hours=4),
                tool_calls=["Read"])
    _write_turn(turns, ts=applied_at + timedelta(hours=4),
                tool_calls=["Read", "Read", "Read"])

    signals = compute_signals(
        proposal,
        events_log=tmp_path / "logs" / "events.jsonl",
        turns_log=turns,
        window_days=7,
        now=applied_at + timedelta(days=7),
    )
    assert len(signals) == 1
    s = signals[0]
    assert s.name == "tool_calls:Read"
    assert s.before == 1
    assert s.after == 3


def test_compute_signals_unknown_kind_returns_empty(tmp_path: Path):
    proposal = AppliedProposal(
        id="x",
        applied_at=NOW.isoformat(),
        predicted_effect="things will be better somehow.",
    )
    signals = compute_signals(
        proposal,
        events_log=tmp_path / "events.jsonl",
        turns_log=tmp_path / "turns.jsonl",
        now=NOW + timedelta(days=7),
    )
    assert signals == []


# ─── audit_window ──────────────────────────────────────────────────────


def test_audit_window_filters_to_age_band(tmp_path: Path):
    home = tmp_path
    log = home / "state" / "applied-proposals.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    # Two proposals: one 2 weeks ago (in band), one 6 weeks ago (out).
    in_band = AppliedProposal(
        id="in-band",
        applied_at=(NOW - timedelta(weeks=2)).isoformat(),
        predicted_effect="Error rate would drop.",
    )
    out_of_band = AppliedProposal(
        id="too-old",
        applied_at=(NOW - timedelta(weeks=6)).isoformat(),
        predicted_effect="Error rate would drop.",
    )
    log.write_text(
        json.dumps(in_band.__dict__) + "\n"
        + json.dumps(out_of_band.__dict__) + "\n"
    )

    rows = audit_window(home, weeks_back_min=1, weeks_back_max=4, now=NOW)
    assert len(rows) == 1
    assert rows[0].proposal.id == "in-band"


# ─── render_audit_block ─────────────────────────────────────────────────


def test_render_audit_block_returns_none_for_empty():
    assert render_audit_block([]) is None


def test_render_audit_block_includes_signals():
    p = AppliedProposal(
        id="2026-04-24 — fix flaky tool",
        applied_at=NOW.isoformat(),
        predicted_effect="Error rate would drop.",
    )
    rows = [AuditRow(
        proposal=p,
        signals=[Signal(name="error_events", before=10, after=4)],
    )]
    out = render_audit_block(rows)
    assert out is not None
    assert "fix flaky tool" in out
    assert "Predicted" in out
    assert "Measured" in out
    assert "10" in out and "4" in out


def test_render_audit_block_marks_unparseable():
    p = AppliedProposal(
        id="2026-04-24 — vague proposal",
        applied_at=NOW.isoformat(),
        predicted_effect="things get better somehow.",
    )
    rows = [AuditRow(proposal=p, signals=[])]
    out = render_audit_block(rows)
    assert out is not None
    assert "no parseable predicted-effect signal" in out
