"""Phone-push alarm helper for the algedonic feedback loop (chainlink #36).

Sends one-shot push notifications to the operator via ntfy.sh. Used by
:mod:`mimir.feedback` (sub B / chainlink #65) to surface the small set
of failure modes that *must* reach a human in real time — cost
runaway, Discord-outbound prolonged failure, OAuth logged-out, etc.
The polling/wiring of those signals is intentionally out of scope for
this module; this is just the "send one alarm, do not crash the
caller" primitive.

Design constraints:

- **Optional infra.** Operator opts in by exporting ``NTFY_TOPIC``. When
  unset, the helper emits a single ``ntfy_skip_no_topic`` event and
  returns silently — mimir must run identically with or without it.
- **Never raises.** Every failure mode (no topic, network error, 4xx,
  5xx) returns cleanly after emitting an event. The caller is the
  algedonic surface — having the alarm-send path itself crash the
  loop would be the worst possible failure mode.
- **In-process dedup.** A re-fire of the same logical alarm within the
  dedup window (default 1h) is a no-op. Caller picks the
  ``dedupe_key`` — typically ``"<category>:<resource>"`` — so re-fires
  from a poller running every minute don't spam the operator's lock
  screen. Per-process only; no cross-restart persistence (intentional —
  a restart often *is* the signal worth re-firing on).
- **No retries.** ntfy.sh is a fire-and-forget push service. Retrying
  on transient 5xx would cost very little but the algedonic surface
  is already the catch-net for "it didn't get through" via the
  ``ntfy_post_failed`` event — the operator will see the failure in
  the next feedback render even if the push itself was lost.

Event kinds emitted (all non-fatal, all consumed by the algedonic
block in mimir/feedback.py — wiring deferred to chainlink #65):

- ``ntfy_skip_no_topic`` — ``NTFY_TOPIC`` env var unset/empty. Carries
  ``{category, dedupe_key}``.
- ``ntfy_post_failed`` — transport error or HTTP 5xx. Carries
  ``{category, dedupe_key, error, status?}``. Re-fires next cycle.
- ``ntfy_post_rejected`` — HTTP 4xx (topic invalid, banned, request
  malformed). Carries ``{category, dedupe_key, status, body_excerpt}``.
  Configuration-shaped; operator action needed.
- ``ntfy_post_ok`` — HTTP 2xx successful send. Carries
  ``{category, dedupe_key}``. Surfaces as the paired positive next to
  ``ntfy_post_failed`` / ``ntfy_post_rejected`` (chainlink #65) so the
  operator can read recovery against the sticky failure line. The
  feedback layer dedups to first-occurrence-only in the 24h window —
  events.jsonl still records each one (tiny) but the algedonic block
  only renders the most recent.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import yaml

from .event_logger import log_event

_log = logging.getLogger(__name__)


# Default dedup window — re-fires of the same dedupe_key within this
# many seconds are silently dropped.
DEFAULT_DEDUP_WINDOW_SECONDS = 3600

# Per-call HTTP timeout. ntfy.sh is normally <100ms; 5s is generous
# enough to ride out a transient hiccup without making the algedonic
# block feel hung.
DEFAULT_TIMEOUT_SECONDS = 5.0

# Module-level dedup table: dedupe_key → datetime of last successful
# post (UTC). Per-process only; cleared on restart by design.
_LAST_POST: dict[str, datetime] = {}


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _within_dedup_window(
    dedupe_key: str, *, window_seconds: int, now: datetime,
) -> bool:
    last = _LAST_POST.get(dedupe_key)
    if last is None:
        return False
    return (now - last).total_seconds() < window_seconds


async def post_algedonic_alarm(
    *,
    category: str,
    title: str,
    body: str,
    dedupe_key: str,
    priority: int = 4,
    tags: list[str] | None = None,
    dedup_window_seconds: int = DEFAULT_DEDUP_WINDOW_SECONDS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """Push one alarm to ntfy.sh, with dedup + soft-fail semantics.

    Parameters
    ----------
    category:
        Short tag identifying the alarm class (``"cost-runaway"``,
        ``"discord-down"``, ``"oauth-logged-out"``…). Surfaces in the
        ``ntfy_*`` events for filtering.
    title:
        One-line summary; sent as the ntfy ``Title`` header. Lock-screen
        first impression.
    body:
        ~3 lines max. The HTTP body of the POST.
    dedupe_key:
        Uniqueness anchor. Same key within ``dedup_window_seconds``
        (default 1h) is a no-op. Caller picks the granularity.
    priority:
        ntfy ``Priority`` header (1..5). 4 = high (default), 5 = urgent.
        Sent as a string per ntfy's wire format.
    tags:
        Emoji shortcodes for the ``Tags`` header (comma-joined). E.g.
        ``["warning", "money_with_wings"]``.
    dedup_window_seconds, timeout_seconds:
        Test/operator overrides. The defaults are the real values.

    Always returns ``None``. Never raises.
    """
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        await log_event(
            "ntfy_skip_no_topic",
            category=category,
            dedupe_key=dedupe_key,
        )
        return

    now = _now_utc()
    if _within_dedup_window(
        dedupe_key, window_seconds=dedup_window_seconds, now=now,
    ):
        # Silent — re-fires are expected, not interesting. events.jsonl
        # would grow without bound from a poller hitting this path
        # every minute.
        return

    headers = {
        "Title": title,
        "Priority": str(priority),
    }
    if tags:
        headers["Tags"] = ",".join(tags)

    url = f"https://ntfy.sh/{topic}"
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, data=body, headers=headers) as resp:
                status = resp.status
                # Read a small body excerpt for diagnostic events. Bound
                # the read so a misbehaving server can't pin memory.
                try:
                    text = await resp.text()
                except Exception as exc:  # noqa: BLE001
                    text = f"<read failed: {type(exc).__name__}: {exc}>"
    except Exception as exc:  # noqa: BLE001 — never crash the caller
        # aiohttp.ClientError, asyncio.TimeoutError, anything DNS/TLS.
        await log_event(
            "ntfy_post_failed",
            category=category,
            dedupe_key=dedupe_key,
            error=repr(exc),
        )
        return

    if 200 <= status < 300:
        # Success. Stamp the dedup table; emit the paired-positive
        # event (chainlink #65) so the algedonic block can show
        # recovery alongside the sticky ``ntfy_post_failed`` /
        # ``ntfy_post_rejected`` line. First-occurrence-only at the
        # feedback layer means events.jsonl carries one record per
        # successful send but only the latest renders.
        _LAST_POST[dedupe_key] = now
        await log_event(
            "ntfy_post_ok",
            category=category,
            dedupe_key=dedupe_key,
        )
        return

    body_excerpt = (text or "")[:200]
    if 400 <= status < 500:
        # Config-shaped: invalid topic, banned, malformed request. No
        # retry will fix this; operator must intervene.
        await log_event(
            "ntfy_post_rejected",
            category=category,
            dedupe_key=dedupe_key,
            status=status,
            body_excerpt=body_excerpt,
        )
        return

    # 5xx (or any other non-2xx) — transient, treated as a regular
    # post failure. The next algedonic cycle will re-fire if the
    # underlying signal is still active.
    await log_event(
        "ntfy_post_failed",
        category=category,
        dedupe_key=dedupe_key,
        error="http_5xx",
        status=status,
        body_excerpt=body_excerpt,
    )


def _reset_dedup_for_tests() -> None:
    """Clear the in-process dedup table. For tests only — production
    callers rely on the table persisting across alarms within the
    process lifetime."""
    _LAST_POST.clear()


# ────────────────────────────────────────────────────────────────────────────
# Dead-man alarm: cost-rate runaway (chainlink #66)
# ────────────────────────────────────────────────────────────────────────────

# Threshold above which a deferred ``cost_rate_alert`` / ``cost_rate_advisory``
# event triggers a phone-push alarm. $50/hr is ~20× the typical working rate
# ($2–3/hr) — clearly a runaway loop or an accidentally-unleashed benchmark,
# not an active coding session.
NTFY_COST_RUNAWAY_USD_PER_HOUR: float = 50.0

_COST_RATE_EVENT_KINDS = frozenset({"cost_rate_alert", "cost_rate_advisory"})


async def fire_cost_runaway_alarm_if_warranted(
    event_kind: str,
    event_kwargs: dict,
    *,
    threshold_usd_per_hour: float = NTFY_COST_RUNAWAY_USD_PER_HOUR,
) -> None:
    """Send a phone-push alarm if a cost-rate event crosses the runaway threshold.

    Designed to be spawned as a background task alongside the normal
    ``log_event`` call in the agent's deferred-event flush loop.  When the
    event is irrelevant or the rate is below ``threshold_usd_per_hour``, the
    function returns immediately without touching ntfy.

    Parameters
    ----------
    event_kind:
        The deferred event kind string (``"cost_rate_alert"`` or
        ``"cost_rate_advisory"``).  Other kinds are ignored silently.
    event_kwargs:
        The event payload dict produced by ``_assemble_usage_block``.
        Must contain ``"rate_now_usd_per_hour"``.
    threshold_usd_per_hour:
        Override the module-level default for tests or operator config.

    Always returns ``None``.  Never raises.
    """
    if event_kind not in _COST_RATE_EVENT_KINDS:
        return
    rate = event_kwargs.get("rate_now_usd_per_hour", 0.0)
    if not isinstance(rate, (int, float)) or rate < threshold_usd_per_hour:
        return
    await post_algedonic_alarm(
        category="cost-runaway",
        title=f"mimir: cost runaway ${rate:.0f}/hr",
        body=(
            f"Hourly spend ${rate:.1f}/hr exceeds alarm threshold "
            f"${threshold_usd_per_hour:.0f}/hr. Possible runaway loop — "
            f"check for stuck heartbeat or bash_async spawn storm."
        ),
        dedupe_key="cost-runaway:rate",
        priority=5,  # urgent
        tags=["warning", "money_with_wings"],
    )


# ────────────────────────────────────────────────────────────────────────────
# Dead-man alarm: scheduler wedge (chainlink #66)
# ────────────────────────────────────────────────────────────────────────────

_HEARTBEAT_CHANNEL_ID = "scheduler:heartbeat"

#: Default safety multiplier applied to the heartbeat period when computing
#: the staleness threshold.  2× gives room for one missed tick without a
#: false alarm, but catches a wedge before the tick-after-that would fire.
NTFY_SCHEDULER_WEDGE_SAFETY_FACTOR: float = 2.0


def _read_heartbeat_cron(scheduler_yaml_path: Path) -> str | None:
    """Return the cron expression for the heartbeat job in ``scheduler_yaml_path``.

    Returns ``None`` if:
    - the file doesn't exist or cannot be parsed,
    - no job named ``'heartbeat'`` is present,
    - the heartbeat job's cron field is empty or missing.

    Never raises.
    """
    try:
        text = scheduler_yaml_path.read_text(encoding="utf-8")
        raw = yaml.safe_load(text)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(raw, list):
        return None
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("name", "")).strip() == "heartbeat":
            cron = str(entry.get("cron", "")).strip()
            return cron if cron else None
    return None


def _cron_period_minutes(cron_expr: str) -> float:
    """Return the repeat period of ``cron_expr`` in minutes.

    Handles the common patterns used in mimir's scheduler.yaml without
    requiring the ``croniter`` package:

    - ``*/N * * * *`` — sub-hourly: N minutes
    - ``M */N * * *`` — step-hours: N × 60 minutes
    - ``M * * * *`` — hourly: 60 minutes
    - ``M H * * *`` — daily or less: 1440 minutes (conservative)

    Returns 60.0 for any expression that doesn't match a recognised pattern.
    """
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        return 60.0

    minute_field, hour_field = parts[0], parts[1]

    # */N in the minute field → sub-hourly cadence of N minutes.
    if minute_field.startswith("*/"):
        try:
            n = int(minute_field[2:])
            if n > 0:
                return float(n)
        except ValueError:
            pass

    # Fixed minute with */N in the hour field → period of N hours.
    if hour_field.startswith("*/"):
        try:
            n = int(hour_field[2:])
            if n > 0:
                return float(n) * 60.0
        except ValueError:
            pass

    # Fixed minute, every hour ("M * * * *") → 60 minutes.
    if hour_field == "*":
        return 60.0

    # Fixed minute and fixed hour (daily or less frequent) → 1440 minutes.
    return 1440.0


def _parse_event_ts(ts_str: object) -> "datetime | None":
    """Parse an events.jsonl ISO timestamp to a tz-aware UTC datetime.

    chainlink #259: normalizes a trailing ``Z`` (so a bare-Z stamp isn't
    silently dropped) and coerces a naive timestamp to UTC, so the
    downstream ``ts <= window_start`` comparisons against tz-aware bounds
    can't raise a ``TypeError`` that escapes this module's "never raises"
    contract. Returns ``None`` on any parse failure — the caller skips
    the record.
    """
    if not isinstance(ts_str, str) or not ts_str:
        return None
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _classify_silence(
    events_path: Path,
    *,
    channel_id: str,
    window_start: datetime,
    window_end: datetime,
) -> tuple[str, str | None]:
    """Look at events in (window_start, window_end] for ``channel_id``.

    Classifies why a scheduled channel has been silent (chainlink #221):

    - **``("suppressed", reason_string)``** — one or more
      ``scheduled_tick_suppressed`` events for this channel landed in
      the window. The silence is intentional (e.g. quota saturated,
      operator-disabled). ``reason_string`` is the most recent
      suppress-event's ``reason`` field, suitable for surfacing to the
      operator (e.g. ``quota_saturated:anthropic:seven_day@1.00``).
    - **``("wedge", None)``** — no suppress events found in the
      window. The scheduler is presumed genuinely stuck. Operator
      action is on the scheduler itself (restart, deadlock check).

    Doesn't distinguish "partial wedge" (some ticks suppressed, some
    silently missing) — that's rare in practice and conservatively
    flagging the whole window as wedge over-alerts rather than
    under-alerts.

    Reads the file in reverse (via :func:`tail_jsonl_records` — 8 KiB
    chunks from the tail, no full-file load) so the freshest suppress
    reason wins. Early-breaks when the iterator goes past the window
    start. Never raises; on read error returns ``("wedge", None)``.

    chainlink #244: prior shape did ``read_text()`` of a file capped at
    300 MB, called every 10 minutes by the wedge probe — 1.8 GB/h of
    disk reads to find a record from the last ~hour. Now O(window).
    """
    from ._jsonl_tail import tail_jsonl_records

    for event in tail_jsonl_records(events_path):
        ts = _parse_event_ts(event.get("timestamp", ""))
        if ts is None:
            continue
        if ts <= window_start:
            # We're past the window — older records can't contribute.
            break
        if (
            event.get("type") != "scheduled_tick_suppressed"
            or event.get("channel_id") != channel_id
        ):
            continue
        if window_start < ts <= window_end:
            return ("suppressed", event.get("reason"))
    return ("wedge", None)


def _last_heartbeat_timestamp(
    events_path: Path,
    *,
    channel_id: str = _HEARTBEAT_CHANNEL_ID,
) -> datetime | None:
    """Scan ``events_path`` in reverse and return the timestamp of the most
    recent ``scheduled_tick`` event for ``channel_id``.

    Returns ``None`` if:
    - the file does not exist or cannot be read,
    - no matching event is found (e.g. first boot, very small log).

    Never raises.

    chainlink #244: switched from ``read_text()`` (full file in memory)
    to :func:`tail_jsonl_records` which yields newest-first via 8 KiB
    chunks. The freshest matching tick wins, so most calls return
    after a handful of records.
    """
    from ._jsonl_tail import tail_jsonl_records

    for event in tail_jsonl_records(events_path):
        if (
            event.get("type") == "scheduled_tick"
            and event.get("channel_id") == channel_id
        ):
            ts = _parse_event_ts(event.get("timestamp", ""))
            if ts is not None:
                return ts
            continue
    return None


async def fire_scheduler_wedge_alarm_if_warranted(
    events_path: Path,
    *,
    scheduler_yaml_path: Path,
    safety_factor: float = NTFY_SCHEDULER_WEDGE_SAFETY_FACTOR,
    channel_id: str = _HEARTBEAT_CHANNEL_ID,
    now: datetime | None = None,
) -> None:
    """Send a phone-push alarm if the heartbeat scheduler hasn't fired recently.

    Designed to be called by a lightweight non-LLM cron (every 10 min) that
    remains runnable even when the heartbeat tick itself is wedged.  When
    APScheduler is partially functional (the health-check job fires but the
    heartbeat job doesn't), this function will detect and alarm on the gap.

    The staleness threshold is derived from the heartbeat job's cron expression
    in ``scheduler_yaml_path`` multiplied by ``safety_factor``.  If the
    heartbeat job is absent or has no cron (i.e. intentionally disabled), the
    function returns silently — a missing heartbeat entry is not a wedge.

    Parameters
    ----------
    events_path:
        Absolute path to ``events.jsonl``.  Passed explicitly so callers
        (and tests) can point at any file without touching the environment.
    scheduler_yaml_path:
        Path to ``scheduler.yaml``.  The heartbeat job's ``cron`` field is
        read here to derive the expected firing period.
    safety_factor:
        Multiplier applied to the heartbeat period to get the alarm threshold.
        Defaults to :data:`NTFY_SCHEDULER_WEDGE_SAFETY_FACTOR` (2.0).  At 2×
        the period, one missed tick is tolerated before alarming.
    channel_id:
        The scheduler channel to watch.  Defaults to
        ``"scheduler:heartbeat"``.  Override in tests or for other channels.
    now:
        Injected current time (UTC-aware).  Defaults to
        ``datetime.now(timezone.utc)``.  Exposed for deterministic tests.

    Always returns ``None``.  Never raises.
    """
    # --- check 1: is the heartbeat job configured at all? ----------------
    heartbeat_cron = _read_heartbeat_cron(scheduler_yaml_path)
    if heartbeat_cron is None or not heartbeat_cron.strip():
        # Heartbeat intentionally disabled or scheduler.yaml unreadable —
        # not a wedge condition; return silently.
        return

    # --- derive threshold from actual cron period ------------------------
    period_min = _cron_period_minutes(heartbeat_cron)
    threshold_minutes = period_min * safety_factor

    if now is None:
        now = datetime.now(timezone.utc)

    last_tick = _last_heartbeat_timestamp(events_path, channel_id=channel_id)
    if last_tick is None:
        # No prior tick in the log — likely first boot or very small log
        # window.  Don't alarm; we have no baseline.
        return

    elapsed_minutes = (now - last_tick).total_seconds() / 60.0
    if elapsed_minutes < threshold_minutes:
        return

    # chainlink #221: distinguish genuine wedge from intentional
    # suppression. The legacy alarm fired identically when APScheduler
    # was actually stuck and when the scheduler was correctly
    # suppressing ticks (quota saturated, operator-disabled, etc.) —
    # but the operator response differs (restart vs. fix the upstream
    # cause), so the alarm shouldn't conflate them.
    classification, suppress_reason = _classify_silence(
        events_path,
        channel_id=channel_id,
        window_start=last_tick,
        window_end=now,
    )

    if classification == "suppressed":
        # Silence is explained by ``scheduled_tick_suppressed`` events
        # in the window. The scheduler is alive and intentionally not
        # firing — operator action is upstream (fix the quota
        # reading, switch model spec, wait for the window to roll).
        # None of those are emergencies that warrant a phone push.
        #
        # The individual ``scheduled_tick_suppressed`` events
        # already render in the per-turn algedonic block via
        # ``feedback.py``, so the operator sees the suppression in
        # the agent's normal turn output. Aggregating that into a
        # wake-up ntfy on top would be redundant noise — the
        # operator complained on first observation that the alarm
        # implied "something to act on" when the situation
        # required none.
        #
        # We do, however, log a one-time event in this poll
        # for ops-dashboard surfacing — same observability without
        # the phone push.
        await log_event(
            "scheduler_suppressed_window_observed",
            channel_id=channel_id,
            elapsed_minutes=round(elapsed_minutes, 1),
            threshold_minutes=round(threshold_minutes, 1),
            suppress_reason=suppress_reason,
        )
        return

    # Genuine wedge — no suppress events in the window. Existing
    # alarm + dedupe_key preserved so dashboards / runbooks pinned
    # to the prior category-string keep working.
    await post_algedonic_alarm(
        category="scheduler-wedge",
        title="mimir: scheduler wedge — heartbeat stale",
        body=(
            f"scheduler:heartbeat hasn't fired in {elapsed_minutes:.0f} min "
            f"(threshold: {threshold_minutes:.0f} min, "
            f"derived from cron '{heartbeat_cron}' × {safety_factor}). "
            "APScheduler may be wedged — check logs and consider restart."
        ),
        dedupe_key="scheduler-wedge:heartbeat",
        priority=5,  # urgent
        tags=["warning", "hourglass_not_done"],
    )
