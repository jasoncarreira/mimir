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

import logging
import os
from datetime import datetime, timezone

import aiohttp

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
