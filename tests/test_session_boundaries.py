"""Session-summary rendering for the prompt-assembly path.

Covers ``render_session_summaries`` layout, the chainlink #63 staleness
markers, the ``closed_since`` corrective-overrides applier, and
``count_turns_since`` arithmetic.
"""

from __future__ import annotations

import json
from pathlib import Path

from mimir.session_boundary_log import render_session_summaries


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


# ---- chainlink #63: staleness markers + closed_since applier -----------

from datetime import datetime, timezone

from mimir.session_boundary_log import (
    _apply_closed_since,
    _format_relative_age,
    _format_turn_count,
    count_turns_since,
    count_turns_since_many,
)


_NOW = datetime(2026, 5, 9, 18, 0, 0, tzinfo=timezone.utc)


# ---- _format_relative_age ----------------------------------------------


def test_format_relative_age_buckets():
    """Buckets match feedback.py's ``target_age`` pattern: <1m / Nm /
    ~Nh / ~Nd. Boundary cases: 60s == 1m (not <1m), 3600s == ~1h
    (not 60m), 86400s == ~1d."""
    assert _format_relative_age("2026-05-09T17:59:30+00:00", _NOW) == "<1m ago"
    assert _format_relative_age("2026-05-09T17:30:00+00:00", _NOW) == "30m ago"
    assert _format_relative_age("2026-05-09T14:00:00+00:00", _NOW) == "~4h ago"
    assert _format_relative_age("2026-05-06T18:00:00+00:00", _NOW) == "~3d ago"


def test_format_relative_age_returns_none_when_unparseable():
    assert _format_relative_age("garbage", _NOW) is None
    assert _format_relative_age("", _NOW) is None


def test_format_relative_age_returns_none_when_now_is_none():
    """The ``now`` param is the wall-clock reference; without it,
    age can't be computed and the renderer skips the marker."""
    assert _format_relative_age("2026-05-09T14:00:00+00:00", None) is None


def test_format_relative_age_handles_z_suffix():
    """ISO-8601 with ``Z`` suffix is common — fromisoformat needs the
    ``+00:00`` form, so the parser converts it."""
    assert _format_relative_age("2026-05-09T14:00:00Z", _NOW) == "~4h ago"


# ---- _format_turn_count -------------------------------------------------


def test_format_turn_count_singular_and_plural():
    assert _format_turn_count(0) == "0 turns this channel"
    assert _format_turn_count(1) == "1 turn this channel"
    assert _format_turn_count(8) == "8 turns this channel"


# ---- _apply_closed_since -----------------------------------------------


def test_apply_closed_since_drops_substring_match():
    out = _apply_closed_since(
        ["PRs #71 + #72 awaiting operator review/merge",
         "schedule follow-up with Alice"],
        ["#71"],
    )
    # "#71" appears in the first item → dropped. Second item stays.
    assert out == ["schedule follow-up with Alice"]


def test_apply_closed_since_partial_resolution_drops_whole_item():
    """Documented behavior (chainlink #63): when only #71 of "#71 + #72"
    is resolved, the whole "PRs #71 + #72" item drops because of the
    substring match. The synthesis turn is expected to re-list the
    still-open piece (e.g. "PR #72") in the new boundary's
    ``unfinished`` so the live state carries forward."""
    out = _apply_closed_since(
        ["PRs #71 + #72 awaiting", "PR #72 still awaiting"],
        ["#71"],
    )
    # First dropped (contains #71); second kept (contains only #72).
    assert out == ["PR #72 still awaiting"]


def test_apply_closed_since_case_insensitive():
    out = _apply_closed_since(
        ["chainlink #29 G17 awaiting"],
        ["CHAINLINK #29 G17"],
    )
    assert out == []


def test_apply_closed_since_empty_refs_keeps_all():
    out = _apply_closed_since(
        ["item one", "item two"], [],
    )
    assert out == ["item one", "item two"]


def test_apply_closed_since_strips_blank_items():
    out = _apply_closed_since(
        ["", "  ", "real item"], [],
    )
    assert out == ["real item"]


# ---- _apply_closed_since: digit-aware word boundary (PR #86 nit) -------


def test_apply_closed_since_digit_boundary_short_ref_no_collision():
    """Mimir's PR #86 nit: ``#1`` must NOT substring-match ``#10`` /
    ``#100``. Digit-aware lookahead/lookbehind closes the collision
    while keeping ``#1`` matching when it stands alone."""
    out = _apply_closed_since(
        ["chainlink #1 awaiting", "PR #10 still pending", "PR #100 in flight"],
        ["#1"],
    )
    # "chainlink #1 awaiting" → #1 followed by space (not digit) → match → drop.
    # "#10 still pending" → #1 followed by 0 (digit) → no match → keep.
    # "#100 in flight" → #1 followed by 0 (digit) → no match → keep.
    assert out == ["PR #10 still pending", "PR #100 in flight"]


def test_apply_closed_since_digit_boundary_left_side():
    """Symmetric guard: ``#1`` shouldn't match a longer ref where #1
    sits at the right edge of digits (e.g. ``#11`` contains ``11``,
    where ``1`` could be confused with the second digit)."""
    out = _apply_closed_since(
        ["#11 still pending"],
        ["#1"],
    )
    # In "#11", the "#1" position is at chars 0-1, followed by digit '1' →
    # lookahead fails → no match → "#11" kept.
    assert out == ["#11 still pending"]


def test_apply_closed_since_multi_digit_refs_still_match():
    """Sanity: the digit-boundary rule doesn't break legitimate
    multi-digit refs. ``#71`` still matches ``"PR #71 awaiting"``."""
    out = _apply_closed_since(
        ["PR #71 awaiting", "PR #72 still open"],
        ["#71"],
    )
    assert out == ["PR #72 still open"]


def test_apply_closed_since_chainlink_id_with_subref():
    """Refs with internal whitespace + non-digit chars (e.g. chainlink
    sub-question IDs like ``chainlink #29 G17``) still match end-to-end
    when present verbatim."""
    out = _apply_closed_since(
        ["chainlink #29 G17 awaiting", "chainlink #29 G18 awaiting"],
        ["chainlink #29 G17"],
    )
    # G17 matches; G18 does not (different sub-id).
    assert out == ["chainlink #29 G18 awaiting"]


def test_apply_closed_since_logs_drops_at_debug(caplog):
    """Mimir's PR #86 observability nit: dropped items log at DEBUG so
    future-mimir debugging "why didn't this show up?" has an audit
    trail. Caller is expected to enable DEBUG logging when needed."""
    import logging
    with caplog.at_level(logging.DEBUG, logger="mimir.session_boundary_log"):
        _apply_closed_since(["PR #71 awaiting"], ["#71"])
    # caplog captures the ``log.debug(...)`` call inside the applier.
    drops = [
        r for r in caplog.records
        if "session_summary_unfinished_filtered" in r.getMessage()
    ]
    assert len(drops) == 1
    assert "PR #71 awaiting" in drops[0].getMessage()


# ---- closed_since asymmetry: later resolves earlier (PR #86 nit) -------


def test_closed_since_does_not_apply_to_later_boundaries():
    """Mimir's PR #86 correctness nit: T1 closes #71, T2 (later)
    re-lists ``"#71 reverted, reopened"`` as unfinished. T2's item
    must NOT be dropped — closed_since is asymmetric (later resolves
    earlier, not the other way around). Pre-fix this test would
    fail because the global aggregation was time-blind."""
    boundaries = [
        # T2 (newest first per recent() ordering) — re-lists #71 as
        # unfinished after a revert.
        {
            "ts": "2026-05-09T17:00:00+00:00",
            "channel_id": "c",
            "summary": "Revert landed.",
            "unfinished": ["#71 reverted, reopened"],
        },
        # T1 — earlier closure.
        {
            "ts": "2026-05-09T13:00:00+00:00",
            "channel_id": "c",
            "summary": "Initial #71 merge.",
            "unfinished": [],
            "closed_since": ["#71"],
        },
    ]
    out = render_session_summaries(boundaries, now=_NOW)
    # T2's "#71 reverted, reopened" must survive — it's the live state.
    assert "#71 reverted, reopened" in out


def test_closed_since_self_does_not_drop_self_unfinished():
    """A boundary's own closed_since records what the synthesis-turn
    resolved during the just-ended session. The boundary's own
    ``unfinished`` is the curated list of what's still open AFTER
    those closures — self-closed_since shouldn't filter
    self-unfinished. The asymmetric rule (later only) handles this
    naturally because the boundary isn't "later than" itself."""
    boundaries = [{
        "ts": "2026-05-09T17:00:00+00:00",
        "channel_id": "c",
        "summary": "s",
        "unfinished": ["#71 still has a follow-up to do"],
        "closed_since": ["#71"],  # initial #71 merge happened mid-session
    }]
    out = render_session_summaries(boundaries, now=_NOW)
    # The agent's intentional re-listing must survive its own
    # closed_since.
    assert "#71 still has a follow-up to do" in out


def test_closed_since_unparseable_timestamps_apply_conservatively():
    """When timestamps can't be parsed, comparison is impossible;
    fall back to applying all closed_since (preserves the older
    symmetric behavior). Documented edge case — production traffic
    has parseable ISO timestamps."""
    boundaries = [
        {
            "ts": "garbage-ts-1",
            "channel_id": "c",
            "summary": "s",
            "unfinished": ["#71 awaiting"],
        },
        {
            "ts": "garbage-ts-2",
            "channel_id": "c",
            "summary": "s",
            "closed_since": ["#71"],
        },
    ]
    out = render_session_summaries(boundaries, now=_NOW)
    assert "#71 awaiting" not in out


# ---- render_session_summaries: header markers ---------------------------


def test_render_no_markers_when_no_now_and_no_turn_counts():
    """Backwards-compat shape: legacy callers passing only
    ``boundaries`` get the original layout — no age, no turn count."""
    boundaries = [{
        "ts": "2026-05-09T14:00:00+00:00",
        "channel_id": "discord-1",
        "summary": "test",
    }]
    out = render_session_summaries(boundaries)
    assert "(discord-1)" in out
    assert "ago" not in out
    assert "turns" not in out


def test_render_age_marker_when_now_supplied():
    boundaries = [{
        "ts": "2026-05-09T14:00:00+00:00",
        "channel_id": "discord-1",
        "summary": "test",
    }]
    out = render_session_summaries(boundaries, now=_NOW)
    assert "(~4h ago) (discord-1)" in out


def test_render_turn_count_marker_when_counts_supplied():
    """Empty turn_counts dict still triggers rendering — explicit
    "0 turns this channel" is more informative than absence."""
    boundaries = [{
        "ts": "2026-05-09T14:00:00+00:00",
        "channel_id": "discord-1",
        "summary": "test",
    }]
    out = render_session_summaries(
        boundaries,
        turn_counts={"2026-05-09T14:00:00+00:00": 8},
    )
    assert "(8 turns this channel) (discord-1)" in out


def test_render_both_markers_when_both_supplied():
    """Mimir's chainlink #63 example shape: ``12:23 (~4h ago, 8 turns
    this channel) — <summary>``."""
    boundaries = [{
        "ts": "2026-05-09T14:00:00+00:00",
        "channel_id": "discord-1",
        "summary": "test",
    }]
    out = render_session_summaries(
        boundaries,
        now=_NOW,
        turn_counts={"2026-05-09T14:00:00+00:00": 8},
    )
    assert "(~4h ago, 8 turns this channel) (discord-1)" in out


# ---- render_session_summaries: [verify before quoting] suffix ----------


def test_verify_suffix_trips_on_age_alone():
    """Age >= 2h, turns < 5 → suffix appears (age signal alone is enough)."""
    boundaries = [{
        "ts": "2026-05-09T14:00:00+00:00",  # 4h ago
        "channel_id": "c",
        "summary": "s",
        "unfinished": ["item one"],
    }]
    out = render_session_summaries(
        boundaries, now=_NOW,
        turn_counts={"2026-05-09T14:00:00+00:00": 0},
        stale_age_hours=2, stale_turns=5,
    )
    assert "Unfinished [verify before quoting]: item one" in out


def test_verify_suffix_trips_on_turns_alone():
    """Age < 2h, turns >= 5 → suffix appears (turn signal alone is enough)."""
    boundaries = [{
        "ts": "2026-05-09T17:30:00+00:00",  # 30m ago — under age threshold
        "channel_id": "c",
        "summary": "s",
        "unfinished": ["item one"],
    }]
    out = render_session_summaries(
        boundaries, now=_NOW,
        turn_counts={"2026-05-09T17:30:00+00:00": 8},
        stale_age_hours=2, stale_turns=5,
    )
    assert "Unfinished [verify before quoting]: item one" in out


def test_verify_suffix_absent_when_neither_threshold_trips():
    boundaries = [{
        "ts": "2026-05-09T17:30:00+00:00",
        "channel_id": "c",
        "summary": "s",
        "unfinished": ["item one"],
    }]
    out = render_session_summaries(
        boundaries, now=_NOW,
        turn_counts={"2026-05-09T17:30:00+00:00": 2},
        stale_age_hours=2, stale_turns=5,
    )
    assert "Unfinished: item one" in out
    assert "verify before quoting" not in out


def test_thresholds_configurable():
    """Tightening the age threshold to 1h trips on the same 30m-old
    boundary IF we lower it below the actual age. Locks the
    contract that callers can tune both knobs."""
    boundaries = [{
        "ts": "2026-05-09T17:00:00+00:00",  # 1h ago
        "channel_id": "c",
        "summary": "s",
        "unfinished": ["item one"],
    }]
    out_relaxed = render_session_summaries(
        boundaries, now=_NOW,
        turn_counts={"2026-05-09T17:00:00+00:00": 0},
        stale_age_hours=2, stale_turns=5,
    )
    assert "verify before quoting" not in out_relaxed
    out_strict = render_session_summaries(
        boundaries, now=_NOW,
        turn_counts={"2026-05-09T17:00:00+00:00": 0},
        stale_age_hours=1, stale_turns=5,
    )
    assert "verify before quoting" in out_strict


# ---- render_session_summaries: closed_since applier --------------------


def test_closed_since_drops_resolved_items_from_earlier_boundary():
    """The acceptance scenario from chainlink #63: T0 boundary lists
    #71 unfinished, T2 boundary's closed_since=[#71], and the prompt
    builder drops #71 from the T0 rendering."""
    boundaries = [
        {
            "ts": "2026-05-09T14:00:00+00:00",
            "channel_id": "discord-1",
            "summary": "Worked on PRs.",
            "unfinished": ["PR #71 awaiting merge", "follow up with Alice"],
        },
        {
            "ts": "2026-05-09T17:00:00+00:00",
            "channel_id": "discord-1",
            "summary": "Followed up.",
            "unfinished": [],
            "closed_since": ["#71"],
        },
    ]
    out = render_session_summaries(boundaries, now=_NOW)
    # T0's "PR #71" got dropped (matches #71); "follow up with Alice"
    # remains because no closed_since ref appears in it.
    assert "PR #71 awaiting" not in out
    assert "follow up with Alice" in out


def test_closed_since_short_refs_ignored():
    """Single-character refs would over-match (e.g. "#" appears in any
    PR ref). The applier filters refs below
    ``_MIN_CLOSED_SINCE_REF_LEN`` before substring-matching."""
    boundaries = [
        {
            "ts": "2026-05-09T14:00:00+00:00",
            "channel_id": "discord-1",
            "summary": "s",
            "unfinished": ["PR #71 awaiting"],
        },
        {
            "ts": "2026-05-09T17:00:00+00:00",
            "channel_id": "discord-1",
            "summary": "s",
            "closed_since": ["#"],  # too short — should be ignored
        },
    ]
    out = render_session_summaries(boundaries, now=_NOW)
    assert "PR #71 awaiting" in out


def test_closed_since_aggregates_across_later_boundaries():
    """Multiple later boundaries' closed_since refs all apply to
    earlier Unfinished items."""
    boundaries = [
        {
            "ts": "2026-05-09T10:00:00+00:00",
            "channel_id": "c",
            "summary": "s",
            "unfinished": ["#71", "#72", "still open"],
        },
        {
            "ts": "2026-05-09T13:00:00+00:00",
            "channel_id": "c",
            "summary": "s",
            "closed_since": ["#71"],
        },
        {
            "ts": "2026-05-09T17:00:00+00:00",
            "channel_id": "c",
            "summary": "s",
            "closed_since": ["#72"],
        },
    ]
    out = render_session_summaries(boundaries, now=_NOW)
    # #71 closed by middle boundary, #72 closed by last; both drop.
    # The "still open" item stays.
    # Note: T1's unfinished list is just ["#71", "#72", "still open"];
    # only "still open" should remain after closed_since application.
    assert "still open" in out
    # Look at the rendered Unfinished line for T1 (the only boundary
    # with unfinished items): it should contain ONLY "still open".
    # Refs #71/#72 substring-match every literal occurrence of those
    # in unfinished items, so they drop.
    unfinished_lines = [l for l in out.splitlines() if "Unfinished" in l]
    assert len(unfinished_lines) == 1
    assert "#71" not in unfinished_lines[0]
    assert "#72" not in unfinished_lines[0]
    assert "still open" in unfinished_lines[0]


# ---- render: SAGA-shape compatibility (latent fix) ---------------------


def test_render_accepts_saga_native_field_names():
    """SAGA's ``get_last_sessions`` historically returned ``timestamp``
    + ``channel`` (not ``ts`` + ``channel_id``). The renderer now
    accepts both shapes so a future SAGA-sourced boundary doesn't
    show up as ``- (-) — (no summary)``."""
    boundaries = [{
        "timestamp": "2026-05-09T14:00:00+00:00",
        "channel": "discord-1",
        "summary": "saga-shape test",
    }]
    out = render_session_summaries(boundaries, now=_NOW)
    assert "2026-05-09 14:00" in out
    assert "(discord-1)" in out
    assert "saga-shape test" in out


# ---- count_turns_since --------------------------------------------------


def test_count_turns_since_basic(tmp_path: Path):
    """Walks turns.jsonl and counts records with ``ts > since`` on the
    target channel. Records on other channels and records at-or-before
    the cutoff don't contribute."""
    path = tmp_path / "turns.jsonl"
    path.write_text(
        json.dumps({"ts": "2026-05-09T13:00:00+00:00", "channel_id": "c"}) + "\n"
        + json.dumps({"ts": "2026-05-09T15:00:00+00:00", "channel_id": "c"}) + "\n"
        + json.dumps({"ts": "2026-05-09T16:00:00+00:00", "channel_id": "c"}) + "\n"
        + json.dumps({"ts": "2026-05-09T15:30:00+00:00", "channel_id": "other"}) + "\n"
    )
    n = count_turns_since(
        path, channel_id="c", since_ts="2026-05-09T14:00:00+00:00",
    )
    # 15:00 + 16:00 are after 14:00 on channel c → 2.
    # 13:00 is before; 15:30 is on the other channel.
    assert n == 2


def test_count_turns_since_missing_path_returns_zero(tmp_path: Path):
    n = count_turns_since(
        tmp_path / "nope.jsonl",
        channel_id="c", since_ts="2026-05-09T14:00:00+00:00",
    )
    assert n == 0


def test_count_turns_since_empty_since_returns_zero(tmp_path: Path):
    """Empty since_ts would otherwise match every record (any string >
    ""). Guard so a boundary with no ``ts`` doesn't accidentally count
    every turn ever."""
    path = tmp_path / "turns.jsonl"
    path.write_text(
        json.dumps({"ts": "2026-05-09T13:00:00+00:00", "channel_id": "c"}) + "\n"
    )
    n = count_turns_since(path, channel_id="c", since_ts="")
    assert n == 0


def test_count_turns_since_uses_snapshot_callable_when_supplied(tmp_path: Path):
    """The agent passes ``self._turns_snapshot.records`` so the cached
    in-memory tail is reused. Confirm the helper consults the
    callable instead of re-reading the file."""
    fake_records: list[dict] = [
        {"ts": "2026-05-09T15:00:00+00:00", "channel_id": "c"},
        {"ts": "2026-05-09T16:00:00+00:00", "channel_id": "c"},
        {"ts": "2026-05-09T13:00:00+00:00", "channel_id": "c"},
    ]
    n = count_turns_since(
        tmp_path / "does-not-exist.jsonl",
        channel_id="c",
        since_ts="2026-05-09T14:00:00+00:00",
        snapshot_records=lambda: fake_records,
    )
    assert n == 2


def test_count_turns_since_many_scans_records_once(tmp_path: Path):
    """Multiple boundary headers should be annotated from one records pass.

    The agent calls this helper inside ``asyncio.to_thread``; keeping all
    cutoffs in one synchronous pass prevents one tail-drain per recent
    boundary.
    """
    calls = 0

    def fake_records():
        nonlocal calls
        calls += 1
        return [
            {"ts": "2026-05-09T17:00:00+00:00", "channel_id": "c"},
            {"ts": "2026-05-09T15:00:00+00:00", "channel_id": "c"},
            {"ts": "2026-05-09T13:00:00+00:00", "channel_id": "c"},
            {"ts": "2026-05-09T16:00:00+00:00", "channel_id": "other"},
        ]

    counts = count_turns_since_many(
        tmp_path / "does-not-exist.jsonl",
        channel_id="c",
        since_timestamps=[
            "2026-05-09T12:00:00+00:00",
            "2026-05-09T14:00:00+00:00",
            "2026-05-09T16:00:00+00:00",
            "",
        ],
        snapshot_records=fake_records,
    )

    assert calls == 1
    assert counts == {
        "2026-05-09T12:00:00+00:00": 3,
        "2026-05-09T14:00:00+00:00": 2,
        "2026-05-09T16:00:00+00:00": 1,
    }


# ---- end-to-end T0/T1/T2 acceptance scenario ---------------------------


def test_acceptance_t0_t1_t2_drops_resolved_items():
    """Acceptance criterion from chainlink #63:
       T0:    boundary written with Unfinished=[#X]
       T0+1h: turn closes #X
       T0+2h: synthesis writes a closed_since=[#X] corrective override.
       Subsequent prompt: #X must NOT appear as unfinished.

    Feeds the renderer the same boundary shape SagaStore's
    ``recent_session_boundaries()`` returns; verifies closed_since
    aggregation flows across boundaries and drops the resolved item."""
    # Newest-first ordering matches what recent_session_boundaries()
    # produces. The renderer accepts whatever order the source supplies;
    # closed_since aggregation walks all boundaries regardless of order.
    boundaries = [
        {
            "ts": "2026-05-09T16:00:00+00:00",  # T0+2h, newer
            "channel_id": "c",
            "saga_session_id": "s2",
            "summary": "Followed up; #X merged.",
            "unfinished": [],
            "closed_since": ["#X"],
        },
        {
            "ts": "2026-05-09T14:00:00+00:00",  # T0, older
            "channel_id": "c",
            "saga_session_id": "s1",
            "summary": "Worked on #X.",
            "unfinished": ["PR #X awaiting operator merge"],
        },
    ]
    out = render_session_summaries(boundaries, now=_NOW)
    assert out is not None
    # The T0 Unfinished item containing "#X" must not appear.
    assert "PR #X awaiting operator merge" not in out
    # The corrective summary line still renders.
    assert "Followed up; #X merged." in out
