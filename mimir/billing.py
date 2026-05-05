"""Billing-mode-aware suppression for the S3-S4 homeostat (chainlink #13).

Two billing modes, per-install (single mode):

- ``quota`` — provider has plan windows (5h + 1-week) with hard caps
  and zero marginal cost up to the cap. Suppression input: on-pace
  projection across configured windows, plus the existing raw-
  utilization "literal wall" check at ``plan_window_suppress_threshold``.
  Cost-rate spikes are demoted to advisory (logged, not suppressing).

- ``pay-as-you-go`` — provider charges per token. Suppression input:
  current ``cost_rate_alert`` behavior (``spike_ratio`` against a
  rolling-week baseline). Plan-window data, if present, is ignored
  for suppression decisions in this mode (it's not the binding
  constraint when every token costs real money — the spike check is).

Auto-detect default: presence of any OAuth signal
(``CLAUDE_CODE_OAUTH_TOKEN`` env var, or ``MIMIR_CLAUDE_OAUTH_CREDENTIALS``
configured for the OAuth usage poller) → ``quota``; else
``pay-as-you-go``. Explicit override via ``MIMIR_BILLING_MODE``.

Quota mode is pluggable through the :class:`QuotaProvider` ABC.
Anthropic / Minimax / z.ai have standardized on the 5h + 1-week
window shape, so the interface assumes that — providers may also
return additional windows (e.g. Anthropic's per-model
``seven_day_opus`` / ``seven_day_sonnet``) and the arbiter treats
them as additional 7d-sized constraints. The first concrete
implementation, :class:`AnthropicQuotaProvider`, reads from the
existing :class:`mimir.rate_limits.RateLimitStore` populated by the
OAuth usage poller (and by SDK rate-limit capture under direct API
keys).
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .rate_limits import (
    RateLimitStore,
    project_window_end,
    running_on_claude_max,
)

log = logging.getLogger(__name__)


# ─── billing mode ──────────────────────────────────────────────────────


class BillingMode(str, Enum):
    """Per-install billing-mode tag. String-valued so it round-trips
    through env vars and JSON without a custom serializer."""

    QUOTA = "quota"
    PAY_AS_YOU_GO = "pay-as-you-go"


def detect_billing_mode(
    *,
    explicit: str | None = None,
    oauth_credentials_path: object | None = None,
) -> BillingMode:
    """Resolve ``BillingMode`` from explicit override + environment.

    Precedence:

    1. ``explicit`` (case-insensitive; rejects unknown values with a
       warning and falls through to auto-detect).
    2. ``CLAUDE_CODE_OAUTH_TOKEN`` env var → quota.
    3. ``oauth_credentials_path`` truthy → quota (poller is configured,
       even if direct OAuth isn't).
    4. Default → pay-as-you-go.
    """
    if explicit:
        try:
            return BillingMode(explicit.strip().lower())
        except ValueError:
            log.warning(
                "MIMIR_BILLING_MODE=%r is not a valid mode "
                "(quota | pay-as-you-go); falling back to auto-detect",
                explicit,
            )
    if running_on_claude_max():
        return BillingMode.QUOTA
    if oauth_credentials_path:
        return BillingMode.QUOTA
    if os.environ.get("MIMIR_CLAUDE_OAUTH_CREDENTIALS", "").strip():
        return BillingMode.QUOTA
    return BillingMode.PAY_AS_YOU_GO


# ─── quota provider interface ──────────────────────────────────────────


@dataclass(frozen=True)
class QuotaWindow:
    """A single plan-window snapshot with current and projected
    utilization. ``utilization`` is the current 0-1 fraction;
    ``on_pace_utilization`` is the projected end-of-window value (None
    when the projection isn't trustworthy — too early in window, no
    ``resets_at``, etc.). The arbiter treats missing data as "no
    signal" — does NOT suppress on absent values."""

    key: str
    window_hours: float
    utilization: Optional[float]
    on_pace_utilization: Optional[float]
    resets_at: Optional[int]


class QuotaProvider(ABC):
    """One billing-mode=quota install carries one or more
    QuotaProvider instances; today single-provider only, the interface
    is shaped so a future second provider just appends to the list."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Stable short name (``"anthropic"``, ``"minimax"``, ``"zai"``).
        Used in suppression-reason strings and event tags."""

    @abstractmethod
    def get_windows(self) -> list[QuotaWindow]:
        """Return every live window for this provider. Empty list when
        the provider has no current data (cold start, poller hasn't run
        yet, credentials missing, ...). The arbiter treats empty as "no
        signal" — does not suppress on missing data."""


# ─── Anthropic concrete implementation ─────────────────────────────────


# Window length per ``rate_limit_type`` we know about, in hours. The
# 5h + 1-week shape is what Anthropic / Minimax / z.ai have all
# standardized on; the per-model 7d windows (``seven_day_opus``,
# ``seven_day_sonnet``) are Anthropic-specific extras with the same
# 7d window-size, so they fall through the same on-pace threshold.
# ``overage`` is open-ended (no fixed window), excluded from
# projection — Anthropic exposes it as the pay-as-you-go bolt-on for
# accounts that have it enabled, which is a different constraint
# entirely.
_ANTHROPIC_WINDOW_HOURS: dict[str, float] = {
    "five_hour": 5.0,
    "seven_day": 24.0 * 7,
    "seven_day_opus": 24.0 * 7,
    "seven_day_sonnet": 24.0 * 7,
}


class AnthropicQuotaProvider(QuotaProvider):
    """Reads from :class:`RateLimitStore`. The store is populated by
    the OAuth usage poller (``mimir/oauth_usage_poller.py``) under
    Max-OAuth and by the SDK rate-limit capture path under direct API
    keys. The provider doesn't care which writer landed the data —
    it just projects from whatever's current."""

    def __init__(self, store: RateLimitStore) -> None:
        self._store = store

    @property
    def provider_name(self) -> str:
        return "anthropic"

    def get_windows(self) -> list[QuotaWindow]:
        out: list[QuotaWindow] = []
        snaps = self._store.current()
        for key, hours in _ANTHROPIC_WINDOW_HOURS.items():
            snap = snaps.get(key)
            if snap is None:
                continue
            proj = project_window_end(snap, hours)
            out.append(
                QuotaWindow(
                    key=key,
                    window_hours=hours,
                    utilization=snap.utilization,
                    on_pace_utilization=(
                        proj.on_pace_utilization if proj is not None else None
                    ),
                    resets_at=snap.resets_at,
                )
            )
        return out


# ─── suppression evaluation ────────────────────────────────────────────


# Defaults. The raw-utilization threshold matches the existing
# ``plan_window_suppress_threshold`` for backward compatibility (we
# previously suppressed at raw 0.80). The on-pace thresholds are
# looser because projections are noisier than ground truth — we want
# to suppress when we WILL blow past quota, not flap on every tick.
# 5h windows get a tighter threshold than 7d because there's less
# time for the projection to be wrong.
DEFAULT_RAW_SUPPRESS_THRESHOLD = 0.80
DEFAULT_ON_PACE_SUPPRESS_5H = 0.90
DEFAULT_ON_PACE_SUPPRESS_7D = 0.95


def _on_pace_threshold(window_key: str) -> float:
    """5h windows are tighter; 7d windows are looser. Unknown keys
    default to the 7d threshold (conservative — projections over
    longer horizons are less reliable, so demand more headroom)."""
    if window_key == "five_hour":
        return DEFAULT_ON_PACE_SUPPRESS_5H
    return DEFAULT_ON_PACE_SUPPRESS_7D


@dataclass(frozen=True)
class QuotaSuppressionResult:
    """Decision output of :func:`evaluate_quota`.

    ``reason`` follows the same shape as the existing
    ``plan_window_saturated:<key>@<util>`` format so downstream
    rendering / introspection counts don't have to special-case the
    new strings:

    - ``"ok"`` — no suppression
    - ``"quota_saturated:<provider>:<key>@<util>"`` — raw utilization
      crossed the wall threshold
    - ``"quota_off_pace:<provider>:<key>@<on_pace>"`` — projection
      crossed the on-pace threshold for that window size
    """

    suppress: bool
    reason: str
    provider: Optional[str]
    window_key: Optional[str]


def evaluate_quota(
    providers: list[QuotaProvider],
    *,
    raw_threshold: float = DEFAULT_RAW_SUPPRESS_THRESHOLD,
) -> QuotaSuppressionResult:
    """Across all configured providers, decide whether to suppress.

    Worst-case wins (most-suppressive provider/window). Raw-utilization
    saturation takes precedence over on-pace projection — if we're
    already at the wall, no point projecting forward.

    Returns ``suppress=False`` when no provider reports any data.
    Missing data is "we don't know" not "we're suppressed" — cold
    starts and poller hiccups shouldn't gate scheduled work."""
    raw_hits: list[tuple[str, str, float]] = []  # (provider, key, util)
    on_pace_hits: list[tuple[str, str, float]] = []  # (provider, key, on_pace)

    for provider in providers:
        try:
            windows = provider.get_windows()
        except Exception:  # noqa: BLE001 — never crash the arbiter
            log.exception(
                "QuotaProvider %s.get_windows raised; treating as empty",
                provider.provider_name,
            )
            continue
        for w in windows:
            if w.utilization is not None and w.utilization >= raw_threshold:
                raw_hits.append((provider.provider_name, w.key, w.utilization))
            if w.on_pace_utilization is not None:
                threshold = _on_pace_threshold(w.key)
                if w.on_pace_utilization >= threshold:
                    on_pace_hits.append(
                        (provider.provider_name, w.key, w.on_pace_utilization)
                    )

    if raw_hits:
        provider, key, util = max(raw_hits, key=lambda t: t[2])
        return QuotaSuppressionResult(
            suppress=True,
            reason=f"quota_saturated:{provider}:{key}@{util:.2f}",
            provider=provider,
            window_key=key,
        )
    if on_pace_hits:
        provider, key, on_pace = max(on_pace_hits, key=lambda t: t[2])
        return QuotaSuppressionResult(
            suppress=True,
            reason=f"quota_off_pace:{provider}:{key}@{on_pace:.2f}",
            provider=provider,
            window_key=key,
        )
    return QuotaSuppressionResult(
        suppress=False,
        reason="ok",
        provider=None,
        window_key=None,
    )
