"""Tests for the daily proposed-changes backlog cron callable
(``mimir/reflection/proposed_changes_health.py``)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from textwrap import dedent

import pytest

from mimir.reflection.proposed_changes_health import (
    BacklogHealth,
    compute_backlog_health,
    run_scheduled_backlog_check,
    _iter_pending_proposal_dates,
)


def _seed_proposed_changes(home: Path, body: str) -> None:
    path = home / "state" / "proposed-changes.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(body).lstrip("\n"), encoding="utf-8")


# ─── _iter_pending_proposal_dates ────────────────────────────────────────


def test_iter_dates_only_under_pending(tmp_path: Path):
    """Proposals under Applied / Rejected must NOT be counted as pending."""
    body = """
        # Proposed Changes

        ## Pending

        ## 2026-05-01 — pending one
        body

        ## 2026-05-10 — pending two
        body

        ## Applied

        ## 2026-04-01 — applied long ago
        body

        ## Rejected

        ## 2026-04-15 — rejected
        body
    """
    raw = dedent(body).lstrip("\n")
    dates = _iter_pending_proposal_dates(raw)
    assert len(dates) == 2
    iso_dates = sorted(str(d) for d in dates)
    assert iso_dates == ["2026-05-01", "2026-05-10"]


def test_iter_dates_fence_aware(tmp_path: Path):
    """A ``##`` line inside a fenced code block is NOT a heading. Regression
    for chainlink #114 — proposal bodies often include fenced samples with
    their own ``##`` headings."""
    body = """
        # Proposed Changes

        ## Pending

        ## 2026-05-01 — pending with fenced sample
        body before

        ```
        ## 2026-04-01 — fake date in fence
        not a real proposal heading
        ```

        more body after
    """
    raw = dedent(body).lstrip("\n")
    dates = _iter_pending_proposal_dates(raw)
    assert len(dates) == 1
    assert str(dates[0]) == "2026-05-01"


def test_iter_dates_ignores_non_date_headings(tmp_path: Path):
    """Headings like ``## Format per item:`` (prose, not a proposal) are
    skipped."""
    body = """
        # Proposed Changes

        ## Pending

        ## Format per item:
        Documentation, not a real proposal.

        ## 2026-05-01 — real proposal
        body
    """
    raw = dedent(body).lstrip("\n")
    dates = _iter_pending_proposal_dates(raw)
    assert len(dates) == 1


# ─── compute_backlog_health ──────────────────────────────────────────────


def test_health_clean_when_file_missing(tmp_path: Path):
    """No proposed-changes.md → no-issues health snapshot (fresh deployment)."""
    health = compute_backlog_health(tmp_path)
    assert health.pending_count == 0
    assert health.oldest_age_days is None
    assert not health.backlog_exceeded
    assert health.issues == []


def test_health_clean_under_thresholds(tmp_path: Path):
    """3 recent pending proposals → under count + age thresholds, no issues."""
    now = datetime(2026, 5, 24, tzinfo=timezone.utc)
    _seed_proposed_changes(tmp_path, """
        # Proposed Changes

        ## Pending

        ## 2026-05-20 — one
        body

        ## 2026-05-21 — two
        body

        ## 2026-05-22 — three
        body

        ## Applied
    """)
    health = compute_backlog_health(tmp_path, now=now)
    assert health.pending_count == 3
    assert health.oldest_age_days == 4  # 2026-05-24 - 2026-05-20
    assert not health.backlog_exceeded
    assert health.issues == []


def test_health_count_threshold_crossed(tmp_path: Path):
    """10 pending proposals → count threshold triggers, regardless of age."""
    now = datetime(2026, 5, 24, tzinfo=timezone.utc)
    entries = "\n\n".join(
        f"## 2026-05-{day:02d} — proposal {day}\nbody"
        for day in range(15, 25)  # 10 proposals
    )
    _seed_proposed_changes(tmp_path, f"""
        # Proposed Changes

        ## Pending

        {entries}

        ## Applied
    """)
    health = compute_backlog_health(tmp_path, now=now)
    assert health.pending_count == 10
    assert health.backlog_exceeded
    assert any("10 pending" in i for i in health.issues)


def test_health_age_threshold_crossed(tmp_path: Path):
    """One proposal aged 30 days → age threshold triggers, even with low count."""
    now = datetime(2026, 5, 24, tzinfo=timezone.utc)
    _seed_proposed_changes(tmp_path, """
        # Proposed Changes

        ## Pending

        ## 2026-04-20 — old proposal
        body
    """)
    health = compute_backlog_health(tmp_path, now=now)
    assert health.pending_count == 1
    assert health.oldest_age_days == 34
    assert health.backlog_exceeded
    assert any("oldest pending is 34d" in i for i in health.issues)


def test_health_both_thresholds_crossed(tmp_path: Path):
    """Both count and age thresholds crossed → both issues reported."""
    now = datetime(2026, 5, 24, tzinfo=timezone.utc)
    entries = "\n\n".join(
        f"## 2026-04-{day:02d} — old proposal {day}\nbody"
        for day in range(1, 12)  # 11 proposals, oldest from April
    )
    _seed_proposed_changes(tmp_path, f"""
        # Proposed Changes

        ## Pending

        {entries}

        ## Applied
    """)
    health = compute_backlog_health(tmp_path, now=now)
    assert health.pending_count == 11
    assert health.backlog_exceeded
    assert len(health.issues) == 2  # one for count, one for age


def test_health_custom_thresholds(tmp_path: Path):
    """Operator-tuned thresholds override the defaults."""
    now = datetime(2026, 5, 24, tzinfo=timezone.utc)
    _seed_proposed_changes(tmp_path, """
        # Proposed Changes

        ## Pending

        ## 2026-05-22 — one
        body

        ## 2026-05-23 — two
        body
    """)
    # Default count threshold is 10; setting to 2 should now trigger.
    health = compute_backlog_health(tmp_path, pending_threshold=2, now=now)
    assert health.backlog_exceeded


def test_health_no_pending_section(tmp_path: Path):
    """File exists but has no ``## Pending`` header → count=0, no issues."""
    _seed_proposed_changes(tmp_path, """
        # Proposed Changes

        ## Applied

        ## 2026-04-15 — applied entry
        body
    """)
    health = compute_backlog_health(tmp_path)
    assert health.pending_count == 0
    assert not health.backlog_exceeded


def test_health_empty_pending_section(tmp_path: Path):
    """``## Pending`` with no proposals under it → count=0."""
    _seed_proposed_changes(tmp_path, """
        # Proposed Changes

        ## Pending

        ## Applied
    """)
    health = compute_backlog_health(tmp_path)
    assert health.pending_count == 0
    assert not health.backlog_exceeded


# ─── run_scheduled_backlog_check ────────────────────────────────────────


@pytest.mark.asyncio
async def test_scheduled_check_emits_when_threshold_crossed(tmp_path: Path):
    """When backlog is over threshold, the cron emits a
    ``proposed_changes_backlog`` event with count + oldest age."""
    from mimir.event_logger import init_logger, _reset_logger_for_tests

    events_path = tmp_path / "logs" / "events.jsonl"
    events_path.parent.mkdir(parents=True)
    init_logger(events_path, session_id="test-backlog")
    try:
        now = datetime(2026, 5, 24, tzinfo=timezone.utc)
        entries = "\n\n".join(
            f"## 2026-04-{day:02d} — old proposal {day}\nbody"
            for day in range(1, 12)
        )
        _seed_proposed_changes(tmp_path, f"""
            # Proposed Changes

            ## Pending

            {entries}
        """)
        # Patch datetime so the age computation is deterministic.
        import mimir.reflection.proposed_changes_health as mod
        original_compute = mod.compute_backlog_health
        mod.compute_backlog_health = lambda home, **kw: original_compute(home, now=now, **kw)
        try:
            await run_scheduled_backlog_check(tmp_path)
        finally:
            mod.compute_backlog_health = original_compute

        # Verify event landed in jsonl.
        lines = events_path.read_text().splitlines()
        events = [json.loads(l) for l in lines if l.strip()]
        backlog_events = [e for e in events if e.get("type") == "proposed_changes_backlog"]
        assert len(backlog_events) == 1
        ev = backlog_events[0]
        assert ev["pending_count"] == 11
        assert ev["oldest_age_days"] >= 21
    finally:
        _reset_logger_for_tests()


@pytest.mark.asyncio
async def test_scheduled_check_silent_when_under_threshold(tmp_path: Path):
    """Under-threshold runs emit nothing — no event noise on healthy state."""
    from mimir.event_logger import init_logger, _reset_logger_for_tests

    events_path = tmp_path / "logs" / "events.jsonl"
    events_path.parent.mkdir(parents=True)
    init_logger(events_path, session_id="test-backlog-quiet")
    try:
        _seed_proposed_changes(tmp_path, """
            # Proposed Changes

            ## Pending

            ## 2026-05-23 — recent one
            body
        """)
        await run_scheduled_backlog_check(tmp_path)
        # No proposed_changes_backlog event should appear.
        if events_path.is_file():
            events = [
                json.loads(l) for l in events_path.read_text().splitlines() if l.strip()
            ]
            assert not any(
                e.get("type") == "proposed_changes_backlog" for e in events
            )
    finally:
        _reset_logger_for_tests()
