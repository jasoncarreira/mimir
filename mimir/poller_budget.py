"""Per-poller budget and usage helpers.

Slice #696 is deliberately read-only: it attributes agent-turn cost to
pollers from existing ``turns.jsonl`` records. Later slices add budget
configuration, external usage signals, and suppression gates.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from ._jsonl_tail import tail_jsonl_records

from .jsonl_snapshot import JsonlSnapshot, iter_window_records


log = logging.getLogger(__name__)

POLLER_USAGE_SOURCE_CHARS = 120
POLLER_USAGE_METRICS = ("api_calls", "api_bytes", "estimated_cost_usd")


POLLER_USAGE_WINDOWS: tuple[tuple[str, float], ...] = (("1h", 1.0), ("24h", 24.0))


@dataclass(frozen=True)
class PollerBudgetWindowConfig:
    """Configured caps for one poller budget window.

    All fields are optional. A configured window may cap one or more of the
    dimensions the poller-budget aggregator can later measure.
    """

    max_agent_turns: int | None = None
    max_agent_usd: float | None = None
    max_api_calls: int | None = None
    max_api_bytes: int | None = None
    max_external_usd: float | None = None

    def to_dict(self) -> dict[str, int | float]:
        out: dict[str, int | float] = {}
        for key in (
            "max_agent_turns",
            "max_agent_usd",
            "max_api_calls",
            "max_api_bytes",
            "max_external_usd",
        ):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out


@dataclass(frozen=True)
class PollerBudgetConfig:
    """Fail-open per-poller budget configuration.

    ``on_exceed`` is intentionally narrow for v1: the only runtime behavior
    planned by #107 is ``suppress``. The dataclass still carries it so future
    warn-only/report-only modes have an obvious extension point.
    """

    windows: dict[str, PollerBudgetWindowConfig] = field(default_factory=dict)
    on_exceed: str = "suppress"

    def to_dict(self) -> dict[str, object]:
        return {
            "windows": {
                label: window.to_dict()
                for label, window in sorted(self.windows.items())
            },
            "on_exceed": self.on_exceed,
        }


_BUDGET_INT_CAPS = frozenset({"max_agent_turns", "max_api_calls", "max_api_bytes"})
_BUDGET_FLOAT_CAPS = frozenset({"max_agent_usd", "max_external_usd"})
_BUDGET_CAPS = _BUDGET_INT_CAPS | _BUDGET_FLOAT_CAPS
_SUPPORTED_BUDGET_WINDOWS = frozenset(label for label, _hours in POLLER_USAGE_WINDOWS)


def _coerce_budget_int(raw: object) -> int | None:
    if isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value


def _coerce_budget_float(raw: object) -> float | None:
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value < 0 or value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def parse_poller_budget_config(
    raw: object,
    *,
    source: Path | str,
    poller_name: str,
) -> PollerBudgetConfig | None:
    """Parse one poller's ``budget`` block, returning ``None`` on invalid input.

    Budget config is operator/deployment policy. It must be fail-open: a typo
    warns and drops only the budget for that poller, never the poller itself.
    """

    if raw is None:
        return None
    prefix = f"poller_budget_invalid: {source} — {poller_name}.budget"
    if not isinstance(raw, dict):
        log.warning("%s must be a mapping; ignoring budget", prefix)
        return None

    on_exceed = str(raw.get("on_exceed", "suppress")).strip().lower()
    if on_exceed != "suppress":
        log.warning(
            "%s.on_exceed=%r unsupported (expected 'suppress'); ignoring budget",
            prefix, raw.get("on_exceed"),
        )
        return None

    windows_raw = raw.get("windows")
    if not isinstance(windows_raw, dict) or not windows_raw:
        log.warning("%s.windows must be a non-empty mapping; ignoring budget", prefix)
        return None

    windows: dict[str, PollerBudgetWindowConfig] = {}
    for label_raw, window_raw in windows_raw.items():
        label = str(label_raw).strip()
        if not label:
            log.warning("%s.windows has an empty window label; ignoring budget", prefix)
            return None
        if label not in _SUPPORTED_BUDGET_WINDOWS:
            log.warning(
                "%s.windows.%s is unsupported (supported: %s); ignoring window",
                prefix,
                label,
                ", ".join(sorted(_SUPPORTED_BUDGET_WINDOWS)),
            )
            continue
        if not isinstance(window_raw, dict):
            log.warning(
                "%s.windows.%s must be a mapping; ignoring budget",
                prefix, label,
            )
            return None
        unknown = set(window_raw) - _BUDGET_CAPS
        if unknown:
            log.warning(
                "%s.windows.%s has unknown cap(s): %s; ignoring budget",
                prefix, label, ", ".join(sorted(str(k) for k in unknown)),
            )
            return None
        parsed: dict[str, int | float] = {}
        for key, value in window_raw.items():
            if key in _BUDGET_INT_CAPS:
                coerced = _coerce_budget_int(value)
            else:
                coerced = _coerce_budget_float(value)
            if coerced is None:
                log.warning(
                    "%s.windows.%s.%s=%r is invalid; ignoring budget",
                    prefix, label, key, value,
                )
                return None
            parsed[str(key)] = coerced
        if not parsed:
            log.warning(
                "%s.windows.%s must configure at least one cap; ignoring budget",
                prefix, label,
            )
            return None
        windows[label] = PollerBudgetWindowConfig(**parsed)

    if not windows:
        log.warning("%s.windows has no supported windows; ignoring budget", prefix)
        return None

    return PollerBudgetConfig(windows=windows, on_exceed=on_exceed)


@dataclass
class PollerUsageWindow:
    """Read-only usage attributed to one poller in one window."""

    label: str
    hours: float
    agent_turns: int = 0
    total_cost_usd: float | None = None
    cost_samples: int = 0
    api_calls: float | int = 0
    api_bytes: float | int = 0
    estimated_external_cost_usd: float | int = 0

    def record_turn(self, cost_usd: float | None) -> None:
        self.agent_turns += 1
        if cost_usd is None:
            return
        if self.total_cost_usd is None:
            self.total_cost_usd = 0.0
        self.total_cost_usd += cost_usd
        self.cost_samples += 1

    def record_external_usage(
        self,
        *,
        api_calls: float | int,
        api_bytes: float | int,
        estimated_cost_usd: float | int,
    ) -> None:
        self.api_calls += api_calls
        self.api_bytes += api_bytes
        self.estimated_external_cost_usd += estimated_cost_usd

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
            "api_calls": self.api_calls,
            "api_bytes": self.api_bytes,
            "estimated_external_cost_usd": round(
                float(self.estimated_external_cost_usd), 6,
            ),
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


def _coerce_usage_metric(raw: object) -> float | int | None:
    """Coerce a poller usage metric, or return ``None`` if invalid.

    ``bool`` is rejected even though it is an ``int`` subclass; usage records
    should not silently treat ``true`` as one API call. Accepted ints remain
    ints for cleaner JSON, while floats must be finite and non-negative.
    """

    if raw is None:
        return 0
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if isinstance(raw, float):
        return raw if math.isfinite(raw) and raw >= 0 else None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return 0
        try:
            value = float(text)
        except ValueError:
            return None
        if not math.isfinite(value) or value < 0:
            return None
        return int(value) if value.is_integer() else value
    return None


def validate_poller_usage_signal(
    parsed: dict[str, object],
    *,
    poller_name: str,
) -> tuple[dict[str, object] | None, str | None]:
    """Validate a ``signal: poller_usage`` stdout record.

    Accepted telemetry is returned as a payload ready for ``log_event`` (without
    the redundant ``poller``/``signal`` keys). Invalid records return a compact
    reason suitable for ``poller_invalid_usage_signal``.
    """

    raw_poller = parsed.get("poller")
    if raw_poller != poller_name:
        return None, "poller_mismatch"

    payload: dict[str, object] = {}
    for metric in POLLER_USAGE_METRICS:
        value = _coerce_usage_metric(parsed.get(metric))
        if value is None:
            return None, f"invalid_{metric}"
        payload[metric] = value

    source = parsed.get("source")
    if source is not None:
        payload["source"] = str(source)[:POLLER_USAGE_SOURCE_CHARS]
    return payload, None


def aggregate_poller_turn_usage(
    turns_path: Path,
    *,
    now: datetime | None = None,
    windows: Iterable[tuple[str, float]] = POLLER_USAGE_WINDOWS,
    snapshot: JsonlSnapshot | None = None,
    events_path: Path | None = None,
) -> dict[str, PollerUsage]:
    """Aggregate poller-triggered agent turns from ``turns.jsonl``.

    Records are attributed when ``channel_id`` is exactly ``poller:<name>``.
    External usage is attributed from ``poller_usage`` events in ``events.jsonl``
    when ``events_path`` is provided. Both scans are newest-first and stop once
    they reach the oldest requested cutoff, matching the bounded/tail pattern
    used by usage aggregation. Missing or unreadable logs yield an empty mapping.
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

    def ensure_summary(poller: str) -> PollerUsage:
        return out.setdefault(
            poller,
            PollerUsage(
                poller=poller,
                windows={
                    label: PollerUsageWindow(label=label, hours=hours)
                    for label, hours in window_defs
                },
            ),
        )

    for rec in iter_window_records(snapshot, turns_path):
        ts = _parse_ts(rec.get("ts") or rec.get("timestamp"))
        if ts is None:
            continue
        if ts < oldest_cutoff:
            break
        poller = _poller_name_from_channel(rec.get("channel_id"))
        if poller is None:
            continue
        summary = ensure_summary(poller)
        cost = _coerce_cost(rec.get("total_cost_usd"))
        for label, _hours in window_defs:
            if ts >= cutoffs[label]:
                summary.windows[label].record_turn(cost)

    if events_path is not None:
        for rec in tail_jsonl_records(events_path):
            ts = _parse_ts(rec.get("ts") or rec.get("timestamp"))
            if ts is None:
                continue
            if ts < oldest_cutoff:
                break
            if rec.get("type") != "poller_usage":
                continue
            poller_raw = rec.get("poller")
            if not isinstance(poller_raw, str) or not poller_raw.strip():
                continue
            api_calls = _coerce_usage_metric(rec.get("api_calls"))
            api_bytes = _coerce_usage_metric(rec.get("api_bytes"))
            estimated_cost_usd = _coerce_usage_metric(rec.get("estimated_cost_usd"))
            if api_calls is None or api_bytes is None or estimated_cost_usd is None:
                continue
            summary = ensure_summary(poller_raw.strip())
            for label, _hours in window_defs:
                if ts >= cutoffs[label]:
                    summary.windows[label].record_external_usage(
                        api_calls=api_calls,
                        api_bytes=api_bytes,
                        estimated_cost_usd=estimated_cost_usd,
                    )

    return out
