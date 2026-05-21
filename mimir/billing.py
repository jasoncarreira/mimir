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
from pathlib import Path
from typing import Optional

from .rate_limits import (
    RateLimitSnapshot,
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
    oauth_credentials_path: Path | None = None,
) -> BillingMode:
    """Resolve ``BillingMode`` from explicit override + environment.

    Precedence:

    1. ``explicit`` (case-insensitive; rejects unknown values with a
       warning and falls through to auto-detect).
    2. ``CLAUDE_CODE_OAUTH_TOKEN`` env var → quota.
    3. ``oauth_credentials_path`` points at an existing file → quota
       (an OAuth credentials file is the OAuth-flow signal). The
       file-existence check matters: ``_oauth_credentials_path()`` in
       ``config.py`` returns the *expected location* even on installs
       that have never run ``claude /login``, so a Path-truthy check
       was effectively always-true on any deployment with ``MIMIR_HOME``
       set — which is virtually every deployment, including pure
       pay-as-you-go API-key installs. Result: API-key installs auto-
       detected as QUOTA, demoting ``cost_rate_alert`` to advisory and
       silently disabling the dollar-cost suppression layer.
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
    if oauth_credentials_path is not None and oauth_credentials_path.is_file():
        return BillingMode.QUOTA
    # MIMIR_CLAUDE_OAUTH_CREDENTIALS env-var presence is treated as
    # operator intent (rather than a default-location heuristic), so
    # we don't apply the ``.is_file()`` guard here. An operator who
    # sets the env var is declaring "I'm on the OAuth flow" — even
    # before ``claude /login`` has written a credentials file. The
    # asymmetry vs. the path branch above is load-bearing as of
    # CR2-#1: the path is a resolved-default-location hint, the env
    # var is an explicit declaration.
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
    signal" — does NOT suppress on absent values.

    ``derived`` (chainlink #17): true when this window's utilization
    was estimated from cost data (the cost-rate-back-derived 5h
    estimator that fires when the endpoint reading is rejected as
    anomalous by layer (a)). Causes :func:`evaluate_quota` to apply
    a looser raw-suppress threshold for this window — derived values
    are approximations and shouldn't trip the wall threshold as
    aggressively as direct readings."""

    key: str
    window_hours: float
    utilization: Optional[float]
    on_pace_utilization: Optional[float]
    resets_at: Optional[int]
    derived: bool = False


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
            is_derived = getattr(snap, "derived", False)
            # chainlink #17: on-pace projection on a derived utilization
            # is methodologically broken — derived is a synthetic
            # point estimate (cost-rate-back-derived during endpoint
            # glitches), not a time-series sample. Extrapolating it
            # forward via ``project_window_end`` would treat a 0.85
            # derived value at minute 10 of a 5h window as a 5x rate
            # and project well past 1.0, tripping the on-pace
            # threshold spuriously. Skip on-pace for derived windows;
            # the looser raw threshold (0.90 vs 0.80, see
            # ``_raw_threshold_for``) is the only suppression signal
            # we trust on derived values.
            if is_derived:
                on_pace = None
            else:
                proj = project_window_end(snap, hours)
                on_pace = (
                    proj.on_pace_utilization if proj is not None else None
                )
            out.append(
                QuotaWindow(
                    key=key,
                    window_hours=hours,
                    utilization=snap.utilization,
                    on_pace_utilization=on_pace,
                    resets_at=snap.resets_at,
                    derived=is_derived,
                )
            )
        return out


# ─── Minimax concrete implementation (stub — Issue #243) ──────────────


# Window sizes for Minimax's quota plan. Presumed 5h + 7d in the same
# shape as Anthropic / z.ai — the QuotaWindow contract should map
# cleanly. NOT VERIFIED until the Minimax usage poller (Issue #243)
# lands and confirms the actual windows the billing API exposes; if
# the real values differ, this dict updates without breaking the
# QuotaWindow interface. Per-model sub-buckets (if any) get added
# here once the integration is wired.
_MINIMAX_WINDOW_HOURS: dict[str, float] = {
    "five_hour": 5.0,
    "seven_day": 24.0 * 7,
}


class MinimaxQuotaProvider(QuotaProvider):
    """Reads Minimax subscription quota windows from
    :class:`RateLimitStore`. The store is populated by the Minimax-
    side usage poller (TODO — Issue #243), which mirrors the
    ``oauth_usage_poller.py`` pattern: queries Minimax's billing
    endpoint on a 3-min cron and writes ``RateLimitSnapshot`` entries
    keyed ``minimax_five_hour`` / ``minimax_seven_day``.

    **Today: stub.** Until the usage poller lands, ``get_windows``
    returns the empty list — the arbiter treats that as "no signal"
    and falls through to cost-rate-only suppression (or accepts the
    spike-ratio default). Once the poller is wired, the snapshot read
    here transcribes the cached values into ``QuotaWindow``
    objects.

    The provider is registered automatically for Minimax-compat
    deployments (``ANTHROPIC_BASE_URL`` pointing at
    ``api.minimax.io``) via :func:`build_quota_providers`. Operators
    on canonical Anthropic + Max OAuth continue to get
    ``AnthropicQuotaProvider``.
    """

    #: ``RateLimitStore`` key prefix for Minimax-side snapshots. The
    #: poller writes keys like ``minimax_five_hour``; this provider
    #: reads them back.
    _STORE_KEY_PREFIX = "minimax_"

    def __init__(self, store: RateLimitStore) -> None:
        self._store = store

    @property
    def provider_name(self) -> str:
        return "minimax"

    def get_windows(self) -> list[QuotaWindow]:
        # TODO(#243): once Minimax usage poller lands, walk
        # _MINIMAX_WINDOW_HOURS and transcribe store snapshots to
        # QuotaWindow with on_pace projections (same shape as
        # AnthropicQuotaProvider above). For now: no data → empty
        # list → arbiter treats as "no signal" (safe fallback).
        out: list[QuotaWindow] = []
        snaps = self._store.current()
        for key, hours in _MINIMAX_WINDOW_HOURS.items():
            snap = snaps.get(f"{self._STORE_KEY_PREFIX}{key}")
            if snap is None:
                continue
            is_derived = getattr(snap, "derived", False)
            if is_derived:
                on_pace = None
            else:
                proj = project_window_end(snap, hours)
                on_pace = (
                    proj.on_pace_utilization if proj is not None else None
                )
            out.append(
                QuotaWindow(
                    key=key,
                    window_hours=hours,
                    utilization=snap.utilization,
                    on_pace_utilization=on_pace,
                    resets_at=snap.resets_at,
                    derived=is_derived,
                )
            )
        return out


# ─── OpenAI concrete implementation (stub — Codex Plus subscription) ──


# OpenAI Codex Plus / Pro subscription documents window-based quota
# in the same 5h + weekly shape as Anthropic Max / Minimax sub. The
# weekly window length differs slightly (rolling 7d vs calendar-week)
# but the QuotaWindow contract handles that fine — the arbiter just
# cares about utilization + resets_at.
_OPENAI_WINDOW_HOURS: dict[str, float] = {
    "five_hour": 5.0,
    "seven_day": 24.0 * 7,
}


class OpenAIQuotaProvider(QuotaProvider):
    """Reads OpenAI Codex Plus / Pro subscription quota windows from
    :class:`RateLimitStore`.

    **Codex doesn't have a polling endpoint** — quota state is piggy-
    backed on every Codex API response via headers, the same way
    Anthropic does it. From ``openai/codex`` source
    (``codex-rs/codex-api/src/rate_limits.rs``):

    * ``x-codex-primary-used-percent`` / ``-window-minutes`` /
      ``-reset-at`` — short window (typically 5h)
    * ``x-codex-secondary-*`` — long window (typically weekly)
    * ``x-codex-credits-*`` — credits balance

    So the writer that populates ``openai_five_hour`` /
    ``openai_seven_day`` in the store will be a response-header
    interceptor on the LangChain Codex client (TODO — follow-up PR
    requires a Codex Plus client integration first; the OpenAI Codex
    subscription protocol hits ``chatgpt.com/backend-api/codex/
    responses``, which is different from ``api.openai.com/v1/chat/
    completions``).

    **Today: stub.** Returns ``[]`` until the header extractor lands;
    the arbiter treats empty as "no signal" and falls through to
    cost-rate suppression — safe fallback.

    Registered when ``MIMIR_MODEL_SPEC`` starts with ``openai:`` AND
    the billing mode is QUOTA (operator declared subscription tier
    via ``mimir setup --subscription``). Pay-per-token API keys
    against ``api.openai.com`` get no quota provider — cost-rate
    handles that side.

    Token storage (for the eventual Codex Plus client): Codex CLI
    persists OAuth tokens at ``$CODEX_HOME/auth.json`` (defaults to
    ``~/.codex/auth.json``). Operators run ``codex login`` to
    populate it.
    """

    _STORE_KEY_PREFIX = "openai_"

    def __init__(self, store: RateLimitStore) -> None:
        self._store = store

    @property
    def provider_name(self) -> str:
        return "openai"

    def get_windows(self) -> list[QuotaWindow]:
        # TODO: once OpenAI usage poller lands, walk
        # _OPENAI_WINDOW_HOURS and transcribe store snapshots to
        # QuotaWindow with on_pace projections. Until then: no
        # ``openai_*`` keys in the store → empty list → arbiter
        # treats as "no signal" (safe fallback).
        out: list[QuotaWindow] = []
        snaps = self._store.current()
        for key, hours in _OPENAI_WINDOW_HOURS.items():
            snap = snaps.get(f"{self._STORE_KEY_PREFIX}{key}")
            if snap is None:
                continue
            is_derived = getattr(snap, "derived", False)
            if is_derived:
                on_pace = None
            else:
                proj = project_window_end(snap, hours)
                on_pace = (
                    proj.on_pace_utilization if proj is not None else None
                )
            out.append(
                QuotaWindow(
                    key=key,
                    window_hours=hours,
                    utilization=snap.utilization,
                    on_pace_utilization=on_pace,
                    resets_at=snap.resets_at,
                    derived=is_derived,
                )
            )
        return out


# ─── Writer side: ChatCodexPlus → RateLimitStore ──────────────────────


def make_codex_plus_rate_limit_callback(
    store: RateLimitStore,
) -> Any:
    """Build the rate_limit_callback that ``ChatCodexPlus`` invokes
    after each successful ``/codex/responses`` call. Transcribes the
    parsed ``CodexRateLimits`` headers into ``RateLimitSnapshot``
    entries keyed ``openai_five_hour`` / ``openai_seven_day`` — the
    keys :class:`OpenAIQuotaProvider` already reads.

    The callback runs inline on the chat model's response path (sync
    or async — depends on which ``invoke``/``ainvoke``/``stream``
    variant the agent uses). We write through the store's sync API
    to avoid having to reason about which thread / loop the callback
    is firing on; the store's last-write-wins behavior is acceptable
    because quota snapshots are monotonically refreshed.

    Argument typed as :class:`Any` to avoid an eager
    ``langchain_codex_plus`` import; the duck-typed ``rl`` object is
    a :class:`langchain_codex_plus.CodexRateLimits` with optional
    ``primary`` / ``secondary`` :class:`CodexQuotaWindow` fields.
    Conversion: ``used_percent`` (0-100) → ``utilization`` (0-1).
    """
    import datetime as _dt

    def _callback(rl: Any) -> None:
        observed_at = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
        if rl is None:
            return
        primary = getattr(rl, "primary", None)
        if primary is not None and primary.used_percent is not None:
            store.record_sync(
                "openai_five_hour",
                RateLimitSnapshot(
                    status="allowed",
                    utilization=float(primary.used_percent) / 100.0,
                    resets_at=primary.reset_at,
                    observed_at=observed_at,
                ),
            )
        secondary = getattr(rl, "secondary", None)
        if secondary is not None and secondary.used_percent is not None:
            store.record_sync(
                "openai_seven_day",
                RateLimitSnapshot(
                    status="allowed",
                    utilization=float(secondary.used_percent) / 100.0,
                    resets_at=secondary.reset_at,
                    observed_at=observed_at,
                ),
            )

    return _callback


# ─── Auto-discovery: which QuotaProvider(s) to register at boot ───────


def build_quota_providers(
    *,
    store: RateLimitStore,
    billing_mode: BillingMode,
    model_spec: str = "",
    anthropic_base_url: str = "",
) -> list[QuotaProvider]:
    """Return the right ``QuotaProvider`` list for this deployment's
    routing. Replaces the hardcoded ``[AnthropicQuotaProvider(...)]``
    that ``mimir.agent`` used to build directly.

    Returns empty list for ``PAY_AS_YOU_GO`` — no quota signal exists,
    cost-rate suppression handles the spending side instead.

    For ``QUOTA`` mode, picks providers based on the agent's routing.
    Detection key precedence:

    1. **``MIMIR_MODEL_SPEC`` prefix** (most specific):
       * ``codex-plus:*`` → :class:`OpenAIQuotaProvider` (Codex Plus
         subscription via ``langchain-codex-plus`` — same store keys
         as ``openai:`` since both feed the same arbiter view, but the
         writer is the chat model's ``rate_limit_callback`` rather
         than a polling endpoint).
       * ``openai:*`` → :class:`OpenAIQuotaProvider` (Codex Plus / Pro)
       * ``claude-code:*`` → :class:`AnthropicQuotaProvider`
         (Max OAuth — provider explicitly chosen via subprocess)
    2. **``ANTHROPIC_BASE_URL`` host** (for ``anthropic:*`` routes
       that didn't qualify the provider):
       * ``api.minimax.io`` → :class:`MinimaxQuotaProvider`
       * everything else (canonical / unset / unknown gateway) →
         :class:`AnthropicQuotaProvider`

    Adding a new provider (Moonshot subscription quota, gateway
    quotas, etc.) is one ``elif`` branch.
    """
    if billing_mode is not BillingMode.QUOTA:
        return []
    spec = (model_spec or "").strip().lower()
    if spec.startswith("codex-plus:") or spec.startswith("openai:"):
        return [OpenAIQuotaProvider(store)]
    if spec.startswith("claude-code:"):
        return [AnthropicQuotaProvider(store)]
    # ``anthropic:*`` and everything else falls through to URL-based
    # detection — the host tells us whether we're routed to a compat
    # endpoint or hitting Anthropic directly.
    from urllib.parse import urlparse
    base = (anthropic_base_url or "").strip()
    host = ""
    if base:
        try:
            host = urlparse(base).hostname or ""
        except (ValueError, AttributeError):
            host = ""
    if host == "api.minimax.io":
        return [MinimaxQuotaProvider(store)]
    # Default: canonical Anthropic (unset URL OR api.anthropic.com OR
    # any other host where we don't have a wrapped quota API yet).
    return [AnthropicQuotaProvider(store)]


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
# chainlink #17: derived 5h utilization (cost-rate-back-derived during
# endpoint glitches) gets a looser raw-suppress threshold than direct
# readings. The estimator rounds to 5pp + uses an empirical ~10× 5h:7d
# back-derive factor (``QUOTA_5H_BACKDERIVE_FACTOR_DEFAULT`` in
# mimir/oauth_usage_poller.py). 90% accommodates the slop without
# giving up the suppression signal entirely on long glitches.
#
# Coupling note: this threshold and the back-derive factor both encode
# the trust we extend to derived values. If the factor changes
# (different plan tier, telemetry-confirmed re-calibration via
# ``MIMIR_QUOTA_5H_BACKDERIVE_FACTOR``), the variance band of the
# derived estimate shifts and this threshold may need to move with it.
DEFAULT_RAW_SUPPRESS_DERIVED = 0.90


def _on_pace_threshold(window_key: str) -> float:
    """5h windows are tighter; 7d windows are looser. Unknown keys
    default to the 7d threshold (conservative — projections over
    longer horizons are less reliable, so demand more headroom)."""
    if window_key == "five_hour":
        return DEFAULT_ON_PACE_SUPPRESS_5H
    return DEFAULT_ON_PACE_SUPPRESS_7D


def _raw_threshold_for(window: "QuotaWindow", direct_threshold: float) -> float:
    """Pick the raw-utilization suppress threshold for a window.
    Derived windows (chainlink #17) get a looser cap — the estimator
    is approximate, so don't suppress as aggressively on it as on a
    direct endpoint reading."""
    if window.derived:
        return DEFAULT_RAW_SUPPRESS_DERIVED
    return direct_threshold


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
            if w.utilization is not None:
                w_threshold = _raw_threshold_for(w, raw_threshold)
                if w.utilization >= w_threshold:
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
