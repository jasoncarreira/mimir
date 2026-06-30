"""Persistent store + render for Anthropic plan-window rate limits.

The SDK emits RateLimitEvent on transitions; we persist per-type and
render until the window resets. These tests stub the SDK shape rather
than depending on the SDK directly so they're cheap."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from mimir.rate_limits import (
    RateLimitSnapshot,
    RateLimitStore,
    render_plan_quota_lines,
    snapshot_from_sdk_event,
)


@dataclass
class _FakeRateLimitInfo:
    """Mimics the SDK's RateLimitInfo shape — only the fields
    snapshot_from_sdk_event reads."""

    status: str = "allowed"
    utilization: float | None = None
    resets_at: int | None = None
    overage_status: str | None = None
    overage_resets_at: int | None = None
    overage_disabled_reason: str | None = None


# ---- snapshot_from_sdk_event -------------------------------------------


def test_snapshot_copies_known_fields():
    info = _FakeRateLimitInfo(
        status="allowed_warning",
        utilization=0.85,
        resets_at=int(time.time()) + 3600,
        overage_status="allowed",
        overage_resets_at=int(time.time()) + 24 * 3600,
    )
    snap = snapshot_from_sdk_event(info)
    assert snap.status == "allowed_warning"
    assert snap.utilization == 0.85
    assert snap.resets_at == info.resets_at
    assert snap.overage_status == "allowed"
    assert snap.observed_at  # stamped


def test_snapshot_tolerates_missing_optional_fields():
    """The SDK's older RateLimitInfo may not carry all fields. The
    converter should treat them as None rather than crashing."""

    class _Sparse:
        status = "allowed"

    snap = snapshot_from_sdk_event(_Sparse())
    assert snap.status == "allowed"
    assert snap.utilization is None
    assert snap.resets_at is None


# ---- snapshot_from_response_bucket (per-response shape) ----------------


def test_response_bucket_with_utilization_fraction():
    from mimir.rate_limits import snapshot_from_response_bucket

    snap = snapshot_from_response_bucket({
        "status": "allowed_warning",
        "utilization": 0.83,
        "resets_at": 1714512345,
    })
    assert snap.status == "allowed_warning"
    assert snap.utilization == 0.83
    assert snap.resets_at == 1714512345


def test_response_bucket_with_used_percentage():
    """Statusline JSON shape uses ``used_percentage`` (0-100). The
    bucket translator accepts either form so capture is robust across
    CLI versions."""
    from mimir.rate_limits import snapshot_from_response_bucket

    snap = snapshot_from_response_bucket({
        "used_percentage": 42,
        "resets_at": 1714512345,
    })
    assert snap.utilization == 0.42


def test_response_bucket_with_camel_case_resets():
    from mimir.rate_limits import snapshot_from_response_bucket

    snap = snapshot_from_response_bucket({
        "utilization": 0.10,
        "resetsAt": 1714512345,
    })
    assert snap.resets_at == 1714512345


def test_response_bucket_minimal_fields():
    """A bucket with only ``utilization`` should still convert."""
    from mimir.rate_limits import snapshot_from_response_bucket

    snap = snapshot_from_response_bucket({"utilization": 0.55})
    assert snap.utilization == 0.55
    assert snap.status == "allowed"  # default
    assert snap.resets_at is None


# ---- snapshot_from_api_usage_bucket (Stage 5: get_context_usage) -------


def test_api_usage_bucket_with_fraction_utilization():
    from mimir.rate_limits import snapshot_from_api_usage_bucket

    snap = snapshot_from_api_usage_bucket({
        "status": "allowed",
        "utilization": 0.42,
        "resets_at": 9_999_999_999,
    })
    assert snap is not None
    assert snap.status == "allowed"
    assert snap.utilization == 0.42
    assert snap.resets_at == 9_999_999_999


def test_api_usage_bucket_rescales_percentage():
    """apiUsage may report 0-100 instead of 0-1. The parser detects
    via ``v > 1.0`` and divides by 100."""
    from mimir.rate_limits import snapshot_from_api_usage_bucket

    snap = snapshot_from_api_usage_bucket({"utilization": 75})
    assert snap is not None
    assert snap.utilization == 0.75


def test_api_usage_bucket_rescales_exact_one_percent_field():
    """Percent-named fields use 0-100 semantics even at boundary values.

    An exact 1% reading must normalize to 0.01, not saturate the plan
    window as 1.0.
    """
    from mimir.rate_limits import snapshot_from_api_usage_bucket

    snap = snapshot_from_api_usage_bucket({"usage_pct": 1})
    assert snap is not None
    assert snap.utilization == 0.01


def test_api_usage_bucket_iso_resets_at_string():
    """``resets_at`` may arrive as an ISO timestamp string; parser
    converts to unix seconds."""
    from mimir.rate_limits import snapshot_from_api_usage_bucket

    snap = snapshot_from_api_usage_bucket({
        "utilization": 0.5,
        "resets_at": "2026-05-05T12:00:00Z",
    })
    assert snap is not None
    assert snap.resets_at is not None and snap.resets_at > 0


def test_api_usage_bucket_returns_none_when_unparseable():
    """A bucket with neither utilization nor resets_at can't say
    anything useful — caller should drop it."""
    from mimir.rate_limits import snapshot_from_api_usage_bucket

    assert snapshot_from_api_usage_bucket({"status": "allowed"}) is None


# ---- record_api_usage ---------------------------------------------------


@pytest.mark.asyncio
async def test_record_api_usage_writes_each_window(tmp_path: Path):
    from mimir.rate_limits import record_api_usage

    store = RateLimitStore(path=tmp_path / "rl.json")
    api_usage = {
        "five_hour": {"utilization": 0.40, "resets_at": 9_999_999_999},
        "seven_day_opus": {"utilization": 0.65, "resets_at": 9_999_999_999},
    }
    recorded = await record_api_usage(store, api_usage)
    assert set(recorded.keys()) == {"five_hour", "seven_day_opus"}
    saved = store.current()
    assert "five_hour" in saved
    assert saved["five_hour"].utilization == 0.40
    assert "seven_day_opus" in saved


@pytest.mark.asyncio
async def test_record_api_usage_empty_or_none(tmp_path: Path):
    """Empty / None apiUsage records nothing and returns an empty
    summary — the agent's capture method then logs an empty-windows
    quota_capture_ok event."""
    from mimir.rate_limits import record_api_usage

    store = RateLimitStore(path=tmp_path / "rl.json")
    assert await record_api_usage(store, None) == {}
    assert await record_api_usage(store, {}) == {}
    assert store.current() == {}


@pytest.mark.asyncio
async def test_record_api_usage_skips_unparseable_buckets(tmp_path: Path):
    """One bad bucket shouldn't drop the whole capture."""
    from mimir.rate_limits import record_api_usage

    store = RateLimitStore(path=tmp_path / "rl.json")
    recorded = await record_api_usage(store, {
        "five_hour": {"utilization": 0.30, "resets_at": 9_999_999_999},
        "broken": {"status": "allowed"},  # no util, no resets — drop
        "also_broken": "not a dict",  # type-mismatched — drop
    })
    assert set(recorded.keys()) == {"five_hour"}


# ---- running_on_claude_max ---------------------------------------------


def test_running_on_claude_max_true_with_oauth_only(monkeypatch):
    from mimir.rate_limits import running_on_claude_max

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-...")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    assert running_on_claude_max() is True


def test_running_on_claude_max_false_when_oauth_missing(monkeypatch):
    from mimir.rate_limits import running_on_claude_max

    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    assert running_on_claude_max() is False


def test_running_on_claude_max_false_with_base_url_override(monkeypatch):
    from mimir.rate_limits import running_on_claude_max

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-...")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://openrouter.ai/api/v1")
    assert running_on_claude_max() is False


# ---- provider quota windows (chainlink #298) ----------------------------


def test_render_uses_clean_labels_for_minimax_and_codex():
    """Provider-prefixed windows render with their registry labels, not
    the raw "minimax five hour" key fallback. chainlink #298."""
    snaps = {
        "minimax_five_hour": RateLimitSnapshot(
            status="allowed", utilization=0.30, resets_at=None,
        ),
        "openai_seven_day": RateLimitSnapshot(
            status="allowed", utilization=0.50, resets_at=None,
        ),
    }
    blob = "\n".join(render_plan_quota_lines(snaps))
    assert "Minimax 5-hour" in blob
    assert "Codex Plus 7-day" in blob
    # No raw underscore-key fallback leaked through.
    assert "minimax five hour" not in blob
    assert "openai seven day" not in blob


def test_filter_to_active_provider_drops_cross_provider_and_junk_keys():
    """chainlink #301: after a provider switch (e.g. the Codex cutover) a
    now-disabled poller can leave stale keys in the store. The view must
    render only the ACTIVE provider's keys so the live provider's quota
    isn't buried under continuously-refreshed stale ones."""
    from mimir.rate_limits import filter_to_active_provider

    snaps = {
        "five_hour": RateLimitSnapshot(status="allowed", utilization=0.1, resets_at=None),
        "seven_day": RateLimitSnapshot(status="allowed", utilization=0.2, resets_at=None),
        "seven_day_sonnet": RateLimitSnapshot(status="allowed", utilization=0.3, resets_at=None),
        "openai_five_hour": RateLimitSnapshot(status="allowed", utilization=0.8, resets_at=None),
        "openai_seven_day": RateLimitSnapshot(status="allowed", utilization=0.18, resets_at=None),
        # junk key not in any provider's window set
        "seven_day_omelette": RateLimitSnapshot(status="allowed", utilization=0.0, resets_at=None),
    }

    # Codex deployment: only the openai_* keys survive.
    assert set(filter_to_active_provider(snaps, "openai")) == {
        "openai_five_hour", "openai_seven_day",
    }

    # Anthropic deployment: bare + per-model keys survive; the junk key and
    # the openai_* keys are dropped.
    anthropic = filter_to_active_provider(snaps, "anthropic")
    assert set(anthropic) == {"five_hour", "seven_day", "seven_day_sonnet"}
    assert "seven_day_omelette" not in anthropic

    # Fail-open: unknown / falsy provider leaves the dict untouched.
    assert filter_to_active_provider(snaps, "") == snaps
    assert filter_to_active_provider(snaps, "nope") == snaps


def test_render_plan_quota_lines_shows_only_active_provider_via_filter():
    """End-to-end: rendering the filtered dict on a Codex box shows the
    Codex lines and none of the leftover Anthropic ones (chainlink #301)."""
    from mimir.rate_limits import filter_to_active_provider

    snaps = {
        "five_hour": RateLimitSnapshot(status="allowed", utilization=0.1, resets_at=None),
        "seven_day": RateLimitSnapshot(status="allowed", utilization=0.2, resets_at=None),
        "openai_five_hour": RateLimitSnapshot(status="allowed", utilization=0.8, resets_at=None),
        "openai_seven_day": RateLimitSnapshot(status="allowed", utilization=0.18, resets_at=None),
    }
    blob = "\n".join(
        render_plan_quota_lines(filter_to_active_provider(snaps, "openai"))
    )
    assert "Codex Plus 5-hour" in blob
    assert "Codex Plus 7-day" in blob
    assert "5-hour rolling" not in blob   # the Anthropic 5h label
    assert "7-day plan-wide" not in blob  # the Anthropic 7d label


def test_off_pace_fires_for_provider_windows():
    """off_pace_buckets must project burn rate for minimax_* / openai_*
    too (now that they're in _WINDOW_HOURS) — otherwise non-Anthropic
    deployments get no scale-back warning. chainlink #298."""
    from mimir.rate_limits import off_pace_buckets

    # 50% used with ~1h elapsed of a 5h window (resets in 4h) → off pace.
    resets = int(time.time()) + 4 * 3600
    for key in ("minimax_five_hour", "openai_five_hour"):
        snaps = {key: RateLimitSnapshot(
            status="allowed", utilization=0.50, resets_at=resets, observed_at=None,
        )}
        buckets = off_pace_buckets(snaps)
        assert [k for k, _, _ in buckets] == [key], (
            f"expected off-pace projection for {key}; got {buckets}"
        )


# ---- RateLimitStore -----------------------------------------------------


@pytest.mark.asyncio
async def test_record_writes_per_type_and_replaces(tmp_path: Path):
    store = RateLimitStore(path=tmp_path / "rate_limits.json")
    snap_a = RateLimitSnapshot(status="allowed", utilization=0.10,
                               resets_at=int(time.time()) + 3600)
    snap_b = RateLimitSnapshot(status="allowed_warning", utilization=0.85,
                               resets_at=int(time.time()) + 3600)
    await store.record("five_hour", snap_a)
    await store.record("five_hour", snap_b)
    body = json.loads((tmp_path / "rate_limits.json").read_text())
    assert body["five_hour"]["utilization"] == 0.85
    assert body["five_hour"]["status"] == "allowed_warning"


@pytest.mark.asyncio
async def test_record_supports_multiple_types(tmp_path: Path):
    store = RateLimitStore(path=tmp_path / "rate_limits.json")
    now = int(time.time())
    await store.record("five_hour", RateLimitSnapshot(
        status="allowed", utilization=0.30, resets_at=now + 3600,
    ))
    await store.record("seven_day_opus", RateLimitSnapshot(
        status="allowed_warning", utilization=0.92, resets_at=now + 86400,
    ))
    body = json.loads((tmp_path / "rate_limits.json").read_text())
    assert set(body.keys()) == {"five_hour", "seven_day_opus"}


def test_current_drops_stale_windows(tmp_path: Path):
    """Entries past resets_at are no longer relevant — drop them so
    the prompt doesn't show last-week's data."""
    path = tmp_path / "rate_limits.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    path.write_text(json.dumps({
        "five_hour": {"status": "allowed_warning", "utilization": 0.8,
                      "resets_at": now - 60, "observed_at": "x"},
        "seven_day": {"status": "allowed", "utilization": 0.4,
                      "resets_at": now + 86400, "observed_at": "x"},
    }))
    current = RateLimitStore(path=path).current()
    assert "five_hour" not in current
    assert "seven_day" in current


def test_current_keeps_entries_without_resets_at(tmp_path: Path):
    """Some SDK events don't populate resets_at — we shouldn't drop
    those just because we can't verify freshness."""
    path = tmp_path / "rate_limits.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "overage": {"status": "rejected", "utilization": None,
                    "resets_at": None, "observed_at": "x"},
    }))
    current = RateLimitStore(path=path).current()
    assert "overage" in current


def test_current_returns_empty_when_file_missing(tmp_path: Path):
    store = RateLimitStore(path=tmp_path / "no" / "such.json")
    assert store.current() == {}


def test_current_recovers_from_corrupt_file(tmp_path: Path):
    path = tmp_path / "rate_limits.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json{{")
    store = RateLimitStore(path=path)
    assert store.current() == {}


# ---- render -------------------------------------------------------------


def test_render_empty_returns_empty_list():
    assert render_plan_quota_lines({}) == []


def test_render_orders_known_keys_first():
    """5h first, then plan-wide 7d, then per-model 7d, then overage."""
    now = int(time.time())
    snaps = {
        "overage": RateLimitSnapshot(status="allowed", utilization=0.10,
                                     resets_at=now + 3600),
        "five_hour": RateLimitSnapshot(status="allowed", utilization=0.55,
                                       resets_at=now + 3600),
        "seven_day_opus": RateLimitSnapshot(status="allowed", utilization=0.40,
                                            resets_at=now + 86400),
        "seven_day": RateLimitSnapshot(status="allowed", utilization=0.30,
                                       resets_at=now + 86400),
    }
    lines = render_plan_quota_lines(snaps)
    # Verify order via the labels present.
    labels = [line.split(" — ")[0] for line in lines]
    assert labels[0] == "5-hour rolling"
    assert labels[1] == "7-day plan-wide"
    assert labels[2] == "7-day Opus"
    assert labels[3] == "Overage / pay-as-you-go"


def test_render_includes_status_when_not_allowed():
    snap = RateLimitSnapshot(status="allowed_warning", utilization=0.85,
                             resets_at=int(time.time()) + 1800)
    [line] = render_plan_quota_lines({"five_hour": snap})
    assert "85% used" in line
    assert "allowed_warning" in line
    assert "resets in" in line


def test_render_omits_status_when_allowed():
    """Default status doesn't add value; render percentage + reset time
    only."""
    snap = RateLimitSnapshot(status="allowed", utilization=0.20,
                             resets_at=int(time.time()) + 3600)
    [line] = render_plan_quota_lines({"five_hour": snap})
    assert "allowed" not in line
    assert "20% used" in line


def test_render_humanizes_resets_in_minutes_or_hours():
    now = int(time.time())
    cases = [
        (now + 30, "in 30s"),
        (now + 90, "in 1m"),
        (now + 5400, "in 1h 30m"),
        (now + 90000, "in 1d 1h"),
    ]
    for resets_at, expected_fragment in cases:
        snap = RateLimitSnapshot(status="allowed", utilization=0.5,
                                 resets_at=resets_at)
        [line] = render_plan_quota_lines({"five_hour": snap})
        assert expected_fragment in line, f"{resets_at} → {line!r}"


# ---- on-pace projection ------------------------------------------------


def test_projection_none_when_utilization_missing():
    from mimir.rate_limits import project_window_end

    snap = RateLimitSnapshot(status="allowed", utilization=None,
                             resets_at=int(time.time()) + 3600)
    assert project_window_end(snap, 5.0) is None


def test_projection_none_when_resets_at_missing():
    from mimir.rate_limits import project_window_end

    snap = RateLimitSnapshot(status="allowed", utilization=0.5, resets_at=None)
    assert project_window_end(snap, 5.0) is None


def test_projection_on_track_at_steady_pace():
    """Halfway through a 5h window at 30% used → on pace for 60%."""
    from mimir.rate_limits import project_window_end

    now = 1_000_000.0
    snap = RateLimitSnapshot(
        status="allowed",
        utilization=0.30,
        resets_at=int(now + 2.5 * 3600),  # 2.5h remaining of a 5h window
    )
    proj = project_window_end(snap, 5.0, reference_time=now)
    assert proj is not None
    assert proj.on_track
    assert abs(proj.on_pace_utilization - 0.60) < 1e-9
    assert abs(proj.elapsed_hours - 2.5) < 1e-9


def test_projection_off_track_when_burning_fast():
    """Quarter through a 5h window at 50% used → on pace for 200%."""
    from mimir.rate_limits import project_window_end

    now = 1_000_000.0
    snap = RateLimitSnapshot(
        status="allowed_warning",
        utilization=0.50,
        resets_at=int(now + 3.75 * 3600),  # 1.25h elapsed
    )
    proj = project_window_end(snap, 5.0, reference_time=now)
    assert proj is not None
    assert not proj.on_track
    assert abs(proj.on_pace_utilization - 2.0) < 1e-9


def test_projection_skips_when_too_early():
    """Below min_elapsed_fraction (5% default) the projection is too
    noisy to surface."""
    from mimir.rate_limits import project_window_end

    now = 1_000_000.0
    # 1 minute into a 5h window (1/300 = 0.33%, below 5% threshold)
    snap = RateLimitSnapshot(
        status="allowed",
        utilization=0.05,
        resets_at=int(now + 5.0 * 3600 - 60),
    )
    assert project_window_end(snap, 5.0, reference_time=now) is None


def test_projection_skips_when_window_already_past():
    from mimir.rate_limits import project_window_end

    now = 1_000_000.0
    snap = RateLimitSnapshot(
        status="allowed",
        utilization=0.5,
        resets_at=int(now - 60),  # already reset
    )
    assert project_window_end(snap, 5.0, reference_time=now) is None


def test_projection_skips_when_bucket_saturated():
    """A reading of 100% is a pegged/saturated bucket — the old window
    is fully consumed. Projecting forward produces absurd multiples like
    1093% when the endpoint still reports the old saturated value 27
    minutes into the new window. Return None; the raw suppress check
    at ≥ 0.80 already handles the suppression decision.
    (See memory/issues/anthropic-5h-bucket-pegged.md.)"""
    from mimir.rate_limits import project_window_end

    now = 1_000_000.0
    # 27 minutes into a fresh 5h window, endpoint still pegged at 100%
    snap = RateLimitSnapshot(
        status="allowed_warning",
        utilization=1.0,
        resets_at=int(now + (5.0 - 0.45) * 3600),  # 4.55h remaining
    )
    assert project_window_end(snap, 5.0, reference_time=now) is None


def test_projection_skips_when_bucket_over_full():
    """Utilization > 1.0 is also saturated — same guard applies."""
    from mimir.rate_limits import project_window_end

    now = 1_000_000.0
    snap = RateLimitSnapshot(
        status="allowed_warning",
        utilization=1.01,
        resets_at=int(now + 2.5 * 3600),
    )
    assert project_window_end(snap, 5.0, reference_time=now) is None


# ---- render projection inline ------------------------------------------


def test_render_includes_on_pace_for_5h_window(monkeypatch):
    """Half-elapsed 5h window at 30% used renders as 'on pace: 60%
    by reset'. No warning marker since on track."""
    now = int(time.time())
    snap = RateLimitSnapshot(
        status="allowed",
        utilization=0.30,
        resets_at=now + 2 * 3600 + 30 * 60,  # 2h 30m remaining
    )
    [line] = render_plan_quota_lines({"five_hour": snap})
    assert "on pace:" in line
    # Allow rounding tolerance — projected ≈ 60%
    assert "60%" in line
    assert "⚠" not in line


def test_render_marks_off_pace_with_warning():
    now = int(time.time())
    snap = RateLimitSnapshot(
        status="allowed_warning",
        utilization=0.80,
        resets_at=now + 3 * 3600,  # 2h elapsed of 5h
    )
    [line] = render_plan_quota_lines({"five_hour": snap})
    assert "⚠ on pace:" in line


def test_render_omits_projection_for_overage():
    """Overage has no fixed window — no projection makes sense."""
    now = int(time.time())
    snap = RateLimitSnapshot(
        status="allowed",
        utilization=0.10,
        resets_at=now + 86400,
    )
    [line] = render_plan_quota_lines({"overage": snap})
    assert "on pace:" not in line


# ---- off-pace warning paragraph ----------------------------------------


def test_off_pace_buckets_returns_only_off_track():
    from mimir.rate_limits import off_pace_buckets

    now = int(time.time())
    snaps = {
        "five_hour": RateLimitSnapshot(
            status="allowed", utilization=0.30, resets_at=now + 2 * 3600,
        ),  # 60% projected — on track
        "seven_day_opus": RateLimitSnapshot(
            status="allowed_warning", utilization=0.80, resets_at=now + 3 * 86400,
        ),  # ~140% projected — off track
    }
    out = off_pace_buckets(snaps)
    assert len(out) == 1
    assert out[0][0] == "seven_day_opus"


def test_off_pace_buckets_sorts_by_severity():
    from mimir.rate_limits import off_pace_buckets

    now = int(time.time())
    snaps = {
        # Halfway through 7d at 60% used → 120% projected.
        "seven_day": RateLimitSnapshot(
            status="allowed", utilization=0.60, resets_at=now + 3.5 * 86400,
        ),
        # Quarter through 5h at 50% used → 200% projected (worst).
        "five_hour": RateLimitSnapshot(
            status="allowed_warning", utilization=0.50, resets_at=now + 3.75 * 3600,
        ),
    }
    out = off_pace_buckets(snaps)
    assert len(out) == 2
    assert out[0][0] == "five_hour"  # worst first
    assert out[1][0] == "seven_day"


def test_off_pace_warning_empty_when_no_off_track():
    from mimir.rate_limits import render_off_pace_warning

    assert render_off_pace_warning([]) == []


def test_off_pace_warning_uses_strong_verb_at_high_severity():
    """At >150% projected, the language steps up from 'scale back'
    to 'defer all expensive work'."""
    from mimir.rate_limits import (
        WindowProjection,
        render_off_pace_warning,
    )

    snap = RateLimitSnapshot(
        status="allowed_warning", utilization=0.50,
        resets_at=int(time.time()) + 3600,
    )
    proj_severe = WindowProjection(
        elapsed_hours=1.25, hours_until_reset=3.75,
        on_pace_utilization=2.0, on_track=False,
    )
    lines = render_off_pace_warning([("five_hour", snap, proj_severe)])
    assert any("PLAN QUOTA AT RISK" in l for l in lines)
    assert any("defer all expensive work" in l for l in lines)
    assert any("Do NOT fan out" in l for l in lines)


def test_off_pace_warning_uses_moderate_verb_at_low_severity():
    from mimir.rate_limits import (
        WindowProjection,
        render_off_pace_warning,
    )

    snap = RateLimitSnapshot(
        status="allowed", utilization=0.60,
        resets_at=int(time.time()) + 3600,
    )
    proj_moderate = WindowProjection(
        elapsed_hours=4.0, hours_until_reset=1.0,
        on_pace_utilization=1.20, on_track=False,
    )
    lines = render_off_pace_warning([("five_hour", snap, proj_moderate)])
    assert any("scale back" in l.lower() for l in lines)
    assert not any("PLAN QUOTA AT RISK" in l for l in lines)


def test_off_pace_warning_lists_each_bucket_with_resets():
    from mimir.rate_limits import (
        WindowProjection,
        render_off_pace_warning,
    )

    snap = RateLimitSnapshot(
        status="allowed_warning", utilization=0.80,
        resets_at=int(time.time()) + 5400,  # 1h 30m
    )
    proj = WindowProjection(
        elapsed_hours=2.0, hours_until_reset=1.5,
        on_pace_utilization=1.40, on_track=False,
    )
    lines = render_off_pace_warning([("seven_day_opus", snap, proj)])
    # The verb line plus one bullet per bucket.
    assert len(lines) == 2
    assert "7-day Opus" in lines[1]
    assert "80% used" in lines[1]
    assert "140%" in lines[1]
    assert "in 1h" in lines[1]


# ---- atomic write + thread-safety (chainlink #181) ---------------------


def test_record_sync_atomic_no_temp_file_left(tmp_path: Path):
    """After record_sync the target file exists and no .tmp file remains."""
    store = RateLimitStore(path=tmp_path / "rl.json")
    snap = RateLimitSnapshot(status="allowed", utilization=0.25,
                             resets_at=int(time.time()) + 3600)
    store.record_sync("five_hour", snap)
    assert (tmp_path / "rl.json").exists()
    tmp_files = list(tmp_path.glob(".rate_limits.tmp.*"))
    assert tmp_files == [], f"Unexpected temp file(s) left: {tmp_files}"


def test_record_sync_survives_corrupt_existing_file(tmp_path: Path):
    """A corrupt on-disk file should be overwritten, not crash the write.
    Corruption triggers _load()'s fallback (empty dict) and record_sync
    replaces the file with valid JSON — quota suppression stays operational."""
    path = tmp_path / "rl.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{corrupted{{", encoding="utf-8")
    store = RateLimitStore(path=path)
    snap = RateLimitSnapshot(status="allowed", utilization=0.40,
                             resets_at=int(time.time()) + 3600)
    store.record_sync("five_hour", snap)  # must not raise
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["five_hour"]["utilization"] == 0.40


def test_record_sync_concurrent_threads_no_corruption(tmp_path: Path):
    """Two threads calling record_sync simultaneously must not corrupt
    the file. The threading.Lock serializes the read-modify-write; the
    atomic rename prevents partial writes on the thread that 'wins'."""
    import concurrent.futures

    path = tmp_path / "rl.json"
    store = RateLimitStore(path=path)
    future_ts = int(time.time()) + 86400

    def write_snap(key: str, utilization: float) -> None:
        for _ in range(50):
            store.record_sync(key, RateLimitSnapshot(
                status="allowed", utilization=utilization,
                resets_at=future_ts,
            ))

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futs = [
            ex.submit(write_snap, "five_hour", 0.10),
            ex.submit(write_snap, "seven_day", 0.20),
            ex.submit(write_snap, "five_hour", 0.30),
            ex.submit(write_snap, "seven_day", 0.40),
        ]
        for f in futs:
            f.result()

    # File must be valid JSON after concurrent writes.
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)  # raises on corruption
    assert isinstance(data, dict)
    assert "five_hour" in data
    assert "seven_day" in data
