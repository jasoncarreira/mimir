"""Feedforward — `## Upcoming` prompt section (FUTURE_WORK §12.1).

Surfaces near-term predictable events the agent should be aware of:

  • Next-N scheduled-tick / cron firings from scheduler.yaml.
  • Plan-window reset times (5h / 7d / 7d_opus / 7d_sonnet) so the
    agent knows when its quota refills.
  • Time-to-next saga consolidation cron.

This is the S4 (intelligence / "there and then") loop the heartbeat
+ rate-limit projection only partially fill. Heartbeat is reactive
("pick from backlog"); rate-limit-off-pace is the only forward-
looking signal we had. The Upcoming section closes that gap by
giving the agent a deterministic look at what's coming next.

Out of scope for this iteration: external event sources (calendar,
Bluesky-poll, RSS), bridge-specific upcoming events. The `sources`
dict is the hook for those.

Render contract: single string body, used by `prompts.py` as the
content of a `## Upcoming` section. Returns None when there's
nothing to surface (suppresses the section entirely so we don't
spam an empty header).
"""

from __future__ import annotations

# VSM: S4 — feedforward. Surfaces predictable upcoming events
#          (cron firings, plan-window resets) before they happen
#          so the agent can prepare instead of being surprised.
# loop_id: 12.1
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


@dataclass
class UpcomingItem:
    """One row in the Upcoming block. Sorted by ``when`` ascending."""

    when: datetime
    label: str             # short human-readable name ("heartbeat", "5h reset")
    detail: str = ""       # optional detail (channel, source schedule)
    source: str = ""       # "scheduler" | "plan_window" | etc.

    def render(self, *, now: datetime) -> str:
        delta = self.when - now
        until = _humanize_delta(delta)
        if self.detail:
            return f"- {self.when.strftime('%Y-%m-%d %H:%M UTC')} (in {until}) — {self.label}: {self.detail}"
        return f"- {self.when.strftime('%Y-%m-%d %H:%M UTC')} (in {until}) — {self.label}"


def _humanize_delta(d: timedelta) -> str:
    """Render a timedelta as ``"3h 12m"`` / ``"45s"`` / ``"2d 3h"``.
    Negative deltas (something fired but we still have it queued)
    return ``"-"`` so the line still reads. Sub-minute precision
    only matters for very-soon firings."""
    secs = int(d.total_seconds())
    if secs <= 0:
        return "now"
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        m, s = divmod(secs, 60)
        return f"{m}m {s}s" if s and m < 5 else f"{m}m"
    if secs < 86400:
        h, rem = divmod(secs, 3600)
        m = rem // 60
        return f"{h}h {m}m" if m else f"{h}h"
    d_days, rem = divmod(secs, 86400)
    h = rem // 3600
    return f"{d_days}d {h}h" if h else f"{d_days}d"


# Auto-registered maintenance crons. Always surface in Upcoming
# (bypass ``limit``) and carry a one-line detail describing what they
# do — bare ids like ``saga-consolidate`` read terse, so the detail
# map gives the agent enough context to know what's coming. Keys are
# the job ids exactly as registered with APScheduler.
_MAINTENANCE_CRON_DETAILS: dict[str, str] = {
    "saga-consolidate": "atom merge / synthesis",
    "introspection-report": "behavioral / health snapshot",
}


def collect_scheduler_jobs(
    scheduler: Any | None, *, limit: int = 5
) -> list[UpcomingItem]:
    """Pull the next-N firing times from APScheduler. ``scheduler`` is
    the wrapper from ``mimir.scheduler``; we read its underlying
    ``self._scheduler.get_jobs()``. Empty list if no scheduler or no
    jobs are due.

    Maintenance crons in ``_MAINTENANCE_CRON_DETAILS`` always surface
    regardless of ``limit`` — operators adding many custom ticks
    shouldn't push weekly maintenance work off the agent's radar."""
    if scheduler is None:
        return []
    try:
        underlying = getattr(scheduler, "_scheduler", None)
        jobs = list(underlying.get_jobs()) if underlying is not None else []
    except Exception:  # noqa: BLE001
        return []

    maintenance: list[UpcomingItem] = []
    regular: list[UpcomingItem] = []
    for job in jobs:
        next_run = getattr(job, "next_run_time", None)
        if next_run is None:
            continue
        if next_run.tzinfo is None:
            next_run = next_run.replace(tzinfo=timezone.utc)
        # Friendly label: scheduler:<name> → just <name>; bare ids
        # (e.g. saga-consolidate) pass through.
        jid = str(getattr(job, "id", "") or "?")
        label = jid.split(":", 1)[1] if jid.startswith("scheduler:") else jid
        item = UpcomingItem(
            when=next_run,
            label=label,
            detail=_MAINTENANCE_CRON_DETAILS.get(jid, ""),
            source="scheduler",
        )
        if jid in _MAINTENANCE_CRON_DETAILS:
            maintenance.append(item)
        else:
            regular.append(item)

    regular.sort(key=lambda it: it.when)
    capped = regular[:limit]
    out = capped + maintenance
    out.sort(key=lambda it: it.when)
    return out


def collect_plan_window_resets(
    rate_limit_store: Any | None, *, limit: int = 5
) -> list[UpcomingItem]:
    """Pull plan-window reset timestamps from ``rate_limits.json``.
    Returns one entry per non-stale window (5h / 7d / per-model /
    overage), sorted by reset time."""
    if rate_limit_store is None:
        return []
    try:
        snapshots = rate_limit_store.current()
    except Exception:  # noqa: BLE001
        return []

    items: list[UpcomingItem] = []
    for window_type, snap in snapshots.items():
        resets_at = getattr(snap, "resets_at", None)
        if not isinstance(resets_at, (int, float)):
            continue
        when = datetime.fromtimestamp(resets_at, tz=timezone.utc)
        util = getattr(snap, "utilization", None)
        detail = ""
        if isinstance(util, (int, float)):
            detail = f"currently {util * 100:.0f}% used"
        items.append(UpcomingItem(
            when=when,
            label=f"{window_type} window resets",
            detail=detail,
            source="plan_window",
        ))
    items.sort(key=lambda it: it.when)
    return items[:limit]


def render_upcoming_block(
    *,
    scheduler: Any | None = None,
    rate_limit_store: Any | None = None,
    limit_scheduler: int = 5,
    limit_plan_windows: int = 5,
    now: datetime | None = None,
) -> str | None:
    """Assemble the Upcoming block body. Returns None when both
    sources are empty (so the prompt assembler can suppress the
    section entirely instead of rendering an empty header).

    Two source-grouped lists rather than a single sort: cron
    firings and plan-window resets are different categories and
    grouping keeps the agent's mental model crisp. Empty groups
    are dropped; if both are empty, the whole block returns None."""
    now = now or datetime.now(tz=timezone.utc)

    sched_items = collect_scheduler_jobs(scheduler, limit=limit_scheduler)
    plan_items = collect_plan_window_resets(rate_limit_store, limit=limit_plan_windows)

    if not sched_items and not plan_items:
        return None

    lines: list[str] = []
    if sched_items:
        lines.append("**Scheduled work**")
        for it in sched_items:
            lines.append(it.render(now=now))
    if plan_items:
        if lines:
            lines.append("")
        lines.append("**Plan-window resets**")
        for it in plan_items:
            lines.append(it.render(now=now))

    return "\n".join(lines)
