"""Subscription quota-window definitions, organized by provider.

Single source of truth for the per-provider rate-limit windows mimir
tracks. Each subscription type (Anthropic Max OAuth, Minimax, OpenAI
Codex Plus) declares its windows once here — the logical key, the
agent-facing label, and the window length (for off-pace projection) —
and the store-key prefix its poller/callback writes under.

Two consumers read this, and previously DUPLICATED it (chainlink #298):

- ``mimir.billing`` — the ``_StorageBackedQuotaProvider`` subclasses
  used local ``_*_WINDOW_HOURS`` dicts + a hard-coded ``_store_key_prefix``
  to poll/project quota for the cost-suppression arbiter.
- ``mimir.rate_limits`` — ``render_plan_quota_lines`` / ``off_pace_buckets``
  used a central ``_LABEL`` / ``_WINDOW_HOURS`` / ``order`` that only
  enumerated the Anthropic keys, so Minimax (``minimax_*``) and Codex
  Plus (``openai_*``) windows rendered with raw fallback labels and got
  NO off-pace burn-rate projection.

Now both derive from the per-provider tables below, so adding a window
(or a whole provider) is a one-place edit and the agent's Resource-usage
view stays in lockstep with what the pollers actually write.

The store key a snapshot lives under is ``<store_key_prefix><key>``
(e.g. ``minimax_`` + ``five_hour`` → ``minimax_five_hour``; Anthropic's
prefix is empty, so its keys are bare ``five_hour`` / ``seven_day`` …).

Pure-data module — no I/O, no imports of ``billing`` / ``rate_limits``
(keeps the dependency graph acyclic: both of those import THIS).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QuotaWindowSpec:
    """One subscription quota window's static definition.

    ``hours`` is the window length used to project end-of-window
    utilization from the current burn rate. ``None`` means open-ended /
    no projection (e.g. Anthropic's pay-as-you-go ``overage`` bolt-on),
    which excludes it from ``_WINDOW_HOURS` and off-pace buckets.
    """

    key: str  # logical key; the store key is store_key_prefix + key
    label: str  # agent-facing label in the Resource-usage block
    hours: float | None  # window length in hours; None = no projection


@dataclass(frozen=True)
class ProviderQuotaWindows:
    """A subscription provider's quota windows + the store-key prefix
    its poller/callback writes them under."""

    provider: str  # quota-provider key (matches billing._QUOTA_PROVIDER_BUILDERS)
    store_key_prefix: str
    windows: tuple[QuotaWindowSpec, ...]

    def store_key(self, logical_key: str) -> str:
        return f"{self.store_key_prefix}{logical_key}"

    def window_hours(self) -> dict[str, float]:
        """``{logical_key: hours}`` for the projecting windows (omits the
        open-ended ones). This is exactly the shape billing's
        ``_StorageBackedQuotaProvider._window_hours`` expects."""
        return {w.key: w.hours for w in self.windows if w.hours is not None}


_HOUR = 1.0
_WEEK_HOURS = 24.0 * 7

# Anthropic Max OAuth (store keys are un-prefixed). Populated by
# ``mimir/oauth_usage_poller.py`` under Max OAuth and the SDK rate-limit
# capture path under direct API keys. The per-model 7d sub-windows are
# Anthropic-specific; ``overage`` is the open-ended pay-as-you-go bolt-on.
ANTHROPIC = ProviderQuotaWindows(
    provider="anthropic",
    store_key_prefix="",
    windows=(
        QuotaWindowSpec("five_hour", "5-hour rolling", 5.0),
        QuotaWindowSpec("seven_day", "7-day plan-wide", _WEEK_HOURS),
        QuotaWindowSpec("seven_day_opus", "7-day Opus", _WEEK_HOURS),
        QuotaWindowSpec("seven_day_sonnet", "7-day Sonnet", _WEEK_HOURS),
        QuotaWindowSpec("overage", "Overage / pay-as-you-go", None),
    ),
)

# Minimax subscription. Populated by ``mimir/minimax_usage_poller.py``,
# which writes ``minimax_five_hour`` / ``minimax_seven_day``.
MINIMAX = ProviderQuotaWindows(
    provider="minimax",
    store_key_prefix="minimax_",
    windows=(
        QuotaWindowSpec("five_hour", "Minimax 5-hour", 5.0),
        QuotaWindowSpec("seven_day", "Minimax 7-day", _WEEK_HOURS),
    ),
)

# OpenAI Codex Plus subscription. Populated by
# ``mimir.billing.make_codex_plus_rate_limit_callback`` (x-codex-*
# response headers), which writes ``openai_five_hour`` / ``openai_seven_day``.
CODEX_PLUS = ProviderQuotaWindows(
    provider="openai",
    store_key_prefix="openai_",
    windows=(
        QuotaWindowSpec("five_hour", "Codex Plus 5-hour", 5.0),
        QuotaWindowSpec("seven_day", "Codex Plus 7-day", _WEEK_HOURS),
    ),
)

#: All providers, in render order (Anthropic first — the default — then
#: the vendor-prefixed ones). Per deployment only one provider's keys are
#: actually present in the store, so cross-provider ordering is cosmetic.
ALL_PROVIDERS: tuple[ProviderQuotaWindows, ...] = (ANTHROPIC, MINIMAX, CODEX_PLUS)


def store_label_map() -> dict[str, str]:
    """Full store key → agent-facing label, across all providers."""
    return {
        p.store_key(w.key): w.label
        for p in ALL_PROVIDERS
        for w in p.windows
    }


def store_window_hours() -> dict[str, float]:
    """Full store key → window hours, omitting open-ended windows."""
    return {
        p.store_key(w.key): w.hours
        for p in ALL_PROVIDERS
        for w in p.windows
        if w.hours is not None
    }


def store_key_order() -> tuple[str, ...]:
    """Full store keys in render order (provider order, windows as declared)."""
    return tuple(
        p.store_key(w.key) for p in ALL_PROVIDERS for w in p.windows
    )


def provider_store_keys(provider: str) -> tuple[str, ...]:
    """Store keys owned by ONE quota provider, e.g. ``"openai"`` →
    ``("openai_five_hour", "openai_seven_day")``; ``"anthropic"`` → the
    bare ``five_hour`` / ``seven_day`` / per-model / ``overage`` keys.
    Empty tuple for an unknown provider.

    Used to filter the Resource-usage view to the ACTIVE provider's keys
    so a deployment that switched providers (e.g. the Codex cutover)
    doesn't render stale keys a now-disabled poller left behind in the
    store (chainlink #301)."""
    for p in ALL_PROVIDERS:
        if p.provider == provider:
            return tuple(p.store_key(w.key) for w in p.windows)
    return ()
