"""Periodic poller for Claude Max plan window utilization.

Anthropic's stream-event ``RateLimitInfo.utilization`` arrives as
``null`` for Max OAuth sessions; the full per-window picture
(5h, 7d_opus, 7d_sonnet, overage) lives behind
``ClaudeSDKClient.get_context_usage()`` — the same data Claude
Code's ``/usage`` slash command shows. This module spins up a
throwaway ``ClaudeSDKClient``, queries the daemon, and writes the
parsed buckets into ``mimir.rate_limits.RateLimitStore``. The
existing Self-state and Upcoming render paths pick the data up
from there with no rendering changes.

The poller is a stop-gap. The proper fix is migrating
``mimir/agent.py`` to ``ClaudeSDKClient`` so the agent's own
client can be queried for usage at end-of-turn — see
``CLAUDE_SDK_CLIENT_MIGRATION.md`` for the staged plan. Once that
ships, this module can be retired.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .event_logger import log_event
from .rate_limits import RateLimitSnapshot, RateLimitStore

log = logging.getLogger(__name__)


# Claude Code's apiUsage shape is undocumented in the SDK type
# annotations (just ``dict[str, Any] | None``); this list is what
# we've observed (and what RateLimitInfo's literal types name).
# Unknown keys still get recorded — the store is a free-form dict.
_KNOWN_WINDOW_TYPES = {
    "five_hour",
    "seven_day",
    "seven_day_opus",
    "seven_day_sonnet",
    "overage",
}


def _coerce_utilization(raw: Any) -> float | None:
    """Apple's apiUsage may report utilization as 0-1, 0-100, or as a
    string. Normalize to 0-1 floats; return None on unparseable."""
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
        log.warning("quota_poller: utilization out of range (%r → %f)", raw, v)
    return v


def _coerce_resets_at(raw: Any) -> int | None:
    """Apple may report resets_at as unix seconds (int/float) or an
    ISO timestamp. Normalize to unix seconds (int)."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str):
        try:
            from datetime import datetime
            return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return None


def _bucket_to_snapshot(bucket: dict[str, Any]) -> RateLimitSnapshot | None:
    """Best-effort parse of one apiUsage bucket. Returns None if the
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
    from datetime import datetime, timezone
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


async def poll_max_plan_quota(
    home: Path,
    store: RateLimitStore,
    *,
    model: str | None = None,
) -> None:
    """One-shot quota poll. Spins up a throwaway ``ClaudeSDKClient``,
    calls ``get_context_usage()``, parses the ``apiUsage`` field, and
    records each window bucket as a ``RateLimitSnapshot``. Logs
    ``quota_poll_ok`` / ``quota_poll_failed`` events for visibility.

    Failures are caught and logged; never propagate. The poller is
    best-effort observability, not a load-bearing dependency.
    """
    try:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    except ImportError as exc:
        log.warning("quota_poller: claude_agent_sdk not installed (%s)", exc)
        await log_event(
            "quota_poll_failed",
            error=f"claude_agent_sdk not installed: {exc}",
        )
        return

    options = ClaudeAgentOptions(cwd=str(home))
    if model:
        options.model = model

    api_usage: dict[str, Any] | None = None
    try:
        async with ClaudeSDKClient(options=options) as client:
            response = await client.get_context_usage()
            api_usage = response.get("apiUsage") if isinstance(response, dict) else None
    except Exception as exc:  # noqa: BLE001
        log.warning("quota_poller: get_context_usage failed: %s", exc)
        await log_event(
            "quota_poll_failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        return

    if not isinstance(api_usage, dict) or not api_usage:
        # No apiUsage in the response — daemon doesn't have plan-window
        # data yet (fresh OAuth session before any messages flow), or
        # the user is on a non-Max plan that doesn't surface this data.
        # Log once at info; don't spam.
        log.info(
            "quota_poller: apiUsage missing or empty (response keys: %s)",
            list(response.keys()) if isinstance(response, dict) else None,
        )
        await log_event("quota_poll_ok", windows={}, note="apiUsage empty")
        return

    recorded: dict[str, dict[str, Any]] = {}
    for window_type, bucket in api_usage.items():
        if not isinstance(bucket, dict):
            continue
        snapshot = _bucket_to_snapshot(bucket)
        if snapshot is None:
            log.debug(
                "quota_poller: skipping unparseable bucket %r: %r",
                window_type, bucket,
            )
            continue
        try:
            await store.record(window_type, snapshot)
        except Exception:  # noqa: BLE001
            log.exception("quota_poller: store.record failed for %s", window_type)
            continue
        recorded[window_type] = {
            "utilization": snapshot.utilization,
            "resets_at": snapshot.resets_at,
            "status": snapshot.status,
        }
        if window_type not in _KNOWN_WINDOW_TYPES:
            log.info("quota_poller: recorded unknown window type %r", window_type)

    await log_event("quota_poll_ok", windows=recorded)


__all__ = ["poll_max_plan_quota"]
