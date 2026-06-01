"""Tests for ``mimir.minimax_usage_poller``.

Fixture data is captured from a real Minimax response (2026-05-21,
muninn's coding plan, ``MiniMax-M*`` bucket). Pinning real values
guards against parser drift if Minimax changes wire field names.
"""
from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any

import pytest

from mimir.event_logger import init_logger
from mimir.minimax_usage_poller import (
    DEFAULT_MODEL_NAME,
    REMAINS_ENDPOINT,
    MinimaxFetchError,
    MinimaxPollerConfig,
    fetch_remains,
    interval_snapshot,
    pick_model_entry,
    poll_once,
    weekly_snapshot,
)
from mimir.rate_limits import RateLimitStore


# Real captured payload from the endpoint, trimmed to two entries
# (the relevant MiniMax-M* bucket + one empty plan to verify the
# filter actually filters).
REAL_PAYLOAD: dict[str, Any] = {
    "base_resp": {"status_code": 0, "status_msg": "success"},
    "model_remains": [
        {
            "start_time":   1779357600000,
            "end_time":     1779375600000,
            "remains_time":    7003258,
            "current_interval_total_count": 4500,
            "current_interval_usage_count": 4152,  # NOTE: REMAINING, not used
            "model_name": "MiniMax-M*",
            "current_weekly_total_count": 45000,
            "current_weekly_usage_count": 41638,
            "weekly_start_time": 1779062400000,
            "weekly_end_time":   1779667200000,
            "weekly_remains_time": 298603258,
        },
        {
            # Plan does NOT cover this model — totals are 0.
            "start_time":   1779357600000,
            "end_time":     1779375600000,
            "remains_time": 0,
            "current_interval_total_count": 0,
            "current_interval_usage_count": 0,
            "model_name": "music-2.5",
            "current_weekly_total_count": 0,
            "current_weekly_usage_count": 0,
            "weekly_start_time": 1779062400000,
            "weekly_end_time":   1779667200000,
            "weekly_remains_time": 0,
        },
    ],
}


# Token-plan shape (2026-06+): chat models grouped under the ``general``
# CATEGORY, quota reported as a remaining PERCENT, request-count fields
# zeroed. ``video`` is the other category. Captured from muninn's live
# response after the Coding Plan -> Token Plan migration.
TOKEN_PAYLOAD: dict[str, Any] = {
    "base_resp": {"status_code": 0, "status_msg": "success"},
    "model_remains": [
        {
            "model_name": "general",
            "start_time": 1780326000000,
            "end_time": 1780344000000,
            "current_interval_total_count": 0,
            "current_interval_usage_count": 0,
            "current_interval_remaining_percent": 83,
            "weekly_start_time": 1780272000000,
            "weekly_end_time": 1780876800000,
            "current_weekly_total_count": 0,
            "current_weekly_usage_count": 0,
            "current_weekly_remaining_percent": 96,
        },
        {
            "model_name": "video",
            "current_interval_remaining_percent": 100,
            "current_weekly_remaining_percent": 100,
            "current_interval_total_count": 0,
            "current_weekly_total_count": 0,
        },
    ],
}


# ─── pick_model_entry ────────────────────────────────────────────────


def test_pick_model_default_is_general():
    """The default bucket is ``general`` — Minimax's Token Plan keys the
    chat-models category that way (2026-06; the old ``MiniMax-M*`` glob is
    gone). Pins the default + that it matches the live ``general`` entry."""
    assert DEFAULT_MODEL_NAME == "general"
    entry = pick_model_entry(TOKEN_PAYLOAD, DEFAULT_MODEL_NAME)
    assert entry is not None
    assert entry["model_name"] == "general"


def test_pick_model_glob_matches_prefix():
    """A trailing ``*`` is a prefix wildcard — operator could set
    ``MIMIR_MINIMAX_USAGE_MODEL=MiniMax-*`` to widen the match. Pins
    the wildcard semantics."""
    payload = {
        "base_resp": {"status_code": 0},
        "model_remains": [
            {"model_name": "MiniMax-M2.7", "current_interval_total_count": 1},
        ],
    }
    entry = pick_model_entry(payload, "MiniMax-*")
    assert entry is not None
    assert entry["model_name"] == "MiniMax-M2.7"


def test_pick_model_exact_name():
    """Without a trailing ``*``, the match is exact."""
    payload = {
        "base_resp": {"status_code": 0},
        "model_remains": [
            {"model_name": "speech-hd"},
            {"model_name": "music-2.5"},
        ],
    }
    assert pick_model_entry(payload, "speech-hd") is not None
    assert pick_model_entry(payload, "speech") is None


def test_pick_model_not_found():
    assert pick_model_entry(REAL_PAYLOAD, "does-not-exist") is None


def test_pick_model_handles_missing_array():
    """Defensive: a response without ``model_remains`` should return
    ``None``, not raise."""
    assert pick_model_entry({"base_resp": {"status_code": 0}}, "MiniMax-M*") is None


# ─── snapshot conversion ─────────────────────────────────────────────


def test_interval_snapshot_real_data():
    """Real captured values: 4152 remaining of 4500 total → 348 used
    → 7.73% utilization. ``end_time`` (ms) → unix seconds for
    ``resets_at``."""
    entry = REAL_PAYLOAD["model_remains"][0]
    snap = interval_snapshot(entry)
    assert snap.status == "allowed"
    assert snap.utilization == pytest.approx(348 / 4500)
    # end_time was 1779375600000 ms = 1779375600 sec
    assert snap.resets_at == 1779375600


def test_weekly_snapshot_real_data():
    """45000 total, 41638 remaining → 3362 used → 7.47% utilization."""
    entry = REAL_PAYLOAD["model_remains"][0]
    snap = weekly_snapshot(entry)
    assert snap.utilization == pytest.approx(3362 / 45000)
    assert snap.resets_at == 1779667200


def test_snapshot_handles_zero_total():
    """A plan that doesn't cover the model has total=0. Utilization
    must be 0.0 (NOT NaN from 0/0)."""
    entry = REAL_PAYLOAD["model_remains"][1]  # music-2.5, totals=0
    snap = interval_snapshot(entry)
    assert snap.utilization == 0.0


def test_snapshot_handles_missing_fields():
    """Defensive: a response that's missing the count fields entirely
    yields utilization=0.0 with resets_at=None, not a crash."""
    snap = interval_snapshot({})
    assert snap.utilization == 0.0
    assert snap.resets_at is None


def test_snapshot_handles_malformed_ms_timestamp():
    """A string timestamp (Minimax's contract says int ms, but defend
    against drift) → resets_at=None rather than ValueError."""
    snap = interval_snapshot({
        "current_interval_total_count": 100,
        "current_interval_usage_count": 50,
        "end_time": "not-a-number",
    })
    assert snap.resets_at is None
    assert snap.utilization == pytest.approx(0.5)


def test_snapshot_clamps_used_when_remaining_exceeds_total():
    """If the API returns remaining > total (shouldn't happen, but
    defend), used clamps to 0 rather than going negative."""
    snap = interval_snapshot({
        "current_interval_total_count": 100,
        "current_interval_usage_count": 150,  # nonsense — over total
    })
    assert snap.utilization == 0.0


# ─── token-plan (percent-based) shape ───────────────────────────────


def test_interval_snapshot_from_remaining_percent():
    """Token plan: count fields are 0 and quota lives in
    ``current_interval_remaining_percent`` → utilization = (100-83)/100."""
    entry = TOKEN_PAYLOAD["model_remains"][0]  # general, 83% remaining
    snap = interval_snapshot(entry)
    assert snap.utilization == pytest.approx(0.17)
    assert snap.resets_at == 1780344000  # end_time ms → s


def test_weekly_snapshot_from_remaining_percent():
    """96% weekly remaining → 4% used."""
    entry = TOKEN_PAYLOAD["model_remains"][0]
    snap = weekly_snapshot(entry)
    assert snap.utilization == pytest.approx(0.04)
    assert snap.resets_at == 1780876800


def test_remaining_percent_preferred_over_counts():
    """When BOTH percent and counts are present the percent wins — the
    token plan is the live shape; stale counts must not override it."""
    entry = {
        "current_interval_total_count": 100,
        "current_interval_usage_count": 50,         # 0.5 via counts
        "current_interval_remaining_percent": 90,   # 0.10 via percent
    }
    assert interval_snapshot(entry).utilization == pytest.approx(0.10)


# ─── poll_once with mocked fetch ─────────────────────────────────────


@pytest.fixture
def event_logger_init(tmp_path: Path):
    """log_event() requires init_logger first."""
    init_logger(tmp_path / "events.jsonl", session_id="test-minimax")


@pytest.mark.asyncio
async def test_poll_once_writes_both_windows(
    tmp_path: Path, event_logger_init, monkeypatch,
):
    """Happy path: fetch → pick → write both snapshots → ok event."""
    store = RateLimitStore(path=tmp_path / "rate_limits.json")

    # Slide the captured payload's window-end timestamps to future-
    # relative so ``RateLimitStore.current()`` doesn't filter the
    # snapshots as stale once we pass the originally-captured wall-
    # clock time. Same class of time-bomb the
    # ``test_callback_writes_both_windows_when_present`` fix in
    # ``tests/test_codex_plus_wireup.py`` documented.
    fresh_payload = copy.deepcopy(REAL_PAYLOAD)
    now_ms = int(time.time() * 1000)
    for entry in fresh_payload["model_remains"]:
        entry["end_time"] = now_ms + 5 * 3600 * 1000           # +5h
        entry["weekly_end_time"] = now_ms + 7 * 24 * 3600 * 1000  # +7d

    async def _fake_fetch(cfg, *, session=None):
        return fresh_payload

    monkeypatch.setattr("mimir.minimax_usage_poller.fetch_remains", _fake_fetch)
    # Legacy coding-plan payload (MiniMax-M* bucket) — point the poller at it
    # explicitly, since the default model_name is now the Token Plan "general".
    cfg = MinimaxPollerConfig(api_key="test-key", model_name="MiniMax-M*")
    result = await poll_once(cfg, store)

    assert result["ok"] is True
    assert result["model_name"] == "MiniMax-M*"
    persisted = store.current()
    assert "minimax_five_hour" in persisted
    assert "minimax_seven_day" in persisted
    # Spot-check the values match what interval_snapshot/weekly_snapshot
    # produced directly — confirms the wiring carries the values through.
    assert persisted["minimax_five_hour"].utilization == pytest.approx(348 / 4500)
    assert persisted["minimax_seven_day"].utilization == pytest.approx(3362 / 45000)


@pytest.mark.asyncio
async def test_poll_once_records_fetch_failure_as_event(
    tmp_path: Path, event_logger_init, monkeypatch,
):
    """``MinimaxFetchError`` should NOT propagate — it becomes a
    ``minimax_usage_failed`` event. The scheduler job loop relies on
    this so a single bad poll doesn't kill the cron."""
    store = RateLimitStore(path=tmp_path / "rate_limits.json")

    async def _explode(cfg, *, session=None):
        raise MinimaxFetchError("HTTP 500: gateway down")

    monkeypatch.setattr("mimir.minimax_usage_poller.fetch_remains", _explode)
    cfg = MinimaxPollerConfig(api_key="test-key")
    result = await poll_once(cfg, store)

    assert result["ok"] is False
    assert result["stage"] == "fetch"
    assert "HTTP 500" in result["error"]


@pytest.mark.asyncio
async def test_poll_once_records_unexpected_error_as_event(
    tmp_path: Path, event_logger_init, monkeypatch,
):
    """Defensive: even an unexpected exception type (not
    MinimaxFetchError) is caught and logged. Same rationale —
    we never want a transient bug to kill the cron loop."""
    store = RateLimitStore(path=tmp_path / "rate_limits.json")

    async def _explode(cfg, *, session=None):
        raise RuntimeError("aiohttp internal error")

    monkeypatch.setattr("mimir.minimax_usage_poller.fetch_remains", _explode)
    cfg = MinimaxPollerConfig(api_key="test-key")
    result = await poll_once(cfg, store)

    assert result["ok"] is False
    assert result["stage"] == "fetch"


@pytest.mark.asyncio
async def test_poll_once_skips_empty_plan(
    tmp_path: Path, event_logger_init, monkeypatch,
):
    """If the matched model has totals=0, the poller doesn't write
    misleading utilization=0 snapshots. It surfaces ``empty_plan``
    instead."""
    store = RateLimitStore(path=tmp_path / "rate_limits.json")
    empty_payload = {
        "base_resp": {"status_code": 0},
        "model_remains": [{
            "model_name": "MiniMax-M*",
            "current_interval_total_count": 0,
            "current_weekly_total_count": 0,
            "end_time": 0,
            "weekly_end_time": 0,
        }],
    }

    async def _fake(cfg, *, session=None):
        return empty_payload

    monkeypatch.setattr("mimir.minimax_usage_poller.fetch_remains", _fake)
    # The empty bucket is named "MiniMax-M*"; match it explicitly (the default
    # is now "general") so we exercise empty_plan, not a model_match miss.
    # No remaining_percent present → empty_plan is still the right verdict.
    cfg = MinimaxPollerConfig(api_key="test-key", model_name="MiniMax-M*")
    result = await poll_once(cfg, store)
    assert result["ok"] is False
    assert result["stage"] == "empty_plan"
    # Nothing was written.
    assert store.current() == {}


@pytest.mark.asyncio
async def test_poll_once_handles_no_matching_model(
    tmp_path: Path, event_logger_init, monkeypatch,
):
    store = RateLimitStore(path=tmp_path / "rate_limits.json")
    other_payload = {
        "base_resp": {"status_code": 0},
        "model_remains": [{
            "model_name": "speech-hd",
            "current_interval_total_count": 100,
            "current_weekly_total_count": 1000,
        }],
    }

    async def _fake(cfg, *, session=None):
        return other_payload

    monkeypatch.setattr("mimir.minimax_usage_poller.fetch_remains", _fake)
    cfg = MinimaxPollerConfig(api_key="test-key", model_name="MiniMax-M*")
    result = await poll_once(cfg, store)
    assert result["ok"] is False
    assert result["stage"] == "model_match"


@pytest.mark.asyncio
async def test_poll_once_token_plan_general(
    tmp_path: Path, event_logger_init, monkeypatch,
):
    """muninn's live scenario: Token Plan, default model_name ("general"),
    quota as remaining_percent with zeroed counts. The poll must SUCCEED
    (not empty_plan) and persist percent-derived utilization."""
    store = RateLimitStore(path=tmp_path / "rate_limits.json")
    fresh = copy.deepcopy(TOKEN_PAYLOAD)
    now_ms = int(time.time() * 1000)
    for entry in fresh["model_remains"]:
        entry["end_time"] = now_ms + 5 * 3600 * 1000
        entry["weekly_end_time"] = now_ms + 7 * 24 * 3600 * 1000

    async def _fake(cfg, *, session=None):
        return fresh

    monkeypatch.setattr("mimir.minimax_usage_poller.fetch_remains", _fake)
    cfg = MinimaxPollerConfig(api_key="test-key")  # default model_name="general"
    result = await poll_once(cfg, store)

    assert result["ok"] is True
    assert result["model_name"] == "general"
    persisted = store.current()
    assert persisted["minimax_five_hour"].utilization == pytest.approx(0.17)
    assert persisted["minimax_seven_day"].utilization == pytest.approx(0.04)


@pytest.mark.asyncio
async def test_poll_once_token_plan_not_empty_when_percent_present(
    tmp_path: Path, event_logger_init, monkeypatch,
):
    """Regression: zeroed COUNT fields must NOT trip empty_plan when the
    token-plan remaining_percent is present — that mis-classification is
    exactly what spammed minimax_usage_failed every poll on M3."""
    store = RateLimitStore(path=tmp_path / "rate_limits.json")
    now_ms = int(time.time() * 1000)
    payload = {
        "base_resp": {"status_code": 0},
        "model_remains": [{
            "model_name": "general",
            "current_interval_total_count": 0,
            "current_weekly_total_count": 0,
            "current_interval_remaining_percent": 100,
            "current_weekly_remaining_percent": 100,
            "end_time": now_ms + 5 * 3600 * 1000,
            "weekly_end_time": now_ms + 7 * 24 * 3600 * 1000,
        }],
    }

    async def _fake(cfg, *, session=None):
        return payload

    monkeypatch.setattr("mimir.minimax_usage_poller.fetch_remains", _fake)
    cfg = MinimaxPollerConfig(api_key="test-key")
    result = await poll_once(cfg, store)
    assert result["ok"] is True
    assert result.get("stage") != "empty_plan"
    # 100% remaining → 0 utilization, but still WRITTEN (not skipped).
    assert store.current()["minimax_five_hour"].utilization == pytest.approx(0.0)


# ─── fetch_remains: response shape validation ───────────────────────


@pytest.mark.asyncio
async def test_fetch_raises_on_non_zero_base_resp():
    """Minimax's in-band error marker: ``base_resp.status_code != 0``
    means the call hit the gateway but failed. Treat as a hard error."""
    from unittest.mock import AsyncMock, MagicMock

    class _FakeResp:
        status = 200
        async def text(self):
            return json.dumps({
                "base_resp": {
                    "status_code": 1004,
                    "status_msg": "rate_limit_exceeded",
                },
            })
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None

    session = MagicMock()
    session.get = MagicMock(return_value=_FakeResp())
    cfg = MinimaxPollerConfig(api_key="x")
    with pytest.raises(MinimaxFetchError) as exc:
        await fetch_remains(cfg, session=session)
    assert "1004" in str(exc.value)
    assert "rate_limit_exceeded" in str(exc.value)


@pytest.mark.asyncio
async def test_fetch_raises_on_non_200():
    from unittest.mock import MagicMock

    class _FakeResp:
        status = 403
        async def text(self):
            return "Forbidden"
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None

    session = MagicMock()
    session.get = MagicMock(return_value=_FakeResp())
    cfg = MinimaxPollerConfig(api_key="x")
    with pytest.raises(MinimaxFetchError) as exc:
        await fetch_remains(cfg, session=session)
    assert "HTTP 403" in str(exc.value)


@pytest.mark.asyncio
async def test_fetch_raises_on_non_json_body():
    from unittest.mock import MagicMock

    class _FakeResp:
        status = 200
        async def text(self):
            return "<html>504 Gateway Timeout</html>"
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None

    session = MagicMock()
    session.get = MagicMock(return_value=_FakeResp())
    cfg = MinimaxPollerConfig(api_key="x")
    with pytest.raises(MinimaxFetchError) as exc:
        await fetch_remains(cfg, session=session)
    assert "non-JSON" in str(exc.value)


# ─── Config defaults ────────────────────────────────────────────────


def test_poller_config_defaults():
    """The config dataclass's defaults match what the poller documents
    in its docstring + module constants. Pinning the defaults so a
    drive-by change doesn't silently flip the endpoint or UA."""
    cfg = MinimaxPollerConfig(api_key="x")
    assert cfg.endpoint == REMAINS_ENDPOINT
    assert cfg.model_name == DEFAULT_MODEL_NAME
    assert "mimir" in cfg.user_agent.lower()
    assert cfg.timeout_seconds == pytest.approx(15.0)
