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
    _VALENCE_GROUPS,
    _annotate_transitions,
    _compute_group_runs,
    _format_chain,
    _synthesize_chain_signals,
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
    """Alg-2 temporal run detection supersedes the old side-by-side paired-positive
    shape (chainlink #36 comment 42): a git_push_failed → git_push_ok sequence is
    now rendered as a recovery chain rather than two separate lines.

    The contrast ("old failure + recent success = transient, recovered") is still
    readable — now encoded as a single chain line in the Positive block:
    "git push: failed ×1 → succeeded ×1 [recovery]"
    """
    log = _make_log(tmp_path, events=[
        # Old failure.
        {"timestamp": _ts(20.0), "type": "git_push_failed",
         "reason": "ssh: connection refused", "returncode": 128,
         "turn_id": "t_old"},
        # Recent recovery.
        {"timestamp": _ts(0.5), "type": "git_push_ok",
         "turn_id": "t_new"},
    ])
    negatives, positives = log.recent()
    block = log.recent_block()
    assert block is not None

    # Chain signal is in the Positive block (most recent run = ok).
    chain = [s for s in positives if s.kind == "git_push_chain"]
    assert len(chain) == 1
    assert "failed ×1" in chain[0].content
    assert "succeeded ×1" in chain[0].content
    assert "[recovery]" in chain[0].content

    # The chain renders the full contrast — individual lines are absent.
    assert not any(s.kind == "git_push_ok" for s in positives)
    assert not any(s.kind == "git_push_failed" for s in negatives)

    # Only the Positive subsection is present (recovery → positive polarity).
    assert "Positive" in block
    assert "git push:" in block


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


# ─── Alg-2: Beer arousal filter — count tracking and threshold gating ───


def test_count_default_is_1_for_single_occurrence(tmp_path: Path):
    """A single event in the window produces a FeedbackSignal with count=1.
    No suffix in the rendered block — discrete events aren't annotated."""
    log = _make_log(tmp_path, events=[
        {"timestamp": _ts(0.5), "type": "git_push_failed",
         "reason": "ssh refused", "returncode": 128, "turn_id": "t"},
    ])
    negatives, _ = log.recent()
    assert len(negatives) == 1
    assert negatives[0].count == 1

    block = log.recent_block()
    assert block is not None
    assert "×" not in block  # no count suffix for one-offs


def test_count_tracks_all_window_occurrences_for_first_occurrence_only_kind(
    tmp_path: Path,
):
    """Kinds in _FIRST_OCCURRENCE_ONLY_KINDS show only the most recent
    occurrence, but the attached count reflects ALL window occurrences.
    This is the core arousal-filter signal: 'git_push_ok ×47' vs '×1'."""
    log = _make_log(tmp_path, events=[
        {"timestamp": _ts(5.0), "type": "git_push_ok", "turn_id": "t_a"},
        {"timestamp": _ts(2.0), "type": "git_push_ok", "turn_id": "t_b"},
        {"timestamp": _ts(0.5), "type": "git_push_ok", "turn_id": "t_c"},
    ])
    _, positives = log.recent()
    assert len(positives) == 1  # only most-recent shown
    assert positives[0].count == 3  # but count reflects all 3


def test_count_suffix_renders_in_block_when_gt_1(tmp_path: Path):
    """The rendered block includes '(×N in 24h)' when a kind fires
    more than once in the window — pattern visibility per Alg-2."""
    log = _make_log(tmp_path, events=[
        {"timestamp": _ts(2.0), "type": "git_push_ok", "turn_id": "t_a"},
        {"timestamp": _ts(0.5), "type": "git_push_ok", "turn_id": "t_b"},
    ])
    block = log.recent_block()
    assert block is not None
    assert "×2 in 24h" in block


def test_count_suffix_uses_actual_window_hours(tmp_path: Path):
    """Count suffix uses the caller-supplied window_hours, not a hardcoded 24."""
    log = _make_log(tmp_path, events=[
        {"timestamp": _ts(0.5), "type": "git_push_ok", "turn_id": "t_a"},
        {"timestamp": _ts(0.2), "type": "git_push_ok", "turn_id": "t_b"},
    ])
    block = log.recent_block(window_hours=48)
    assert block is not None
    assert "×2 in 48h" in block


def test_count_tracks_by_rule_kind_not_event_type(tmp_path: Path):
    """Different event types that map to the same rule kind share the
    kind-level count. E.g. tool_call_budget_denied and
    tool_call_budget_soft_warning both map to rule kind 'tool_budget'."""
    log = _make_log(tmp_path, events=[
        {"timestamp": _ts(2.0), "type": "tool_call_budget_denied",
         "channel_id": "c", "count": 11, "budget": 10},
        {"timestamp": _ts(0.5), "type": "tool_call_budget_soft_warning",
         "channel_id": "c", "count": 9, "budget": 10},
    ])
    negatives, _ = log.recent()
    # Both events have kind "tool_budget"; combined count = 2.
    tool_budget_signals = [s for s in negatives if s.kind == "tool_budget"]
    assert len(tool_budget_signals) >= 1
    assert tool_budget_signals[0].count == 2


def test_arousal_threshold_suppresses_below_min_occurrences(tmp_path: Path):
    """A kind with threshold=2 and only 1 occurrence is suppressed.
    Per Beer: single-occurrence doesn't clear the statistical filter."""
    log = _make_log(tmp_path, events=[
        {"timestamp": _ts(0.5), "type": "error",
         "where": "test", "error": "one-off", "channel_id": "c"},
    ])
    # Override: require 2 occurrences for "error" kind before surfacing.
    log = _make_log(tmp_path, events=[
        {"timestamp": _ts(0.5), "type": "error",
         "where": "test", "error": "one-off", "channel_id": "c"},
    ])
    log.arousal_thresholds = {"error": 2}
    negatives, positives = log.recent()
    # error was the only event; below threshold → suppressed.
    assert len(negatives) == 0
    assert len(positives) == 0


def test_arousal_threshold_surfaces_when_count_meets_threshold(tmp_path: Path):
    """A kind with threshold=2 and exactly 2 occurrences clears the filter
    and surfaces with count=2 attached."""
    log = _make_log(tmp_path, events=[
        {"timestamp": _ts(2.0), "type": "error",
         "where": "test", "error": "first", "channel_id": "c"},
        {"timestamp": _ts(0.5), "type": "error",
         "where": "test", "error": "second", "channel_id": "c"},
    ])
    log.arousal_thresholds = {"error": 2}
    negatives, _ = log.recent()
    assert len(negatives) > 0
    assert negatives[0].count == 2


def test_arousal_threshold_default_1_does_not_suppress_anything(tmp_path: Path):
    """With default thresholds (empty dict → all default to 1), every
    matching event surfaces on first occurrence — existing behaviour preserved."""
    log = _make_log(tmp_path, events=[
        {"timestamp": _ts(0.5), "type": "tool_call_denied",
         "tool": "file_search", "reason": "budget", "channel_id": "c"},
    ])
    # No custom thresholds — should behave exactly as before Alg-2.
    assert log.arousal_thresholds is None
    negatives, _ = log.recent()
    assert len(negatives) == 1


def test_count_on_non_first_occurrence_kind_aggregates(tmp_path: Path):
    """For kinds NOT in _FIRST_OCCURRENCE_ONLY_KINDS with distinct content,
    each occurrence gets its own slot AND carries the total kind count.
    The count on each is the same (total kind occurrences in window), not
    the per-content occurrence count."""
    log = _make_log(tmp_path, events=[
        {"timestamp": _ts(2.0), "type": "tool_call_denied",
         "tool": "Read", "reason": "outside_home", "channel_id": "c"},
        {"timestamp": _ts(0.5), "type": "tool_call_denied",
         "tool": "Write", "reason": "outside_home", "channel_id": "c"},
    ])
    negatives, _ = log.recent()
    # Both denials surface (distinct content).
    assert len(negatives) == 2
    # Each carries the total kind count (2 tool_denied events).
    assert all(s.count == 2 for s in negatives)
    block = log.recent_block()
    # Both lines show the count suffix.
    assert block.count("×2 in 24h") == 2


# ---------------------------------------------------------------------------
# TestRunDetection — Alg-2 temporal run detection
# (spec: state/spec/alg2-temporal-runs-spec.md)
# ---------------------------------------------------------------------------

def _ts_seq(*offsets_hours: float) -> list[str]:
    """Return ISO timestamps for a sequence of offsets_hours-ago values."""
    now = datetime.now(tz=timezone.utc)
    return [(now - timedelta(hours=h)).isoformat() for h in offsets_hours]


def _push_events(ts: str, kind_str: str) -> dict:
    """Build a git_push_ok or git_push_failed event at the given timestamp."""
    type_map = {
        "ok": "git_push_ok",
        "failed": "git_push_failed",
        "stale": "git_push_stale",
    }
    return {"type": type_map[kind_str], "timestamp": ts}


def test_basic_recovery_chain(tmp_path: Path):
    """20× git_push_ok → 5× git_push_failed → 1× git_push_ok → [recovery] in positives."""
    # Timestamps: oldest (0.9h) to newest (0.1h), in chronological order.
    ts_ok1 = [_ts(0.9 - i * 0.03) for i in range(20)]   # oldest 20
    ts_fail = [_ts(0.30 - i * 0.02) for i in range(5)]   # middle 5
    ts_ok2 = [_ts(0.09)]                                   # newest 1
    events = (
        [{"type": "git_push_ok", "timestamp": t} for t in ts_ok1]
        + [{"type": "git_push_failed", "timestamp": t} for t in ts_fail]
        + [{"type": "git_push_ok", "timestamp": t} for t in ts_ok2]
    )
    log = _make_log(tmp_path, events=events)
    negatives, positives = log.recent()

    # No separate git_push_ok / git_push_failed lines — all consumed by chain.
    assert not any(s.kind == "git_push_ok" for s in positives)
    assert not any(s.kind == "git_push_failed" for s in negatives)

    # Chain signal is in the positive bucket (most recent run = ok).
    chain = [s for s in positives if s.kind == "git_push_chain"]
    assert len(chain) == 1
    sig = chain[0]
    assert sig.polarity == "positive"
    assert "succeeded ×20" in sig.content
    assert "failed ×5" in sig.content
    assert "succeeded ×1" in sig.content
    assert "[recovery]" in sig.content
    assert sig.content.startswith("git push:")


def test_basic_degradation_chain(tmp_path: Path):
    """10× git_push_ok → 3× git_push_failed → [degradation] in negatives."""
    ts_ok = [_ts(0.5 - i * 0.02) for i in range(10)]
    ts_fail = [_ts(0.25 - i * 0.02) for i in range(3)]
    events = (
        [{"type": "git_push_ok", "timestamp": t} for t in ts_ok]
        + [{"type": "git_push_failed", "timestamp": t} for t in ts_fail]
    )
    log = _make_log(tmp_path, events=events)
    negatives, positives = log.recent()

    chain = [s for s in negatives if s.kind == "git_push_chain"]
    assert len(chain) == 1
    sig = chain[0]
    assert sig.polarity == "negative"
    assert "succeeded ×10" in sig.content
    assert "failed ×3" in sig.content
    assert "[degradation]" in sig.content


def test_steady_run_no_chain(tmp_path: Path):
    """47× git_push_ok with no failures → no chain, standard count display."""
    ts_list = [_ts(0.9 - i * 0.01) for i in range(47)]
    events = [{"type": "git_push_ok", "timestamp": t} for t in ts_list]
    log = _make_log(tmp_path, events=events)
    negatives, positives = log.recent()

    # No chain signal.
    assert not any(s.kind == "git_push_chain" for s in positives)
    assert not any(s.kind == "git_push_chain" for s in negatives)

    # Standard first-occurrence-only with count.
    ok_signals = [s for s in positives if s.kind == "git_push_ok"]
    assert len(ok_signals) == 1
    assert ok_signals[0].count == 47
    block = log.recent_block()
    assert "×47 in 24h" in block


def test_multiple_transitions(tmp_path: Path):
    """5×ok → 2×failed → 3×ok → 1×failed → [degradation] in negatives."""
    times = [
        *[_ts(1.0 - i * 0.05) for i in range(5)],   # 5 ok
        *[_ts(0.70 - i * 0.05) for i in range(2)],  # 2 failed
        *[_ts(0.50 - i * 0.05) for i in range(3)],  # 3 ok
        *[_ts(0.20 - i * 0.05) for i in range(1)],  # 1 failed
    ]
    types = (
        ["git_push_ok"] * 5
        + ["git_push_failed"] * 2
        + ["git_push_ok"] * 3
        + ["git_push_failed"] * 1
    )
    events = [{"type": t, "timestamp": ts} for t, ts in zip(types, times)]
    log = _make_log(tmp_path, events=events)
    negatives, _ = log.recent()

    chain = [s for s in negatives if s.kind == "git_push_chain"]
    assert len(chain) == 1
    sig = chain[0]
    assert sig.polarity == "negative"
    assert "succeeded ×5" in sig.content
    assert "failed ×2" in sig.content
    assert "succeeded ×3" in sig.content
    assert "failed ×1" in sig.content
    assert "[degradation]" in sig.content


def test_chain_over_5_runs_compressed(tmp_path: Path):
    """6 runs: ok×10, failed×3, ok×5, failed×2, ok×8, failed×1 — middle 2 compressed."""
    times = [
        *[_ts(1.2 - i * 0.05) for i in range(10)],  # ok×10
        *[_ts(0.65 - i * 0.05) for i in range(3)],  # failed×3
        *[_ts(0.40 - i * 0.05) for i in range(5)],  # ok×5
        *[_ts(0.15 - i * 0.03) for i in range(2)],  # failed×2
        *[_ts(0.07 - i * 0.005) for i in range(8)], # ok×8
        *[_ts(0.02)],                                # failed×1
    ]
    types = (
        ["git_push_ok"] * 10
        + ["git_push_failed"] * 3
        + ["git_push_ok"] * 5
        + ["git_push_failed"] * 2
        + ["git_push_ok"] * 8
        + ["git_push_failed"] * 1
    )
    events = [{"type": t, "timestamp": ts} for t, ts in zip(types, times)]
    log = _make_log(tmp_path, events=events)
    negatives, _ = log.recent()

    chain = [s for s in negatives if s.kind == "git_push_chain"]
    assert len(chain) == 1
    content = chain[0].content
    # First 2 runs visible.
    assert "succeeded ×10" in content
    assert "failed ×3" in content
    # Middle 2 compressed.
    assert "... (2 more)" in content
    # Last 2 runs visible.
    assert "succeeded ×8" in content
    assert "failed ×1" in content
    assert "[degradation]" in content


def test_chain_consumed_kinds_not_duplicated(tmp_path: Path):
    """Chain signal is present; no separate git_push_ok / git_push_failed line."""
    # Same setup as test_basic_recovery_chain (abbreviated).
    ts_ok1 = [_ts(0.9 - i * 0.03) for i in range(5)]
    ts_fail = [_ts(0.30 - i * 0.02) for i in range(2)]
    ts_ok2 = [_ts(0.09)]
    events = (
        [{"type": "git_push_ok", "timestamp": t} for t in ts_ok1]
        + [{"type": "git_push_failed", "timestamp": t} for t in ts_fail]
        + [{"type": "git_push_ok", "timestamp": t} for t in ts_ok2]
    )
    log = _make_log(tmp_path, events=events)
    negatives, positives = log.recent()
    all_signals = negatives + positives

    # Chain rendered it.
    assert any(s.kind == "git_push_chain" for s in all_signals)
    # Individual kinds not present.
    assert not any(s.kind == "git_push_ok" for s in all_signals)
    assert not any(s.kind == "git_push_failed" for s in all_signals)


def test_non_grouped_kind_unaffected(tmp_path: Path):
    """A kind not in any valence group uses the existing count-display path."""
    events = [
        {"type": "saga_query_error", "timestamp": _ts(0.5),
         "error": "db locked"},
        {"type": "saga_query_error", "timestamp": _ts(0.3),
         "error": "db locked"},
        {"type": "saga_query_error", "timestamp": _ts(0.1),
         "error": "db locked"},
    ]
    log = _make_log(tmp_path, events=events)
    negatives, _ = log.recent()

    # 3 identical-content events → content-dedup collapses to 1, count=3.
    saga_signals = [s for s in negatives if s.kind == "saga_query_error"]
    assert len(saga_signals) == 1
    assert saga_signals[0].count == 3
    block = log.recent_block()
    assert "×3 in 24h" in block


def test_single_group_event_no_chain(tmp_path: Path):
    """Only 1 git_push_ok event → 1 run, no transition, no chain."""
    events = [{"type": "git_push_ok", "timestamp": _ts(0.5)}]
    log = _make_log(tmp_path, events=events)
    negatives, positives = log.recent()

    assert not any(s.kind == "git_push_chain" for s in positives + negatives)
    assert any(s.kind == "git_push_ok" for s in positives)


def test_interleaved_unrelated_events(tmp_path: Path):
    """oauth_usage_ok between two git_push events doesn't break the git_push run."""
    events = [
        {"type": "git_push_ok", "timestamp": _ts(0.5)},
        {"type": "oauth_usage_ok", "timestamp": _ts(0.3),   # unrelated
         "recorded": {}},
        {"type": "git_push_failed", "timestamp": _ts(0.1)},
    ]
    log = _make_log(tmp_path, events=events)
    negatives, positives = log.recent()

    # git_push chain is in negatives (most recent run = failed).
    chain = [s for s in negatives if s.kind == "git_push_chain"]
    assert len(chain) == 1
    assert "succeeded ×1" in chain[0].content
    assert "failed ×1" in chain[0].content
    assert "[degradation]" in chain[0].content

    # oauth chain: only one event, so no chain — displays normally.
    assert not any(s.kind == "oauth_chain" for s in positives + negatives)


def test_chain_timestamp_is_most_recent(tmp_path: Path):
    """The chain FeedbackSignal.ts matches the ts of the last event in the chain."""
    ts_ok = _ts(0.5)
    ts_fail = _ts(0.1)
    events = [
        {"type": "git_push_ok", "timestamp": ts_ok},
        {"type": "git_push_failed", "timestamp": ts_fail},
    ]
    log = _make_log(tmp_path, events=events)
    negatives, _ = log.recent()

    chain = [s for s in negatives if s.kind == "git_push_chain"]
    assert len(chain) == 1
    # ts_fail is the most recent event.
    assert chain[0].ts == ts_fail


def test_chain_no_count_suffix_in_rendered_block(tmp_path: Path):
    """Chain signals don't get a redundant (×N in 24h) suffix in the block."""
    ts_ok = [_ts(0.5 - i * 0.05) for i in range(3)]
    ts_fail = [_ts(0.2 - i * 0.05) for i in range(2)]
    events = (
        [{"type": "git_push_ok", "timestamp": t} for t in ts_ok]
        + [{"type": "git_push_failed", "timestamp": t} for t in ts_fail]
    )
    log = _make_log(tmp_path, events=events)
    block = log.recent_block()

    # Chain line is present.
    assert "git push:" in block
    assert "[degradation]" in block
    # No total count suffix tacked on (counts are inline per-run).
    assert "(×5 in 24h)" not in block


def test_same_polarity_runs_no_chain(tmp_path: Path):
    """git_push_stale followed by git_push_failed — both negative, no transition."""
    events = [
        {"type": "git_push_stale", "timestamp": _ts(0.5)},
        {"type": "git_push_stale", "timestamp": _ts(0.4)},
        {"type": "git_push_failed", "timestamp": _ts(0.2)},
    ]
    log = _make_log(tmp_path, events=events)
    negatives, positives = log.recent()

    # No chain (both runs are negative polarity → no polarity transition).
    assert not any(s.kind == "git_push_chain" for s in negatives + positives)


# Unit tests for internal functions.

def _make_runs_source(tmp_path: Path, events: list[dict]) -> Path:
    """Write events.jsonl and return the path."""
    p = tmp_path / "events.jsonl"
    _write_jsonl(p, events)
    return p


def test_compute_group_runs_basic(tmp_path: Path):
    """_compute_group_runs returns chronological runs per group."""
    now = datetime.now(tz=timezone.utc)
    cutoff = (now - timedelta(hours=24)).isoformat()
    ts1 = (now - timedelta(minutes=30)).isoformat()
    ts2 = (now - timedelta(minutes=20)).isoformat()
    ts3 = (now - timedelta(minutes=10)).isoformat()
    events = [
        {"type": "git_push_ok", "timestamp": ts1},
        {"type": "git_push_ok", "timestamp": ts2},
        {"type": "git_push_failed", "timestamp": ts3},
    ]
    p = _make_runs_source(tmp_path, events)
    group_runs = _compute_group_runs(None, p, cutoff, _VALENCE_GROUPS)

    assert "git_push" in group_runs
    runs = group_runs["git_push"]
    assert len(runs) == 2
    assert runs[0].kind == "git_push_ok" and runs[0].count == 2
    assert runs[1].kind == "git_push_failed" and runs[1].count == 1
    # Chronological: start_ts of run1 < start_ts of run2.
    assert runs[0].start_ts < runs[1].start_ts


def test_annotate_transitions_recovery():
    from mimir.feedback import Run
    runs = [
        Run("git_push", "git_push_failed", "negative", 3, "t1", "t2"),
        Run("git_push", "git_push_ok", "positive", 1, "t3", "t3"),
    ]
    annotated = _annotate_transitions(runs)
    assert annotated[0].transition_type is None
    assert annotated[1].transition_type == "recovery"


def test_annotate_transitions_degradation():
    from mimir.feedback import Run
    runs = [
        Run("git_push", "git_push_ok", "positive", 10, "t1", "t2"),
        Run("git_push", "git_push_failed", "negative", 3, "t3", "t4"),
    ]
    annotated = _annotate_transitions(runs)
    assert annotated[0].transition_type is None
    assert annotated[1].transition_type == "degradation"


def test_format_chain_no_compression():
    from mimir.feedback import Run, AnnotatedRun
    group = _VALENCE_GROUPS["git_push"]
    runs = [
        Run("git_push", "git_push_ok", "positive", 20, "t1", "t2"),
        Run("git_push", "git_push_failed", "negative", 5, "t3", "t4"),
        Run("git_push", "git_push_ok", "positive", 1, "t5", "t5"),
    ]
    annotated = [
        AnnotatedRun(run=runs[0], transition_type=None),
        AnnotatedRun(run=runs[1], transition_type="degradation"),
        AnnotatedRun(run=runs[2], transition_type="recovery"),
    ]
    result = _format_chain(annotated, group)
    assert result == "git push: succeeded ×20 → failed ×5 → succeeded ×1 [recovery]"


def test_synthesize_chain_signals_has_transition(tmp_path: Path):
    """_synthesize_chain_signals returns a signal for groups with transitions."""
    from mimir.feedback import Run
    group_runs = {
        "git_push": [
            Run("git_push", "git_push_ok", "positive", 10, "t1", "t2"),
            Run("git_push", "git_push_failed", "negative", 3, "t3", "t4"),
        ]
    }
    signals, consumed = _synthesize_chain_signals(group_runs, _VALENCE_GROUPS)
    assert len(signals) == 1
    assert signals[0].kind == "git_push_chain"
    assert signals[0].polarity == "negative"
    assert "git_push_ok" in consumed
    assert "git_push_failed" in consumed
    assert "git_push_stale" in consumed


def test_synthesize_chain_signals_no_transition(tmp_path: Path):
    """_synthesize_chain_signals returns nothing when all runs share a polarity."""
    from mimir.feedback import Run
    group_runs = {
        "git_push": [
            Run("git_push", "git_push_ok", "positive", 10, "t1", "t2"),
            Run("git_push", "git_push_ok", "positive", 5, "t3", "t4"),
        ]
    }
    signals, consumed = _synthesize_chain_signals(group_runs, _VALENCE_GROUPS)
    assert signals == []
    assert consumed == set()


def test_valence_groups_disjoint():
    """All _VALENCE_GROUPS kind sets are disjoint — no kind appears twice."""
    all_kinds: list[str] = []
    for group in _VALENCE_GROUPS.values():
        all_kinds.extend(group.positive_kinds)
        all_kinds.extend(group.negative_kinds)
    assert len(all_kinds) == len(set(all_kinds)), (
        "Duplicate kind found across valence groups"
    )


def test_viability_group_all_kinds_covered():
    """All viability event kinds in _EVENT_RULES are in the viability ValenceGroup."""
    from mimir.feedback import _EVENT_RULES
    viability_rule_kinds = {
        v[1] for k, v in _EVENT_RULES.items()
        if k.startswith(("collapse_risk_", "curation_below_threshold_",
                          "viability_report_"))
    }
    vg = _VALENCE_GROUPS["viability"]
    covered = vg.positive_kinds | vg.negative_kinds
    assert viability_rule_kinds <= covered, (
        f"Uncovered viability kinds: {viability_rule_kinds - covered}"
    )


# ---------------------------------------------------------------------------
# Alg-3: auto-escalation tests
# ---------------------------------------------------------------------------

def test_alg3_escalation_emits_event_when_threshold_crossed(tmp_path: Path):
    """When a negative kind crosses its threshold, recent() emits an
    algedonic_escalation event visible via _escalated_kinds_in_window on
    the next call, and the rendered negative block contains the escalation line."""
    from mimir.feedback import (
        _ESCALATION_THRESHOLDS,
        _escalated_kinds_in_window,
    )
    from mimir.event_logger import init_logger, _reset_logger_for_tests
    from datetime import datetime, timedelta, timezone

    events_path = tmp_path / "logs" / "events.jsonl"
    events_path.parent.mkdir(parents=True)

    # Use git_push_failed (threshold=3) — seed exactly at threshold.
    threshold = _ESCALATION_THRESHOLDS["git_push_failed"]
    assert threshold == 3  # guard — update test if constant changes
    _write_jsonl(events_path, [
        {"timestamp": _ts(h), "type": "git_push_failed", "reason": "ssh refused"}
        for h in [20.0, 10.0, 1.0]
    ])

    init_logger(events_path, session_id="test-alg3-emit")
    try:
        feedback_log = FeedbackLog(
            events_path=events_path,
            turns_path=tmp_path / "logs" / "turns.jsonl",
        )
        negatives, _ = feedback_log.recent()

        # algedonic_escalation event should now be in events.jsonl.
        cutoff_iso = (
            datetime.now(tz=timezone.utc) - timedelta(hours=24)
        ).isoformat()
        escalated = _escalated_kinds_in_window(None, events_path, cutoff_iso)
        assert "git_push_failed" in escalated, (
            "expected algedonic_escalation(kind=git_push_failed) written to events.jsonl"
        )

        # Rendered block should contain the escalation line.
        block = feedback_log.recent_block()
        assert block is not None
        assert "algedonic escalation" in block
        assert "git_push_failed" in block
    finally:
        _reset_logger_for_tests()


def test_alg3_escalation_deduped_within_window(tmp_path: Path):
    """Calling recent() twice in the same window emits the escalation event
    exactly once — the second call sees it in already_escalated and skips."""
    from mimir.event_logger import init_logger, _reset_logger_for_tests

    events_path = tmp_path / "logs" / "events.jsonl"
    events_path.parent.mkdir(parents=True)
    _write_jsonl(events_path, [
        {"timestamp": _ts(h), "type": "git_push_failed", "reason": "auth"}
        for h in [20.0, 10.0, 1.0]  # at threshold of 3
    ])

    init_logger(events_path, session_id="test-alg3-dedup")
    try:
        feedback_log = FeedbackLog(
            events_path=events_path,
            turns_path=tmp_path / "logs" / "turns.jsonl",
        )
        feedback_log.recent()   # first call — emits 1 escalation
        feedback_log.recent()   # second call — must NOT emit again

        escalation_events = [
            json.loads(line)
            for line in events_path.read_text().splitlines()
            if line.strip()
            and json.loads(line).get("type") == "algedonic_escalation"
            and json.loads(line).get("kind") == "git_push_failed"
        ]
        assert len(escalation_events) == 1, (
            f"expected exactly 1 escalation event, got {len(escalation_events)}"
        )
    finally:
        _reset_logger_for_tests()


def test_alg3_below_threshold_no_escalation(tmp_path: Path):
    """When count < threshold, no algedonic_escalation event is emitted."""
    from mimir.event_logger import init_logger, _reset_logger_for_tests
    from mimir.feedback import _ESCALATION_THRESHOLDS

    threshold = _ESCALATION_THRESHOLDS["git_push_failed"]  # 3
    events_path = tmp_path / "logs" / "events.jsonl"
    events_path.parent.mkdir(parents=True)
    # Only threshold-1 events — must not trigger escalation.
    _write_jsonl(events_path, [
        {"timestamp": _ts(h), "type": "git_push_failed", "reason": "timeout"}
        for h in range(threshold - 1)
    ])

    init_logger(events_path, session_id="test-alg3-below")
    try:
        FeedbackLog(
            events_path=events_path,
            turns_path=tmp_path / "logs" / "turns.jsonl",
        ).recent()

        escalation_events = [
            json.loads(line)
            for line in events_path.read_text().splitlines()
            if line.strip()
            and json.loads(line).get("type") == "algedonic_escalation"
        ]
        assert escalation_events == [], (
            f"expected no escalation events below threshold, got {escalation_events}"
        )
    finally:
        _reset_logger_for_tests()
