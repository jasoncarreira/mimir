"""Model-name → routing-config resolution.

Operators say:

    mimir setup --home ~/muninn --model MiniMax-M2.7

instead of:

    mimir setup --home ~/muninn
    # then dig through README, learn that you need to set
    # MIMIR_MODEL_SPEC=anthropic:MiniMax-M2.7 AND
    # ANTHROPIC_BASE_URL=https://api.minimax.io/anthropic in .env

This module owns the bare-name → ``ModelRoute`` mapping that powers
the ``--model`` flag. The detection is prefix-based — cheap,
deterministic, easy to extend. Unknown names default to direct
Anthropic API (the durable path — Anthropic is sunsetting claude-code
on subscription plans, and the API path works regardless of plan
tier).

Add a new provider:

  1. Add the prefix → ``ModelRoute`` mapping in ``detect_route``.
  2. Pick the right ``MIMIR_MODEL_SPEC`` provider prefix
     (``claude-code:``, ``anthropic:``, ``openai:``).
  3. Document the ``env`` overrides the provider needs
     (``ANTHROPIC_BASE_URL`` for routed Anthropic-compat
     endpoints, etc.).
  4. Add a row to ``tests/test_model_registry.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


#: Stable short labels for human-facing setup output + quota-poller
#: selection logic. Keep in sync with ``QuotaProvider.provider_name``
#: in ``mimir/billing.py``.
PROVIDER_ANTHROPIC_MAX = "anthropic-max"
PROVIDER_ANTHROPIC_API = "anthropic-api"
PROVIDER_MINIMAX = "minimax"
PROVIDER_MOONSHOT = "moonshot"
PROVIDER_OPENAI = "openai"

#: Billing modes drive what ``--quota`` actually enables:
#:
#: * ``subscription`` — fixed-period plan with quota windows (Anthropic
#:   Max OAuth, the future OpenAI Codex / Minimax-sub paths). ``--quota``
#:   writes ``MIMIR_QUOTA_POLL_ENABLED=1`` so the runtime registers the
#:   provider's usage poller (Anthropic OAuth one ships; Minimax-sub
#:   poller is in flight in #243).
#: * ``api`` — pay-per-token. ``--quota`` writes a default
#:   ``MIMIR_COST_HOURLY_LIMIT_USD`` so the per-turn cost tracker has
#:   a threshold to alert on. (Spike-ratio check is on by default
#:   regardless — see ``mimir/cost_tracking.py``.)
BILLING_SUBSCRIPTION = "subscription"
BILLING_API = "api"

#: Default API-mode cost ceiling written by ``--quota``. $5/hr is a
#: rough "watch your wallet" floor for most production workloads;
#: operators tune via ``MIMIR_COST_HOURLY_LIMIT_USD`` post-setup.
DEFAULT_API_HOURLY_COST_LIMIT_USD = "5.0"


@dataclass(frozen=True)
class ModelRoute:
    """Resolved routing config for a model name.

    Attributes:
        model_spec: The string operators set as ``MIMIR_MODEL_SPEC``,
            with the provider prefix included
            (e.g., ``"anthropic:MiniMax-M2.7"``).
        env: Additional env vars the deployment needs (e.g.,
            ``ANTHROPIC_BASE_URL`` for routed Anthropic-compat
            endpoints). Setup merges these into the generated ``.env``.
        provider_name: Short label for human-facing messages.
        billing_mode: ``"subscription"`` (quota windows) or ``"api"``
            (pay-per-token). Drives which monitor the ``--quota`` flag
            enables.
        monitor_env: Env vars to set when ``--quota`` is passed. Picked
            per billing mode — quota-poller-enabled for subscription,
            default cost ceiling for API.
        monitor_label: One-line human-readable status of what
            ``--quota`` enabled, printed by the setup report.
    """

    model_spec: str
    env: dict[str, str] = field(default_factory=dict)
    provider_name: str = PROVIDER_ANTHROPIC_API
    billing_mode: str = BILLING_API
    monitor_env: dict[str, str] = field(default_factory=dict)
    monitor_label: str = ""


#: Default model when ``mimir setup`` runs without ``--model``.
#: Resolves to ``anthropic:claude-sonnet-4-6`` (direct Anthropic API)
#: by default; ``mimir setup --subscription`` flips Claude family
#: routes to ``claude-code:`` (legacy Max OAuth subprocess).
DEFAULT_MODEL_NAME = "claude-sonnet-4-6"


def detect_route(
    model: str | None, *, subscription: bool = False,
) -> ModelRoute:
    """Resolve a bare model name to its canonical routing config.

    ``model`` is what the operator typed verbatim. ``None`` / empty
    falls back to ``DEFAULT_MODEL_NAME``. Detection is prefix-based —
    unknown names route to direct Anthropic API (the safest forward-
    looking default since Anthropic is sunsetting claude-code on
    subscription plans — the API path stays working regardless of
    Max-plan availability).

    ``subscription`` (operator-passed via ``mimir setup
    --subscription``) tells setup the operator's billing is a fixed
    subscription rather than pay-per-token. The flag's effect is
    provider-polymorphic:

    * **Claude family** → swaps to ``claude-code:`` provider (the
      protocol IS different — Max OAuth via the claude CLI
      subprocess, not langchain-anthropic HTTP). Wires the
      Anthropic OAuth usage poller.
    * **OpenAI / Minimax / Moonshot / etc.** → same ``model_spec``
      (same HTTP endpoint; only the API token's tier differs).
      Wires the quota poller env flag for whenever the per-provider
      subscription poller lands.

    Without the flag, every route is API mode → cost-monitor with
    a default ``$/hr`` ceiling.
    """
    name = (model or "").strip() or DEFAULT_MODEL_NAME
    name_lower = name.lower()

    # API-mode monitor: enable per-turn cost tracking with a sane
    # default ceiling so unexpected runaway burn alerts. Spike-ratio
    # check is on by default regardless (see ``mimir/cost_tracking.py``).
    api_monitor_env = {
        "MIMIR_COST_HOURLY_LIMIT_USD": DEFAULT_API_HOURLY_COST_LIMIT_USD,
    }
    api_monitor_label = (
        f"cost monitoring (alert at "
        f"${DEFAULT_API_HOURLY_COST_LIMIT_USD}/hr; tune via "
        f"MIMIR_COST_HOURLY_LIMIT_USD)"
    )
    # Subscription-mode monitor: register the provider's quota usage
    # poller at server boot.
    sub_monitor_env = {"MIMIR_QUOTA_POLL_ENABLED": "1"}

    # ── Pre-qualified spec: operator passed ``<provider>:<model>``
    # directly. Pass through unchanged — don't double-prefix, don't
    # auto-inject ``ANTHROPIC_BASE_URL`` (we can't infer the gateway
    # from the prefix alone). For Claude family, the explicit prefix
    # WINS over ``--subscription`` (operator has chosen the protocol
    # explicitly); for other providers, ``--subscription`` still
    # toggles the monitor.
    if ":" in name:
        prefix, _, _ = name.partition(":")
        prefix_lower = prefix.lower()
        if prefix_lower == "claude-code":
            return ModelRoute(
                model_spec=name,
                provider_name=PROVIDER_ANTHROPIC_MAX,
                billing_mode=BILLING_SUBSCRIPTION,
                monitor_env=sub_monitor_env,
                monitor_label="Anthropic OAuth quota poller (5h + 7d windows)",
            )
        if prefix_lower == "openai":
            return _api_or_sub_route(
                model_spec=name,
                provider_name=PROVIDER_OPENAI,
                subscription=subscription,
                api_monitor_env=api_monitor_env,
                api_monitor_label=api_monitor_label,
                sub_monitor_env=sub_monitor_env,
            )
        # Default fallback for ``anthropic:`` and any other prefix:
        # API-mode unless the operator opted into subscription. The
        # explicit ``anthropic:`` prefix has already routed AWAY from
        # claude-code, so subscription here means "this is an
        # anthropic-compat endpoint with subscription-tier billing"
        # (e.g., Minimax sub via api.minimax.io/anthropic) — same
        # protocol, different monitor.
        return _api_or_sub_route(
            model_spec=name,
            provider_name=PROVIDER_ANTHROPIC_API,
            subscription=subscription,
            api_monitor_env=api_monitor_env,
            api_monitor_label=api_monitor_label,
            sub_monitor_env=sub_monitor_env,
        )

    # ── Minimax (MiniMax-M2/M2.5/M2.7/M2.x, MiniMax-Text-01, abab*)
    # Anthropic-compat endpoint at api.minimax.io/anthropic — same
    # protocol whether you're on subscription or pay-per-token; only
    # the API token's tier differs (per operator clarification
    # 2026-05-20).
    if name.startswith("MiniMax") or name_lower.startswith("abab"):
        return _api_or_sub_route(
            model_spec=f"anthropic:{name}",
            env={"ANTHROPIC_BASE_URL": "https://api.minimax.io/anthropic"},
            provider_name=PROVIDER_MINIMAX,
            subscription=subscription,
            api_monitor_env=api_monitor_env,
            api_monitor_label=api_monitor_label,
            sub_monitor_env=sub_monitor_env,
        )

    # ── Moonshot Kimi (kimi-k2-*, kimi-*) ───────────────────────────
    # Same Anthropic-compat pattern as Minimax.
    if name_lower.startswith("kimi") or name_lower.startswith("moonshot"):
        return _api_or_sub_route(
            model_spec=f"anthropic:{name}",
            env={"ANTHROPIC_BASE_URL": "https://api.moonshot.ai/anthropic"},
            provider_name=PROVIDER_MOONSHOT,
            subscription=subscription,
            api_monitor_env=api_monitor_env,
            api_monitor_label=api_monitor_label,
            sub_monitor_env=sub_monitor_env,
        )

    # ── OpenAI (gpt-*, o1-*, o3-*, o4-*) ────────────────────────────
    # Direct OpenAI API. Same endpoint whether on a Codex subscription
    # or pay-per-token — ``--subscription`` only toggles the monitor.
    if (
        name_lower.startswith("gpt-")
        or name_lower.startswith("o1-")
        or name_lower.startswith("o3-")
        or name_lower.startswith("o4-")
    ):
        return _api_or_sub_route(
            model_spec=f"openai:{name}",
            provider_name=PROVIDER_OPENAI,
            subscription=subscription,
            api_monitor_env=api_monitor_env,
            api_monitor_label=api_monitor_label,
            sub_monitor_env=sub_monitor_env,
        )

    # ── Claude family + unknown (default route) ─────────────────────
    # ``--subscription`` for Claude family flips to ``claude-code:``
    # provider — the protocol IS different here (Max OAuth via the
    # claude CLI subprocess, not langchain-anthropic HTTP). Wires the
    # Anthropic OAuth usage poller. Without the flag, direct API is
    # the default — the durable path (Anthropic is sunsetting
    # claude-code subscriptions; the API path works regardless).
    if subscription:
        return ModelRoute(
            model_spec=f"claude-code:{name}",
            env={},
            provider_name=PROVIDER_ANTHROPIC_MAX,
            billing_mode=BILLING_SUBSCRIPTION,
            monitor_env=sub_monitor_env,
            monitor_label="Anthropic OAuth quota poller (5h + 7d windows)",
        )
    return ModelRoute(
        model_spec=f"anthropic:{name}",
        env={},
        provider_name=PROVIDER_ANTHROPIC_API,
        billing_mode=BILLING_API,
        monitor_env=api_monitor_env,
        monitor_label=api_monitor_label,
    )


def _api_or_sub_route(
    *,
    model_spec: str,
    provider_name: str,
    subscription: bool,
    api_monitor_env: dict[str, str],
    api_monitor_label: str,
    sub_monitor_env: dict[str, str],
    env: dict[str, str] | None = None,
) -> ModelRoute:
    """Build a route for providers where subscription vs API tier is
    pure billing — same HTTP endpoint, just a different API token.
    Only the monitor flips. Used for OpenAI, Minimax, Moonshot,
    and any direct-Anthropic-API operator opting into subscription
    monitoring.

    Note: the subscription-side quota POLLER for OpenAI / Minimax /
    Moonshot is provider-side TODO (Issue #243 covers Minimax;
    OpenAI / Moonshot subscription quota APIs not yet wrapped). The
    flag's effect today is just to write ``MIMIR_QUOTA_POLL_ENABLED=1``
    so the runtime picks up the right poller when it lands.
    """
    if subscription:
        return ModelRoute(
            model_spec=model_spec,
            env=dict(env or {}),
            provider_name=provider_name,
            billing_mode=BILLING_SUBSCRIPTION,
            monitor_env=sub_monitor_env,
            monitor_label=(
                f"{provider_name} subscription quota poller "
                f"(env flag set; provider-side poller pending)"
            ),
        )
    return ModelRoute(
        model_spec=model_spec,
        env=dict(env or {}),
        provider_name=provider_name,
        billing_mode=BILLING_API,
        monitor_env=api_monitor_env,
        monitor_label=api_monitor_label,
    )
