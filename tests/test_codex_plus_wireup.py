"""Tests for the Codex Plus end-to-end wire-up.

Covers the four glue points that connect ``langchain-codex-plus`` to
mimir's existing quota infrastructure:

* ``RateLimitStore.record_sync`` — sync writer used by the chat
  model's rate-limit callback (sync because we don't want to assume
  whether the callback fires on the event loop or a thread executor).
* ``make_codex_plus_rate_limit_callback`` — transcribes a
  :class:`langchain_codex_plus.CodexRateLimits` snapshot into the
  ``openai_five_hour`` / ``openai_seven_day`` keys
  :class:`OpenAIQuotaProvider` reads.
* ``mimir.agent._resolve_model`` — recognizes ``codex-plus:`` specs
  and lazy-imports :class:`langchain_codex_plus.ChatCodexPlus`.
* ``mimir.model_registry.detect_route`` — routes ``--subscription``
  on a gpt-shaped model to ``codex-plus:`` (different protocol),
  keeps the existing ``openai:`` route for pay-per-token API.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mimir.billing import (
    BillingMode,
    OpenAIQuotaProvider,
    build_quota_providers,
    make_codex_plus_rate_limit_callback,
)
from mimir.model_registry import detect_route
from mimir.rate_limits import RateLimitSnapshot, RateLimitStore


# ─── RateLimitStore.record_sync ────────────────────────────────────────


def test_record_sync_writes_snapshot_to_disk(tmp_path: Path):
    """The sync path produces the same on-disk JSON shape as the
    async ``record`` method — ``OpenAIQuotaProvider.current()`` can
    read either."""
    store = RateLimitStore(path=tmp_path / "rate_limits.json")
    snap = RateLimitSnapshot(
        status="allowed",
        utilization=0.42,
        resets_at=int(time.time() + 3600),
        observed_at="2026-05-21T03:00:00+00:00",
    )
    store.record_sync("openai_five_hour", snap)
    loaded = store.current()
    assert "openai_five_hour" in loaded
    assert loaded["openai_five_hour"].utilization == pytest.approx(0.42)
    assert loaded["openai_five_hour"].status == "allowed"


def test_record_sync_overwrites_existing_key(tmp_path: Path):
    """Last-write-wins: subsequent calls replace the existing entry."""
    store = RateLimitStore(path=tmp_path / "rate_limits.json")
    now = int(time.time())
    s1 = RateLimitSnapshot(
        status="allowed", utilization=0.10, resets_at=now + 3600,
        observed_at="t1",
    )
    s2 = RateLimitSnapshot(
        status="allowed", utilization=0.85, resets_at=now + 3600,
        observed_at="t2",
    )
    store.record_sync("openai_five_hour", s1)
    store.record_sync("openai_five_hour", s2)
    loaded = store.current()
    assert loaded["openai_five_hour"].utilization == pytest.approx(0.85)
    assert loaded["openai_five_hour"].observed_at == "t2"


def test_record_sync_preserves_other_keys(tmp_path: Path):
    """Writing one key doesn't blow away unrelated entries (e.g., the
    Anthropic snapshots that the OAuth poller wrote earlier)."""
    store = RateLimitStore(path=tmp_path / "rate_limits.json")
    now = int(time.time())
    ant = RateLimitSnapshot(
        status="allowed", utilization=0.5, resets_at=now + 3600,
        observed_at="t",
    )
    oai = RateLimitSnapshot(
        status="allowed", utilization=0.1, resets_at=now + 3600,
        observed_at="t",
    )
    store.record_sync("five_hour", ant)
    store.record_sync("openai_five_hour", oai)
    loaded = store.current()
    assert "five_hour" in loaded
    assert "openai_five_hour" in loaded
    assert loaded["five_hour"].utilization == pytest.approx(0.5)


# ─── make_codex_plus_rate_limit_callback ──────────────────────────────


class _FakeQuotaWindow:
    """Stand-in for ``langchain_codex_plus.CodexQuotaWindow`` — avoids
    pulling in the real package for unit tests of the bridge."""

    def __init__(
        self,
        used_percent: float,
        window_minutes: int | None = None,
        reset_at: int | None = None,
    ) -> None:
        self.used_percent = used_percent
        self.window_minutes = window_minutes
        self.reset_at = reset_at
        self.reset_after_seconds = None


class _FakeRateLimits:
    def __init__(
        self,
        primary: _FakeQuotaWindow | None = None,
        secondary: _FakeQuotaWindow | None = None,
    ) -> None:
        self.primary = primary
        self.secondary = secondary
        self.plan_type = "plus"
        self.active_limit = "premium"
        self.credits = None
        self.primary_over_secondary_limit_percent = None


def test_callback_writes_primary_to_openai_five_hour(tmp_path: Path):
    """Mapping: ``primary`` (5h window in Codex's vocabulary) →
    ``openai_five_hour`` (the key OpenAIQuotaProvider reads).
    Percent (0-100) → utilization (0-1)."""
    store = RateLimitStore(path=tmp_path / "rl.json")
    callback = make_codex_plus_rate_limit_callback(store)
    callback(_FakeRateLimits(
        primary=_FakeQuotaWindow(
            used_percent=1.0, window_minutes=300,
            reset_at=int(time.time() + 5 * 3600),
        ),
    ))
    loaded = store.current()
    assert "openai_five_hour" in loaded
    assert loaded["openai_five_hour"].utilization == pytest.approx(0.01)
    # No secondary in the input → no secondary written.
    assert "openai_seven_day" not in loaded


def test_callback_writes_both_windows_when_present(tmp_path: Path):
    """Both windows present → both keys written. Percent-to-fraction
    conversion checked on both."""
    store = RateLimitStore(path=tmp_path / "rl.json")
    callback = make_codex_plus_rate_limit_callback(store)
    # Use timestamps in the future — ``RateLimitStore.current()``
    # filters entries whose ``resets_at`` is in the past as stale.
    # (Pinning absolute unix-ts here was a latent bug that surfaced
    # ~5h after a fixed value goes past now().)
    primary_reset = int(time.time()) + 5 * 3600        # 5h window
    secondary_reset = int(time.time()) + 7 * 24 * 3600  # 7d window
    callback(_FakeRateLimits(
        primary=_FakeQuotaWindow(used_percent=25.0, reset_at=primary_reset),
        secondary=_FakeQuotaWindow(used_percent=8.0, reset_at=secondary_reset),
    ))
    loaded = store.current()
    assert loaded["openai_five_hour"].utilization == pytest.approx(0.25)
    assert loaded["openai_five_hour"].resets_at == primary_reset
    assert loaded["openai_seven_day"].utilization == pytest.approx(0.08)
    assert loaded["openai_seven_day"].resets_at == secondary_reset


def test_callback_skips_when_used_percent_missing(tmp_path: Path):
    """A window with ``used_percent=None`` (gateway omitted the
    header) — skip rather than write a misleading utilization=0."""
    store = RateLimitStore(path=tmp_path / "rl.json")
    callback = make_codex_plus_rate_limit_callback(store)
    callback(_FakeRateLimits(
        primary=_FakeQuotaWindow(used_percent=None),
    ))
    assert store.current() == {}


def test_callback_observed_at_is_iso_utc(tmp_path: Path):
    """The observed_at field gets stamped with the current UTC time
    in ISO format — used for staleness checks elsewhere."""
    store = RateLimitStore(path=tmp_path / "rl.json")
    callback = make_codex_plus_rate_limit_callback(store)
    before = datetime.now(tz=timezone.utc)
    callback(_FakeRateLimits(
        primary=_FakeQuotaWindow(used_percent=5.0, reset_at=int(time.time() + 3600)),
    ))
    after = datetime.now(tz=timezone.utc)
    obs = datetime.fromisoformat(
        store.current()["openai_five_hour"].observed_at
    )
    assert before <= obs <= after


def test_callback_tolerates_none_input(tmp_path: Path):
    """``ChatCodexPlus`` fires the callback only when headers parse
    cleanly; defensively accept None anyway."""
    store = RateLimitStore(path=tmp_path / "rl.json")
    callback = make_codex_plus_rate_limit_callback(store)
    callback(None)
    assert store.current() == {}


# ─── End-to-end read: OpenAIQuotaProvider sees what the callback wrote ─


def test_openai_quota_provider_reads_callback_writes(tmp_path: Path):
    """The full loop: callback writes → provider reads → arbiter sees
    a populated QuotaWindow. This is the load-bearing integration
    point (PR #248 stub now has a writer feeding it)."""
    store = RateLimitStore(path=tmp_path / "rl.json")
    callback = make_codex_plus_rate_limit_callback(store)
    callback(_FakeRateLimits(
        primary=_FakeQuotaWindow(
            used_percent=15.0, window_minutes=300,
            reset_at=int(time.time() + 5 * 3600),
        ),
        secondary=_FakeQuotaWindow(
            used_percent=3.0, window_minutes=10080,
            reset_at=int(time.time() + 7 * 24 * 3600),
        ),
    ))
    provider = OpenAIQuotaProvider(store)
    windows = provider.get_windows()
    keys = {w.key for w in windows}
    assert keys == {"five_hour", "seven_day"}
    five = next(w for w in windows if w.key == "five_hour")
    assert five.utilization == pytest.approx(0.15)
    seven = next(w for w in windows if w.key == "seven_day")
    assert seven.utilization == pytest.approx(0.03)


# ─── build_quota_providers picks OpenAIQuotaProvider for codex-plus ────


def test_discovery_registers_openai_provider_for_codex_plus_spec(
    tmp_path: Path,
):
    """A ``codex-plus:gpt-5.4`` spec registers :class:`OpenAIQuotaProvider`
    — the chat model's rate-limit callback feeds the same
    ``openai_five_hour`` / ``openai_seven_day`` keys this provider
    reads, so the arbiter sees a coherent quota window for the
    deployment."""
    from mimir.billing import OpenAIQuotaProvider

    store = RateLimitStore(path=tmp_path / "rl.json")
    providers = build_quota_providers(
        store=store,
        billing_mode=BillingMode.QUOTA,
        model_spec="codex-plus:gpt-5.4",
    )
    assert len(providers) == 1
    assert isinstance(providers[0], OpenAIQuotaProvider)


def test_discovery_codex_plus_overrides_anthropic_base_url(tmp_path: Path):
    """``codex-plus:`` is a model_spec prefix branch, so it takes
    precedence over a stale ``ANTHROPIC_BASE_URL`` env (e.g., from a
    previous run that routed to Minimax). The chat model isn't
    actually talking to ``ANTHROPIC_BASE_URL`` for codex-plus specs;
    quota dispatch must follow."""
    from mimir.billing import OpenAIQuotaProvider

    store = RateLimitStore(path=tmp_path / "rl.json")
    providers = build_quota_providers(
        store=store,
        billing_mode=BillingMode.QUOTA,
        model_spec="codex-plus:gpt-5.4",
        anthropic_base_url="https://api.minimax.io/anthropic",
    )
    assert isinstance(providers[0], OpenAIQuotaProvider)


# ─── _resolve_model: codex-plus: spec lazy-imports + constructs ────────


def test_resolve_model_codex_plus_builds_chat_codex_plus():
    from langchain_codex_plus import ChatCodexPlus

    from mimir.agent import _resolve_model

    model = _resolve_model("codex-plus:gpt-5.4")
    assert isinstance(model, ChatCodexPlus)
    assert model.model == "gpt-5.4"
    assert model.reasoning_effort == "none"
    # No callback was passed → field defaults to None.
    assert model.rate_limit_callback is None


def test_resolve_model_codex_plus_propagates_callback():
    """When the agent passes a ``rate_limit_callback``, it's wired
    onto the ChatCodexPlus instance."""
    from mimir.agent import _resolve_model

    def _seen(_rl):
        pass

    model = _resolve_model(
        "codex-plus:gpt-5.4",
        rate_limit_callback=_seen,
    )
    assert model.rate_limit_callback is _seen


# ─── model_registry: --subscription on gpt-* routes to codex-plus: ─────


def test_detect_route_openai_api_default():
    """No ``--subscription`` → ``openai:`` (langchain-openai
    ChatOpenAI), API monitor."""
    route = detect_route("gpt-5.4", subscription=False)
    assert route.model_spec == "openai:gpt-5.4"
    assert route.billing_mode == "api"


def test_detect_route_openai_subscription_routes_to_codex_plus():
    """``--subscription`` on a gpt-* model → ``codex-plus:`` spec.
    Different wire protocol (chatgpt.com/backend-api), so the
    provider has to actually differ — not just a monitor flip."""
    route = detect_route("gpt-5.4", subscription=True)
    assert route.model_spec == "codex-plus:gpt-5.4"
    assert route.billing_mode == "subscription"
    assert "Codex Plus" in route.monitor_label


def test_detect_route_o3_subscription_also_codex_plus():
    """Same routing applies to o3/o4 model families — they're served
    on Codex Plus subscriptions too."""
    route = detect_route("o3-pro", subscription=True)
    assert route.model_spec == "codex-plus:o3-pro"
    assert route.billing_mode == "subscription"


def test_detect_route_prequalified_codex_plus_passes_through():
    """A spec that's already qualified with ``codex-plus:`` should
    pass through unchanged (the operator has made the protocol
    choice explicit)."""
    route = detect_route("codex-plus:gpt-5.4")
    assert route.model_spec == "codex-plus:gpt-5.4"
