"""Rolling subscription-quota history for the ops dashboard.

Reads ``logs/events.jsonl`` and normalizes the subscription-quota
events emitted by mimir's three poller/callback paths into a uniform,
provider-keyed time series:

- ``minimax_usage_ok`` (``mimir.minimax_usage_poller``) →
  ``provider="minimax"``
- ``oauth_usage_ok`` (Anthropic OAuth poller) →
  ``provider="anthropic"``
- ``codex_plus_usage_ok`` (``mimir.billing`` callback writer,
  emitted by ``make_codex_plus_rate_limit_callback`` once a Codex
  Plus model has been called) → ``provider="codex_plus"``

The output schema is provider → window → list[point]:

.. code-block:: python

    {
      "anthropic": {
        "five_hour":          [{"ts": "...", "utilization": 0.02, "resets_at": ...}, ...],
        "seven_day":          [...],
        "seven_day_sonnet":   [...],
        "seven_day_omelette": [...]
      },
      "minimax":    {"five_hour": [...], "seven_day": [...]},
      "codex_plus": {"five_hour": [...], "seven_day": [...]}
    }

Window naming is uniform across providers (``five_hour``,
``seven_day``, plus model-scoped sub-windows where applicable) so the
dashboard can render one chart per provider with one line per window,
without per-provider casing.

Multiple subscriptions can be active in the same deployment — e.g. an
agent using Claude Max OAuth for the chat model AND Codex Plus for
saga LLM calls. Each emits its own ``*_usage_ok`` event stream, so the
output dict carries both providers concurrently.

Downsampling: a 7-day, 3-min cadence stream is ~3360 raw points per
provider. ``compute_usage_history`` downsamples to at most
``max_points`` (default 200) via last-value-per-bucket, keeping the
chart payload small without sacrificing the recent-window shape.

Pure-data module — no I/O, no async. The dashboard caller passes
already-loaded events; tests pass synthetic dicts.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Provider detection + key normalization
# ---------------------------------------------------------------------------

# Event-type → provider name. Listed explicitly (rather than pattern-matched)
# so adding a new subscription provider is a deliberate one-line change here.
_PROVIDER_BY_EVENT_TYPE: dict[str, str] = {
    "minimax_usage_ok": "minimax",
    "oauth_usage_ok": "anthropic",
    "codex_plus_usage_ok": "codex_plus",
}


def _normalize_window_key(provider: str, raw_key: str) -> str:
    """Strip the provider prefix from window keys so the schema is uniform.

    The Minimax poller writes ``minimax_five_hour`` / ``minimax_seven_day``
    (the provider-prefixed RateLimitStore key) into the event payload.
    Anthropic OAuth and Codex Plus emit the un-prefixed names
    (``five_hour`` / ``seven_day`` plus model-scoped variants).
    Normalize Minimax to match so the dashboard's per-provider chart
    doesn't need provider-specific casing.
    """
    prefix = f"{provider}_"
    if raw_key.startswith(prefix):
        return raw_key[len(prefix):]
    return raw_key


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass
class UsagePoint:
    """One sample of a quota window's state at a given timestamp."""

    ts: str  # ISO 8601 with timezone
    utilization: float | None  # 0.0 → 1.0; None if the event reported null
    resets_at: int | None  # unix epoch seconds; None if absent

    def to_json(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "utilization": self.utilization,
            "resets_at": self.resets_at,
        }


def normalize_subscription_events(
    events: Iterable[dict[str, Any]],
) -> dict[str, dict[str, list[UsagePoint]]]:
    """Turn raw events.jsonl records into provider → window → [point].

    Empty / malformed records are skipped silently. Non-quota events
    are dropped at the dispatch step (event type not in the provider
    table). Points are returned in input order; callers downstream
    sort + downsample.
    """
    out: dict[str, dict[str, list[UsagePoint]]] = defaultdict(
        lambda: defaultdict(list),
    )
    for ev in events:
        if not isinstance(ev, dict):
            continue
        provider = _PROVIDER_BY_EVENT_TYPE.get(ev.get("type", ""))
        if provider is None:
            continue
        ts = ev.get("timestamp")
        if not isinstance(ts, str):
            continue
        # Codex Plus + Minimax + Anthropic OAuth all use the same shape:
        # a ``recorded`` dict mapping window keys to ``{utilization,
        # resets_at, status}`` snapshots. Codex Plus's older event
        # variant used ``windows`` instead of ``recorded``; tolerate
        # both so we don't lose the historical data when the writer
        # gets renamed.
        windows = ev.get("recorded") or ev.get("windows") or {}
        if not isinstance(windows, dict):
            continue
        for raw_key, snap in windows.items():
            if not isinstance(snap, dict):
                continue
            window = _normalize_window_key(provider, raw_key)
            util = snap.get("utilization")
            if util is not None:
                try:
                    util = float(util)
                except (TypeError, ValueError):
                    util = None
            resets = snap.get("resets_at")
            if resets is not None:
                try:
                    resets = int(resets)
                except (TypeError, ValueError):
                    resets = None
            out[provider][window].append(
                UsagePoint(ts=ts, utilization=util, resets_at=resets),
            )
    # Convert nested defaultdicts to plain dicts for serialization.
    return {p: dict(w) for p, w in out.items()}


def _downsample_last_per_bucket(
    points: list[UsagePoint], max_points: int,
) -> list[UsagePoint]:
    """Keep the last point per uniform time bucket so the series stays
    smooth without overwhelming the chart payload.

    Picks bucket width based on the input range so a 7-day stream and
    a 1-hour stream both produce ≤max_points samples. The "last" point
    in each bucket wins — chart users care about the most recent value
    in a window, not an average.

    Edge cases:

    - Empty input → empty output.
    - len(points) ≤ max_points → return as-is (no downsampling needed).
    - Points without parseable timestamps fall back to input-order
      and rely on the caller-side max_points truncation.
    """
    if not points or len(points) <= max_points:
        return list(points)
    parsed: list[tuple[datetime, UsagePoint]] = []
    for p in points:
        try:
            dt = datetime.fromisoformat(p.ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        parsed.append((dt, p))
    if not parsed:
        # Couldn't parse any timestamps — just truncate to the tail so
        # the chart at least shows the most recent samples.
        return list(points[-max_points:])
    parsed.sort(key=lambda kv: kv[0])
    start = parsed[0][0]
    end = parsed[-1][0]
    span = (end - start).total_seconds()
    if span <= 0:
        return [parsed[-1][1]]
    # Bucket size in seconds. With max_points buckets numbered 0..max-1,
    # bucket_s = span / max_points means the final point's index is
    # exactly max_points (off-by-one — span/bucket_s = max_points, not
    # max_points - 1). Clamping each index into [0, max_points - 1]
    # collapses the last sample into the trailing bucket without
    # affecting earlier ones.
    bucket_s = max(1.0, span / max_points)
    buckets: dict[int, UsagePoint] = {}
    for dt, p in parsed:
        idx = int((dt - start).total_seconds() // bucket_s)
        idx = min(idx, max_points - 1)
        buckets[idx] = p  # last write wins → most recent in the bucket
    return [buckets[k] for k in sorted(buckets.keys())]


def compute_usage_history(
    events: Iterable[dict[str, Any]],
    days: int,
    *,
    max_points_per_series: int = 200,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """End-to-end: filter to window, normalize, downsample, JSON-ify.

    Returns the schema the ops dashboard renders:

    .. code-block:: python

        {
          "<provider>": {
            "<window>": [{"ts": ..., "utilization": ..., "resets_at": ...}, ...]
          }
        }

    ``days`` is the lookback window in days (caller-applied via the
    events query). Providers with zero records are omitted entirely so
    the dashboard can render charts per non-empty provider — an
    Anthropic-OAuth-only deployment doesn't get an empty Codex Plus
    chart.
    """
    del days  # event filtering by date happens at the caller (compute_stats)
    normalized = normalize_subscription_events(events)
    result: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for provider, windows in normalized.items():
        out_windows: dict[str, list[dict[str, Any]]] = {}
        for window, points in windows.items():
            ds = _downsample_last_per_bucket(points, max_points_per_series)
            if ds:
                out_windows[window] = [p.to_json() for p in ds]
        if out_windows:
            result[provider] = out_windows
    return result
