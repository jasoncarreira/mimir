"""Persistent store for Anthropic plan-window rate-limit state.

The Claude Agent SDK emits ``RateLimitEvent`` messages on status
transitions, carrying the actual Max-plan window utilization that
Claude Code's ``/usage`` surfaces — five-hour rolling, seven-day
plan-wide, seven-day Opus-specific, seven-day Sonnet-specific, and
the overage / pay-as-you-go bucket. These events are sparse (one
per state transition, not one per turn), so mimir persists the
most recently observed entry per ``rate_limit_type`` and renders it
in the prompt's Resource usage block until the window resets.

Store path: ``<home>/.mimir/rate_limits.json``. Single JSON object
keyed by ``rate_limit_type``:

    {
      "five_hour": {
        "status": "allowed" | "allowed_warning" | "rejected",
        "utilization": 0.0-1.0,
        "resets_at": <unix ts>,
        "observed_at": <iso8601 ts>,
        "overage_status": ... | null,
        "overage_resets_at": ... | null
      },
      "seven_day_opus": { ... },
      ...
    }

Stale entries (``now > resets_at``) are filtered out by ``current()``
so a forgotten record from last week doesn't pollute the prompt.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class RateLimitSnapshot:
    """Persisted shape of a single rate-limit-type entry. Mirrors the
    SDK's ``RateLimitInfo`` minus the type tag (which is the dict key)
    plus an ``observed_at`` field so we can reason about staleness."""

    status: str  # "allowed" | "allowed_warning" | "rejected"
    utilization: float | None = None
    resets_at: int | None = None
    observed_at: str = ""
    overage_status: str | None = None
    overage_resets_at: int | None = None
    overage_disabled_reason: str | None = None


@dataclass
class RateLimitStore:
    """Lock-serialized JSON file at ``<home>/.mimir/rate_limits.json``.
    Multiple turns can in principle write concurrently (subagents),
    so writes are gated through an asyncio.Lock."""

    path: Path
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def record(
        self,
        rate_limit_type: str,
        snapshot: RateLimitSnapshot,
    ) -> None:
        """Replace the entry for ``rate_limit_type`` with ``snapshot``.
        Best effort — on IO failure we log and move on; the prompt
        section degrades to "no plan data" rather than crashing the
        turn."""
        async with self._lock:
            data = self._load()
            data[rate_limit_type] = asdict(snapshot)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(
                    json.dumps(data, indent=2, default=str), encoding="utf-8",
                )
            except OSError as exc:
                log.warning("rate_limits.json write failed: %s", exc)

    def current(self) -> dict[str, RateLimitSnapshot]:
        """Return only entries whose window hasn't reset. Drops stale
        entries (resets_at < now) but keeps records with ``resets_at=None``
        (the SDK doesn't always populate it; surface what we have)."""
        data = self._load()
        now = int(time.time())
        out: dict[str, RateLimitSnapshot] = {}
        for key, raw in data.items():
            if not isinstance(raw, dict):
                continue
            resets_at = raw.get("resets_at")
            if isinstance(resets_at, (int, float)) and resets_at < now:
                continue
            out[key] = RateLimitSnapshot(
                status=str(raw.get("status") or "allowed"),
                utilization=_as_float(raw.get("utilization")),
                resets_at=int(resets_at) if isinstance(resets_at, (int, float)) else None,
                observed_at=str(raw.get("observed_at") or ""),
                overage_status=raw.get("overage_status"),
                overage_resets_at=raw.get("overage_resets_at"),
                overage_disabled_reason=raw.get("overage_disabled_reason"),
            )
        return out

    def _load(self) -> dict[str, Any]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            log.warning("rate_limits.json is corrupt; treating as empty")
            return {}
        return data if isinstance(data, dict) else {}


def _as_float(v: Any) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    return None


def render_plan_quota_lines(
    snapshots: dict[str, RateLimitSnapshot],
) -> list[str]:
    """Format snapshots as bullet lines for the Resource usage block.
    Empty list when there's nothing to show.

    Order matters for readability — five_hour first (most-frequently
    relevant), then seven_day (plan-wide), then per-model entries,
    then overage. Unknown / future types fall through alphabetically."""
    if not snapshots:
        return []
    order = ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet", "overage")
    keys = list(order) + sorted(k for k in snapshots if k not in order)
    lines: list[str] = []
    for key in keys:
        snap = snapshots.get(key)
        if snap is None:
            continue
        lines.append(_render_one(key, snap))
    return lines


def _render_one(key: str, snap: RateLimitSnapshot) -> str:
    label = _LABEL.get(key, key.replace("_", " "))
    parts: list[str] = [label]
    if snap.utilization is not None:
        parts.append(f"{snap.utilization * 100:.0f}% used")
    if snap.status and snap.status != "allowed":
        parts.append(snap.status)
    if snap.resets_at:
        parts.append(f"resets {_humanize_resets(snap.resets_at)}")
    window = _WINDOW_HOURS.get(key)
    if window is not None:
        proj = project_window_end(snap, window)
        if proj is not None:
            pct = proj.on_pace_utilization * 100
            marker = "" if proj.on_track else "⚠ "
            parts.append(f"{marker}on pace: {pct:.0f}% by reset")
    return " — ".join(parts)


_LABEL: dict[str, str] = {
    "five_hour": "5-hour rolling",
    "seven_day": "7-day plan-wide",
    "seven_day_opus": "7-day Opus",
    "seven_day_sonnet": "7-day Sonnet",
    "overage": "Overage / pay-as-you-go",
}


# Window length per rate_limit_type, in hours. Used to project
# end-of-window utilization from current rate. Overage is open-ended;
# excluded so we don't print a misleading projection.
_WINDOW_HOURS: dict[str, float] = {
    "five_hour": 5.0,
    "seven_day": 24.0 * 7,
    "seven_day_opus": 24.0 * 7,
    "seven_day_sonnet": 24.0 * 7,
}


@dataclass(frozen=True)
class WindowProjection:
    """Where current burn rate puts us at window end.

    ``on_pace_utilization`` is the projected end-of-window utilization
    (>1.0 = will exceed quota). ``elapsed_fraction`` is how far into
    the window we are; below ``min_elapsed_fraction`` the projection
    is too noisy to surface (a single call in the first 30s of a 5h
    window looks like 100× growth)."""

    elapsed_hours: float
    hours_until_reset: float
    on_pace_utilization: float
    on_track: bool


def project_window_end(
    snapshot: RateLimitSnapshot,
    window_size_hours: float,
    *,
    min_elapsed_fraction: float = 0.05,
    reference_time: float | None = None,
) -> WindowProjection | None:
    """Project end-of-window utilization assuming the current burn
    rate continues. Returns None when:

    - ``utilization`` is unknown (no signal to project from)
    - ``resets_at`` is unknown (no time anchor for the window)
    - elapsed fraction of the window < ``min_elapsed_fraction``
      (early-window noise dominates: a single call in the first
      few minutes of a 5h window projects to absurd multiples)
    - elapsed time has gone non-positive (window already past)

    The math is the simplest possible: rate = util / elapsed; project
    = rate × window. Equivalent to ``util × (window / elapsed)``.
    """
    if snapshot.utilization is None or snapshot.resets_at is None:
        return None
    if window_size_hours <= 0:
        return None
    now = reference_time if reference_time is not None else time.time()
    if snapshot.resets_at <= now:
        # Window already reset — the snapshot is stale; the next event
        # will refresh. Projecting from stale data is misleading.
        return None
    hours_until_reset = (snapshot.resets_at - now) / 3600.0
    elapsed_hours = window_size_hours - hours_until_reset
    if elapsed_hours <= 0:
        return None
    if elapsed_hours / window_size_hours < min_elapsed_fraction:
        return None
    on_pace = snapshot.utilization * (window_size_hours / elapsed_hours)
    return WindowProjection(
        elapsed_hours=elapsed_hours,
        hours_until_reset=hours_until_reset,
        on_pace_utilization=on_pace,
        on_track=on_pace < 1.0,
    )


def _humanize_resets(unix_ts: int) -> str:
    """Format a future reset time relative to now: 'in 1h 23m', or
    fall back to ISO8601 when the offset isn't human-friendly."""
    delta = unix_ts - int(time.time())
    if delta <= 0:
        return "now"
    if delta < 60:
        return f"in {delta}s"
    if delta < 3600:
        return f"in {delta // 60}m"
    if delta < 24 * 3600:
        return f"in {delta // 3600}h {(delta % 3600) // 60}m"
    days = delta // (24 * 3600)
    hours = (delta % (24 * 3600)) // 3600
    return f"in {days}d {hours}h"


def snapshot_from_sdk_event(rate_limit_info: Any) -> RateLimitSnapshot:
    """Convert an SDK ``RateLimitInfo`` dataclass to our persisted
    snapshot. Tolerates objects that lack one of the optional fields
    (older SDK versions or sparse events)."""
    return RateLimitSnapshot(
        status=str(getattr(rate_limit_info, "status", "allowed")),
        utilization=_as_float(getattr(rate_limit_info, "utilization", None)),
        resets_at=getattr(rate_limit_info, "resets_at", None),
        observed_at=datetime.now(tz=timezone.utc).isoformat(),
        overage_status=getattr(rate_limit_info, "overage_status", None),
        overage_resets_at=getattr(rate_limit_info, "overage_resets_at", None),
        overage_disabled_reason=getattr(
            rate_limit_info, "overage_disabled_reason", None,
        ),
    )


def snapshot_from_response_bucket(bucket: dict[str, Any]) -> RateLimitSnapshot:
    """Convert one ``rate_limits[<bucket_type>]`` dict from an
    Anthropic ``message_start`` API response to our snapshot.

    The per-response shape is undocumented (Claude.ai subscription
    private path) but observed with two possible utilization fields:
    ``utilization`` (0.0-1.0 fraction, matches the SDK's transition
    event shape) and ``used_percentage`` (0-100, matches the
    statusline JSON jq expressions). We accept either so the
    capture path is robust across CLI versions."""
    util = _as_float(bucket.get("utilization"))
    if util is None:
        pct = bucket.get("used_percentage")
        if isinstance(pct, (int, float)):
            util = float(pct) / 100.0
    resets_at = bucket.get("resets_at")
    if resets_at is None:
        resets_at = bucket.get("resetsAt")
    return RateLimitSnapshot(
        status=str(bucket.get("status") or "allowed"),
        utilization=util,
        resets_at=int(resets_at) if isinstance(resets_at, (int, float)) else None,
        observed_at=datetime.now(tz=timezone.utc).isoformat(),
        overage_status=bucket.get("overage_status") or bucket.get("overageStatus"),
        overage_resets_at=(
            bucket.get("overage_resets_at") or bucket.get("overageResetsAt")
        ),
        overage_disabled_reason=(
            bucket.get("overage_disabled_reason")
            or bucket.get("overageDisabledReason")
        ),
    )
