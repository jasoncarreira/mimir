"""Tests for ``mimir.model_registry.detect_route`` — the bare-name
→ ``ModelRoute`` resolution that powers ``mimir setup --model``.

Each route's contract:
  * ``model_spec`` — the value written to ``MIMIR_MODEL_SPEC``
  * ``env`` — env-var overrides written to ``.env``
  * ``provider_name`` — human-facing label
  * ``billing_mode`` — ``"subscription"`` or ``"api"``; drives which
    usage monitor setup wires up
  * ``monitor_env`` — env vars setup writes to enable that monitor
  * ``monitor_label`` — operator-facing one-line description
"""
from __future__ import annotations

import pytest

from mimir.model_registry import (
    BILLING_API,
    BILLING_SUBSCRIPTION,
    DEFAULT_API_HOURLY_COST_LIMIT_USD,
    DEFAULT_MODEL_NAME,
    PROVIDER_ANTHROPIC_API,
    PROVIDER_ANTHROPIC_MAX,
    PROVIDER_MINIMAX,
    PROVIDER_MOONSHOT,
    PROVIDER_OPENAI,
    detect_route,
)


# ── default behavior (now direct Anthropic API, not claude-code) ─────


def test_detect_route_none_uses_default_anthropic_api():
    """No model arg → default to ``anthropic:<DEFAULT>``. Forward-
    looking default since Anthropic is sunsetting claude-code on
    subscription plans; the API path stays working regardless of
    Max plan availability."""
    route = detect_route(None)
    assert route.model_spec == f"anthropic:{DEFAULT_MODEL_NAME}"
    assert route.provider_name == PROVIDER_ANTHROPIC_API
    assert route.billing_mode == BILLING_API
    assert route.env == {}


def test_detect_route_empty_uses_default():
    route = detect_route("")
    assert route.model_spec == f"anthropic:{DEFAULT_MODEL_NAME}"
    assert route.provider_name == PROVIDER_ANTHROPIC_API


def test_detect_route_whitespace_uses_default():
    route = detect_route("   ")
    assert route.model_spec == f"anthropic:{DEFAULT_MODEL_NAME}"


# ── --subscription flag (provider-polymorphic) ─────────────────────────


def test_subscription_flag_routes_claude_to_claude_code():
    """Claude family + ``--subscription`` swaps the provider prefix —
    the protocol IS different (Max OAuth via claude CLI subprocess,
    not langchain-anthropic HTTP). Wires the Anthropic quota poller."""
    route = detect_route("claude-sonnet-4-6", subscription=True)
    assert route.model_spec == "claude-code:claude-sonnet-4-6"
    assert route.provider_name == PROVIDER_ANTHROPIC_MAX
    assert route.billing_mode == BILLING_SUBSCRIPTION


def test_default_off_for_claude():
    """Without ``--subscription``, Claude routes go through direct API."""
    route = detect_route("claude-haiku-4-5")
    assert route.model_spec == "anthropic:claude-haiku-4-5"
    assert route.provider_name == PROVIDER_ANTHROPIC_API


def test_subscription_keeps_model_spec_for_openai_minimax_moonshot():
    """For OpenAI / Minimax / Moonshot, subscription vs API tier is
    pure billing (same HTTP endpoint, just a different API token).
    ``--subscription`` keeps the same ``model_spec`` but flips the
    monitor."""
    # OpenAI
    api_route = detect_route("gpt-4.1-mini")
    sub_route = detect_route("gpt-4.1-mini", subscription=True)
    assert api_route.model_spec == sub_route.model_spec == "openai:gpt-4.1-mini"
    assert api_route.billing_mode == BILLING_API
    assert sub_route.billing_mode == BILLING_SUBSCRIPTION
    # Minimax — base URL stays the same; only billing mode flips.
    api_route = detect_route("MiniMax-M2.7")
    sub_route = detect_route("MiniMax-M2.7", subscription=True)
    assert api_route.model_spec == sub_route.model_spec == "anthropic:MiniMax-M2.7"
    assert api_route.env == sub_route.env  # base_url preserved
    assert sub_route.billing_mode == BILLING_SUBSCRIPTION
    # Moonshot
    api_route = detect_route("kimi-k2")
    sub_route = detect_route("kimi-k2", subscription=True)
    assert api_route.model_spec == sub_route.model_spec
    assert sub_route.billing_mode == BILLING_SUBSCRIPTION


def test_subscription_flips_monitor_for_non_claude_providers():
    """The actual operator-facing difference for OpenAI / Minimax /
    Moonshot when subscription is set: monitor switches from
    cost-tracking to quota-polling."""
    for name in ["gpt-4.1-mini", "MiniMax-M2.7", "kimi-k2-1.0"]:
        api_route = detect_route(name)
        sub_route = detect_route(name, subscription=True)
        assert "MIMIR_COST_HOURLY_LIMIT_USD" in api_route.monitor_env
        assert "MIMIR_QUOTA_POLL_ENABLED" in sub_route.monitor_env
        assert "subscription quota poller" in sub_route.monitor_label


# ── Minimax (API mode) ──────────────────────────────────────────────


@pytest.mark.parametrize("name", [
    "MiniMax-M2", "MiniMax-M2.5", "MiniMax-M2.7", "MiniMax-Text-01",
])
def test_minimax_models_route_via_anthropic_compat(name: str):
    route = detect_route(name)
    assert route.model_spec == f"anthropic:{name}"
    assert route.env == {
        "ANTHROPIC_BASE_URL": "https://api.minimax.io/anthropic",
    }
    assert route.provider_name == PROVIDER_MINIMAX
    assert route.billing_mode == BILLING_API
    # API mode → cost monitoring with the default ceiling.
    assert route.monitor_env == {
        "MIMIR_COST_HOURLY_LIMIT_USD": DEFAULT_API_HOURLY_COST_LIMIT_USD,
    }


@pytest.mark.parametrize("name", ["abab6.5-chat", "abab5.5-chat", "abab7"])
def test_abab_legacy_family_routes_to_minimax(name: str):
    route = detect_route(name)
    assert route.provider_name == PROVIDER_MINIMAX
    assert route.billing_mode == BILLING_API


# ── Moonshot Kimi (API mode) ────────────────────────────────────────


@pytest.mark.parametrize("name", [
    "kimi-k2-0905-preview", "kimi-k2-1.0", "moonshot-v1-128k",
])
def test_moonshot_models_route_via_anthropic_compat(name: str):
    route = detect_route(name)
    assert route.model_spec == f"anthropic:{name}"
    assert route.env == {
        "ANTHROPIC_BASE_URL": "https://api.moonshot.ai/anthropic",
    }
    assert route.provider_name == PROVIDER_MOONSHOT
    assert route.billing_mode == BILLING_API


# ── OpenAI (API mode) ───────────────────────────────────────────────


@pytest.mark.parametrize("name", [
    "gpt-4.1-mini", "gpt-5.4-nano", "gpt-4o",
    "o1-preview", "o3-mini", "o4-mini",
])
def test_openai_models_route_with_openai_prefix(name: str):
    route = detect_route(name)
    assert route.model_spec == f"openai:{name}"
    assert route.env == {}
    assert route.provider_name == PROVIDER_OPENAI
    assert route.billing_mode == BILLING_API


# ── Claude family / unknown ─────────────────────────────────────────


@pytest.mark.parametrize("name", [
    "claude-sonnet-4-6", "claude-opus-4-7", "claude-haiku-4-5",
    "claude-future-not-yet-released",
])
def test_claude_models_route_to_anthropic_api_by_default(name: str):
    route = detect_route(name)
    assert route.model_spec == f"anthropic:{name}"
    assert route.env == {}
    assert route.provider_name == PROVIDER_ANTHROPIC_API
    assert route.billing_mode == BILLING_API


def test_unknown_name_falls_back_to_anthropic_api_default():
    """Unknown names route to direct Anthropic API (forward-looking
    safe default — works for any Anthropic-shaped endpoint and the
    operator can override post-setup)."""
    route = detect_route("some-future-thing")
    assert route.model_spec == "anthropic:some-future-thing"
    assert route.provider_name == PROVIDER_ANTHROPIC_API


# ── case sensitivity ────────────────────────────────────────────────


def test_minimax_prefix_is_case_sensitive():
    """``MiniMax`` is the canonical capitalization; their API accepts
    only the canonical form. Wrong case falls through to default
    Anthropic API (will fail loudly at the provider rather than
    silently routing to the wrong place)."""
    assert detect_route("MiniMax-M2.7").provider_name == PROVIDER_MINIMAX
    typo = detect_route("minimax-m2.7")
    assert typo.provider_name == PROVIDER_ANTHROPIC_API


# ── usage-monitor contracts ─────────────────────────────────────────


def test_api_routes_get_default_cost_ceiling_monitor():
    """Every API-mode route writes the default ``MIMIR_COST_HOURLY_LIMIT_USD``
    so the per-turn cost tracker has a sane threshold to alert on."""
    for name in [
        "claude-haiku-4-5",       # anthropic API
        "MiniMax-M2.7",           # minimax via compat
        "kimi-k2-0905-preview",   # moonshot via compat
        "gpt-4.1-mini",           # openai
    ]:
        route = detect_route(name)
        assert route.billing_mode == BILLING_API
        assert route.monitor_env["MIMIR_COST_HOURLY_LIMIT_USD"] == \
            DEFAULT_API_HOURLY_COST_LIMIT_USD


def test_subscription_route_gets_quota_poller_monitor():
    """The claude-code (Max OAuth) route wires the quota poller env
    flag — the runtime registers the OAuth usage poller at boot."""
    route = detect_route("claude-sonnet-4-6", subscription=True)
    assert route.billing_mode == BILLING_SUBSCRIPTION
    assert route.monitor_env == {"MIMIR_QUOTA_POLL_ENABLED": "1"}


def test_monitor_label_present_for_every_route():
    """Setup prints ``monitor_label`` so the operator sees what was
    wired. Every route must populate it (never empty)."""
    for name in [
        None, "", "claude-sonnet-4-6", "MiniMax-M2.7", "kimi-k2",
        "gpt-4.1-mini", "some-unknown-model",
    ]:
        assert detect_route(name).monitor_label
    # And the --subscription path:
    assert detect_route("claude-sonnet-4-6", subscription=True).monitor_label


# ── pre-qualified specs (operator passes <provider>:<model>) ───────


def test_qualified_anthropic_spec_passes_through_unchanged():
    """Operator-typed ``anthropic:claude-opus-4-7`` is already
    qualified — don't double-prefix to ``anthropic:anthropic:...``."""
    route = detect_route("anthropic:claude-opus-4-7")
    assert route.model_spec == "anthropic:claude-opus-4-7"
    assert route.provider_name == PROVIDER_ANTHROPIC_API
    assert route.billing_mode == BILLING_API


def test_qualified_claude_code_spec_passes_through():
    """``claude-code:claude-sonnet-4-6`` qualified → subscription route."""
    route = detect_route("claude-code:claude-sonnet-4-6")
    assert route.model_spec == "claude-code:claude-sonnet-4-6"
    assert route.provider_name == PROVIDER_ANTHROPIC_MAX
    assert route.billing_mode == BILLING_SUBSCRIPTION


def test_qualified_openai_spec_passes_through():
    route = detect_route("openai:gpt-5-future")
    assert route.model_spec == "openai:gpt-5-future"
    assert route.provider_name == PROVIDER_OPENAI


def test_qualified_spec_ignores_max_oauth_flag():
    """Pre-qualified spec wins over ``--subscription`` — operator's
    explicit ``anthropic:`` qualification means "I want direct API";
    flag is ignored. Tested via the combo that previously double-
    prefixed to ``claude-code:anthropic:claude-opus-4-7``."""
    route = detect_route("anthropic:claude-opus-4-7", subscription=True)
    assert route.model_spec == "anthropic:claude-opus-4-7"
    assert route.provider_name == PROVIDER_ANTHROPIC_API


def test_bare_name_with_max_oauth_still_routes_to_claude_code():
    """The natural combo for an operator on Max plan: ``--model
    claude-opus-4-7 --subscription`` (bare name + flag). Should resolve
    cleanly to claude-code without double-prefixing."""
    route = detect_route("claude-opus-4-7", subscription=True)
    assert route.model_spec == "claude-code:claude-opus-4-7"
    assert route.provider_name == PROVIDER_ANTHROPIC_MAX


# ── general invariants ──────────────────────────────────────────────


def test_all_routes_produce_provider_prefixed_spec():
    """Every resolved spec must include a provider prefix
    (``<provider>:<model>``) — that's the format
    ``mimir.agent._resolve_model`` expects."""
    for name in ["MiniMax-M2", "kimi-k2", "gpt-4.1-mini",
                 "claude-haiku-4-5", "?"]:
        route = detect_route(name)
        assert ":" in route.model_spec
        provider, model = route.model_spec.split(":", 1)
        assert provider in {"claude-code", "anthropic", "openai"}
        assert model


def test_anthropic_routed_providers_set_base_url():
    """Minimax/Moonshot routes inject the right ``ANTHROPIC_BASE_URL``."""
    assert "ANTHROPIC_BASE_URL" in detect_route("MiniMax-M2.7").env
    assert "ANTHROPIC_BASE_URL" in detect_route("kimi-k2").env


def test_direct_provider_routes_dont_override_base_url():
    """anthropic-api / openai / claude-code use canonical endpoints —
    no base-url override needed."""
    assert detect_route("claude-sonnet-4-6").env == {}
    assert detect_route("gpt-4.1-mini").env == {}
    assert detect_route(
        "claude-sonnet-4-6", subscription=True,
    ).env == {}
