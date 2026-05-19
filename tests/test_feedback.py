"""v0.4 §2: algedonic surfacing.

FeedbackLog reads logs/events.jsonl + logs/turns.jsonl tail-first,
classifies records by polarity, renders a prompt block."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mimir.feedback import (
    FeedbackLog,
    FeedbackSignal,
    pending_forget_candidates_count,
    render_feedback_block,
)


def _ts(hours_ago: float = 0) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _make_log(tmp_path: Path, events: list[dict] | None = None,
              turns: list[dict] | None = None) -> FeedbackLog:
    events_path = tmp_path / "logs" / "events.jsonl"
    turns_path = tmp_path / "logs" / "turns.jsonl"
    if events is not None:
        _write_jsonl(events_path, events)
    if turns is not None:
        _write_jsonl(turns_path, turns)
    return FeedbackLog(events_path=events_path, turns_path=turns_path)


# ---- Empty / missing files ----------------------------------------------


def test_recent_block_returns_none_when_logs_missing(tmp_path: Path):
    log = FeedbackLog(
        events_path=tmp_path / "logs" / "events.jsonl",
        turns_path=tmp_path / "logs" / "turns.jsonl",
    )
    assert log.recent_block() is None


def test_recent_block_returns_none_when_logs_have_nothing_relevant(tmp_path: Path):
    log = _make_log(
        tmp_path,
        events=[{"timestamp": _ts(0.5), "type": "event_queued", "channel_id": "x"}],
    )
    assert log.recent_block() is None


# ---- Polarity rendering --------------------------------------------------


def test_negative_only_signals_render(tmp_path: Path):
    log = _make_log(
        tmp_path,
        events=[
            {
                "timestamp": _ts(0.1),
                "type": "tool_call_denied",
                "tool": "file_search",
                "reason": "budget exhausted",
                "channel_id": "slack-eng",
            }
        ],
    )
    block = log.recent_block()
    assert block is not None
    assert "Negative (last 24h):" in block
    assert "Positive" not in block
    assert "tool_denied file_search" in block
    assert "budget exhausted" in block
    assert "[slack-eng]" in block


def test_positive_only_signals_render(tmp_path: Path):
    log = _make_log(
        tmp_path,
        events=[
            {
                "timestamp": _ts(0.5),
                "type": "saga_feedback_sent",
                "n_atoms": 3,
                "channel_id": "slack-eng",
            }
        ],
    )
    block = log.recent_block()
    assert block is not None
    assert "Positive (last 24h):" in block
    assert "Negative" not in block
    assert "saga_feedback_sent (3 atoms credited)" in block


def test_mixed_polarity_renders_both_subsections_with_blank_separator(tmp_path: Path):
    log = _make_log(
        tmp_path,
        events=[
            {"timestamp": _ts(0.1), "type": "saga_feedback_sent", "n_atoms": 2,
             "channel_id": "slack-eng"},
            {"timestamp": _ts(0.2), "type": "tool_call_denied", "tool": "saga_query",
             "reason": "rate-limited", "channel_id": "discord-99"},
        ],
    )
    block = log.recent_block()
    assert block is not None
    assert "Negative (last 24h):" in block
    assert "Positive (last 24h):" in block
    # Negative comes first; subsections separated by a blank line for
    # readability.
    neg_idx = block.index("Negative")
    pos_idx = block.index("Positive")
    assert neg_idx < pos_idx
    assert "\n\nPositive" in block


# ---- Window cutoff -------------------------------------------------------


def test_records_outside_window_are_dropped(tmp_path: Path):
    log = _make_log(
        tmp_path,
        events=[
            # JSONL is appended chronologically — oldest event first.
            {"timestamp": _ts(48), "type": "tool_call_denied", "tool": "y",
             "channel_id": "c2"},
            {"timestamp": _ts(1), "type": "tool_call_denied", "tool": "x",
             "channel_id": "c1"},
        ],
    )
    block = log.recent_block(window_hours=24)
    assert block is not None
    assert "tool_denied x" in block
    assert "tool_denied y" not in block


def test_short_window_excludes_recent_but_older_records(tmp_path: Path):
    log = _make_log(
        tmp_path,
        events=[
            {"timestamp": _ts(2), "type": "tool_call_denied", "tool": "stale",
             "channel_id": "c"},
            {"timestamp": _ts(0.1), "type": "tool_call_denied", "tool": "fresh",
             "channel_id": "c"},
        ],
    )
    block = log.recent_block(window_hours=1)
    assert block is not None
    assert "fresh" in block
    assert "stale" not in block


# ---- Per-polarity cap ----------------------------------------------------


def test_per_polarity_cap_truncates(tmp_path: Path):
    events = [
        {"timestamp": _ts(0.01 * i), "type": "tool_call_denied", "tool": f"t{i}",
         "channel_id": "c"}
        for i in range(10)
    ]
    log = _make_log(tmp_path, events=events)
    negatives, _ = log.recent(limit_per_polarity=3)
    assert len(negatives) == 3


# ---- turns.jsonl integration --------------------------------------------


def test_turn_errors_surface_as_negative_signals(tmp_path: Path):
    log = _make_log(
        tmp_path,
        events=[],
        turns=[
            {
                "ts": _ts(0.5),
                "turn_id": "abc",
                "channel_id": "slack-eng",
                "error": "TimeoutError: query() crashed",
                "result_is_error": True,
            }
        ],
    )
    block = log.recent_block()
    assert block is not None
    assert "Negative" in block
    assert "turn error: TimeoutError" in block


def test_turn_records_without_error_are_ignored(tmp_path: Path):
    log = _make_log(
        tmp_path,
        events=[],
        turns=[
            {"ts": _ts(0.5), "turn_id": "abc", "channel_id": "x",
             "error": None, "result_is_error": False}
        ],
    )
    assert log.recent_block() is None


# ---- Channel scoping (no scoping — feedback is global) ------------------


def test_feedback_is_global_not_channel_scoped(tmp_path: Path):
    """A turn for slack-eng should still see signals from discord-99 —
    the whole point is self-feedback across channels."""
    log = _make_log(
        tmp_path,
        events=[
            {"timestamp": _ts(0.5), "type": "tool_call_denied", "tool": "x",
             "channel_id": "discord-99"},
            {"timestamp": _ts(0.6), "type": "saga_feedback_sent", "n_atoms": 1,
             "channel_id": "slack-eng"},
        ],
    )
    block = log.recent_block()
    assert block is not None
    # Both surface in the same block.
    assert "[discord-99]" in block
    assert "[slack-eng]" in block


# ---- Renderer direct -----------------------------------------------------


def test_render_feedback_block_returns_none_for_empty_inputs():
    assert render_feedback_block([], []) is None


def test_render_feedback_block_uses_window_hours_in_header():
    sig = FeedbackSignal(
        ts="2026-05-01T12:00:00+00:00",
        polarity="negative",
        kind="error",
        channel_id="x",
        content="error in foo: bar",
    )
    block = render_feedback_block([sig], [], window_hours=72)
    assert block is not None
    assert "Negative (last 72h):" in block
    assert "2026-05-01 12:00" in block


def test_heartbeat_health_degraded_renders_in_feedback(tmp_path: Path):
    log = _make_log(tmp_path, events=[
        {
            "timestamp": _ts(0.5),
            "type": "heartbeat_health_degraded",
            "session_id": "introspection-report",
            "success_rate": 0.25,
            "threshold": 0.80,
            "fired": 4,
            "successful": 1,
        },
    ])
    block = log.recent_block()
    assert block is not None
    assert "heartbeat pipeline degraded" in block
    assert "25%" in block
    assert "1/4 fired" in block


def test_saga_consolidate_ok_renders_as_positive(tmp_path: Path):
    log = _make_log(tmp_path, events=[
        {
            "timestamp": _ts(0.5),
            "type": "saga_consolidate_ok",
            "session_id": "s",
            "dry_run": False,
            "result": {
                "clusters_processed": 4,
                "atoms_merged": 7,
                "atoms_retired": 2,
                "duration_s": 12.4,
            },
        },
    ])
    block = log.recent_block()
    assert block is not None
    assert "Positive" in block
    assert "saga consolidation ran" in block
    assert "4 clusters" in block
    assert "7 merged" in block
    assert "2 retired" in block


def test_introspection_report_ok_surfaces_output_path(tmp_path: Path):
    """The agent should see the report's file path so it can Read it."""
    log = _make_log(tmp_path, events=[
        {
            "timestamp": _ts(0.5),
            "type": "introspection_report_ok",
            "session_id": "s",
            "output": "/home/agent/state/reports/introspection-2026-05-02.md",
            "days": 7,
            "pipeline_success_rate": 0.92,
            "fired": 50,
            "successful": 46,
            "algedonic_emitted": False,
        },
    ])
    block = log.recent_block()
    assert block is not None
    assert "Positive" in block
    assert "introspection report ready" in block
    assert "/home/agent/state/reports/introspection-2026-05-02.md" in block
    assert "92%" in block


def test_predictions_pending_review_renders(tmp_path: Path):
    """The predictions skill emits predictions_pending_review when
    past-horizon items pile up; algedonic surfacing nudges the agent
    to run `mimir predictions review`."""
    log = _make_log(tmp_path, events=[
        {"timestamp": _ts(0.5), "type": "predictions_pending_review",
         "session_id": "s", "count": 3},
    ])
    block = log.recent_block()
    assert block is not None
    assert "3 predictions past horizon" in block
    assert "mimir predictions review" in block


def test_cron_events_dedup_to_most_recent(tmp_path: Path):
    """§12.4 review #13: hourly heartbeats × 24h algedonic window
    means saga_consolidate_ok would re-appear in 24 prompts. Only the
    most recent occurrence should render."""
    # JSONL is appended chronologically — write oldest first so
    # tail-first iteration reads most-recent first.
    log = _make_log(tmp_path, events=[
        {"timestamp": _ts(5.0), "type": "saga_consolidate_ok",
         "session_id": "s",
         "result": {"clusters_processed": 1, "atoms_merged": 1}},
        {"timestamp": _ts(2.0), "type": "saga_consolidate_ok",
         "session_id": "s",
         "result": {"clusters_processed": 99, "atoms_merged": 99}},
        # Most recent — should win.
        {"timestamp": _ts(0.5), "type": "saga_consolidate_ok",
         "session_id": "s",
         "result": {"clusters_processed": 5, "atoms_merged": 7}},
    ])
    block = log.recent_block()
    assert block is not None
    # Most-recent (5 clusters / 7 merged) should be the only one rendered.
    assert "5 clusters" in block
    assert "7 merged" in block
    assert "99 clusters" not in block
    assert "1 clusters" not in block


def test_content_dedup_collapses_identical_negative_lines(tmp_path: Path):
    """Three tool_denied events with identical (tool, reason) collapse
    to the most recent. This handles the "tool_denied Read:
    path_outside_home × 3" case visible in the operator's prompt
    when the agent repeatedly hits the same path-confinement boundary."""
    log = _make_log(tmp_path, events=[
        # Oldest first (jsonl is appended chronologically; tail-first
        # iteration means the LAST one written is what the dedup keeps).
        {"timestamp": _ts(5.0), "type": "tool_call_denied",
         "tool": "Read", "reason": "path_outside_home", "channel_id": "c"},
        {"timestamp": _ts(3.0), "type": "tool_call_denied",
         "tool": "Read", "reason": "path_outside_home", "channel_id": "c"},
        {"timestamp": _ts(0.5), "type": "tool_call_denied",
         "tool": "Read", "reason": "path_outside_home", "channel_id": "c"},
    ])
    block = log.recent_block()
    assert block is not None
    # Exactly one rendered line with this content.
    assert block.count("tool_denied Read: path_outside_home") == 1


def test_content_dedup_keeps_distinct_tool_denied_variants(tmp_path: Path):
    """Different (tool, reason) tuples render as distinct content
    strings → both surface. Validates that dedup is per-content,
    not over-collapsing on rule_kind."""
    log = _make_log(tmp_path, events=[
        {"timestamp": _ts(2.0), "type": "tool_call_denied",
         "tool": "Read", "reason": "path_outside_home", "channel_id": "c"},
        {"timestamp": _ts(1.0), "type": "tool_call_denied",
         "tool": "Write", "reason": "path_outside_home", "channel_id": "c"},
        {"timestamp": _ts(0.5), "type": "tool_call_denied",
         "tool": "Read", "reason": "tool_call_budget_exceeded",
         "channel_id": "c"},
    ])
    block = log.recent_block()
    assert block is not None
    assert "tool_denied Read: path_outside_home" in block
    assert "tool_denied Write: path_outside_home" in block
    assert "tool_denied Read: tool_call_budget_exceeded" in block


def test_content_dedup_keeps_distinct_spawn_failures(tmp_path: Path):
    """Each spawn failure has a distinct job_id baked into the
    rendered content, so multiple spawn_work_failed events surface
    distinctly. Regression guard against over-collapsing per-incident
    negative signals."""
    log = _make_log(tmp_path, events=[
        {"timestamp": _ts(2.0), "type": "claude_code_spawn_work_failed",
         "job_id": "j_aaa", "agent": "code-implementer", "elapsed_s": 30,
         "terminal_reason": "max_turns", "channel_id": "c"},
        {"timestamp": _ts(0.5), "type": "claude_code_spawn_work_failed",
         "job_id": "j_bbb", "agent": "code-implementer", "elapsed_s": 15,
         "terminal_reason": "parse_failed", "channel_id": "c"},
    ])
    negatives, _ = log.recent()
    # Both incidents surface — distinct job_ids → distinct rendered lines.
    assert len(negatives) == 2
    contents = [s.content for s in negatives]
    assert any("j_aaa" in c for c in contents)
    assert any("j_bbb" in c for c in contents)


def test_content_dedup_independent_across_polarities(tmp_path: Path):
    """A line with the same text appearing as both positive and
    negative (hypothetical) wouldn't collide — dedup is per-polarity.
    Practical case: ensures positive saga_feedback_sent dedup doesn't
    interact with negative lines that happen to share content shape."""
    log = _make_log(tmp_path, events=[
        # Two negatives that collapse, two positives that collapse.
        {"timestamp": _ts(3.0), "type": "tool_call_denied",
         "tool": "Read", "reason": "path_outside_home", "channel_id": "c"},
        {"timestamp": _ts(2.0), "type": "tool_call_denied",
         "tool": "Read", "reason": "path_outside_home", "channel_id": "c"},
        {"timestamp": _ts(1.0), "type": "saga_feedback_sent", "n_atoms": 5,
         "channel_id": "c"},
        {"timestamp": _ts(0.5), "type": "saga_feedback_sent", "n_atoms": 5,
         "channel_id": "c"},
    ])
    negatives, positives = log.recent()
    assert len(negatives) == 1
    assert len(positives) == 1


def test_saga_feedback_sent_dedups_to_most_recent(tmp_path: Path):
    """Poller-heavy windows fire saga_feedback_sent once per wakeup;
    without dedup the algedonic block fills with 5+ identical-shape
    "N atoms credited" lines that crowd out actually-actionable
    positive signals. Only the most recent should render.

    Stacks with the content-level dedup above: kind-level
    (``saga_feedback`` in ``_FIRST_OCCURRENCE_ONLY_KINDS``) catches
    different-content saga_feedback_sent firings (different
    ``n_atoms``); content-level catches identical strings under any
    kind."""
    log = _make_log(tmp_path, events=[
        {"timestamp": _ts(5.0), "type": "saga_feedback_sent",
         "n_atoms": 99, "channel_id": "poller:github-activity"},
        {"timestamp": _ts(3.0), "type": "saga_feedback_sent",
         "n_atoms": 88, "channel_id": "poller:github-activity"},
        # Most recent — only this one should render.
        {"timestamp": _ts(0.5), "type": "saga_feedback_sent",
         "n_atoms": 7, "channel_id": "discord-1"},
    ])
    block = log.recent_block()
    assert block is not None
    assert "7 atoms credited" in block
    assert "99 atoms credited" not in block
    assert "88 atoms credited" not in block
    # And the algedonic block should contain exactly one
    # saga_feedback_sent line (no double-render).
    assert block.count("saga_feedback_sent") == 1


def test_chainlink65_paired_positive_kinds_render(tmp_path: Path):
    """chainlink #65 (sub B): the new positive event kinds —
    ``ntfy_post_ok`` / ``git_push_ok`` / ``git_pull_ok`` /
    ``git_fetch_ok`` / ``shell_job_complete_enqueue_ok`` — each
    render with a brief past-tense one-liner so the operator can
    read recovery against the sticky 24h failure line."""
    log = _make_log(tmp_path, events=[
        {"timestamp": _ts(0.5), "type": "ntfy_post_ok",
         "category": "discord-down", "dedupe_key": "discord-down:outbound"},
        {"timestamp": _ts(0.4), "type": "git_push_ok", "turn_id": "t1"},
        {"timestamp": _ts(0.3), "type": "git_pull_ok",
         "path": "/mimir-home"},
        {"timestamp": _ts(0.2), "type": "git_fetch_ok",
         "path": "/mimir-home"},
        {"timestamp": _ts(0.1), "type": "shell_job_complete_enqueue_ok",
         "job_id": "j_abc"},
    ])
    block = log.recent_block()
    assert block is not None
    assert "Positive" in block
    assert "ntfy post succeeded" in block
    assert "git push to mimirbot-state succeeded" in block
    assert "git pull --ff-only succeeded" in block
    assert "git fetch succeeded" in block
    assert "shell job j_abc wake-up enqueued" in block


def test_chainlink65_paired_positives_surface_next_to_failures(tmp_path: Path):
    """Operator-decided shape (chainlink #36 comment 42): the paired
    success and the sticky failure line must surface side-by-side so
    the contrast ("old failure + recent success = transient,
    recovered") is readable. Both polarity blocks render with their
    respective lines."""
    log = _make_log(tmp_path, events=[
        # Old failure — first-occurrence-only-deduped sticky line.
        {"timestamp": _ts(20.0), "type": "git_push_failed",
         "reason": "ssh: connection refused", "returncode": 128,
         "turn_id": "t_old"},
        # Recent recovery — the paired positive.
        {"timestamp": _ts(0.5), "type": "git_push_ok",
         "turn_id": "t_new"},
    ])
    block = log.recent_block()
    assert block is not None
    # Negative block carries the sticky failure.
    assert "git push to mimirbot-state failed" in block
    # Positive block carries the paired success.
    assert "git push to mimirbot-state succeeded" in block
    # Both subsections present.
    assert "Negative" in block
    assert "Positive" in block


def test_chainlink65_paired_positives_dedup_to_most_recent(tmp_path: Path):
    """All five new kinds are in ``_FIRST_OCCURRENCE_ONLY_KINDS`` so
    a healthy pipeline emitting on every poll/turn doesn't crowd the
    24h algedonic window. Tail-first iteration means the kept item
    is always the most recent."""
    log = _make_log(tmp_path, events=[
        # Three git_push_ok in the window — only most recent renders.
        {"timestamp": _ts(5.0), "type": "git_push_ok", "turn_id": "t_a"},
        {"timestamp": _ts(2.0), "type": "git_push_ok", "turn_id": "t_b"},
        {"timestamp": _ts(0.5), "type": "git_push_ok", "turn_id": "t_c"},
        # And three ntfy_post_ok — same dedup shape.
        {"timestamp": _ts(4.0), "type": "ntfy_post_ok",
         "category": "x", "dedupe_key": "k1"},
        {"timestamp": _ts(1.5), "type": "ntfy_post_ok",
         "category": "x", "dedupe_key": "k1"},
        {"timestamp": _ts(0.2), "type": "ntfy_post_ok",
         "category": "x", "dedupe_key": "k1"},
    ])
    negatives, positives = log.recent()
    # Exactly one git_push_ok line and one ntfy_post_ok line surface.
    push_oks = [s for s in positives if s.kind == "git_push_ok"]
    ntfy_oks = [s for s in positives if s.kind == "ntfy_post_ok"]
    assert len(push_oks) == 1
    assert len(ntfy_oks) == 1


def test_chainlink65_polarity_dynamic_invariant_holds(tmp_path: Path):
    """Acceptance criterion #6: adding the new positive kinds must not
    violate the polarity-dynamic invariant (line 257 assertion in
    feedback.py). The assertion would fire at import time if any of
    the new kinds were polarity-dynamic AND in
    ``_FIRST_OCCURRENCE_ONLY_KINDS``. This test re-confirms the
    expectation by checking the sets directly."""
    from mimir.feedback import (
        _FIRST_OCCURRENCE_ONLY_KINDS,
        _POLARITY_DYNAMIC_KINDS,
    )
    new_kinds = {
        "ntfy_post_ok", "git_push_ok", "git_pull_ok", "git_fetch_ok",
        "shell_job_complete_enqueue_ok",
    }
    # All new kinds are in the first-only set.
    assert new_kinds.issubset(_FIRST_OCCURRENCE_ONLY_KINDS)
    # None of the new kinds are polarity-dynamic.
    assert _POLARITY_DYNAMIC_KINDS.isdisjoint(new_kinds)
    # And the global invariant still holds.
    assert _POLARITY_DYNAMIC_KINDS.isdisjoint(_FIRST_OCCURRENCE_ONLY_KINDS)


def test_introspection_report_error_renders_as_negative(tmp_path: Path):
    log = _make_log(tmp_path, events=[
        {
            "timestamp": _ts(0.5),
            "type": "introspection_report_error",
            "session_id": "s",
            "error": "OSError: events.jsonl missing",
        },
    ])
    block = log.recent_block()
    assert block is not None
    assert "Negative" in block
    assert "introspection report failed" in block
    assert "events.jsonl missing" in block


def test_scheduled_tick_suppressed_renders_in_feedback(tmp_path: Path):
    log = _make_log(tmp_path, events=[
        {
            "timestamp": _ts(0.5),
            "type": "scheduled_tick_suppressed",
            "session_id": "s",
            "reason": "plan_window_saturated:7d_opus@0.92",
        },
    ])
    block = log.recent_block()
    assert block is not None
    assert "suppressed by arbiter" in block
    assert "plan_window_saturated" in block


# ---- pending_forget_candidates_count -------------------------------------


def test_pending_forget_returns_none_when_no_decay_event(tmp_path: Path):
    events = tmp_path / "events.jsonl"
    _write_jsonl(events, [
        {"timestamp": _ts(1), "type": "saga_consolidate_ok",
         "result": {"clusters_processed": 3}},
    ])
    assert pending_forget_candidates_count(events) is None


def test_pending_forget_returns_none_when_decay_had_zero_candidates(tmp_path: Path):
    events = tmp_path / "events.jsonl"
    _write_jsonl(events, [
        {"timestamp": _ts(1), "type": "saga_decay_ok",
         "result": {"forgetting_candidates": 0}},
    ])
    assert pending_forget_candidates_count(events) is None


def test_pending_forget_returns_count_from_latest_decay(tmp_path: Path):
    events = tmp_path / "events.jsonl"
    _write_jsonl(events, [
        {"timestamp": _ts(50), "type": "saga_decay_ok",
         "result": {"forgetting_candidates": 3}},  # older — ignored
        {"timestamp": _ts(1), "type": "saga_decay_ok",
         "result": {"forgetting_candidates": 7}},  # latest — wins
    ])
    assert pending_forget_candidates_count(events) == 7


def test_pending_forget_clears_when_forget_after_decay(tmp_path: Path):
    """Block clears on saga_forget_ok presence newer than the latest
    saga_decay_ok — count arithmetic deliberately not attempted."""
    events = tmp_path / "events.jsonl"
    _write_jsonl(events, [
        {"timestamp": _ts(2), "type": "saga_decay_ok",
         "result": {"forgetting_candidates": 7}},
        {"timestamp": _ts(1), "type": "saga_forget_ok",
         "actions_taken": 4},  # any forget event clears, regardless of count
    ])
    assert pending_forget_candidates_count(events) is None


def test_pending_forget_persists_when_forget_predates_decay(tmp_path: Path):
    """A forget from BEFORE the most recent decay doesn't clear the
    block — the new decay's count is the source of truth."""
    events = tmp_path / "events.jsonl"
    _write_jsonl(events, [
        {"timestamp": _ts(50), "type": "saga_forget_ok",
         "actions_taken": 4},
        {"timestamp": _ts(1), "type": "saga_decay_ok",
         "result": {"forgetting_candidates": 5}},
    ])
    assert pending_forget_candidates_count(events) == 5


def test_pending_forget_returns_none_when_file_missing(tmp_path: Path):
    assert pending_forget_candidates_count(tmp_path / "nope.jsonl") is None


# ─── Commitments Phase 2b ───────────────────────────────────────────


def test_commitment_due_renders_with_metadata(tmp_path: Path):
    log = _make_log(tmp_path, events=[
        {"timestamp": _ts(0.5), "type": "commitment_due",
         "commitment_id": "c-abc12", "channel_id": "chan-1",
         "text": "Review PR #111",
         "recipient_identity": "alice",
         "suggested_reminder": "PR #111 still open",
         "kind": "agent_promise"},
    ])
    block = log.recent_block()
    assert block is not None
    assert "Positive" in block
    assert "c-abc12" in block
    assert "Review PR #111" in block
    assert "@alice" in block
    assert "chan=chan-1" in block


def test_commitment_expired_is_negative(tmp_path: Path):
    """commitment_expired surfaces under Negative with the 'reflect at
    next session boundary' framing — operator can grep for EXPIRED in
    the prompt block."""
    log = _make_log(tmp_path, events=[
        {"timestamp": _ts(0.5), "type": "commitment_expired",
         "commitment_id": "c-ghi34", "channel_id": "chan-2",
         "text": "Send draft",
         "kind": "agent_promise"},
    ])
    block = log.recent_block()
    assert block is not None
    assert "Negative" in block
    assert "EXPIRED" in block
    assert "c-ghi34" in block
    assert "Send draft" in block
    assert "next session boundary" in block


def test_commitment_due_dedup_to_latest(tmp_path: Path):
    """First-occurrence-only at the algedonic layer — multiple
    commitment_due lines in a single window surface only the most
    recent (Phase 3 prompt block carries the full pending list)."""
    log = _make_log(tmp_path, events=[
        {"timestamp": _ts(5.0), "type": "commitment_due",
         "commitment_id": "c-old", "text": "First", "kind": "agent_promise"},
        {"timestamp": _ts(2.0), "type": "commitment_due",
         "commitment_id": "c-mid", "text": "Middle", "kind": "agent_promise"},
        {"timestamp": _ts(0.5), "type": "commitment_due",
         "commitment_id": "c-new", "text": "Newest", "kind": "agent_promise"},
    ])
    block = log.recent_block()
    assert block is not None
    # Only the newest should render.
    assert "c-new" in block
    assert "c-old" not in block
    assert "c-mid" not in block


def test_commitment_snooze_pileup_negative(tmp_path: Path):
    log = _make_log(tmp_path, events=[
        {"timestamp": _ts(0.5), "type": "commitment_snooze_pileup",
         "commitment_id": "c-punted",
         "text": "Read paper",
         "snooze_count": 4,
         "threshold": 3,
         "kind": "open_loop"},
    ])
    block = log.recent_block()
    assert block is not None
    assert "Negative" in block
    assert "c-punted" in block
    assert "4×" in block
    assert "threshold 3" in block
    assert "committing or dismissing" in block


def test_commitments_polarity_dynamic_invariant_holds():
    """Adding the 3 new commitment_* kinds must not break the
    polarity-dynamic disjointness invariant (assertion in
    feedback.py at import time)."""
    from mimir.feedback import (
        _FIRST_OCCURRENCE_ONLY_KINDS,
        _POLARITY_DYNAMIC_KINDS,
    )
    new_kinds = {
        "commitment_due", "commitment_expired", "commitment_snooze_pileup",
    }
    assert new_kinds.issubset(_FIRST_OCCURRENCE_ONLY_KINDS)
    assert _POLARITY_DYNAMIC_KINDS.isdisjoint(new_kinds)


# ─── Algedonic pipeline gaps (PR algedonic-gaps-5) ──────────────────


def test_tool_call_budget_denied_renders_as_negative(tmp_path: Path):
    """Gap 4 fix: budget_gate.py emits ``tool_call_budget_denied`` but
    _EVENT_RULES previously only had ``tool_call_budget_warning``.
    The two actual event names must now surface as negatives."""
    log = _make_log(tmp_path, events=[
        {"timestamp": _ts(1.0), "type": "tool_call_budget_denied",
         "channel_id": "c", "tool": "bash_exec", "limit": 10, "used": 11},
    ])
    negatives, positives = log.recent()
    assert any("tool_budget" in s.kind for s in negatives), (
        "tool_call_budget_denied should surface as a tool_budget negative"
    )
    assert len(positives) == 0


def test_tool_call_budget_soft_warning_renders_as_negative(tmp_path: Path):
    """Gap 4 fix: ``tool_call_budget_soft_warning`` (the other event name
    emitted by budget_gate.py) must also render as a negative."""
    log = _make_log(tmp_path, events=[
        {"timestamp": _ts(1.0), "type": "tool_call_budget_soft_warning",
         "channel_id": "c", "tool": "shell_exec", "limit": 20, "used": 18},
    ])
    negatives, _ = log.recent()
    assert any("tool_budget" in s.kind for s in negatives), (
        "tool_call_budget_soft_warning should surface as a tool_budget negative"
    )
