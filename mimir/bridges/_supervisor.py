"""Shared retry / throttle helpers for bridge supervisors (chainlink #246).

Both ``DiscordBridge`` and ``SlackBridge`` run a long-lived supervisor
coroutine that connects to its upstream service, catches transient
failures, and retries with exponential backoff. The supervisors differ
in their fatal-exception classification and their start-call shape, but
they share two helpers:

- :func:`should_emit_retry_algedonic` — throttles the retry-event emit
  so a multi-hour outage doesn't spam events.jsonl.
- :func:`safe_log_event` — best-effort wrapper around
  :func:`mimir.event_logger.log_event` so a misbehaving logger can't
  wedge the reconnect loop.

Pre-chainlink-#246, each bridge carried a private copy of both. A fix
to one (the discord-side throttling tweak that took weeks to land on
the slack-side) was a recurring source of bridge-behavior drift.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def should_emit_retry_algedonic(attempt: int) -> bool:
    """Throttle ``*_bridge_retry`` events during sustained outages.

    Fires every attempt from 3-9 inclusive (so the operator sees the
    early "is this real?" signal fast), then every 10th attempt
    thereafter (10, 20, 30...). A multi-hour outage at the 5-min
    backoff cap would otherwise produce ~12 retry events/hour;
    throttling drops that to ~1.2/hour for the sustained case while
    keeping the early-warning shape.
    """
    if attempt < 3:
        return False
    if attempt < 10:
        return True
    return attempt % 10 == 0


async def safe_log_event(bridge_label: str, event_kind: str, **fields: Any) -> None:
    """Best-effort wrapper around :func:`mimir.event_logger.log_event`.

    Swallows any logger-side error so a misbehaving event sink can
    never wedge the reconnect loop. *bridge_label* is the prefix used
    in the failure log message (``"DiscordBridge"`` / ``"SlackBridge"``).
    """
    try:
        from ..event_logger import log_event
        await log_event(event_kind, **fields)
    except Exception:  # noqa: BLE001
        log.exception("%s: log_event(%r) failed", bridge_label, event_kind)
