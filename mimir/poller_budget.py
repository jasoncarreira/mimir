"""Per-poller budget and usage helpers.

Slice #696 is deliberately read-only: it attributes agent-turn cost to
pollers from existing ``turns.jsonl`` records. Later slices add budget
configuration, external usage signals, and suppression gates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .jsonl_snapshot import JsonlSnapshot, iter_window_records


POLLER_USAGE_WINDOWS: tuple[tuple[str, float], ...] = (("1h", 1.0), ("24h", 24.0))


@dataclass
class PollerUsageWindow:
    """Read-only LLM turn usage attributed to one poller in one window."""

    label: str
    hours: float
    agent_turns: int = 0
    total_cost_usd: float | None = None
    cost_samples: int = 0

    def record_turn(self, cost_usd: float | None) -> None:
        self.agent_turns += 1
        if cost_usd is None:
            return
        if self.total_cost_usd is None:
            self.total_cost_usd = 0.0
        self.total_cost_usd += cost_usd
        self.cost_samples += 1

    def to_dict(self) -> dict[str, float | int | str | None]:
        if self.agent_turns == 0:
            total_cost_usd = 0.0
        elif self.cost_samples == 0:
            total_cost_usd = None
        else:
            total_cost_usd = round(self.total_cost_usd or 0.0, 6)
        return {
            "label": self.label,
            "hours": self.hours,
            "agent_turns": self.agent_turns,
            "total_cost_usd": total_cost_usd,
        }


@dataclass
class PollerUsage:
    """Read-only usage summary for one poller."""

    poller: str
    windows: dict[str, PollerUsageWindow] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "poller": self.poller,
            "windows": {
                label: window.to_dict()
                for label, window in sorted(self.windows.items())
            },
        }


def _parse_ts(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _poller_name_from_channel(channel_id: object) -> str | None:
    if not isinstance(channel_id, str):
        return None
    prefix = "poller:"
    if not channel_id.startswith(prefix):
        return None
    name = channel_id[len(prefix):].strip()
    return name or None


def _coerce_cost(raw: object) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def aggregate_poller_turn_usage(
    turns_path: Path,
    *,
    now: datetime | None = None,
    windows: Iterable[tuple[str, float]] = POLLER_USAGE_WINDOWS,
    snapshot: JsonlSnapshot | None = None,
) -> dict[str, PollerUsage]:
    """Aggregate poller-triggered agent turns from ``turns.jsonl``.

    Records are attributed when ``channel_id`` is exactly ``poller:<name>``.
    The scan is newest-first and stops once it reaches the oldest requested
    cutoff, matching the bounded/tail pattern used by usage aggregation.
    Missing or unreadable logs yield an empty mapping.
    """

    window_defs = [(label, float(hours)) for label, hours in windows]
    if not window_defs:
        return {}
    if now is None:
        now = datetime.now(tz=timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    cutoffs = {label: now - timedelta(hours=hours) for label, hours in window_defs}
    oldest_cutoff = min(cutoffs.values())
    out: dict[str, PollerUsage] = {}

    for rec in iter_window_records(snapshot, turns_path):
        ts = _parse_ts(rec.get("ts"))
        if ts is None:
            continue
        if ts < oldest_cutoff:
            break
        poller = _poller_name_from_channel(rec.get("channel_id"))
        if poller is None:
            continue
        summary = out.setdefault(
            poller,
            PollerUsage(
                poller=poller,
                windows={
                    label: PollerUsageWindow(label=label, hours=hours)
                    for label, hours in window_defs
                },
            ),
        )
        cost = _coerce_cost(rec.get("total_cost_usd"))
        for label, _hours in window_defs:
            if ts >= cutoffs[label]:
                summary.windows[label].record_turn(cost)

    return out
