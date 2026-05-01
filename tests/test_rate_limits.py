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
