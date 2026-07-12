"""Billing-mode-aware severity grading for the S3-S4 homeostat
(chainlink #13; priority-banded suppression).

Two billing modes, per-install (single mode):

- ``quota`` — provider has plan windows (5h + 1-week) with hard caps
  and zero marginal cost up to the cap. Severity input: the burst-
  multiple pace bands per window (:func:`evaluate_quota_severity`),
  plus the raw-utilization "literal wall" check at
  ``plan_window_suppress_threshold``. Cost-rate spikes are demoted to
  advisory (logged, not a severity input).

- ``pay-as-you-go`` — provider charges per token. Severity input:
  ``cost_rate_alert`` (hourly limit / ``spike_ratio`` against a
  rolling-week baseline) → TIGHT, near-trip → ELEVATED (graded in
  ``mimir.budget``). Plan-window data, when present, stays a TIGHT
  sanity wall.

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

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any, Optional

from .rate_limits import (
    RateLimitSnapshot,
    RateLimitStore,
    project_window_end,
    running_on_claude_max,
)
# Per-provider quota-window registry (chainlink #298) — the single source
# the agent's Resource-usage view (rate_limits.py) also reads. The provider
# classes below derive their window-hours from it instead of each carrying
# a duplicate literal dict.
from .quota_windows import ANTHROPIC, CODEX_PLUS, MINIMAX

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
    model_spec: str = "",
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
    4. ``model_spec`` starts with ``codex-plus:`` → quota (the Codex /
       ChatGPT-account subscription; the OpenAI analog of the OAuth
       signals above). A bare ``openai:`` API-key spec is NOT a
       subscription, so it's excluded — cost-rate handles that side.
    5. Default → pay-as-you-go.
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
    # A Codex (ChatGPT-account) subscription is QUOTA: the ``codex-plus:``
    # model_spec is the operator's subscription declaration — the OpenAI
    # analog of the Anthropic OAuth signals. Without it a Codex-only install
    # (no Anthropic creds, no explicit MIMIR_BILLING_MODE) auto-detected
    # PAY_AS_YOU_GO, so build_quota_providers returned [] and the Codex quota
    # view disappeared (chainlink #315). A bare ``openai:`` spec is the
    # pay-per-token API, NOT a subscription, so it is intentionally excluded.
    if model_spec.strip().lower().startswith("codex-plus:"):
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
    anomalous by layer (a)). Causes :func:`evaluate_quota_severity` to apply
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


# Window-hours per quota window now live in mimir/quota_windows.py (the
# per-provider single source). ``window_hours()`` returns the same
# ``{logical_key: hours}`` shape these provider classes expect, minus the
# open-ended windows (Anthropic's ``overage``, excluded from projection).
# (chainlink #298)
_ANTHROPIC_WINDOW_HOURS = ANTHROPIC.window_hours()


def _is_stale_observation(observed_at: object, window_hours: float) -> bool:
    """True when an ISO ``observed_at`` is older than ``window_hours``
    (the window it was read from has definitionally rolled since).
    Unparseable / missing timestamps are NOT treated as stale — they
    carry no age signal either way, and suppressing on them would turn
    a logging quirk into a policy decision (chainlink #424)."""
    if not isinstance(observed_at, str) or not observed_at:
        return False
    import datetime as _dt
    try:
        ts = _dt.datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.timezone.utc)
    age_hours = (
        _dt.datetime.now(tz=_dt.timezone.utc) - ts
    ).total_seconds() / 3600.0
    return age_hours > window_hours


class _StorageBackedQuotaProvider(QuotaProvider):
    """Shared base for providers that read :class:`RateLimitStore` with a
    fixed ``{key → window_hours}`` mapping and an optional store-key
    prefix.

    Subclasses declare three class-level attributes:

    - ``_provider_name``: the string returned by :prop:`provider_name`.
    - ``_window_hours``: ``{logical_key: hours}`` mapping.
    - ``_store_key_prefix``: empty for canonical Anthropic, non-empty
      for vendor-prefixed snapshots (e.g. ``"minimax_"``).

    chainlink #245: the three concrete providers had ~95% identical
    ``get_windows`` implementations differing only in the dict + prefix.
    The chainlink #17 derived-skip and ``project_window_end`` plumbing
    now live in one place — adding a fourth provider takes ~10 lines.
    """

    _provider_name: str = ""
    _window_hours: dict[str, float] = {}
    _store_key_prefix: str = ""

    def __init__(self, store: RateLimitStore) -> None:
        self._store = store

    @property
    def provider_name(self) -> str:
        return self._provider_name

    def get_windows(self) -> list[QuotaWindow]:
        out: list[QuotaWindow] = []
        snaps = self._store.current()
        for key, hours in self._window_hours.items():
            snap = snaps.get(f"{self._store_key_prefix}{key}")
            if snap is None:
                continue
            # Staleness guard (chainlink #424): ``RateLimitStore.current()``
            # only expires entries past their ``resets_at`` — a snapshot
            # with ``resets_at=None`` (derived 5h estimates by design;
            # reset-at-less Codex headers) survives forever. Grading the
            # raw wall from a reading older than its own window length is
            # grading a window that has definitionally rolled since — on
            # Codex (no independent poller; quota arrives only on
            # responses) a stale ≥wall reading could wedge severity TIGHT
            # with nothing left under suppression to refresh it. Treat
            # such a reading as no-signal; entries WITH a resets_at keep
            # the store's own expiry.
            if snap.resets_at is None and _is_stale_observation(
                getattr(snap, "observed_at", ""), hours,
            ):
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


class AnthropicQuotaProvider(_StorageBackedQuotaProvider):
    """Reads from :class:`RateLimitStore`. The store is populated by
    the OAuth usage poller (``mimir/oauth_usage_poller.py``) under
    Max-OAuth and by the SDK rate-limit capture path under direct API
    keys. The provider doesn't care which writer landed the data —
    it just projects from whatever's current."""

    _provider_name = "anthropic"
    _window_hours = _ANTHROPIC_WINDOW_HOURS
    _store_key_prefix = ""


# ─── Minimax concrete implementation (stub — Issue #243) ──────────────


# Minimax quota windows — see mimir/quota_windows.MINIMAX. (chainlink #298)
_MINIMAX_WINDOW_HOURS = MINIMAX.window_hours()


class MinimaxQuotaProvider(_StorageBackedQuotaProvider):
    """Reads Minimax subscription quota windows from
    :class:`RateLimitStore`. The store is populated by the Minimax-
    side usage poller (TODO — Issue #243), which mirrors the
    ``oauth_usage_poller.py`` pattern: queries Minimax's billing
    endpoint on a 3-min cron and writes ``RateLimitSnapshot`` entries
    keyed ``minimax_five_hour`` / ``minimax_seven_day``.

    **Today: stub.** Until the usage poller lands, ``get_windows``
    returns the empty list — the arbiter treats that as "no signal"
    and falls through to cost-rate-only suppression (or accepts the
    spike-ratio default).

    The provider is registered automatically for Minimax-compat
    deployments (``ANTHROPIC_BASE_URL`` pointing at
    ``api.minimax.io``) via :func:`build_quota_providers`. Operators
    on canonical Anthropic + Max OAuth continue to get
    ``AnthropicQuotaProvider``.
    """

    _provider_name = "minimax"
    _window_hours = _MINIMAX_WINDOW_HOURS
    _store_key_prefix = "minimax_"


# ─── OpenAI concrete implementation (stub — Codex Plus subscription) ──


# OpenAI Codex Plus quota windows — see mimir/quota_windows.CODEX_PLUS.
# (chainlink #298)
_OPENAI_WINDOW_HOURS = CODEX_PLUS.window_hours()


class OpenAIQuotaProvider(_StorageBackedQuotaProvider):
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

    The writer that populates ``openai_five_hour`` /
    ``openai_seven_day`` in the store is :func:`make_codex_plus_rate_limit_callback`
    — a response-header interceptor on the LangChain Codex client.

    Registered when ``MIMIR_MODEL_SPEC`` starts with ``openai:`` AND
    the billing mode is QUOTA (operator declared subscription tier
    via ``mimir setup --subscription``). Pay-per-token API keys
    against ``api.openai.com`` get no quota provider — cost-rate
    handles that side.

    Token storage (for the Codex Plus client): Codex CLI persists
    OAuth tokens at ``$CODEX_HOME/auth.json`` (defaults to
    ``~/.codex/auth.json``). Operators run ``codex login`` to
    populate it.
    """

    _provider_name = "openai"
    _window_hours = _OPENAI_WINDOW_HOURS
    _store_key_prefix = "openai_"


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

    from .event_logger import log_event_sync

    def _sync_callback(rl: Any) -> None:
        observed_at = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
        if rl is None:
            return
        # Collect snapshots first so we can emit a single combined
        # ``codex_plus_usage_ok`` event at the end — same shape as
        # ``minimax_usage_ok`` / ``oauth_usage_ok`` so ``usage_history``
        # treats all three providers uniformly.
        # Route each window to its store key by WINDOW LENGTH, not by
        # primary/secondary position. The Codex backend does not fix the
        # position: on a Plus plan primary=5h + secondary=7d, but on a Pro plan
        # the single window is the 7d one reported as PRIMARY (window_minutes
        # 10080) with an empty secondary. Position-based mapping put the real
        # 7d usage under the 5h key and left the 7d panel reading the empty
        # secondary → a false 0%. Classify by window_minutes instead: >= 1 day
        # is the long (7d) window, shorter is the 5h window. An empty window
        # (window_minutes == 0) is skipped; unknown length (None) falls back to
        # positional so Plus behavior is unchanged.
        recorded: dict[str, dict[str, Any]] = {}
        for window, positional_key, positional_short in (
            (getattr(rl, "primary", None), "openai_five_hour", "five_hour"),
            (getattr(rl, "secondary", None), "openai_seven_day", "seven_day"),
        ):
            if window is None or window.used_percent is None:
                continue
            minutes = getattr(window, "window_minutes", None)
            if minutes == 0:
                continue  # empty/absent window (e.g. Pro plan's unused secondary)
            if minutes is None:
                store_key, short = positional_key, positional_short
            elif minutes >= 1440:  # >= 1 day → the long (weekly/7d) window
                store_key, short = "openai_seven_day", "seven_day"
            else:
                store_key, short = "openai_five_hour", "five_hour"
            util = float(window.used_percent) / 100.0
            store.record_sync(
                store_key,
                RateLimitSnapshot(
                    status="allowed",
                    utilization=util,
                    resets_at=window.reset_at,
                    observed_at=observed_at,
                ),
            )
            recorded[short] = {
                "utilization": util,
                "resets_at": window.reset_at,
                "status": "allowed",
            }
        # Emit one event per response so the ops dashboard can build
        # a time series. Best-effort — log_event_sync's OSError path
        # already swallows file IO errors at WARN; we belt-and-suspender
        # against logger-not-initialized too (some tests construct the
        # callback before event_logger.init_logger has run).
        if recorded:
            try:
                log_event_sync("codex_plus_usage_ok", recorded=recorded)
            except (RuntimeError, OSError):
                pass

    background_lock = asyncio.Lock()
    background_tasks: set[asyncio.Task[None]] = set()

    async def _async_callback(rl: Any) -> None:
        async with background_lock:
            await asyncio.to_thread(_sync_callback, rl)

    def _background_done(task: asyncio.Task[None]) -> None:
        background_tasks.discard(task)
        try:
            task.result()
        except Exception:  # noqa: BLE001 — callback is best-effort telemetry
            log.exception("codex_plus rate-limit callback background write failed")

    def _callback(rl: Any) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _sync_callback(rl)
            return
        task = loop.create_task(_async_callback(rl))
        background_tasks.add(task)
        task.add_done_callback(_background_done)

    return _callback


# ─── Auto-discovery: which QuotaProvider(s) to register at boot ───────


# Binds a ProviderSpec.quota_provider_key (the registry's billing key) to
# its concrete QuotaProvider. The registry (mimir/providers.py) owns the
# routing taxonomy — which model_spec / base_url maps to which provider —
# and this table binds the resolved provider to its poller class.
# Providers without a wrapped quota API (Moonshot today) carry the
# ``"anthropic"`` key — the same fallback the old host-based detection gave.
_QUOTA_PROVIDER_BUILDERS = {
    "anthropic": AnthropicQuotaProvider,
    "minimax": MinimaxQuotaProvider,
    "openai": OpenAIQuotaProvider,
}


def build_quota_providers(
    *,
    store: RateLimitStore,
    billing_mode: BillingMode,
    model_spec: str = "",
    anthropic_base_url: str = "",
) -> list[QuotaProvider]:
    """Return the ``QuotaProvider`` list for this deployment's routing.

    Empty for ``PAY_AS_YOU_GO`` — no quota signal exists; cost-rate
    suppression handles the spending side instead.

    For ``QUOTA`` mode the provider is resolved by the registry
    (:func:`mimir.providers.provider_for_quota`) from the
    ``MIMIR_MODEL_SPEC`` prefix + ``ANTHROPIC_BASE_URL`` host —
    ``codex-plus:`` / ``openai:`` → OpenAI, ``claude-code:`` → Anthropic
    Max, an ``anthropic:`` route on a ``minimax`` host → Minimax (chainlink
    #259: matched by substring so regional gateways still qualify), and
    everything else → Anthropic direct. ``_QUOTA_PROVIDER_BUILDERS`` binds
    the resolved provider's ``quota_provider_key`` to its poller class.
    Adding a provider is one ``ProviderSpec`` entry in the registry
    (chainlink #292).
    """
    if billing_mode is not BillingMode.QUOTA:
        return []
    from .providers import provider_for_quota
    prov = provider_for_quota(model_spec, anthropic_base_url)
    builder = _QUOTA_PROVIDER_BUILDERS.get(prov.quota_provider_key)
    return [builder(store)] if builder else []


# ─── suppression evaluation ────────────────────────────────────────────


# Defaults. The raw-utilization wall moved 0.80 → 0.90 with the pace
# floor below: pace bands now carry the "heading toward the cap"
# signal once the projection clears the floor, so the absolute wall
# only needs to catch "genuinely almost out" — at 90% consumed,
# autonomous work defers regardless of how flattering the pace looks
# (subject to the coasting demotion).
DEFAULT_RAW_SUPPRESS_THRESHOLD = 0.90

# Pace floor: the M bands engage only when the projected end-of-window
# utilization exceeds this. A window not even projected to reach 75%
# of its cap has no quota story to tell — grading its pace headroom
# just starves low-priority maintenance (observed live: muninn's
# heartbeats went dark for days over a projected-68% week because
# M sat at 1.79). Below the floor, severity from this window is CLEAR
# no matter how small M is.
DEFAULT_PACE_PROJECTION_FLOOR = 0.75
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
# NOTE: with the direct wall now also at 0.90 the derived/direct gap
# has closed — kept as a named constant so the looser-for-derived
# distinction can reopen (e.g. 0.95) if derived readings false-trip
# the wall in practice.
DEFAULT_RAW_SUPPRESS_DERIVED = 0.90

# ─── severity bands (priority-banded suppression) ──────────────────────
#
# The binary suppress/fire decision is generalized into a severity
# ladder so autonomous work can shed by priority instead of all-or-
# nothing (heartbeats only). The QUOTA-mode signal is the **burst
# multiple** M — how many times the established burn pace the agent
# would have to sustain for the REST of the window to hit 100%:
#
#   M = (1 - util) / (pace × time_left) = ((1 - util) / util) × (elapsed / left)
#
# which, given the on-pace projection P = util / elapsed_fraction the
# providers already compute, reduces to M = (1 - u) / (P - u). The same
# projected end-of-window value means very different risk depending on
# time remaining: projected 80% with 1 day left of a 7d window needs
# ~2.75× the established pace to actually bust the cap (safe), while
# projected 80% with 5 days left busts at only ~1.35× (fragile). M
# captures that directly — utilization, pace, and time-left in one
# number; lower M = less headroom.
#
# Band edges (post-ramp): M < BURST_TIGHT → TIGHT, M < BURST_ELEVATED →
# ELEVATED, else CLEAR. Early-window wiggle: pace estimated over a
# small elapsed fraction is noisy (one busy hour at the start of a 7d
# window reads as a furious pace), so the edges are scaled by
# γ = min(1, elapsed_fraction / RAMP_FRACTION) — at 10% elapsed the
# TIGHT edge is 1.5×0.4 = 0.6, so only a genuinely extreme burn rate
# suppresses anything early; by RAMP_FRACTION elapsed the bands apply
# at full strength. Below ``project_window_end``'s
# ``min_elapsed_fraction`` there is no projection at all and severity
# falls back to raw utilization only.
DEFAULT_BURST_TIGHT = 1.5
DEFAULT_BURST_ELEVATED = 2.0
DEFAULT_RAMP_FRACTION = 0.25


class Severity(IntEnum):
    """Autonomy-throttle severity, worst-signal-wins. Ordered so
    comparisons read naturally (``severity >= Severity.TIGHT``)."""

    CLEAR = 0      # full speed — nothing tripped
    ELEVATED = 1   # headroom shrinking — shed low-priority work
    TIGHT = 2      # raw wall / on pace to exceed — shed low + normal
    BLOCKED = 3    # recorded 429 pause — provider refusing; shed all


# Work priorities and the worst severity each still fires under.
# ``low`` (heartbeats by default) yields at the first sign of pressure;
# ``high`` digs into the quota tail for near-interactive feeds; nothing
# fires under BLOCKED (the provider is actively refusing — headroom
# math is moot).
PRIORITY_LEVELS = ("low", "normal", "high")
_PRIORITY_TOLERANCE: dict[str, Severity] = {
    "low": Severity.CLEAR,
    "normal": Severity.ELEVATED,
    "high": Severity.TIGHT,
}


def normalize_priority(raw: object, *, default: str = "normal") -> str:
    """Coerce an operator-supplied priority value to a known level.
    Unknown / non-string values fall back to ``default`` (callers warn
    with their own file/entry context)."""
    if isinstance(raw, str) and raw.strip().lower() in PRIORITY_LEVELS:
        return raw.strip().lower()
    return default


def priority_tolerates(priority: str, severity: Severity) -> bool:
    """True when work of ``priority`` should still fire under
    ``severity``. Unknown priorities are treated as ``normal``."""
    tolerance = _PRIORITY_TOLERANCE.get(priority, _PRIORITY_TOLERANCE["normal"])
    return severity <= tolerance


def burst_multiple(
    utilization: float, on_pace_utilization: float,
) -> float | None:
    """The burst multiple M from current utilization + the on-pace
    projection (see the band-edges comment block above). None when the
    inputs carry no usable rate signal (zero utilization, projection
    not above current — i.e. window effectively over)."""
    if utilization <= 0.0 or on_pace_utilization <= utilization:
        return None
    if utilization >= 1.0:
        return 0.0
    return (1.0 - utilization) / (on_pace_utilization - utilization)


def _raw_threshold_for(window: "QuotaWindow", direct_threshold: float) -> float:
    """Pick the raw-utilization suppress threshold for a window.
    Derived windows (chainlink #17) get a looser cap — the estimator
    is approximate, so don't suppress as aggressively on it as on a
    direct endpoint reading."""
    if window.derived:
        return DEFAULT_RAW_SUPPRESS_DERIVED
    return direct_threshold


@dataclass(frozen=True)
class QuotaSeverityResult:
    """Decision output of :func:`evaluate_quota_severity`.

    ``reason`` keeps the legacy shapes for the suppressing bands
    (``quota_saturated:<provider>:<key>@<util>`` /
    ``quota_off_pace:<provider>:<key>@<on_pace>``) so downstream
    rendering / introspection counts don't have to special-case;
    ELEVATED adds ``quota_pace_elevated:<provider>:<key>@<on_pace>``.
    ``burst_multiple`` / ``gamma`` are carried for event payloads and
    operator triage (None when the deciding signal was raw-utilization
    rather than pace)."""

    severity: Severity
    reason: str
    provider: Optional[str]
    window_key: Optional[str]
    burst_multiple: Optional[float] = None
    gamma: Optional[float] = None


def evaluate_quota_severity(
    providers: list[QuotaProvider],
    *,
    raw_threshold: float = DEFAULT_RAW_SUPPRESS_THRESHOLD,
    burst_tight: float = DEFAULT_BURST_TIGHT,
    burst_elevated: float = DEFAULT_BURST_ELEVATED,
    ramp_fraction: float = DEFAULT_RAMP_FRACTION,
    pace_floor: float = DEFAULT_PACE_PROJECTION_FLOOR,
) -> QuotaSeverityResult:
    """Across all configured providers, grade quota pressure into a
    :class:`Severity`. Worst window wins; within the same severity a
    raw-utilization hit outranks a pace hit (we're AT the wall vs.
    heading toward it), then the tighter reading wins.

    Signals per window:

    - **raw wall** — ``utilization >= raw_threshold`` (looser for
      ``derived`` readings, chainlink #17) → TIGHT. Near the cap the
      absolute headroom is small and one big turn can peg the bucket
      — M can't see burst risk because a burst is precisely a
      departure from established pace. Also the only signal for
      pegged buckets / derived / early windows, where
      ``on_pace_utilization`` is None by design. **Coasting
      demotion:** when the same window's pace shows the cap won't be
      hit (M ≥ the ELEVATED edge), the wall grades ELEVATED instead —
      "85% used, slow pace, reset near" sheds low-priority work only.
    - **burst multiple** — M from :func:`burst_multiple`, band edges
      scaled by the early-window ramp γ (see the band-edges comment
      block above): ``M < burst_tight×γ`` → TIGHT, ``M <
      burst_elevated×γ`` → ELEVATED. The bands engage only when the
      projection clears ``pace_floor`` (default 0.75) — a window not
      projected to get near its cap has no quota story to tell, no
      matter how small M is.

    Returns CLEAR when no provider reports any data. Missing data is
    "we don't know" not "we're suppressed" — cold starts and poller
    hiccups shouldn't gate scheduled work. BLOCKED is never produced
    here: a recorded 429 pause is the arbiter's call (it owns the
    pause tracker), layered above this evaluation."""
    # Candidates: (severity, kind_rank, tightness, reason, provider,
    # key, M, gamma). kind_rank 0 = raw (outranks pace at equal
    # severity); tightness orders within a kind (higher = worse).
    candidates: list[
        tuple[Severity, int, float, str, str, str, Optional[float], Optional[float]]
    ] = []

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
            pname = provider.provider_name
            # Pace signal first — the raw wall consults it below.
            m: Optional[float] = None
            gamma: Optional[float] = None
            if (
                w.on_pace_utilization is not None
                and w.utilization is not None
            ):
                m = burst_multiple(w.utilization, w.on_pace_utilization)
                if m is not None:
                    # elapsed_fraction = util / on_pace (P = u / ef).
                    # Both inputs are positive per burst_multiple's
                    # guards.
                    elapsed_fraction = w.utilization / w.on_pace_utilization
                    gamma = min(1.0, elapsed_fraction / ramp_fraction)

            if w.utilization is not None:
                w_threshold = _raw_threshold_for(w, raw_threshold)
                if w.utilization >= w_threshold:
                    # Coasting demotion: when this window's own pace
                    # shows the cap won't be hit (M clears the
                    # ELEVATED edge — e.g. 85% used, slow pace, reset
                    # near), the wall grades ELEVATED instead of
                    # TIGHT: low-priority work still yields (absolute
                    # headroom IS thin and turns are bursty — reserve
                    # the tail for interactive work), but normal
                    # pollers keep their feeds fresh through the
                    # window tail. Without pace evidence (pegged /
                    # derived / early window — projection absent by
                    # design) the wall stays TIGHT: it's the only
                    # signal we have.
                    wall_severity = Severity.TIGHT
                    if (
                        m is not None and gamma is not None
                        and m >= burst_elevated * gamma
                    ):
                        wall_severity = Severity.ELEVATED
                    candidates.append((
                        wall_severity, 0, w.utilization,
                        f"quota_saturated:{pname}:{w.key}@{w.utilization:.2f}",
                        pname, w.key, m, gamma,
                    ))

            if (
                m is not None and gamma is not None
                and w.on_pace_utilization is not None
                and w.on_pace_utilization > pace_floor
            ):
                if m < burst_tight * gamma:
                    candidates.append((
                        Severity.TIGHT, 1, -m,
                        f"quota_off_pace:{pname}:{w.key}@{w.on_pace_utilization:.2f}",
                        pname, w.key, m, gamma,
                    ))
                elif m < burst_elevated * gamma:
                    candidates.append((
                        Severity.ELEVATED, 1, -m,
                        f"quota_pace_elevated:{pname}:{w.key}@{w.on_pace_utilization:.2f}",
                        pname, w.key, m, gamma,
                    ))

    if not candidates:
        return QuotaSeverityResult(
            severity=Severity.CLEAR, reason="ok",
            provider=None, window_key=None,
        )
    severity, _kind, _tight, reason, pname, key, m, gamma = max(
        candidates, key=lambda c: (c[0], -c[1], c[2]),
    )
    return QuotaSeverityResult(
        severity=severity, reason=reason,
        provider=pname, window_key=key,
        burst_multiple=m, gamma=gamma,
    )


