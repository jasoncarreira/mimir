"""Per-day token usage rollup for the ops dashboard.

Reads ``logs/turns.jsonl`` and aggregates each turn's ``usage`` dict
into daily totals so /ops can render a volume trend chart. Useful for
**all** deployments — subscription-quota deployments get the
utilization-% view from ``usage_history.py``, but a token-volume view
is the only relevant Usage chart for API-mode (pay-per-token)
deployments that don't have quota windows.

Output schema is a list of per-day buckets:

.. code-block:: python

    [
        {
            "date": "2026-05-23",
            "input_tokens": 250,
            "cache_creation_input_tokens": 16529,
            "cache_read_input_tokens": 313683,
            "output_tokens": 1981,
            "total_cost_usd": 0.42,   # None on subscription / non-API routes
            "turn_count": 12,
        },
        ...
    ]

Date keys are ISO calendar dates (UTC). Buckets are emitted in
chronological order (oldest first) so the chart x-axis flows
left-to-right naturally.

Anthropic-shaped usage fields (``input_tokens`` / ``output_tokens`` /
``cache_creation_input_tokens`` / ``cache_read_input_tokens``) are the
only ones aggregated. Other providers (OpenAI's ``prompt_tokens`` /
``completion_tokens``) would need separate handling; not implemented
because both production deployments (Anthropic Max + Minimax via
anthropic-compat) emit Anthropic-shaped usage.

Pure-data module — no I/O, no async. The dashboard caller passes
already-loaded turn records; tests pass synthetic dicts.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable


def _parse_ts(text: str) -> datetime | None:
    """Parse an ISO timestamp; tolerate the ``Z`` suffix."""
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _day_key(ts: datetime) -> str:
    """Return the ISO calendar-day key for a UTC timestamp."""
    return ts.astimezone(timezone.utc).date().isoformat()


def compute_token_usage_history(
    turns: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Aggregate per-turn token usage into per-day buckets.

    ``turns`` is any iterable of TurnRecord-shaped dicts loaded from
    ``logs/turns.jsonl``. Each turn's ``usage`` dict is summed into the
    bucket keyed by the turn's ``ts`` calendar day (UTC).

    Turns with missing or non-dict ``usage`` are counted in the daily
    ``turn_count`` (they still ran) but contribute zero tokens. Turns
    with missing or unparseable ``ts`` are skipped entirely — there's
    no meaningful bucket for them.

    Cost is summed when ``total_cost_usd`` is a number; otherwise the
    bucket's ``total_cost_usd`` stays None. A bucket with cost from
    SOME turns but None from others reports the sum of what's
    available (silent partial-data is the right default; the operator
    can cross-reference total_cost_usd presence via ``turn_count``).
    """
    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": 0,
            "total_cost_usd": None,
            "turn_count": 0,
        }
    )

    for turn in turns:
        ts = _parse_ts(turn.get("ts") or turn.get("timestamp") or "")
        if ts is None:
            continue
        key = _day_key(ts)
        bucket = buckets[key]
        bucket["turn_count"] += 1

        usage = turn.get("usage")
        if isinstance(usage, dict):
            for field in (
                "input_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
                "output_tokens",
            ):
                value = usage.get(field)
                if isinstance(value, (int, float)):
                    bucket[field] += int(value)

        cost = turn.get("total_cost_usd")
        if isinstance(cost, (int, float)):
            if bucket["total_cost_usd"] is None:
                bucket["total_cost_usd"] = 0.0
            bucket["total_cost_usd"] += float(cost)

    # Emit chronological order (oldest first) so the chart x-axis
    # reads naturally left-to-right.
    out: list[dict[str, Any]] = []
    for date_key in sorted(buckets.keys()):
        bucket = buckets[date_key]
        out.append({"date": date_key, **bucket})
    return out
