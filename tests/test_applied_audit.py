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
    _list_pending_proposals,
    audit_window,
    compute_signals,
    format_reflection_digest,
    load_applied_proposals,
    mark_applied,
    mark_reject,
    parse_resolve_string,
    render_audit_block,
    run_scheduled_applied_audit,
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


def test_inner_heading_in_proposal_body_not_split(tmp_path: Path):
    """#497: a ``## `` prose subheading inside an LLM-authored proposal body
    (e.g. ``## Risks``) is NOT a section boundary. Pre-fix it became a phantom
    Pending entry and mark_applied moved only the head, stranding the rest."""
    pc = tmp_path / "state" / "proposed-changes.md"
    _seed_proposed_changes(pc, """
        # Proposed Changes

        ## Pending

        ## 2026-04-12 — split persona block
        Source: reflection 2026-04-12
        Proposal: Split memory/core/00-persona.md.

        ## Risks
        Could fragment the persona voice.

        ## Applied

        ## Rejected
    """)
    log = tmp_path / "state" / "applied-proposals.jsonl"

    # Exactly ONE pending proposal — the ``## Risks`` subheading is body, not a
    # phantom entry that would inflate backlog-health counts.
    pending = _list_pending_proposals(pc)
    assert len(pending) == 1
    assert "split persona block" in pending[0][1]

    mark_applied(pc, log, "split persona", now=NOW)
    new_body = pc.read_text()
    pending_idx = new_body.find("## Pending")
    applied_idx = new_body.find("## Applied")
    rejected_idx = new_body.find("## Rejected")
    split_idx = new_body.find("split persona block")
    risks_idx = new_body.find("## Risks")
    # The WHOLE proposal — including its inner ``## Risks`` — moved to Applied;
    # nothing stranded in Pending.
    assert applied_idx < split_idx < rejected_idx
    assert applied_idx < risks_idx < rejected_idx


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


def test_compute_signals_drops_phantom_tool_match(tmp_path: Path):
    """Prose like 'Adding the introspection' or 'Unblocks chainlink #61'
    used to match the loose CamelCase regex and produce a phantom
    ``tool_calls:Adding 0 → 0`` row. The tightened heuristic + the
    0/0 drop guard means these surface as 'no parseable signal' rows
    instead."""
    proposal = AppliedProposal(
        id="2026-05-09 — unblock chainlink #61",
        applied_at=NOW.isoformat(),
        predicted_effect=(
            "Unblocks chainlink #61. Adding the introspection description "
            "makes find-skills' awk extractor surface non-empty."
        ),
    )
    # Empty turns log → any tool name would yield 0/0.
    signals = compute_signals(
        proposal,
        events_log=tmp_path / "events.jsonl",
        turns_log=tmp_path / "turns.jsonl",
        now=NOW + timedelta(days=7),
    )
    assert signals == []


def test_compute_signals_expect_line_events_kind(tmp_path: Path):
    """A structured ``Expect: events:<type> drops`` line should pick the
    new ``events`` signal kind and measure that specific event type."""
    applied_at = NOW
    proposal = AppliedProposal(
        id="2026-04-24 — drop synth-skip boundary",
        applied_at=applied_at.isoformat(),
        predicted_effect=(
            "The post-check should fire less often.\n"
            "Expect: events:saga_synthesis_skipped_boundary drops"
        ),
    )
    events = tmp_path / "logs" / "events.jsonl"
    for i in range(4):
        _write_event(events, ts=applied_at - timedelta(hours=12 + i),
                     type="saga_synthesis_skipped_boundary")
    _write_event(events, ts=applied_at + timedelta(hours=2),
                 type="saga_synthesis_skipped_boundary")
    # Noise: a different event type in the same window.
    _write_event(events, ts=applied_at + timedelta(hours=3),
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
    assert s.name == "events:saga_synthesis_skipped_boundary"
    assert s.before == 4
    assert s.after == 1


def test_compute_signals_expect_line_tool_calls(tmp_path: Path):
    """Structured ``Expect: tool_calls:<name> rises`` should map to the
    tool_calls kind even when the prose says nothing about tools."""
    applied_at = NOW
    proposal = AppliedProposal(
        id="2026-04-24 — promote memory_query",
        applied_at=applied_at.isoformat(),
        predicted_effect=(
            "Sessions that touched the new core block reach for it more.\n"
            "Expect: tool_calls:memory_query rises"
        ),
    )
    turns = tmp_path / "logs" / "turns.jsonl"
    _write_turn(turns, ts=applied_at - timedelta(hours=4),
                tool_calls=["memory_query"])
    _write_turn(turns, ts=applied_at + timedelta(hours=4),
                tool_calls=["memory_query", "memory_query", "memory_query"])

    signals = compute_signals(
        proposal,
        events_log=tmp_path / "logs" / "events.jsonl",
        turns_log=turns,
        window_days=7,
        now=applied_at + timedelta(days=7),
    )
    assert len(signals) == 1
    assert signals[0].name == "tool_calls:memory_query"
    assert signals[0].before == 1
    assert signals[0].after == 3


def test_compute_signals_expect_line_error_events(tmp_path: Path):
    """``Expect: error_events drops`` (no target) routes through the
    error-events path."""
    applied_at = NOW
    proposal = AppliedProposal(
        id="x",
        applied_at=applied_at.isoformat(),
        predicted_effect="Expect: error_events drops",
    )
    events = tmp_path / "logs" / "events.jsonl"
    for i in range(3):
        _write_event(events, ts=applied_at - timedelta(hours=12 + i),
                     type="tool_denied")
    _write_event(events, ts=applied_at + timedelta(hours=2),
                 type="tool_denied")
    signals = compute_signals(
        proposal,
        events_log=events,
        turns_log=tmp_path / "logs" / "turns.jsonl",
        now=applied_at + timedelta(days=7),
    )
    assert len(signals) == 1
    assert signals[0].name == "error_events"
    assert signals[0].before == 3
    assert signals[0].after == 1


def test_compute_signals_expect_line_wins_over_prose(tmp_path: Path):
    """If both a structured Expect: line and prose error-rate phrasing
    are present, the Expect: line wins — it's the operator's explicit
    instruction about what to measure."""
    applied_at = NOW
    proposal = AppliedProposal(
        id="x",
        applied_at=applied_at.isoformat(),
        predicted_effect=(
            "Error rate would drop a lot.\n"
            "Expect: events:saga_synthesis_skipped_boundary drops"
        ),
    )
    events = tmp_path / "logs" / "events.jsonl"
    _write_event(events, ts=applied_at - timedelta(hours=4),
                 type="saga_synthesis_skipped_boundary")
    signals = compute_signals(
        proposal,
        events_log=events,
        turns_log=tmp_path / "logs" / "turns.jsonl",
        now=applied_at + timedelta(days=7),
    )
    # Expect: line specifies the event-type kind, not the generic
    # error-events kind — so we measure the specific event type, not
    # the broad error bucket.
    assert len(signals) == 1
    assert signals[0].name == "events:saga_synthesis_skipped_boundary"


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


# ─── run_scheduled_applied_audit ───────────────────────────────────────


def _write_applied_proposal(home: Path, *, applied_at: str) -> None:
    """Seed a minimal applied-proposals.jsonl entry."""
    log_path = home / "state" / "applied-proposals.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "id": "2026-04-15 — test proposal",
        "applied_at": applied_at,
        "source": "reflection 2026-04-15",
        "proposal": "Split the persona block.",
        "rationale": "Reduce prompt size.",
        "affected": "memory/core/00-persona.md",
        "predicted_effect": "Drift indicator drops.",
    }
    with log_path.open("a") as fh:
        fh.write(json.dumps(record) + "\n")


@pytest.mark.asyncio
async def test_scheduled_applied_audit_writes_report_and_emits_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Happy path: audit_window returns rows → report file created +
    applied_audit_ok event emitted with correct rows_audited count."""
    import mimir.reflection.applied_audit as aa_mod

    fake_proposal = AppliedProposal(
        id="2026-04-15 — test proposal",
        applied_at=datetime(2026, 4, 15, tzinfo=timezone.utc).isoformat(),
        predicted_effect="Drift indicator drops.",
    )
    fake_rows = [AuditRow(proposal=fake_proposal, signals=[])]

    # Monkeypatch audit_window to return a known row without date arithmetic.
    monkeypatch.setattr(aa_mod, "audit_window", lambda *a, **kw: fake_rows)

    events: list[tuple[str, dict]] = []

    async def fake_log(kind, **kw):
        events.append((kind, kw))

    monkeypatch.setattr("mimir.event_logger.log_event", fake_log)
    await run_scheduled_applied_audit(tmp_path)

    # Report file must exist under state/reports/
    reports = list((tmp_path / "state" / "reports").glob("applied-audit-*.md"))
    assert reports, "expected at least one applied-audit-*.md report"
    content = reports[0].read_text()
    assert "2026-04-15 — test proposal" in content

    kinds = [k for k, _ in events]
    assert "applied_audit_ok" in kinds
    ok_payloads = [kw for k, kw in events if k == "applied_audit_ok"]
    assert ok_payloads[0]["rows_audited"] == 1


@pytest.mark.asyncio
async def test_scheduled_applied_audit_empty_window_still_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """When no applied-proposals.jsonl exists (or it has no entries in
    the window), the run still emits applied_audit_ok with rows_audited=0."""
    events: list[tuple[str, dict]] = []

    async def fake_log(kind, **kw):
        events.append((kind, kw))

    monkeypatch.setattr("mimir.event_logger.log_event", fake_log)
    # No applied-proposals.jsonl seeded — fresh home.
    await run_scheduled_applied_audit(tmp_path)

    kinds = [k for k, _ in events]
    assert "applied_audit_ok" in kinds
    ok_payloads = [kw for k, kw in events if k == "applied_audit_ok"]
    assert ok_payloads[0]["rows_audited"] == 0

    # Report file must still be written (confirms job fired).
    reports = list((tmp_path / "state" / "reports").glob("applied-audit-*.md"))
    assert reports, "report file must be written even for empty window"
    assert "No proposals in window" in reports[0].read_text()


@pytest.mark.asyncio
async def test_scheduled_applied_audit_catches_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """If audit_window raises, the job catches it and emits
    applied_audit_error without propagating the exception."""
    events: list[tuple[str, dict]] = []

    async def fake_log(kind, **kw):
        events.append((kind, kw))

    def _boom(*a, **kw):
        raise RuntimeError("test failure")

    monkeypatch.setattr("mimir.event_logger.log_event", fake_log)
    monkeypatch.setattr(
        "mimir.reflection.applied_audit.audit_window", _boom,
    )
    # Must not raise.
    await run_scheduled_applied_audit(tmp_path)

    kinds = [k for k, _ in events]
    assert "applied_audit_error" in kinds
    assert "applied_audit_ok" not in kinds


# ─── _list_pending_proposals ────────────────────────────────────────────


_FIVE_PROPOSALS = """\
# Proposed Changes

Pending HITL items from the reflection skill.

## Pending

## 2026-05-01 — promote spawn-model-tier
Source: reflection 2026-05-01
Proposal: Promote spawn-model-tier heuristic to core/40-learned-behaviors.md.
Impact: core memory addition (~200 chars).

## 2026-05-02 — add wiki concept
Source: reflection 2026-05-02
Proposal: New eigenbehavior concept page from Von Foerster Ch 11 synthesis.
Impact: wiki expansion.

## 2026-05-03 — strengthen frame-check
Source: reflection 2026-05-03
Proposal: Strengthen source-frame check language in 05-non-goals.md.
Impact: core memory edit.

## 2026-05-04 — update heartbeat patterns
Source: reflection 2026-05-04
Proposal: Document the active-conversation-yield rule in 50-heartbeat-patterns.md.
Impact: core memory addition.

## 2026-05-05 — retire oauth-usage-poller issue
Source: reflection 2026-05-05
Proposal: Re-file oauth-usage-poller.md from memory/issues/ to state/wiki/topics/.
Impact: drift-amplifier fix.

## Applied

## Rejected
"""


def test_list_pending_proposals_five_proposals(tmp_path: Path):
    """Five proposals → correct numbers, headings, and excerpts."""
    pc = tmp_path / "state" / "proposed-changes.md"
    pc.parent.mkdir(parents=True, exist_ok=True)
    pc.write_text(_FIVE_PROPOSALS, encoding="utf-8")

    proposals = _list_pending_proposals(pc)

    assert len(proposals) == 5
    nums = [n for n, _, _ in proposals]
    assert nums == [1, 2, 3, 4, 5]

    # Heading check.
    headings = [h for _, h, _ in proposals]
    assert "2026-05-01 — promote spawn-model-tier" in headings[0]
    assert "2026-05-05 — retire oauth-usage-poller issue" in headings[4]

    # Excerpt check — should be the first non-empty non-## line of each body.
    excerpts = [e for _, _, e in proposals]
    assert "Source:" in excerpts[0]          # first line in body is Source:
    assert len(excerpts[0]) <= 120


def test_list_pending_proposals_fence_aware(tmp_path: Path):
    """Inner ## inside fenced block must NOT create a new proposal."""
    pc = tmp_path / "state" / "proposed-changes.md"
    pc.parent.mkdir(parents=True, exist_ok=True)
    pc.write_text(dedent("""\
        # Proposed Changes

        ## Pending

        ## 2026-05-10 — add fenced example
        Source: reflection 2026-05-10
        Proposal: Add a fenced code sample.
        ```
        ## this is inside a fence, not a heading
        some code here
        ```
        Impact: cosmetic.

        ## Applied

        ## Rejected
    """), encoding="utf-8")

    proposals = _list_pending_proposals(pc)

    # Only ONE proposal should be found — the inner ## is inside a fence.
    assert len(proposals) == 1
    assert proposals[0][0] == 1
    assert "add fenced example" in proposals[0][1]


def test_list_pending_proposals_empty_backlog(tmp_path: Path):
    """Empty Pending section → empty list, no exception."""
    pc = tmp_path / "state" / "proposed-changes.md"
    pc.parent.mkdir(parents=True, exist_ok=True)
    pc.write_text(dedent("""\
        # Proposed Changes

        ## Pending

        ## Applied

        ## Rejected
    """), encoding="utf-8")

    proposals = _list_pending_proposals(pc)

    assert proposals == []


def test_list_pending_proposals_json_output(tmp_path: Path, capsys):
    """--json mode: output is a valid JSON array of {num, heading, excerpt}."""
    from mimir.cli import main as cli_main

    pc = tmp_path / "state" / "proposed-changes.md"
    pc.parent.mkdir(parents=True, exist_ok=True)
    pc.write_text(_FIVE_PROPOSALS, encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        cli_main(["reflection", "list-pending", "--json", "--home", str(tmp_path)])
    assert exc_info.value.code == 0

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert isinstance(data, list)
    assert len(data) == 5
    for item in data:
        assert "num" in item
        assert "heading" in item
        assert "excerpt" in item
    assert data[0]["num"] == 1
    assert "2026-05-01" in data[0]["heading"]


# ─── format_reflection_digest ───────────────────────────────────────────


def test_format_reflection_digest_zero_proposals_returns_none():
    """Empty proposal list → None (silent reflection, no message)."""
    assert format_reflection_digest([]) is None


def test_format_reflection_digest_single_proposal():
    """One proposal → correctly formatted single-item digest."""
    proposals = [(1, "2026-05-27 — promote spawn-model-tier", "Promote heuristic to core.")]
    result = format_reflection_digest(proposals)

    assert result is not None
    assert "Reflection complete — 1 pending proposal:" in result
    # Plural check: singular "proposal" not "proposals"
    assert "proposals" not in result.split("—")[1].split(":")[0]
    assert "1. **2026-05-27 — promote spawn-model-tier**:" in result
    assert "Promote heuristic to core." in result
    assert "accept 1" in result
    assert "reject" in result


def test_format_reflection_digest_multiple_proposals():
    """Three proposals → numbered list with correct header count."""
    proposals = [
        (1, "2026-05-27 — promote spawn-model-tier", "Promote heuristic to core."),
        (2, "2026-05-27 — add wiki concept", "New eigenbehavior concept page."),
        (3, "2026-05-27 — strengthen frame-check", "Strengthen non-goals language."),
    ]
    result = format_reflection_digest(proposals)

    assert result is not None
    assert "Reflection complete — 3 pending proposals:" in result
    assert "1. **2026-05-27 — promote spawn-model-tier**:" in result
    assert "2. **2026-05-27 — add wiki concept**:" in result
    assert "3. **2026-05-27 — strengthen frame-check**:" in result


def test_format_reflection_digest_truncates_long_heading():
    """Heading longer than 60 chars is truncated with ellipsis."""
    long_heading = "2026-05-27 — " + "x" * 60  # total > 60 chars
    proposals = [(1, long_heading, "Some excerpt.")]
    result = format_reflection_digest(proposals)

    assert result is not None
    # The heading in bold should be ≤60 chars (plus ellipsis)
    import re
    match = re.search(r"\*\*(.+?)\*\*", result)
    assert match is not None
    bold_text = match.group(1)
    # After truncation the text is at most 60 chars + "…"
    assert len(bold_text) <= 61  # 60 + "…"
    assert bold_text.endswith("…")


def test_format_reflection_digest_empty_excerpt():
    """Proposal with no excerpt omits the ': excerpt' suffix."""
    proposals = [(1, "2026-05-27 — some proposal", "")]
    result = format_reflection_digest(proposals)

    assert result is not None
    # Should have the bold heading but no trailing ": "
    assert "1. **2026-05-27 — some proposal**\n" in result or (
        "1. **2026-05-27 — some proposal**" in result
        and not result.split("**2026-05-27 — some proposal**")[1].startswith(":")
    )


def test_format_reflection_digest_reply_hint_present():
    """Digest always ends with the reply-format hint."""
    proposals = [(1, "2026-05-27 — test", "excerpt")]
    result = format_reflection_digest(proposals)

    assert result is not None
    assert "accept 1 3" in result
    assert "reject 2" in result
    assert "defer 1" in result


# ─── mark_reject ────────────────────────────────────────────────────────

_FOUR_PROPOSAL_DOC = """\
# Proposed Changes

## Pending

## 2026-05-01 — proposal alpha
Proposal: Alpha change.

## 2026-05-02 — proposal beta
Proposal: Beta change.

## 2026-05-03 — proposal gamma
Proposal: Gamma change.

## 2026-05-04 — proposal delta
Proposal: Delta change.

## Applied

## Rejected
"""


def test_mark_reject_moves_to_rejected_with_reason(tmp_path: Path):
    """mark_reject moves the matched proposal to ## Rejected with annotation."""
    pc = tmp_path / "state" / "proposed-changes.md"
    _seed_proposed_changes(pc, _FOUR_PROPOSAL_DOC)

    heading = mark_reject(pc, "proposal beta", "not a priority", now=NOW)

    assert "beta" in heading
    body = pc.read_text()
    pending_idx = body.find("## Pending")
    rejected_idx = body.find("## Rejected")
    beta_idx = body.find("proposal beta")
    alpha_idx = body.find("proposal alpha")
    # beta must now be under Rejected, not Pending.
    assert rejected_idx < beta_idx
    # alpha still under Pending.
    assert pending_idx < alpha_idx < rejected_idx
    # Rejection annotation present.
    assert "not a priority" in body
    assert "<!-- rejected:" in body
    # No JSONL log (mark_reject does not write applied-proposals.jsonl).
    assert not (tmp_path / "state" / "applied-proposals.jsonl").exists()


def test_mark_reject_creates_rejected_section_when_missing(tmp_path: Path):
    """mark_reject creates ## Rejected if it doesn't exist."""
    pc = tmp_path / "state" / "proposed-changes.md"
    _seed_proposed_changes(pc, """\
## Pending

## 2026-05-01 — alpha proposal
Proposal: Alpha.

## Applied
""")

    mark_reject(pc, "alpha", "no thanks", now=NOW)

    body = pc.read_text()
    assert "## Rejected" in body
    rejected_idx = body.find("## Rejected")
    alpha_idx = body.find("alpha proposal")
    assert rejected_idx < alpha_idx
    assert "no thanks" in body


def test_mark_reject_empty_reason_defaults_to_operator_declined(tmp_path: Path):
    """mark_reject uses 'operator declined' when reason is empty string."""
    pc = tmp_path / "state" / "proposed-changes.md"
    _seed_proposed_changes(pc, """\
## Pending

## 2026-05-01 — gamma proposal
Proposal: Gamma.

## Rejected
""")

    mark_reject(pc, "gamma", "", now=NOW)

    body = pc.read_text()
    assert "operator declined" in body


# ─── parse_resolve_string ───────────────────────────────────────────────


def test_parse_resolve_string_pure_accept():
    """'accept 1 3' → two accept ops with no reason."""
    ops = parse_resolve_string("accept 1 3")
    assert len(ops) == 2
    assert ops[0] == ("accept", 1, "")
    assert ops[1] == ("accept", 3, "")


def test_parse_resolve_string_pure_reject_with_reason():
    """'reject 2 \"not now\"' → single reject op with reason."""
    ops = parse_resolve_string('reject 2 "not now"')
    assert len(ops) == 1
    assert ops[0] == ("reject", 2, "not now")


def test_parse_resolve_string_mixed_accept_reject():
    """'accept 1 3 / reject 2 \"reason\"' → three ops in order."""
    ops = parse_resolve_string("accept 1 3 / reject 2 'deferred'")
    assert len(ops) == 3
    actions = [(a, n) for a, n, _ in ops]
    assert ("accept", 1) in actions
    assert ("accept", 3) in actions
    assert ("reject", 2) in actions
    # Reason on the reject op.
    reasons = {n: r for a, n, r in ops if a == "reject"}
    assert reasons[2] == "deferred"


# ─── resolve CLI integration ────────────────────────────────────────────

def test_resolve_cli_mixed_apply_and_reject(tmp_path: Path, capsys):
    """resolve: pure accept 1 3 + reject 2 on a 4-proposal file."""
    from mimir.cli import main as _main

    pc = tmp_path / "state" / "proposed-changes.md"
    _seed_proposed_changes(pc, _FOUR_PROPOSAL_DOC)

    with pytest.raises(SystemExit) as exc_info:
        _main(["reflection", "resolve",
               "--home", str(tmp_path),
               "accept 1 3 / reject 2 'not now'"])
    assert exc_info.value.code == 0

    body = pc.read_text()
    captured = capsys.readouterr()

    # alpha (1) and gamma (3) in Applied; beta (2) in Rejected.
    applied_idx = body.find("## Applied")
    rejected_idx = body.find("## Rejected")
    pending_idx = body.find("## Pending")
    alpha_idx = body.find("proposal alpha")
    beta_idx = body.find("proposal beta")
    gamma_idx = body.find("proposal gamma")
    delta_idx = body.find("proposal delta")

    assert applied_idx < alpha_idx  # alpha moved to Applied
    assert applied_idx < gamma_idx  # gamma moved to Applied
    assert rejected_idx < beta_idx  # beta moved to Rejected
    assert pending_idx < delta_idx < applied_idx  # delta still Pending

    assert "not now" in body          # reject reason persisted
    assert "Applied: 1, 3." in captured.out
    assert "Rejected: 2" in captured.out


def test_resolve_cli_invalid_number_still_processes_valid(tmp_path: Path, capsys):
    """Invalid proposal number produces error but valid ops proceed."""
    from mimir.cli import main as _main

    pc = tmp_path / "state" / "proposed-changes.md"
    _seed_proposed_changes(pc, """\
## Pending

## 2026-05-01 — proposal one
Proposal: One.

## Applied

## Rejected
""")

    # Proposal 99 doesn't exist; proposal 1 does.
    with pytest.raises(SystemExit) as exc_info:
        _main(["reflection", "resolve",
               "--home", str(tmp_path),
               "accept 1 / reject 99"])
    # non-zero because of the error on 99, but 1 was still applied
    ret = exc_info.value.code

    body = pc.read_text()
    captured = capsys.readouterr()

    # proposal 1 was applied despite the error on 99.
    applied_idx = body.find("## Applied")
    one_idx = body.find("proposal one")
    assert applied_idx < one_idx

    assert "Applied: 1." in captured.out
    assert "99" in captured.out  # error mentions the invalid number
