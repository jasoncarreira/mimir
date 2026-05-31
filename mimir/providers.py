"""Canonical LLM-provider registry (chainlink #292).

The provider taxonomy used to be re-derived in three places, each keying
off the same two signals — the ``MIMIR_MODEL_SPEC`` prefix and the
``ANTHROPIC_BASE_URL`` host:

* ``model_registry.detect_route`` — bare model name → routing config
  (which ``provider:`` prefix to use, which base URL to inject).
* ``billing.build_quota_providers`` — model spec + base URL → which
  ``QuotaProvider`` to poll.
* ``agent._build_chat_model`` — spec prefix → chat adapter + pip extra
  (migrates onto this registry in a follow-up slice).

The facts were duplicated (the Minimax endpoint lives in
``detect_route`` as a full base URL and in ``build_quota_providers`` as
the ``"minimax"`` host substring) and adding a provider meant editing
every consumer. This module holds the taxonomy **once** — a
:class:`ProviderSpec` per provider — and exposes the two resolution
directions the consumers need:

* :func:`provider_for_model_name` — forward: a bare model name (what an
  operator types for ``mimir setup --model``) → its provider. Used by
  ``detect_route``.
* :func:`provider_for_quota` — reverse: a resolved ``MIMIR_MODEL_SPEC``
  + ``ANTHROPIC_BASE_URL`` → the provider whose quota poller to register.
  Used by ``build_quota_providers``.

Adding a provider is now one :class:`ProviderSpec` entry. (Credential-
based discovery + the chat-adapter/extra and embedding axes fold in via
the later slices in chainlink #292.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

# Canonical provider labels. These are the values written to
# ``ModelRoute.provider_name`` (human-facing setup output + quota
# selection). ``model_registry`` re-exports them for back-compat.
PROVIDER_ANTHROPIC_MAX = "anthropic-max"
PROVIDER_ANTHROPIC_API = "anthropic-api"
PROVIDER_MINIMAX = "minimax"
PROVIDER_MOONSHOT = "moonshot"
PROVIDER_OPENAI = "openai"


@dataclass(frozen=True)
class ProviderSpec:
    """One LLM provider's facts, consulted by the routing + quota paths.

    Attributes:
        name: Canonical label (``PROVIDER_*`` above). Becomes
            ``ModelRoute.provider_name``.

        Forward match (bare model name → provider, used by
        ``detect_route``):
        name_prefixes: Bare-name prefixes matched case-INsensitively
            against the operator's ``--model`` value (e.g.
            ``("kimi", "moonshot")``).
        name_prefixes_exact: Bare-name prefixes matched case-SENSITIVELY
            (e.g. ``("MiniMax",)``). For providers whose API accepts only
            the canonical capitalization — a wrong-case typo falls through
            to the default and fails loudly at the provider rather than
            silently misrouting.
        is_default: True for the single fallback provider chosen when no
            ``name_prefixes`` match (the Claude family / unknown names).
        spec_prefix: The ``MIMIR_MODEL_SPEC`` provider prefix to build
            for a bare name in API mode (e.g. ``"anthropic"`` →
            ``anthropic:<model>``).
        base_url: ``ANTHROPIC_BASE_URL`` to inject for a bare-name match
            (the Anthropic-compat gateway endpoint). Empty = none.
        subscription_spec_prefix: When ``--subscription`` flips the *wire
            protocol* for a bare name (Claude family → ``claude-code``,
            OpenAI → ``codex-plus``). Empty = subscription is monitor-only
            (same spec, different billing).
        subscription_provider: ``provider_name`` after a subscription
            flip (Claude family → ``anthropic-max``; OpenAI stays
            ``openai``). Empty = unchanged.
        subscription_monitor_label: Setup-report label for the
            subscription/flip case. Empty = the generic
            "<provider> subscription quota poller" template.

        Reverse match (resolved spec + base URL → provider, used by
        ``build_quota_providers``):
        spec_prefixes: ``MIMIR_MODEL_SPEC`` prefixes this provider owns
            (``("openai", "codex-plus")`` for OpenAI). Non-``anthropic``
            prefixes here fully determine the provider; ``anthropic`` is
            intentionally NOT listed (it falls through to host matching,
            since ``anthropic:`` can route to several compat gateways).
        base_url_host_match: Substring identifying this provider from an
            ``ANTHROPIC_BASE_URL`` host (``"minimax"``). Empty = not
            host-identified.

        Quota:
        quota_provider_key: ``billing`` ``QuotaProvider`` key
            (``"anthropic"`` / ``"minimax"`` / ``"openai"``). Empty = no
            poller. Providers without their own poller (Moonshot today)
            map to ``"anthropic"`` — the same fallback the old
            ``build_quota_providers`` produced.
    """

    name: str
    name_prefixes: tuple[str, ...] = ()
    name_prefixes_exact: tuple[str, ...] = ()
    is_default: bool = False
    spec_prefix: str = "anthropic"
    base_url: str = ""
    subscription_spec_prefix: str = ""
    subscription_provider: str = ""
    subscription_monitor_label: str = ""
    spec_prefixes: tuple[str, ...] = ()
    base_url_host_match: str = ""
    quota_provider_key: str = ""


# ── The registry ───────────────────────────────────────────────────────
#
# Order matters only for forward matching: ``provider_for_model_name``
# returns the first entry whose ``name_prefixes`` match, so keep the
# specific providers ahead of the default. ``anthropic-api`` is the
# ``is_default`` fallback.

_MINIMAX = ProviderSpec(
    name=PROVIDER_MINIMAX,
    # ``MiniMax`` is case-sensitive (the API accepts only the canonical
    # capitalization, so a wrong-case typo should fall through to the
    # default and fail loudly rather than misroute). ``abab`` is the
    # legacy lowercase family.
    name_prefixes=("abab",),
    name_prefixes_exact=("MiniMax",),
    spec_prefix="anthropic",
    base_url="https://api.minimax.io/anthropic",
    base_url_host_match="minimax",
    quota_provider_key="minimax",
)
_MOONSHOT = ProviderSpec(
    name=PROVIDER_MOONSHOT,
    name_prefixes=("kimi", "moonshot"),
    spec_prefix="anthropic",
    base_url="https://api.moonshot.ai/anthropic",
    base_url_host_match="moonshot",
    # No Moonshot quota API wrapped yet — same fallback the old
    # build_quota_providers gave a Moonshot host (default → Anthropic).
    quota_provider_key="anthropic",
)
_OPENAI = ProviderSpec(
    name=PROVIDER_OPENAI,
    name_prefixes=("gpt-", "o1-", "o3-", "o4-"),
    spec_prefix="openai",
    subscription_spec_prefix="codex-plus",
    subscription_provider=PROVIDER_OPENAI,
    subscription_monitor_label=(
        "OpenAI Codex Plus quota (x-codex-* response headers; "
        "no separate poller — fed by ChatCodexPlus callback)"
    ),
    spec_prefixes=("openai", "codex-plus"),
    quota_provider_key="openai",
)
_ANTHROPIC_MAX = ProviderSpec(
    name=PROVIDER_ANTHROPIC_MAX,
    # Reached via an explicit ``claude-code:`` spec or the Claude-family
    # ``--subscription`` flip — never by a bare name of its own.
    spec_prefixes=("claude-code",),
    quota_provider_key="anthropic",
    subscription_monitor_label="Anthropic OAuth quota poller (5h + 7d windows)",
)
_ANTHROPIC_API = ProviderSpec(
    name=PROVIDER_ANTHROPIC_API,
    is_default=True,
    spec_prefix="anthropic",
    # Bare Claude-family name + ``--subscription`` flips to Max OAuth
    # (the protocol IS different — claude CLI subprocess, not HTTP).
    subscription_spec_prefix="claude-code",
    subscription_provider=PROVIDER_ANTHROPIC_MAX,
    subscription_monitor_label="Anthropic OAuth quota poller (5h + 7d windows)",
    spec_prefixes=("anthropic",),
    quota_provider_key="anthropic",
)

#: The registry, specific-first (the default last). Public for tests +
#: future credential-discovery (chainlink #292 PR3).
PROVIDERS: tuple[ProviderSpec, ...] = (
    _MINIMAX,
    _MOONSHOT,
    _OPENAI,
    _ANTHROPIC_MAX,
    _ANTHROPIC_API,
)


def _default_provider() -> ProviderSpec:
    """The ``is_default`` fallback provider (Anthropic direct API)."""
    for p in PROVIDERS:
        if p.is_default:
            return p
    # Unreachable while the table above keeps an is_default entry; guard
    # so a future table edit fails loudly rather than silently.
    raise RuntimeError("provider registry has no is_default provider")


def provider_for_model_name(model: str) -> ProviderSpec:
    """Forward: bare model name → its provider.

    Checks ``name_prefixes_exact`` (case-sensitive) then ``name_prefixes``
    (case-insensitive). A name matching neither — the Claude family and
    anything unknown — falls to the ``is_default`` provider. (``MiniMax``
    is case-sensitive on purpose: its API rejects other casings, so a
    wrong-case typo lands on the default and fails loudly rather than
    silently misrouting.) Callers handle the explicit ``provider:model``
    form (a colon in the name) separately.
    """
    name = (model or "").strip()
    name_lower = name.lower()
    for p in PROVIDERS:
        if any(name.startswith(prefix) for prefix in p.name_prefixes_exact):
            return p
        if any(name_lower.startswith(prefix) for prefix in p.name_prefixes):
            return p
    return _default_provider()


def _host_of(base_url: str) -> str:
    """Lowercase hostname of ``base_url`` (``""`` when empty/invalid)."""
    base = (base_url or "").strip()
    if not base:
        return ""
    try:
        return (urlparse(base).hostname or "").lower()
    except (ValueError, AttributeError):
        return ""


def provider_for_quota(model_spec: str, anthropic_base_url: str = "") -> ProviderSpec:
    """Reverse: a resolved ``MIMIR_MODEL_SPEC`` (+ ``ANTHROPIC_BASE_URL``)
    → the provider whose quota poller to register.

    Precedence mirrors the old ``build_quota_providers``:

    1. A non-``anthropic`` owned spec prefix fully determines the
       provider (``codex-plus:`` / ``openai:`` → OpenAI; ``claude-code:``
       → Anthropic Max).
    2. Otherwise (``anthropic:`` / bare / unknown) the
       ``ANTHROPIC_BASE_URL`` host disambiguates — a compat gateway host
       (``minimax``) selects that provider.
    3. Default: Anthropic direct API.
    """
    spec = (model_spec or "").strip().lower()
    for p in PROVIDERS:
        for prefix in p.spec_prefixes:
            if prefix != "anthropic" and spec.startswith(f"{prefix}:"):
                return p
    host = _host_of(anthropic_base_url)
    if host:
        for p in PROVIDERS:
            if p.base_url_host_match and p.base_url_host_match in host:
                return p
    return _default_provider()
