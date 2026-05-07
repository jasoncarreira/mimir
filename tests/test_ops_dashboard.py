"""Tests for the /ops dashboard analytics + route wiring.

Covers:
- ``parse_days_param`` validation (default, valid int, error cases)
- ``_load_events`` behavior (missing log, malformed lines, cutoff)
- ``compute_stats`` shape: summary counts, queued attribution,
  resolution-path histograms, shell-job counters, failure detection
  via suffix matching
- HTML render (script tag injection)
- Route wiring through ``register_routes``
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from mimir.ops_dashboard import (
    build_dashboard_payload,
    compute_stats,
    parse_days_param,
    render_dashboard_html,
)
from mimir.web_ui import register_routes


def _ts(offset_days: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=offset_days)).isoformat()


def _write_events(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


# ─── parse_days_param ────────────────────────────────────────────────


def test_parse_days_param_default():
    assert parse_days_param(None) == 7
    assert parse_days_param("") == 7


def test_parse_days_param_valid_int():
    assert parse_days_param("30") == 30
    assert parse_days_param("1") == 1
    assert parse_days_param("365") == 365


def test_parse_days_param_rejects_non_integer():
    with pytest.raises(ValueError, match="must be an integer"):
        parse_days_param("abc")


def test_parse_days_param_rejects_below_one():
    with pytest.raises(ValueError, match=">= 1"):
        parse_days_param("0")
    with pytest.raises(ValueError, match=">= 1"):
        parse_days_param("-5")


def test_parse_days_param_rejects_above_max():
    with pytest.raises(ValueError, match="<= 365"):
        parse_days_param("366")


# ─── _load_events / build_dashboard_payload ──────────────────────────


def test_build_dashboard_payload_missing_log_returns_empty(tmp_path: Path):
    """No events.jsonl yet — payload returns successfully with zeros."""
    payload = build_dashboard_payload(tmp_path / "events.jsonl", days=7)
    assert payload["summary"]["total_events"] == 0
    assert payload["timeseries"] == []
    assert payload["recent_failures"] == []


def test_build_dashboard_payload_skips_malformed_lines(tmp_path: Path):
    """A garbled line shouldn't crash the dashboard."""
    log = tmp_path / "events.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w") as f:
        f.write('{"timestamp":"' + _ts() + '","type":"event_queued","trigger":"user_message","channel_id":"c1"}\n')
        f.write("not-json-at-all\n")
        f.write('{"timestamp":"' + _ts() + '","type":"auto_dispatch_ok"}\n')

    payload = build_dashboard_payload(log, days=7)
    assert payload["summary"]["total_events"] == 2
    assert payload["summary"]["events_queued"] == 1
    assert payload["summary"]["auto_dispatch_ok"] == 1


def test_build_dashboard_payload_respects_cutoff(tmp_path: Path):
    """Events older than the cutoff are dropped."""
    log = tmp_path / "events.jsonl"
    _write_events(log, [
        {"timestamp": _ts(0.5), "type": "event_queued", "trigger": "user_message", "channel_id": "c1"},
        {"timestamp": _ts(50), "type": "event_queued", "trigger": "scheduled_tick", "channel_id": "c1"},
    ])
    payload = build_dashboard_payload(log, days=7)
    assert payload["summary"]["events_queued"] == 1
    assert payload["queued_by_trigger"].get("user_message") == 1
    assert "scheduled_tick" not in payload["queued_by_trigger"]


# ─── compute_stats: summary + buckets ────────────────────────────────


def test_compute_stats_summary_counts(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    _write_events(log, [
        {"timestamp": _ts(0.1), "type": "event_queued", "trigger": "user_message", "channel_id": "c1"},
        {"timestamp": _ts(0.1), "type": "event_queued", "trigger": "scheduled_tick", "channel_id": "c1"},
        {"timestamp": _ts(0.1), "type": "auto_dispatch_ok"},
        {"timestamp": _ts(0.1), "type": "subagent_started", "task_id": "t1"},
        {"timestamp": _ts(0.1), "type": "subagent_notification", "task_id": "t1"},
        {"timestamp": _ts(0.1), "type": "client_pool_drained"},
        {"timestamp": _ts(0.1), "type": "event_queue_high_water"},
    ])
    payload = build_dashboard_payload(log, days=1)
    s = payload["summary"]
    assert s["total_events"] == 7
    assert s["events_queued"] == 2
    assert s["auto_dispatch_ok"] == 1
    assert s["subagents_started"] == 1
    assert s["subagents_completed"] == 1
    assert s["client_pool_drains"] == 1
    assert s["high_water_events"] == 1


def test_compute_stats_queued_attribution(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    _write_events(log, [
        {"timestamp": _ts(0.1), "type": "event_queued", "trigger": "user_message", "channel_id": "c1"},
        {"timestamp": _ts(0.1), "type": "event_queued", "trigger": "user_message", "channel_id": "c2"},
        {"timestamp": _ts(0.1), "type": "event_queued", "trigger": "scheduled_tick", "channel_id": "c1"},
    ])
    payload = build_dashboard_payload(log, days=1)
    assert payload["queued_by_trigger"]["user_message"] == 2
    assert payload["queued_by_trigger"]["scheduled_tick"] == 1
    assert payload["queued_by_channel"]["c1"] == 2
    assert payload["queued_by_channel"]["c2"] == 1


def test_compute_stats_resolution_paths_per_tool(tmp_path: Path):
    """Each saga_*_ctx_resolution + bash_async_ctx_resolution event
    type gets its own histogram, keyed by the kind."""
    log = tmp_path / "events.jsonl"
    _write_events(log, [
        {"timestamp": _ts(0.1), "type": "saga_query_ctx_resolution", "resolution_path": "saga_session_id"},
        {"timestamp": _ts(0.1), "type": "saga_query_ctx_resolution", "resolution_path": "saga_session_id"},
        {"timestamp": _ts(0.1), "type": "saga_query_ctx_resolution", "resolution_path": "single_active"},
        {"timestamp": _ts(0.1), "type": "saga_query_ctx_resolution", "resolution_path": "missing"},
        {"timestamp": _ts(0.1), "type": "bash_async_ctx_resolution", "resolution_path": "saga_session_id"},
    ])
    payload = build_dashboard_payload(log, days=1)
    paths = payload["resolution_paths"]
    assert paths["saga_query_ctx_resolution"]["saga_session_id"] == 2
    assert paths["saga_query_ctx_resolution"]["single_active"] == 1
    assert paths["saga_query_ctx_resolution"]["missing"] == 1
    assert paths["bash_async_ctx_resolution"]["saga_session_id"] == 1


def test_compute_stats_shell_job_counters(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    _write_events(log, [
        {"timestamp": _ts(0.1), "type": "bash_async_spawned", "channel_id": "c1"},
        {"timestamp": _ts(0.1), "type": "bash_async_spawned", "channel_id": "c1"},
        {"timestamp": _ts(0.1), "type": "bash_async_spawned", "channel_id": "c2"},
        {"timestamp": _ts(0.1), "type": "shell_job_complete_routed"},
        {"timestamp": _ts(0.1), "type": "shell_job_complete_routed"},
        {"timestamp": _ts(0.1), "type": "shell_job_complete_no_channel"},
        {"timestamp": _ts(0.1), "type": "shell_job_complete_enqueue_failed"},
    ])
    payload = build_dashboard_payload(log, days=1)
    sj = payload["shell_jobs"]
    assert sj["spawned"] == 3
    assert sj["routed"] == 2
    assert sj["no_channel"] == 1
    assert sj["enqueue_failed"] == 1
    assert sj["spawn_by_channel"]["c1"] == 2
    assert sj["spawn_by_channel"]["c2"] == 1
    # Spawned shows up in the top-level summary too.
    assert payload["summary"]["shell_jobs_spawned"] == 3
    assert payload["summary"]["shell_jobs_routed"] == 2


def test_compute_stats_failure_detection_by_suffix(tmp_path: Path):
    """``compute_stats`` buckets any event whose type ends in
    ``_failed`` / ``_error`` / ``_blocked`` / ``_anomalous`` /
    ``_rejected`` as a failure. Generic suffix matching avoids
    needing a hardcoded list every time a new failure event lands."""
    log = tmp_path / "events.jsonl"
    _write_events(log, [
        {"timestamp": _ts(0.1), "type": "git_push_failed", "error": "auth failed"},
        {"timestamp": _ts(0.1), "type": "git_pull_blocked", "reason": "non-fast-forward"},
        {"timestamp": _ts(0.1), "type": "oauth_quota_anomalous", "detail": "100% jump"},
        {"timestamp": _ts(0.1), "type": "event_admission_rejected"},
        {"timestamp": _ts(0.1), "type": "shell_job_complete_enqueue_failed", "error": "dispatcher down"},
        {"timestamp": _ts(0.1), "type": "auto_dispatch_ok"},  # not a failure
        {"timestamp": _ts(0.1), "type": "event_queued", "trigger": "user_message"},  # not a failure
    ])
    payload = build_dashboard_payload(log, days=1)
    assert payload["summary"]["failures"] == 5
    fbk = payload["failures_by_kind"]
    assert "git_push_failed" in fbk
    assert "git_pull_blocked" in fbk
    assert "oauth_quota_anomalous" in fbk
    assert "event_admission_rejected" in fbk
    assert "shell_job_complete_enqueue_failed" in fbk
    assert "auto_dispatch_ok" not in fbk
    assert "event_queued" not in fbk


def test_compute_stats_recent_failures_capped_and_sorted(tmp_path: Path):
    """Recent failures list is sorted most-recent-first and capped at 30."""
    log = tmp_path / "events.jsonl"
    records = []
    for i in range(40):
        # Spread across last 6 days, oldest first.
        records.append({
            "timestamp": _ts(6 - i * 0.1),
            "type": "git_push_failed",
            "error": f"failure-{i}",
        })
    _write_events(log, records)
    payload = build_dashboard_payload(log, days=7)
    rf = payload["recent_failures"]
    assert len(rf) == 30
    # Sorted descending — first entry has the latest timestamp.
    assert rf[0]["t"] > rf[-1]["t"]
    # Detail field carries the captured error.
    assert "failure-" in rf[0]["detail"]


def test_compute_stats_recent_failures_keeps_newest_when_over_cap(tmp_path: Path):
    """Regression for Mimir's PR review catch: the prior implementation
    capped during accumulation, keeping the OLDEST 60 failures encountered
    (since _load_events walks oldest-first), then sort-descending picked
    the latest 30 of those 60 — silently dropping anything past position
    60. With >60 failures in the window, the dashboard would have shown
    the 30-most-recent-of-the-oldest-60, not the 30-most-recent-overall.

    This generates 80 failures spread across 7 days where the *latest 10*
    carry distinct ``signature-N`` strings; the assertion pins that those
    latest signatures appear in the rendered output. With the prior bug
    they never even made it into the recent_failures list."""
    log = tmp_path / "events.jsonl"
    records = []
    # 70 "old" failures, oldest first, all with the same noise string.
    # Land within the window but well past the latest-10 below.
    for i in range(70):
        records.append({
            "timestamp": _ts(6.0 - i * 0.05),  # day 6 down to ~day 2.5
            "type": "git_push_failed",
            "error": "old-noise",
        })
    # Latest 10 — distinct signatures, very recent.
    for i in range(10):
        records.append({
            "timestamp": _ts(0.5 - i * 0.01),
            "type": "oauth_quota_anomalous",
            "detail": f"signature-{i}",
        })
    _write_events(log, records)

    payload = build_dashboard_payload(log, days=7)
    rf = payload["recent_failures"]
    assert len(rf) == 30

    # Each of the latest 10 ``signature-N`` strings must appear in the
    # rendered list. With the prior cap-during-append bug they're
    # absent (the cap kept the oldest 60 from ``records``, none of
    # which carried these signatures).
    rendered_details = {entry["detail"] for entry in rf}
    for i in range(10):
        assert f"signature-{i}" in rendered_details, (
            f"signature-{i} missing — recent_failures cap is dropping "
            f"newest entries. Rendered details: {sorted(rendered_details)}"
        )

    # And the rendered kinds should include the newer
    # ``oauth_quota_anomalous`` events, not just ``git_push_failed``.
    rendered_kinds = {entry["kind"] for entry in rf}
    assert "oauth_quota_anomalous" in rendered_kinds


def test_compute_stats_recent_failures_pulls_from_alt_fields(tmp_path: Path):
    """``_failure_detail`` walks a few candidate fields — error /
    reason / stderr / message / detail / stage."""
    log = tmp_path / "events.jsonl"
    _write_events(log, [
        {"timestamp": _ts(0.1), "type": "a_failed", "error": "from-error"},
        {"timestamp": _ts(0.2), "type": "b_failed", "reason": "from-reason"},
        {"timestamp": _ts(0.3), "type": "c_failed", "stderr": "from-stderr"},
        {"timestamp": _ts(0.4), "type": "d_failed"},  # no detail field
    ])
    payload = build_dashboard_payload(log, days=1)
    by_kind = {r["kind"]: r["detail"] for r in payload["recent_failures"]}
    assert by_kind["a_failed"] == "from-error"
    assert by_kind["b_failed"] == "from-reason"
    assert by_kind["c_failed"] == "from-stderr"
    assert by_kind["d_failed"] == ""


def test_compute_stats_timeseries_per_day(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    _write_events(log, [
        # 2 events 3 days ago, 1 event today.
        {"timestamp": _ts(3.0), "type": "event_queued", "trigger": "user_message"},
        {"timestamp": _ts(3.0), "type": "auto_dispatch_ok"},
        {"timestamp": _ts(0.0), "type": "event_queued", "trigger": "user_message"},
    ])
    payload = build_dashboard_payload(log, days=7)
    ts = payload["timeseries"]
    assert len(ts) == 2  # two distinct days
    # Both days must carry events / queued counts.
    totals = {row["day"]: row for row in ts}
    days_sorted = sorted(totals.keys())
    assert totals[days_sorted[0]]["events"] == 2
    assert totals[days_sorted[1]]["events"] == 1
    assert totals[days_sorted[0]]["queued"] == 1
    assert totals[days_sorted[1]]["queued"] == 1


# ─── HTML render ─────────────────────────────────────────────────────


def test_render_dashboard_html_injects_data(tmp_path: Path):
    """The render must replace the ``__DATA__`` placeholder with valid
    JSON, so the frontend can parse it on load."""
    log = tmp_path / "events.jsonl"
    _write_events(log, [
        {"timestamp": _ts(0.1), "type": "event_queued", "trigger": "user_message"},
    ])
    payload = build_dashboard_payload(log, days=1)
    html = render_dashboard_html(payload)
    assert "__DATA__" not in html  # placeholder replaced
    # The injected JSON has a known field; its presence confirms the
    # injection worked.
    assert '"window_days": 1' in html or '"window_days":1' in html
    assert '"summary"' in html


def test_render_dashboard_html_handles_empty_payload(tmp_path: Path):
    """Empty events log still produces a valid HTML doc."""
    payload = build_dashboard_payload(tmp_path / "nonexistent.jsonl", days=7)
    html = render_dashboard_html(payload)
    assert "<!doctype html>" in html
    assert "mimir Ops" in html


# ─── Route wiring through register_routes ────────────────────────────


@pytest.fixture
def web_app(tmp_path: Path) -> tuple[web.Application, Path, Path]:
    """Build an aiohttp app with /ops + /api/ops wired up. Returns
    (app, turns_log, events_log) for the test to populate."""
    app = web.Application()
    turns_log = tmp_path / "turns.jsonl"
    events_log = tmp_path / "events.jsonl"
    register_routes(app, turns_log=turns_log, events_log=events_log)
    return app, turns_log, events_log


@pytest.mark.asyncio
async def test_route_ops_html_renders(web_app, aiohttp_client):
    app, _, events_log = web_app
    _write_events(events_log, [
        {"timestamp": _ts(0.1), "type": "event_queued", "trigger": "user_message", "channel_id": "c1"},
    ])
    client = await aiohttp_client(app)
    resp = await client.get("/ops")
    assert resp.status == 200
    assert resp.content_type == "text/html"
    text = await resp.text()
    assert "mimir Ops" in text


@pytest.mark.asyncio
async def test_route_api_ops_returns_json(web_app, aiohttp_client):
    app, _, events_log = web_app
    _write_events(events_log, [
        {"timestamp": _ts(0.1), "type": "event_queued", "trigger": "user_message", "channel_id": "c1"},
    ])
    client = await aiohttp_client(app)
    resp = await client.get("/api/ops")
    assert resp.status == 200
    body = await resp.json()
    assert body["summary"]["events_queued"] == 1
    assert body["window_days"] == 7


@pytest.mark.asyncio
async def test_route_ops_invalid_days_param_returns_400(web_app, aiohttp_client):
    app, _, _ = web_app
    client = await aiohttp_client(app)

    resp = await client.get("/ops?days=abc")
    assert resp.status == 400
    text = await resp.text()
    assert "integer" in text

    resp = await client.get("/api/ops?days=999")
    assert resp.status == 400
    body = await resp.json()
    assert "<= 365" in body["error"]


@pytest.mark.asyncio
async def test_route_ops_with_missing_events_log_still_serves(web_app, aiohttp_client):
    """Mimir setups won't have an events.jsonl until first event fires.
    The dashboard must still serve a valid empty page rather than 500."""
    app, _, _ = web_app
    client = await aiohttp_client(app)
    resp = await client.get("/ops")
    assert resp.status == 200
    text = await resp.text()
    assert "<!doctype html>" in text


@pytest.mark.asyncio
async def test_route_ops_days_param_filters_window(web_app, aiohttp_client):
    """Events older than the requested window are excluded."""
    app, _, events_log = web_app
    _write_events(events_log, [
        {"timestamp": _ts(0.5), "type": "event_queued", "trigger": "user_message"},
        {"timestamp": _ts(50), "type": "event_queued", "trigger": "scheduled_tick"},
    ])
    client = await aiohttp_client(app)
    resp = await client.get("/api/ops?days=1")
    body = await resp.json()
    assert body["summary"]["events_queued"] == 1
