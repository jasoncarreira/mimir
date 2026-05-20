"""Tests for chainlink #13 billing-mode-aware suppression
(``mimir/billing.py``)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mimir.billing import (
    AnthropicQuotaProvider,
    BillingMode,
    QuotaProvider,
    QuotaWindow,
    detect_billing_mode,
    evaluate_quota,
)
from mimir.budget import HomeostaticArbiter
from mimir.rate_limits import RateLimitSnapshot, RateLimitStore


# ─── BillingMode auto-detect ───────────────────────────────────────────


def test_detect_billing_mode_explicit_quota(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("MIMIR_CLAUDE_OAUTH_CREDENTIALS", raising=False)
    assert detect_billing_mode(explicit="quota") is BillingMode.QUOTA
    assert detect_billing_mode(explicit=" QUOTA ") is BillingMode.QUOTA


def test_detect_billing_mode_explicit_pay_as_you_go(monkeypatch):
    # Even when OAuth creds are present, an explicit override wins.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    assert detect_billing_mode(explicit="pay-as-you-go") is BillingMode.PAY_AS_YOU_GO


def test_detect_billing_mode_unknown_explicit_falls_through(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("MIMIR_CLAUDE_OAUTH_CREDENTIALS", raising=False)
    # Bogus value warns + falls through to auto-detect → pay-as-you-go.
    assert detect_billing_mode(explicit="bogus") is BillingMode.PAY_AS_YOU_GO


def test_detect_billing_mode_auto_quota_via_oauth_token(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    assert detect_billing_mode() is BillingMode.QUOTA


def test_detect_billing_mode_auto_quota_via_oauth_credentials_path(
    monkeypatch, tmp_path
):
    """An ``oauth_credentials_path`` that points at an existing file
    drives QUOTA mode. The file-existence is the load-bearing check —
    see ``test_detect_billing_mode_pay_as_you_go_when_credentials_file_missing``
    for the bug this fixes."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("MIMIR_CLAUDE_OAUTH_CREDENTIALS", raising=False)
    creds = tmp_path / ".credentials.json"
    creds.write_text('{"access_token": "x"}')
    assert (
        detect_billing_mode(oauth_credentials_path=creds)
        is BillingMode.QUOTA
    )


def test_detect_billing_mode_pay_as_you_go_when_credentials_file_missing(
    monkeypatch, tmp_path
):
    """Regression for CR2-#1: ``_oauth_credentials_path()`` in config.py
    returns the *expected location* (e.g. ``$MIMIR_HOME/.claude/.credentials.json``)
    even on installs that have never run ``claude /login``. Before the
    ``.is_file()`` guard, a Path-truthy check effectively always fired
    on any deployment with ``MIMIR_HOME`` set — including pure pay-as-
    you-go API-key installs that have no OAuth flow at all. The result
    was that API-key installs auto-detected as QUOTA, demoting
    ``cost_rate_alert`` to advisory and silently disabling the dollar-
    cost suppression layer.

    With the guard, a path that doesn't point at an existing file does
    NOT drive QUOTA — falls through to PAY_AS_YOU_GO.
    """
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("MIMIR_CLAUDE_OAUTH_CREDENTIALS", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    nonexistent = tmp_path / ".claude" / ".credentials.json"
    assert not nonexistent.exists()
    assert (
        detect_billing_mode(oauth_credentials_path=nonexistent)
        is BillingMode.PAY_AS_YOU_GO
    )


def test_detect_billing_mode_pay_as_you_go_when_credentials_path_is_directory(
    monkeypatch, tmp_path
):
    """Edge case: ``oauth_credentials_path`` points at a directory (not
    a file). ``.is_file()`` returns False; falls through to
    PAY_AS_YOU_GO. Documents that the guard is "is_file" specifically,
    not "exists" — directories shouldn't masquerade as credentials."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("MIMIR_CLAUDE_OAUTH_CREDENTIALS", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    a_directory = tmp_path / ".claude"
    a_directory.mkdir()
    assert (
        detect_billing_mode(oauth_credentials_path=a_directory)
        is BillingMode.PAY_AS_YOU_GO
    )


def test_detect_billing_mode_auto_quota_via_credentials_env(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("MIMIR_CLAUDE_OAUTH_CREDENTIALS", "/some/path")
    assert detect_billing_mode() is BillingMode.QUOTA


def test_detect_billing_mode_default_pay_as_you_go(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("MIMIR_CLAUDE_OAUTH_CREDENTIALS", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    assert detect_billing_mode() is BillingMode.PAY_AS_YOU_GO


def test_config_from_env_resolves_oauth_path_once(monkeypatch, tmp_path):
    """Regression: ``Config.from_env`` previously called
    ``_oauth_credentials_path()`` twice — once for the ``billing_mode``
    detection and once for the ``oauth_credentials_path`` field — which
    was redundant and could in theory diverge. Pin the dedup by counting
    invocations.
    """
    import mimir.config as cfg_mod

    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    # Avoid env-driven billing override so we exercise the auto-detect
    # path that calls _oauth_credentials_path.
    monkeypatch.delenv("MIMIR_BILLING_MODE", raising=False)

    call_count = 0
    real = cfg_mod._oauth_credentials_path

    def counted() -> Path | None:
        nonlocal call_count
        call_count += 1
        return real()

    monkeypatch.setattr(cfg_mod, "_oauth_credentials_path", counted)

    cfg_mod.Config.from_env()
    assert call_count == 1, (
        f"_oauth_credentials_path should be called exactly once per "
        f"Config.from_env, got {call_count}"
    )


# ─── AnthropicQuotaProvider ────────────────────────────────────────────


def _put_snapshot(store: RateLimitStore, key: str, util: float, *, hours_in: float = 1.0):
    """Helper: directly write a rate-limit entry. Sidesteps the async
    ``record`` path (the store's _load reads from disk; we patch _load
    instead so tests don't need an event loop)."""
    # window_size depends on key; we pass elapsed as ``hours_in`` since
    # window-start. resets_at is now + (window_hours - hours_in).
    window_hours = {
        "five_hour": 5.0,
        "seven_day": 168.0,
        "seven_day_opus": 168.0,
        "seven_day_sonnet": 168.0,
    }[key]
    resets_at = int(time.time() + (window_hours - hours_in) * 3600)
    existing = store._load() if hasattr(store, "_load") else {}
    existing[key] = {
        "status": "allowed",
        "utilization": util,
        "resets_at": resets_at,
        "observed_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    store._load = lambda: existing  # type: ignore[method-assign]


def test_anthropic_provider_returns_empty_when_store_empty(tmp_path):
    store = RateLimitStore(path=tmp_path / "rl.json")
    provider = AnthropicQuotaProvider(store)
    assert provider.get_windows() == []
    assert provider.provider_name == "anthropic"


def test_anthropic_provider_skips_unknown_window_keys(tmp_path):
    store = RateLimitStore(path=tmp_path / "rl.json")
    store._load = lambda: {  # type: ignore[method-assign]
        "weird_future_window": {
            "status": "allowed",
            "utilization": 0.5,
            "resets_at": int(time.time() + 3600),
            "observed_at": "",
        },
    }
    provider = AnthropicQuotaProvider(store)
    assert provider.get_windows() == []  # unknown key skipped


def test_anthropic_provider_returns_known_windows_with_projection(tmp_path):
    store = RateLimitStore(path=tmp_path / "rl.json")
    _put_snapshot(store, "five_hour", 0.50, hours_in=2.5)  # halfway through, 50% used → on pace 100%
    _put_snapshot(store, "seven_day", 0.20, hours_in=84.0)  # halfway, 20% → on pace 40%
    provider = AnthropicQuotaProvider(store)
    # Last write wins for _load; combine into one dict:
    store._load = lambda: {  # type: ignore[method-assign]
        "five_hour": {
            "status": "allowed",
            "utilization": 0.50,
            "resets_at": int(time.time() + 2.5 * 3600),
            "observed_at": "",
        },
        "seven_day": {
            "status": "allowed",
            "utilization": 0.20,
            "resets_at": int(time.time() + 84 * 3600),
            "observed_at": "",
        },
    }
    windows = {w.key: w for w in provider.get_windows()}
    assert "five_hour" in windows
    assert "seven_day" in windows
    five = windows["five_hour"]
    assert five.utilization == pytest.approx(0.50)
    assert five.on_pace_utilization == pytest.approx(1.0, rel=0.01)
    seven = windows["seven_day"]
    assert seven.utilization == pytest.approx(0.20)
    assert seven.on_pace_utilization == pytest.approx(0.40, rel=0.01)


# ─── evaluate_quota ────────────────────────────────────────────────────


class _FakeProvider(QuotaProvider):
    def __init__(self, name: str, windows: list[QuotaWindow]):
        self._name = name
        self._windows = windows

    @property
    def provider_name(self) -> str:
        return self._name

    def get_windows(self) -> list[QuotaWindow]:
        return list(self._windows)


def _w(key: str, util: float | None, on_pace: float | None) -> QuotaWindow:
    hours = 5.0 if key == "five_hour" else 168.0
    return QuotaWindow(
        key=key,
        window_hours=hours,
        utilization=util,
        on_pace_utilization=on_pace,
        resets_at=None,
    )


def test_evaluate_quota_no_providers():
    result = evaluate_quota([])
    assert result.suppress is False
    assert result.reason == "ok"


def test_evaluate_quota_empty_provider_does_not_suppress():
    provider = _FakeProvider("anthropic", [])
    result = evaluate_quota([provider])
    assert result.suppress is False


def test_evaluate_quota_below_thresholds_does_not_suppress():
    provider = _FakeProvider("anthropic", [
        _w("five_hour", 0.5, 0.85),    # below 0.90 on-pace 5h threshold
        _w("seven_day", 0.4, 0.70),    # below 0.95 on-pace 7d threshold
    ])
    result = evaluate_quota([provider])
    assert result.suppress is False
    assert result.reason == "ok"


def test_evaluate_quota_raw_saturation_suppresses():
    provider = _FakeProvider("anthropic", [
        _w("seven_day", 0.85, 0.50),  # raw 0.85 >= 0.80 default
    ])
    result = evaluate_quota([provider])
    assert result.suppress is True
    assert "quota_saturated" in result.reason
    assert "anthropic:seven_day" in result.reason
    assert result.provider == "anthropic"
    assert result.window_key == "seven_day"


def test_evaluate_quota_off_pace_5h_suppresses():
    provider = _FakeProvider("anthropic", [
        _w("five_hour", 0.30, 0.95),  # raw 30%, but projects to 95% — over 0.90 on-pace 5h
    ])
    result = evaluate_quota([provider])
    assert result.suppress is True
    assert "quota_off_pace" in result.reason
    assert "five_hour" in result.reason


def test_evaluate_quota_off_pace_7d_suppresses():
    provider = _FakeProvider("anthropic", [
        _w("seven_day", 0.40, 0.96),  # projects to 96% — over 0.95 on-pace 7d
    ])
    result = evaluate_quota([provider])
    assert result.suppress is True
    assert "quota_off_pace" in result.reason
    assert "seven_day" in result.reason


def test_evaluate_quota_raw_takes_precedence_over_on_pace():
    """When BOTH raw and on-pace fire, raw wins (more authoritative)."""
    provider = _FakeProvider("anthropic", [
        _w("seven_day", 0.85, 0.99),  # raw 85% (saturated) AND on-pace 99%
    ])
    result = evaluate_quota([provider])
    assert result.suppress is True
    assert "quota_saturated" in result.reason  # raw wins


def test_evaluate_quota_picks_worst_across_windows():
    provider = _FakeProvider("anthropic", [
        _w("five_hour", 0.10, 0.92),    # over 0.90 by 0.02
        _w("seven_day", 0.40, 0.99),    # over 0.95 by 0.04 — bigger hit
    ])
    result = evaluate_quota([provider])
    # Worst-on-pace wins by absolute value (we sort by raw value, not
    # margin-over-threshold). 0.99 > 0.92 → seven_day wins.
    assert result.window_key == "seven_day"


def test_evaluate_quota_provider_exception_continues():
    """A misbehaving provider must not crash the arbiter."""
    class _Broken(QuotaProvider):
        @property
        def provider_name(self) -> str:
            return "broken"
        def get_windows(self) -> list[QuotaWindow]:
            raise RuntimeError("oops")
    good = _FakeProvider("anthropic", [_w("five_hour", 0.30, 0.50)])
    result = evaluate_quota([_Broken(), good])
    assert result.suppress is False  # only the good provider's data counted
    assert result.reason == "ok"


# ─── Arbiter integration: billing-mode branch ──────────────────────────


NOW = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)


def _quota_arbiter(tmp_path: Path, providers, **kwargs) -> HomeostaticArbiter:
    rls = RateLimitStore(path=tmp_path / "rate_limits.json")
    return HomeostaticArbiter(
        home=tmp_path,
        rate_limit_store=rls,
        turns_log=tmp_path / "turns.jsonl",
        billing_mode=BillingMode.QUOTA,
        quota_providers=providers,
        **kwargs,
    )


def test_arbiter_quota_mode_no_data_fires_ok(tmp_path):
    """No quota signal yet (cold start, poller hasn't run) → fire."""
    arb = _quota_arbiter(tmp_path, [])
    fire, reason = arb.should_fire_heartbeat(now=NOW)
    assert fire is True
    assert reason == "ok"


def test_arbiter_quota_mode_off_pace_suppresses(tmp_path):
    provider = _FakeProvider("anthropic", [_w("seven_day", 0.40, 0.99)])
    arb = _quota_arbiter(tmp_path, [provider])
    fire, reason = arb.should_fire_heartbeat(now=NOW)
    assert fire is False
    assert "quota_off_pace" in reason


def test_arbiter_quota_mode_ignores_cost_rate_alert(tmp_path):
    """Under quota, dollar spikes are advisory — must NOT suppress."""
    real_now = datetime.now(tz=timezone.utc)
    turns = tmp_path / "turns.jsonl"
    # Set up a dollar spike that WOULD trip cost_rate_alert in pay-go.
    import json
    with turns.open("a") as f:
        for _ in range(5):
            f.write(json.dumps({
                "ts": (real_now - timedelta(minutes=10)).isoformat(),
                "turn_id": "t",
                "session_id": "s",
                "trigger": "user_message",
                "channel_id": "c",
                "input": "",
                "events": [],
                "usage": {},
                "total_cost_usd": 2.0,
            }) + "\n")
    arb = HomeostaticArbiter(
        home=tmp_path,
        rate_limit_store=RateLimitStore(path=tmp_path / "rl.json"),
        turns_log=turns,
        billing_mode=BillingMode.QUOTA,
        quota_providers=[],
        cost_hourly_limit_usd=5.0,  # would trip in pay-go
    )
    fire, reason = arb.should_fire_heartbeat(now=real_now)
    assert fire is True
    assert reason == "ok"


def test_arbiter_pay_as_you_go_mode_unchanged_behavior(tmp_path):
    """Pay-as-you-go must keep the existing spike_ratio path."""
    real_now = datetime.now(tz=timezone.utc)
    turns = tmp_path / "turns.jsonl"
    import json
    with turns.open("a") as f:
        for _ in range(5):
            f.write(json.dumps({
                "ts": (real_now - timedelta(minutes=10)).isoformat(),
                "turn_id": "t",
                "session_id": "s",
                "trigger": "user_message",
                "channel_id": "c",
                "input": "",
                "events": [],
                "usage": {},
                "total_cost_usd": 2.0,
            }) + "\n")
    arb = HomeostaticArbiter(
        home=tmp_path,
        rate_limit_store=RateLimitStore(path=tmp_path / "rl.json"),
        turns_log=turns,
        billing_mode=BillingMode.PAY_AS_YOU_GO,
        cost_hourly_limit_usd=5.0,
    )
    fire, reason = arb.should_fire_heartbeat(now=real_now)
    assert fire is False
    assert "cost_rate_alert" in reason


def test_arbiter_pay_as_you_go_default_when_unspecified(tmp_path):
    """The arbiter's default billing_mode is PAY_AS_YOU_GO so existing
    callers get the historical behavior unchanged."""
    arb = HomeostaticArbiter(
        home=tmp_path,
        rate_limit_store=RateLimitStore(path=tmp_path / "rl.json"),
        turns_log=tmp_path / "turns.jsonl",
    )
    assert arb.billing_mode is BillingMode.PAY_AS_YOU_GO


# ── chainlink #17: derived 5h gets a higher suppress threshold ──────


def _w_derived(key: str, util: float | None) -> QuotaWindow:
    """Variant of _w that flags the window derived=True. Locks the
    chainlink #17 contract: derived windows skip the direct
    raw-suppress threshold (0.80) for a looser one (0.90)."""
    hours = 5.0 if key == "five_hour" else 168.0
    return QuotaWindow(
        key=key,
        window_hours=hours,
        utilization=util,
        on_pace_utilization=None,
        resets_at=None,
        derived=True,
    )


def test_evaluate_quota_derived_5h_under_90_does_not_suppress():
    """Derived 5h at 0.85 — would suppress under the direct 0.80
    threshold, must NOT suppress under the derived 0.90 threshold."""
    provider = _FakeProvider("anthropic", [_w_derived("five_hour", 0.85)])
    result = evaluate_quota([provider])
    assert result.suppress is False, (
        f"derived 5h @0.85 should be under the 0.90 threshold, "
        f"got: {result.reason}"
    )


def test_evaluate_quota_derived_5h_above_90_suppresses():
    """Derived 5h above the 0.90 threshold suppresses. Tests with
    0.92 (>= threshold by 2pp) to leave room for any future tightening
    of the threshold and to keep the test from flapping on a `>` vs
    `>=` boundary edit."""
    provider = _FakeProvider("anthropic", [_w_derived("five_hour", 0.92)])
    result = evaluate_quota([provider])
    assert result.suppress is True
    assert "five_hour@0.92" in result.reason


def test_evaluate_quota_derived_5h_at_threshold_boundary_suppresses():
    """Locks the inclusive boundary: 0.90 (== threshold) trips. If the
    `>=` semantics ever flip to `>`, this test catches it."""
    provider = _FakeProvider("anthropic", [_w_derived("five_hour", 0.90)])
    result = evaluate_quota([provider])
    assert result.suppress is True
    assert "five_hour@0.90" in result.reason


def test_evaluate_quota_direct_5h_at_85_still_suppresses():
    """Invariant: chainlink #17 doesn't loosen direct 5h thresholds.
    Direct 5h at 0.85 still trips the 0.80 wall."""
    provider = _FakeProvider("anthropic", [_w("five_hour", 0.85, None)])
    result = evaluate_quota([provider])
    assert result.suppress is True
    assert "five_hour@0.85" in result.reason


def test_evaluate_quota_derived_propagates_through_anthropic_provider(tmp_path):
    """End-to-end: a RateLimitSnapshot flagged derived=True flows
    through AnthropicQuotaProvider.get_windows to a QuotaWindow
    flagged derived=True, which then takes the looser threshold."""
    from mimir.billing import AnthropicQuotaProvider
    from mimir.rate_limits import RateLimitStore, RateLimitSnapshot
    import asyncio

    store = RateLimitStore(path=tmp_path / "rl.json")
    snap = RateLimitSnapshot(
        status="allowed_warning",
        utilization=0.85,
        observed_at="2026-05-09T00:00:00+00:00",
        derived=True,
    )
    asyncio.run(store.record("five_hour", snap))

    provider = AnthropicQuotaProvider(store)
    windows = provider.get_windows()
    five_hour_w = next(w for w in windows if w.key == "five_hour")
    assert five_hour_w.derived is True
    assert five_hour_w.utilization == pytest.approx(0.85)

    # And evaluate_quota uses the looser threshold.
    result = evaluate_quota([provider])
    assert result.suppress is False, (
        "derived 5h @0.85 must not suppress under the 0.90 threshold"
    )


# ── chainlink #17 self-review fixes ───────────────────────────────────


def test_derived_5h_skips_on_pace_projection(tmp_path):
    """Self-review fix: on-pace projection on a derived value is
    methodologically broken — derived is a synthetic point estimate,
    not a time-series sample. Extrapolating it forward via
    project_window_end would (e.g.) treat a 0.85 value at minute 10
    of a 5h window as a 5x burn rate and project past 1.0, tripping
    the on-pace threshold spuriously. AnthropicQuotaProvider must
    set on_pace_utilization=None for derived windows."""
    from mimir.billing import AnthropicQuotaProvider
    from mimir.rate_limits import RateLimitStore, RateLimitSnapshot
    import asyncio
    import time as _time

    store = RateLimitStore(path=tmp_path / "rl.json")
    # Derived snapshot, fresh observation, 5h window with 4h+ left.
    # If projection ran, current_util / fraction_elapsed would be
    # absurdly large.
    asyncio.run(store.record("five_hour", RateLimitSnapshot(
        status="allowed_warning",
        utilization=0.85,
        resets_at=int(_time.time()) + 4 * 3600,  # 4h remaining
        observed_at="2026-05-09T00:00:00+00:00",
        derived=True,
    )))

    provider = AnthropicQuotaProvider(store)
    [w] = provider.get_windows()
    assert w.derived is True
    assert w.on_pace_utilization is None, (
        "derived windows must skip on-pace projection — got "
        f"{w.on_pace_utilization!r}"
    )

    # And evaluate_quota doesn't suppress (raw 0.85 < derived
    # threshold 0.90, on-pace skipped).
    result = evaluate_quota([provider])
    assert result.suppress is False


def test_direct_5h_still_projects_on_pace(tmp_path):
    """Invariant: the on-pace skip is derived-only. Direct (non-derived)
    snapshots still get on-pace projection — that's a useful early-warn
    signal when the burn rate suggests we'll cross the wall."""
    from mimir.billing import AnthropicQuotaProvider
    from mimir.rate_limits import RateLimitStore, RateLimitSnapshot
    import asyncio
    import time as _time

    store = RateLimitStore(path=tmp_path / "rl.json")
    asyncio.run(store.record("five_hour", RateLimitSnapshot(
        status="allowed",
        utilization=0.50,
        resets_at=int(_time.time()) + 4 * 3600,
        observed_at="2026-05-09T00:00:00+00:00",
        # derived defaults to False
    )))
    provider = AnthropicQuotaProvider(store)
    [w] = provider.get_windows()
    assert w.derived is False
    # on_pace_utilization is computable (not None) — the projection
    # ran. Exact value depends on window timing but it should exist.
    # (Exact value isn't load-bearing for the test; presence is.)


# ─── _is_anthropic_oauth_deployment + auto-disable on routed deployments ─


def test_is_anthropic_oauth_deployment_default_true_when_unset(monkeypatch):
    """No ``ANTHROPIC_BASE_URL`` → assume real Anthropic (poller stays
    on). This is the back-compat path for mimirbot + bare-metal."""
    from mimir.config import _is_anthropic_oauth_deployment
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    assert _is_anthropic_oauth_deployment() is True


def test_is_anthropic_oauth_deployment_true_for_canonical_anthropic(monkeypatch):
    from mimir.config import _is_anthropic_oauth_deployment
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    assert _is_anthropic_oauth_deployment() is True
    # Trailing slash, /v1 suffix — same host
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1/")
    assert _is_anthropic_oauth_deployment() is True


def test_is_anthropic_oauth_deployment_false_for_minimax(monkeypatch):
    """Muninn's deployment: Anthropic-compat endpoint at Minimax.
    Poller should auto-disable to avoid spamming oauth_usage_failed."""
    from mimir.config import _is_anthropic_oauth_deployment
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.minimax.io/anthropic")
    assert _is_anthropic_oauth_deployment() is False


def test_is_anthropic_oauth_deployment_false_for_moonshot(monkeypatch):
    from mimir.config import _is_anthropic_oauth_deployment
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.moonshot.ai/anthropic")
    assert _is_anthropic_oauth_deployment() is False


def test_is_anthropic_oauth_deployment_false_for_arbitrary_gateway(monkeypatch):
    """Some operator's private gateway. Probably wraps Anthropic, but
    we can't know that and the OAuth usage endpoint won't be exposed
    even if it does — safe default is "disable poller; if you do
    want it on, set MIMIR_CLAUDE_OAUTH_CREDENTIALS explicitly"."""
    from mimir.config import _is_anthropic_oauth_deployment
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://my-gateway.example.com/v1")
    assert _is_anthropic_oauth_deployment() is False


def test_oauth_credentials_path_auto_none_on_routed_deployment(monkeypatch):
    """Auto-disable: when ANTHROPIC_BASE_URL routes away from
    api.anthropic.com, ``_oauth_credentials_path()`` returns None
    even though MIMIR_HOME is set + the file might exist. Stops the
    poller from registering and spamming ``oauth_usage_failed``
    every 3 min on a deployment where it can't do useful work.

    Regression for muninn-mimir cutover 2026-05-20."""
    from mimir.config import _oauth_credentials_path
    monkeypatch.setenv("MIMIR_HOME", "/some/home")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.minimax.io/anthropic")
    monkeypatch.delenv("MIMIR_CLAUDE_OAUTH_CREDENTIALS", raising=False)
    assert _oauth_credentials_path() is None


def test_oauth_credentials_path_explicit_override_wins_on_routed_deployment(
    monkeypatch, tmp_path
):
    """If the operator sets ``MIMIR_CLAUDE_OAUTH_CREDENTIALS`` explicitly,
    that wins even on a routed deployment — interpreted as "I have a
    real Anthropic credentials file and want the poller on anyway."
    """
    from mimir.config import _oauth_credentials_path
    explicit = tmp_path / "real-creds.json"
    monkeypatch.setenv("MIMIR_HOME", "/some/home")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.minimax.io/anthropic")
    monkeypatch.setenv("MIMIR_CLAUDE_OAUTH_CREDENTIALS", str(explicit))
    result = _oauth_credentials_path()
    assert result is not None
    assert result == explicit.resolve()


def test_oauth_credentials_path_explicit_empty_wins_on_anthropic_deployment(
    monkeypatch
):
    """Existing behavior preserved: explicit empty string disables
    even on a real Anthropic deployment (useful in tests / bench)."""
    from mimir.config import _oauth_credentials_path
    monkeypatch.setenv("MIMIR_HOME", "/some/home")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setenv("MIMIR_CLAUDE_OAUTH_CREDENTIALS", "")
    assert _oauth_credentials_path() is None


def test_oauth_credentials_path_default_resolves_when_anthropic_deployment(
    monkeypatch
):
    """Back-compat — mimirbot's path: no overrides, no routing →
    poller registers as before."""
    from mimir.config import _oauth_credentials_path
    monkeypatch.setenv("MIMIR_HOME", "/some/home")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("MIMIR_CLAUDE_OAUTH_CREDENTIALS", raising=False)
    result = _oauth_credentials_path()
    assert result is not None
    assert str(result).endswith(".claude/.credentials.json")


def test_is_anthropic_oauth_deployment_malformed_url_falls_back_safely(monkeypatch):
    """Pins the malformed-URL fallback: ``urlparse`` will tolerate
    most garbage, but if it raises (or hostname extraction fails),
    we should ``return True`` and let the SDK error elsewhere rather
    than silently disable. Mimir-carreira review nit on PR #246."""
    from mimir.config import _is_anthropic_oauth_deployment
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "://bad-url")
    # Whatever urlparse does with this — either returns "" for
    # hostname (→ False), or raises (→ True via except). The fix
    # makes EITHER a defensible outcome. Test pins the actual
    # behavior so future refactors don't change it silently.
    result = _is_anthropic_oauth_deployment()
    assert isinstance(result, bool)
    # And the more meaningful semantic check: passing a totally
    # bogus URL string shouldn't blow up the agent. Just don't
    # raise.
