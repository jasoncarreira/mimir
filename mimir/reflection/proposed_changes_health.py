"""Backlog health check for ``state/proposed-changes.md``.

The reflection skill writes proposals to ``state/proposed-changes.md`` under
the ``## Pending`` header. The operator reviews and applies them via
``mimir reflection mark-applied <id>`` on their own cadence. If the operator
falls behind, the pending list grows unbounded — the agent has no
between-reflection signal that the human-in-the-loop loop is broken.

This module is the daily cron callable that surfaces backlog growth as an
algedonic event. Cadence: 07:00 UTC daily (chosen to land before typical
operator work hours so the signal is visible at the start of the day).

Thresholds (calibrated against early production data):
- ``pending_threshold = 10`` — operator reviews have fallen behind enough
  to be visible at-a-glance.
- ``oldest_age_threshold_days = 21`` — three weeks of inaction on the oldest
  pending suggests it was de-facto dropped rather than waiting on review.

Either threshold crossing triggers an emit; the algedonic block then surfaces
the count + oldest-age every turn until the operator reduces the backlog.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


# Matches a proposal heading: ``## YYYY-MM-DD — title`` (or ``-`` instead of
# ``—``, and an optional time/qualifier like ``(night)`` or ``(PM)``). The
# date is the first 10 chars after ``## `` so the regex is generous with the
# rest of the line.
_PROPOSAL_HEADING_RE = re.compile(
    r"^##\s+(\d{4}-\d{2}-\d{2})\b",
)

# Top-level bucket headers — ``## Pending`` / ``## Applied`` / ``## Rejected``.
# Anything else under ``##`` at the document level is a proposal or a fenced
# inner heading (the latter handled by the fence-tracker below).
_BUCKET_HEADERS = ("pending", "applied", "rejected")


@dataclass(frozen=True)
class BacklogHealth:
    """Snapshot of pending-proposal backlog state.

    ``backlog_exceeded`` is True when either ``pending_count`` exceeds the
    count threshold OR ``oldest_age_days`` exceeds the age threshold.
    ``issues`` is a human-readable list of which threshold(s) crossed.
    """
    pending_count: int
    oldest_age_days: int | None
    backlog_exceeded: bool
    issues: list[str]


def _iter_pending_proposal_dates(raw: str) -> list[date]:
    """Walk the file body, collect the ISO-date prefix of every proposal
    under ``## Pending`` (before the next top-level bucket header).

    Fence-aware: a ``##`` line inside a fenced code block is part of the
    surrounding section body, not a new heading. Same convention as
    ``applied_audit._split_md_sections`` (regression for chainlink #114
    where a proposal body contained a fenced sample with its own ``##``).
    """
    dates: list[date] = []
    in_pending = False
    in_fence = False
    for line in raw.splitlines():
        stripped = line.lstrip()
        # Toggle fence state on any ``` line; lines inside a fence are
        # body, not headings.
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not stripped.startswith("## "):
            continue
        # ``## Pending`` / ``## Applied`` / ``## Rejected`` — toggle which
        # bucket we're in. Anything else under ``## `` at top level is a
        # proposal heading.
        head_lower = stripped[3:].strip().lower()
        if head_lower in _BUCKET_HEADERS:
            in_pending = (head_lower == "pending")
            continue
        if not in_pending:
            continue
        m = _PROPOSAL_HEADING_RE.match(stripped)
        if not m:
            continue
        try:
            dates.append(date.fromisoformat(m.group(1)))
        except ValueError:
            # Heading looked like a date but didn't parse — ignore rather
            # than fail the whole audit. The proposal still exists; we
            # just can't age-rank it.
            continue
    return dates


def compute_backlog_health(
    home: Path,
    *,
    pending_threshold: int = 10,
    oldest_age_threshold_days: int = 21,
    now: datetime | None = None,
) -> BacklogHealth:
    """Read ``<home>/state/proposed-changes.md`` and report backlog state.

    Returns a clean (zero-count, no-issues) result when the file is missing
    — that's the expected steady-state of a fresh deployment, not a problem.
    Empty Pending section likewise returns clean.
    """
    now = now or datetime.now(tz=timezone.utc)
    today = now.date()
    path = home / "state" / "proposed-changes.md"
    if not path.is_file():
        return BacklogHealth(
            pending_count=0,
            oldest_age_days=None,
            backlog_exceeded=False,
            issues=[],
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("could not read %s: %s", path, exc)
        return BacklogHealth(
            pending_count=0,
            oldest_age_days=None,
            backlog_exceeded=False,
            issues=[],
        )

    dates = _iter_pending_proposal_dates(raw)
    count = len(dates)
    oldest_age: int | None = None
    if dates:
        oldest = min(dates)
        oldest_age = (today - oldest).days

    issues: list[str] = []
    if count >= pending_threshold:
        issues.append(
            f"{count} pending proposals (threshold {pending_threshold})"
        )
    if oldest_age is not None and oldest_age >= oldest_age_threshold_days:
        issues.append(
            f"oldest pending is {oldest_age}d old "
            f"(threshold {oldest_age_threshold_days}d)"
        )

    return BacklogHealth(
        pending_count=count,
        oldest_age_days=oldest_age,
        backlog_exceeded=bool(issues),
        issues=issues,
    )


async def run_scheduled_backlog_check(home: Path) -> None:
    """Daily cron callable. Computes backlog health; emits
    ``proposed_changes_backlog`` (negative algedonic) when either threshold
    is exceeded. Below-threshold runs are silent — no event noise on
    healthy backlog state.

    Best-effort: any exception is logged + emits ``proposed_changes_backlog_error``
    but does NOT propagate. The cron retry framework would otherwise pile up
    failures every day until the operator fixes the underlying issue.
    """
    from ..event_logger import log_event  # avoid import cycle at module load

    try:
        health = compute_backlog_health(home)
    except Exception as exc:  # noqa: BLE001 — defensive scheduler boundary
        log.exception("proposed_changes backlog check failed")
        await log_event(
            "proposed_changes_backlog_error",
            error=f"{type(exc).__name__}: {exc}",
        )
        return

    if not health.backlog_exceeded:
        return

    await log_event(
        "proposed_changes_backlog",
        pending_count=health.pending_count,
        oldest_age_days=health.oldest_age_days,
        issues=health.issues,
    )
