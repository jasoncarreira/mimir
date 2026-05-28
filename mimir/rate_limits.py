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
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# chainlink #239: this module's prior ``_write_json_atomic`` did NOT
# fsync the file or the parent dir — a crash between os.replace and
# writeback could revert the rename. ``mimir._atomic.atomic_write_json``
# now applies the CR#7 invariant (fsync file + fsync parent dir)
# uniformly across rate_limits, oauth_usage_poller, and quota_pause.
from ._atomic import atomic_write_json

log = logging.getLogger(__name__)


def running_on_claude_max() -> bool:
    """True iff the agent appears to be configured for Claude Max
    OAuth — the only config where ``ClaudeSDKClient.get_context_usage()``
    returns useful per-window utilization data.

    Heuristic:
    - ``CLAUDE_CODE_OAUTH_TOKEN`` set (Max OAuth path).
    - ``ANTHROPIC_BASE_URL`` empty (no proxy / OpenRouter / Minimax
      redirect that would route the agent's calls outside Anthropic).

    Either condition flipping means we're in a config where the
    daemon's ``apiUsage`` will be empty — direct API keys deliver
    rate-limit headers per-response (already captured by
    ``RateLimitEvent``) but no plan-window picture; OpenRouter /
    Minimax don't have a plan-window concept at all.

    The check is environment-only — no network calls, safe to invoke
    at scheduler-registration time before the agent is up.
    """
    has_oauth = bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip())
    has_base_url_override = bool(os.environ.get("ANTHROPIC_BASE_URL", "").strip())
    return has_oauth and not has_base_url_override


@dataclass
class RateLimitSnapshot:
    """Persisted shape of a single rate-limit-type entry. Mirrors the
    SDK's ``RateLimitInfo`` minus the type tag (which is the dict key)
    plus an ``observed_at`` field so we can reason about staleness.

    ``derived`` (chainlink #17): true when ``utilization`` was estimated
    from cost data rather than read from a usage endpoint. The OAuth
    poller's cost-rate-back-derived 5h estimator sets this when the
    layer-(a) anomaly detector rejected an endpoint reading and we
    need a usable 5h signal during a long endpoint glitch. The
    arbiter (mimir/billing.py:evaluate_quota) applies a higher
    suppress threshold (90% vs 80% direct) when this is set —
    derived values are approximations and shouldn't trip the wall
    threshold as quickly as ground truth."""

    status: str  # "allowed" | "allowed_warning" | "rejected"
    utilization: float | None = None
    resets_at: int | None = None
    observed_at: str = ""
    overage_status: str | None = None
    overage_resets_at: int | None = None
    overage_disabled_reason: str | None = None
    derived: bool = False


@dataclass
class RateLimitStore:
    """Lock-serialized JSON file at ``<home>/.mimir/rate_limits.json``.
    Multiple turns can in principle write concurrently (subagents),
    so writes are gated through an asyncio.Lock (async path) and a
    threading.Lock (sync path). Both paths use atomic write-to-temp +
    rename so a concurrent write or crash never leaves a corrupted file."""

    path: Path
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _thread_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    async def record(
        self,
        rate_limit_type: str,
        snapshot: RateLimitSnapshot,
    ) -> None:
        """Replace the entry for ``rate_limit_type`` with ``snapshot``.
        Best effort — on IO failure we log and move on; the prompt
        section degrades to "no plan data" rather than crashing the
        turn.

        The asyncio.Lock serializes concurrent coroutine callers.
        Writes are atomic (temp + rename) so a crash mid-write cannot
        corrupt the file."""
        async with self._lock:
            data = self._load()
            data[rate_limit_type] = asdict(snapshot)
            try:
                atomic_write_json(self.path, data)
            except OSError as exc:
                log.warning("rate_limits.json write failed: %s", exc)

    def record_sync(
        self,
        rate_limit_type: str,
        snapshot: RateLimitSnapshot,
    ) -> None:
        """Synchronous version of :meth:`record` for callers that can't
        ``await`` (e.g. ``ChatCodexPlus.rate_limit_callback``, which
        fires inline from a streaming SSE handler on either the loop
        thread or a thread executor — figuring out which is brittle).

        A threading.Lock serializes concurrent thread callers; writes
        are atomic (temp + rename) so a crash mid-write cannot corrupt
        the file. Last-write-wins semantics are acceptable because
        snapshots are monotonically refreshed (each successful response
        carries the latest quota state). Best-effort: IO errors are
        logged and swallowed.
        """
        with self._thread_lock:
            data = self._load()
            data[rate_limit_type] = asdict(snapshot)
            try:
                atomic_write_json(self.path, data)
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
                derived=bool(raw.get("derived", False)),
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


def off_pace_buckets(
    snapshots: dict[str, RateLimitSnapshot],
) -> list[tuple[str, RateLimitSnapshot, "WindowProjection"]]:
    """Return all (key, snapshot, projection) triples whose projection
    is off track (on_pace_utilization >= 1.0). Ordered by severity
    (highest projected utilization first) so the worst case leads."""
    out: list[tuple[str, RateLimitSnapshot, WindowProjection]] = []
    for key, snap in snapshots.items():
        window = _WINDOW_HOURS.get(key)
        if window is None:
            continue
        proj = project_window_end(snap, window)
        if proj is None or proj.on_track:
            continue
        out.append((key, snap, proj))
    out.sort(key=lambda t: t[2].on_pace_utilization, reverse=True)
    return out


def render_off_pace_warning(
    off_pace: list[tuple[str, RateLimitSnapshot, "WindowProjection"]],
) -> list[str]:
    """Multi-line callout block for the off-pace alert. The inline ⚠
    on the bucket line is a marker; this paragraph is the "scale back
    NOW" message the agent reads when deciding what to do next.

    Tier the verb by severity:
    - on pace > 150% → "defer all expensive work"
    - on pace 100-150% → "scale back"

    Returns empty list when nothing's off pace."""
    if not off_pace:
        return []
    worst = off_pace[0][2].on_pace_utilization
    if worst >= 1.5:
        verb_line = (
            "🛑 PLAN QUOTA AT RISK — defer all expensive work. "
            "Bash-only investigations, memory cleanup, or end the turn "
            "silently. Do NOT fan out subagents. Multi-turn research is "
            "off the table until the burn rate normalizes."
        )
    else:
        verb_line = (
            "⚠ Plan quota tracking off pace — scale back. "
            "Pick the cheapest backlog items, avoid fan-out, prefer "
            "Bash queries over subagent tasks. End silently more readily "
            "than usual."
        )
    lines: list[str] = [verb_line]
    for key, snap, proj in off_pace:
        label = _LABEL.get(key, key.replace("_", " "))
        cur_pct = (
            f"{snap.utilization * 100:.0f}% used"
            if snap.utilization is not None
            else "unknown"
        )
        lines.append(
            f"- {label}: {cur_pct}, projects to "
            f"{proj.on_pace_utilization * 100:.0f}% by reset "
            f"({_humanize_resets(snap.resets_at) if snap.resets_at else 'no reset time'})"
        )
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
    - ``utilization >= 1.0`` (bucket saturated / pegged). A reading of
      100% means the old bucket is fully consumed; it carries no rate
      information useful for projecting the new window. The window will
      reset to 0% — projecting forward from 1.0 at the start of the new
      window produces absurd multiples like 1093% (fingerprinted in
      ``memory/issues/anthropic-5h-bucket-pegged.md``). The raw-utilization
      suppress check (≥ 0.80) already handles the suppression decision in
      this case; adding a spurious "⚠ on pace: 1093% by reset" banner is
      pure noise.
    - elapsed fraction of the window < ``min_elapsed_fraction``
      (early-window noise dominates: a single call in the first
      few minutes of a 5h window projects to absurd multiples)
    - elapsed time has gone non-positive (window already past)

    The math is the simplest possible: rate = util / elapsed; project
    = rate × window. Equivalent to ``util × (window / elapsed)``.
    """
    if snapshot.utilization is None or snapshot.resets_at is None:
        return None
    if snapshot.utilization >= 1.0:
        # Saturated / pegged bucket — see docstring. Raw suppress fires
        # at ≥ 0.80; no useful rate signal to extrapolate here.
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


# ─── apiUsage parsing (ClaudeSDKClient.get_context_usage) ────────────
#
# Stage 5 of CLAUDE_SDK_CLIENT_MIGRATION.md: each turn calls the
# shared persistent client's ``get_context_usage()`` and writes the
# returned ``apiUsage`` buckets here. This replaces the throwaway-
# subprocess cron poller (``mimir/quota_poller.py``) — same data,
# cheaper path, fresher cadence (every turn vs every 10 min).
#
# Claude Code's apiUsage shape is undocumented in the SDK type
# annotations (just ``dict[str, Any] | None``); ``_KNOWN_WINDOW_TYPES``
# is what we've observed (and what RateLimitInfo's literal types
# name). Unknown keys still get recorded — the store is a free-form
# dict.

_KNOWN_WINDOW_TYPES = {
    "five_hour",
    "seven_day",
    "seven_day_opus",
    "seven_day_sonnet",
    "overage",
}


def _coerce_utilization(raw: Any) -> float | None:
    """apiUsage may report utilization as 0-1, 0-100, or as a string.
    Normalize to 0-1 floats; return None on unparseable."""
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v > 1.0:
        # Looks like a percentage; rescale.
        v = v / 100.0
    if v < 0.0 or v > 1.5:  # >1.5 means we're past 150% — store anyway but
                            # likely indicates a parse error, log it.
        log.warning("apiUsage: utilization out of range (%r → %f)", raw, v)
    return v


def _coerce_resets_at(raw: Any) -> int | None:
    """resets_at may be unix seconds (int/float) or an ISO timestamp.
    Normalize to unix seconds (int)."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return None


def snapshot_from_api_usage_bucket(bucket: dict[str, Any]) -> RateLimitSnapshot | None:
    """Best-effort parse of one ``apiUsage[<window_type>]`` bucket from
    ``ClaudeSDKClient.get_context_usage()``. Returns None if the
    bucket is too malformed to be useful (no resets_at AND no
    utilization)."""
    util = _coerce_utilization(
        bucket.get("utilization")
        or bucket.get("usage_pct")
        or bucket.get("percentage"),
    )
    resets = _coerce_resets_at(
        bucket.get("resets_at")
        or bucket.get("reset_at")
        or bucket.get("resetsAt"),
    )
    if util is None and resets is None:
        return None
    status = str(bucket.get("status") or "allowed")
    overage_status = bucket.get("overage_status")
    overage_resets = _coerce_resets_at(
        bucket.get("overage_resets_at") or bucket.get("overage_reset_at"),
    )
    overage_disabled = bucket.get("overage_disabled_reason")
    return RateLimitSnapshot(
        status=status,
        utilization=util,
        resets_at=resets,
        observed_at=datetime.now(tz=timezone.utc).isoformat(),
        overage_status=overage_status if isinstance(overage_status, str) else None,
        overage_resets_at=overage_resets,
        overage_disabled_reason=(
            overage_disabled if isinstance(overage_disabled, str) else None
        ),
    )


async def record_api_usage(
    store: "RateLimitStore",
    api_usage: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Write each parseable ``apiUsage`` bucket into ``store``.
    Returns a summary mapping window_type → {utilization, resets_at,
    status} of what was recorded, so the caller can emit a structured
    log event without re-loading the store. Empty dict when there's
    nothing useful to record (apiUsage is None, empty, or all buckets
    are unparseable)."""
    recorded: dict[str, dict[str, Any]] = {}
    if not isinstance(api_usage, dict) or not api_usage:
        return recorded
    for window_type, bucket in api_usage.items():
        if not isinstance(bucket, dict):
            continue
        snapshot = snapshot_from_api_usage_bucket(bucket)
        if snapshot is None:
            log.debug(
                "apiUsage: skipping unparseable bucket %r: %r",
                window_type, bucket,
            )
            continue
        try:
            await store.record(window_type, snapshot)
        except Exception:  # noqa: BLE001
            log.exception("apiUsage: store.record failed for %s", window_type)
            continue
        recorded[window_type] = {
            "utilization": snapshot.utilization,
            "resets_at": snapshot.resets_at,
            "status": snapshot.status,
        }
        if window_type not in _KNOWN_WINDOW_TYPES:
            log.info("apiUsage: recorded unknown window type %r", window_type)
    return recorded


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
