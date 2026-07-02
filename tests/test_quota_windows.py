"""Tests for the per-provider quota-window registry (chainlink #298).

This is the single source both ``mimir.billing`` (the QuotaProvider
pollers) and ``mimir.rate_limits`` (the agent's Resource-usage view)
read from, so the key tests guard that (a) the store-key derivation is
correct and (b) the billing providers don't drift from it.
"""
from __future__ import annotations

from mimir import quota_windows as qw


def test_store_key_applies_prefix():
    assert qw.ANTHROPIC.store_key("five_hour") == "five_hour"  # empty prefix
    assert qw.MINIMAX.store_key("five_hour") == "minimax_five_hour"
    assert qw.CODEX_PLUS.store_key("five_hour") == "openai_five_hour"


def test_window_hours_omits_open_ended_windows():
    # Anthropic's overage is open-ended (hours=None) → excluded.
    assert qw.ANTHROPIC.window_hours() == {
        "five_hour": 5.0, "seven_day": 168.0,
        "seven_day_opus": 168.0, "seven_day_sonnet": 168.0,
    }
    assert "overage" not in qw.ANTHROPIC.window_hours()
    assert qw.MINIMAX.window_hours() == {"five_hour": 5.0, "seven_day": 168.0}
    assert qw.CODEX_PLUS.window_hours() == {"five_hour": 5.0, "seven_day": 168.0}


def test_store_label_map_covers_all_providers():
    labels = qw.store_label_map()
    assert labels["five_hour"] == "Claude Code Max 5-hour"
    assert labels["minimax_five_hour"] == "Minimax 5-hour"
    assert labels["openai_seven_day"] == "Codex Plus 7-day"
    assert labels["overage"] == "Claude Code Max overage / pay-as-you-go"


def test_store_window_hours_omits_overage():
    wh = qw.store_window_hours()
    assert "overage" not in wh
    for k in ("five_hour", "minimax_five_hour", "openai_five_hour"):
        assert wh[k] == 5.0


def test_store_keys_unique_across_providers():
    keys = qw.store_key_order()
    assert len(keys) == len(set(keys)), f"duplicate store keys: {keys}"


def test_billing_providers_derive_from_registry():
    """The billing QuotaProviders must read window-hours + prefixes from
    THIS registry — no separate literals to drift. chainlink #298."""
    from mimir import billing

    assert billing._ANTHROPIC_WINDOW_HOURS == qw.ANTHROPIC.window_hours()
    assert billing._MINIMAX_WINDOW_HOURS == qw.MINIMAX.window_hours()
    assert billing._OPENAI_WINDOW_HOURS == qw.CODEX_PLUS.window_hours()
    # Store-key prefixes the pollers read under must match the registry's.
    assert billing.MinimaxQuotaProvider._store_key_prefix == qw.MINIMAX.store_key_prefix
    assert billing.OpenAIQuotaProvider._store_key_prefix == qw.CODEX_PLUS.store_key_prefix
    assert billing.AnthropicQuotaProvider._store_key_prefix == qw.ANTHROPIC.store_key_prefix


def test_rate_limits_maps_derive_from_registry():
    """The agent view's label + window-hours maps come from the registry,
    so every provider's windows render with proper labels + off-pace."""
    from mimir import rate_limits

    assert rate_limits._LABEL == qw.store_label_map()
    assert rate_limits._WINDOW_HOURS == qw.store_window_hours()
