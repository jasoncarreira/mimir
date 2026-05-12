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
    """Events older than the cutoff are dropped. File ordering is
    chronological (oldest first) per production reality verified by
    trace-further #3 — the ``_load_events`` tail-read breaks early on
    cutoff crossings, so out-of-order test data would mask in-window
    records."""
    log = tmp_path / "events.jsonl"
    _write_events(log, [
        # Oldest first.
        {"timestamp": _ts(50), "type": "event_queued", "trigger": "scheduled_tick", "channel_id": "c1"},
        {"timestamp": _ts(0.5), "type": "event_queued", "trigger": "user_message", "channel_id": "c1"},
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


def test_render_dashboard_html_returns_static_shell(tmp_path: Path):
    """Pattern B refactor (2026-05-10): the dashboard HTML is now a
    static shell — no server-side data injection. The frontend AJAX-
    fetches /api/ops with X-API-Key. Previously the same HTML carried
    a ``__DATA__`` placeholder replaced with JSON at render time;
    that path leaked the dashboard contents to anyone who could load
    the auth-exempt /ops route. The shell contains the bootstrap JS
    that prompts for an API key on first visit and the render(D)
    function called once data arrives."""
    log = tmp_path / "events.jsonl"
    _write_events(log, [
        {"timestamp": _ts(0.1), "type": "event_queued", "trigger": "user_message"},
    ])
    payload = build_dashboard_payload(log, days=1)
    # render_dashboard_html now ignores the payload arg (kept for one
    # release of API compat). Verify the shell shape:
    html = render_dashboard_html(payload)
    # Placeholder gone — no script tag carrying server-injected data.
    # (The string ``__DATA__`` may appear in a JS comment explaining the
    # historical shape, so check the script-tag form specifically.)
    assert '<script id="data"' not in html
    assert "<!doctype html>" in html
    assert "mimir Ops" in html
    # The shell must NOT contain the actual stats — those come via AJAX.
    assert '"window_days": 1' not in html
    assert '"window_days":1' not in html
    # The shell MUST contain the bootstrap markers proving the new path.
    assert "authedFetch('/api/ops'" in html
    assert "function render(D)" in html
    assert "API_KEY_LS" in html


def test_render_dashboard_html_handles_empty_payload(tmp_path: Path):
    """Empty events log still produces a valid HTML doc. The render is
    static now so the payload is irrelevant; this test just pins the
    no-args call shape (also accepted) plus the legacy
    payload-arg-passing path stays callable for one release."""
    html = render_dashboard_html()
    assert "<!doctype html>" in html
    assert "mimir Ops" in html
    payload = build_dashboard_payload(tmp_path / "nonexistent.jsonl", days=7)
    assert render_dashboard_html(payload) == html  # arg ignored


def test_dashboard_html_js_escapes_survive_python_rendering():
    """Pinned regression for the 2026-05-12 ops-page-broken bug.

    ``_DASHBOARD_HTML`` is a Python triple-double-quoted string that
    contains a ``<script>`` block. Python's lexer processes backslash
    escapes in that string BEFORE the browser sees it — so a JS source
    line written as ``msg += 'a\\n\\nb'`` (intended to produce a
    JS-escape ``\\n``) gets emitted to the wire as ``msg += 'a<LF><LF>b'``.
    A single-quoted JS string can't span lines, so the script throws
    SyntaxError at parse time, all the bootstrap functions
    (``getApiKey``, ``setApiKey``, ``authedFetch``) never get defined,
    and the dashboard silently fails to load /api/ops. From the
    operator's POV: the page loads, the bootstrap prompt never fires,
    and "Failed to load /api/ops" is the only signal.

    Concrete check: the rendered JS string literal that holds the
    API-key prompt message MUST contain literal ``\\n`` (a real
    backslash followed by ``n`` — the JS-escape form), NOT a raw
    newline character. Same for the ``you\\'ll`` apostrophe escape.

    Future authors adding more JS string literals to ``_DASHBOARD_HTML``:
    every backslash escape inside a JS string must be DOUBLED in the
    Python source. See the comment above ``_DASHBOARD_HTML``.
    """
    html = render_dashboard_html()
    # Locate the promptApiKey message-building line. Search by the
    # operator-visible marker ("Saved to this browser") — robust against
    # cosmetic edits to the function body.
    candidates = [
        ln for ln in html.split("\n")
        if "msg +=" in ln and "Saved to this browser" in ln
    ]
    assert len(candidates) == 1, (
        f"expected exactly 1 JS line containing the API-key prompt "
        f"message; got {len(candidates)}. Likely cause: Python "
        f"interpreted ``\\n`` in the Python source as a real LF, so the "
        f"JS string got split across two lines and the marker text now "
        f"lives on a separate line from ``msg +=``. Fix: double the "
        f"backslashes in the Python source (``\\\\n\\\\n`` not "
        f"``\\n\\n``). Alternate causes: marker text was edited or the "
        f"function was duplicated."
    )
    line = candidates[0]
    # The JS-escape form survives Python rendering — the backslash is
    # literal, followed by ``n``. We assert on the exact substring.
    assert r"\n\n" in line, (
        f"JS string literal for the prompt message lost its '\\n\\n' "
        f"escape — Python interpreted the backslashes as escape "
        f"sequences instead of passing them through to the JS source. "
        f"Double the backslashes in the Python source. "
        f"Got rendered line: {line!r}"
    )
    # And the apostrophe escape — Python ate ``\'`` in the prior bug,
    # leaving a bare ``'`` that closed the JS string early at ``you'll``.
    assert r"you\'ll" in line, (
        f"JS string literal lost its escaped apostrophe in 'you\\'ll' — "
        f"Python interpreted ``\\'`` as a bare ``'``, closing the JS "
        f"string early. Double the backslash in the Python source. "
        f"Got rendered line: {line!r}"
    )


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
    """Events older than the requested window are excluded.
    Chronological file order (oldest first) — see
    test_build_dashboard_payload_respects_cutoff."""
    app, _, events_log = web_app
    _write_events(events_log, [
        {"timestamp": _ts(50), "type": "event_queued", "trigger": "scheduled_tick"},
        {"timestamp": _ts(0.5), "type": "event_queued", "trigger": "user_message"},
    ])
    client = await aiohttp_client(app)
    resp = await client.get("/api/ops?days=1")
    body = await resp.json()
    assert body["summary"]["events_queued"] == 1


# ─── XSS hardening (legacy) ──────────────────────────────────────────
# The previous tests verified that ``</`` was escaped to ``<\\/`` when
# user-controlled strings (failure detail messages, channel ids) were
# server-side injected into the dashboard HTML. After Pattern B refactor
# (2026-05-10), the dashboard data is fetched via XHR from /api/ops
# instead of injected at render time — JSON in an XHR response body
# can't break out of a script tag because it's never inside one. The
# escape concern is moot; tests removed.
#
# The /api/ops route returns ``Content-Type: application/json`` (set
# by aiohttp ``web.json_response``), and the frontend parses it via
# ``r.json()`` — the response body is never written to the DOM. The
# HTML ``render(D)`` function uses ``td.textContent`` and direct
# ``innerHTML`` only with payload values it constructs (e.g. backlog
# items). The backlog text is sourced from the dashboard module's own
# constants, not from events.jsonl, so XSS via user-controlled input
# doesn't have a route into the rendered page either.


# ─── _load_chainlink_issues — graceful failure paths ─────────────────


@pytest.mark.asyncio
async def test_chainlink_helper_handles_missing_cli(tmp_path: Path, monkeypatch):
    """When chainlink isn't on PATH, the helper returns an unavailable
    envelope rather than raising. Simulated by monkeypatching
    asyncio.create_subprocess_exec to raise FileNotFoundError."""
    from mimir import ops_dashboard
    import asyncio

    async def raise_fnf(*args, **kwargs):
        raise FileNotFoundError("chainlink: command not found")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", raise_fnf)
    result = await ops_dashboard._load_chainlink_issues(tmp_path)
    assert result["available"] is False
    assert "chainlink CLI not on PATH" in result["error"]
    assert result["issues"] == []


@pytest.mark.asyncio
async def test_chainlink_helper_handles_nonzero_exit(tmp_path: Path, monkeypatch):
    """A non-zero exit code (e.g. 'not a chainlink repository') yields
    an unavailable envelope with stderr captured."""
    from mimir import ops_dashboard
    import asyncio

    class _FakeProc:
        returncode = 1
        async def communicate(self):
            return (b"", b"Error: Not a chainlink repository (or any parent).\n")

    async def fake_exec(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = await ops_dashboard._load_chainlink_issues(tmp_path)
    assert result["available"] is False
    assert "Not a chainlink repository" in result["error"]
    assert result["issues"] == []


@pytest.mark.asyncio
async def test_chainlink_helper_passes_status_open_filter(tmp_path: Path, monkeypatch):
    """Defensive against CLI default drift: the helper must always
    invoke chainlink with ``--status open`` so closed / archived
    issues never bleed into the dashboard even if a future CLI
    release flips its default."""
    from mimir import ops_dashboard
    import asyncio

    captured_args: list[tuple[str, ...]] = []

    class _FakeProc:
        returncode = 0
        async def communicate(self):
            return (b"[]", b"")

    async def fake_exec(*args, **kwargs):
        captured_args.append(args)
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await ops_dashboard._load_chainlink_issues(tmp_path)
    assert len(captured_args) == 1
    assert "--status" in captured_args[0]
    status_idx = captured_args[0].index("--status")
    assert captured_args[0][status_idx + 1] == "open"


@pytest.mark.asyncio
async def test_chainlink_helper_drains_pipes_after_timeout_kill(tmp_path: Path, monkeypatch):
    """Mimir review item on PR #62: after a timeout the helper kills
    the subprocess but must also drain stdout/stderr so file
    descriptors release immediately. Without the drain, a hung
    chainlink CLI under heavy /ops traffic could accumulate FDs.

    Verified by counting communicate() calls — once for the
    initial wait_for that times out, once for the post-kill drain."""
    from mimir import ops_dashboard
    import asyncio

    communicate_calls = 0
    kill_called = False

    class _FakeProc:
        returncode = None

        async def communicate(self):
            nonlocal communicate_calls
            communicate_calls += 1
            # First call: hang past the timeout.
            # Second call (post-kill): return empty and complete fast.
            if communicate_calls == 1:
                await asyncio.sleep(60)
            return (b"", b"")

        def kill(self):
            nonlocal kill_called
            kill_called = True

    async def fake_exec(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(ops_dashboard, "_CHAINLINK_TIMEOUT_SECONDS", 0.05)

    result = await ops_dashboard._load_chainlink_issues(tmp_path)
    assert result["available"] is False
    assert "timed out" in result["error"]
    # Both the timed-out wait_for and the post-kill drain ran.
    assert communicate_calls == 2
    assert kill_called is True


@pytest.mark.asyncio
async def test_chainlink_helper_handles_garbled_json(tmp_path: Path, monkeypatch):
    """If chainlink succeeds but emits invalid JSON the helper still
    soft-fails. Defensive: spec drift in the CLI output shouldn't
    break the dashboard."""
    from mimir import ops_dashboard
    import asyncio

    class _FakeProc:
        returncode = 0
        async def communicate(self):
            return (b"this is not json", b"")

    async def fake_exec(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = await ops_dashboard._load_chainlink_issues(tmp_path)
    assert result["available"] is False
    assert "chainlink output" in result["error"]


@pytest.mark.asyncio
async def test_chainlink_helper_success_path(tmp_path: Path, monkeypatch):
    """Happy path: returncode 0, valid JSON list of issues — wrapped
    in an envelope with available=True and the issues passed through."""
    from mimir import ops_dashboard
    import asyncio

    issues_payload = [
        {
            "id": 23,
            "title": "chainlink #23 — saga MCP context resolution",
            "description": "...",
            "status": "open",
            "priority": "high",
            "parent_id": None,
            "created_at": "2026-05-06T12:00:00Z",
            "updated_at": "2026-05-07T18:00:00Z",
            "closed_at": None,
        },
        {
            "id": 24,
            "title": "subissue #24 — context lookups",
            "status": "open",
            "priority": "medium",
            "parent_id": 23,
            "created_at": "2026-05-06T13:00:00Z",
            "updated_at": "2026-05-07T15:00:00Z",
            "closed_at": None,
        },
    ]

    class _FakeProc:
        returncode = 0
        async def communicate(self):
            return (json.dumps(issues_payload).encode("utf-8"), b"")

    async def fake_exec(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = await ops_dashboard._load_chainlink_issues(tmp_path)
    assert result["available"] is True
    assert result["error"] is None
    assert len(result["issues"]) == 2
    assert result["issues"][0]["id"] == 23
    assert result["truncated"] is False
    assert result["total_count"] == 2


@pytest.mark.asyncio
async def test_chainlink_helper_caps_at_max(tmp_path: Path, monkeypatch):
    """A deployment with thousands of issues should not blow the
    payload — cap at _CHAINLINK_MAX_ISSUES (200) and mark truncated."""
    from mimir import ops_dashboard
    import asyncio

    big_payload = [
        {"id": i, "title": f"issue {i}", "status": "open", "priority": "low"}
        for i in range(500)
    ]

    class _FakeProc:
        returncode = 0
        async def communicate(self):
            return (json.dumps(big_payload).encode("utf-8"), b"")

    async def fake_exec(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    result = await ops_dashboard._load_chainlink_issues(tmp_path)
    assert result["available"] is True
    assert len(result["issues"]) == 200
    assert result["truncated"] is True
    assert result["total_count"] == 500


# ─── async wrapper integration ───────────────────────────────────────


@pytest.mark.asyncio
async def test_build_dashboard_payload_async_includes_chainlink(tmp_path: Path, monkeypatch):
    """The async wrapper attaches chainlink_issues when home is given."""
    from mimir import ops_dashboard
    import asyncio

    log = tmp_path / "events.jsonl"
    _write_events(log, [
        {"timestamp": _ts(0.1), "type": "event_queued", "trigger": "user_message"},
    ])

    class _FakeProc:
        returncode = 0
        async def communicate(self):
            return (b'[{"id": 1, "title": "test", "status": "open"}]', b"")

    async def fake_exec(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    payload = await ops_dashboard.build_dashboard_payload_async(
        log, days=7, home=tmp_path,
    )
    assert payload["summary"]["events_queued"] == 1
    assert payload["chainlink_issues"]["available"] is True
    assert payload["chainlink_issues"]["issues"][0]["id"] == 1


@pytest.mark.asyncio
async def test_build_dashboard_payload_async_home_none_skips_chainlink(tmp_path: Path):
    """When home is None the async wrapper still runs (sync work +
    an empty chainlink envelope). Used by callers that don't want
    the subprocess overhead (e.g. tests, future flag-off path)."""
    from mimir import ops_dashboard
    log = tmp_path / "events.jsonl"
    _write_events(log, [
        {"timestamp": _ts(0.1), "type": "event_queued", "trigger": "user_message"},
    ])
    payload = await ops_dashboard.build_dashboard_payload_async(log, days=7, home=None)
    assert payload["summary"]["events_queued"] == 1
    assert payload["chainlink_issues"]["available"] is False


# ─── /ops route renders chainlink data ───────────────────────────────


@pytest.mark.asyncio
async def test_route_ops_renders_chainlink_unavailable_when_home_unset(tmp_path: Path, aiohttp_client):
    """When home is None on register_routes (e.g. unit-test setup),
    the dashboard renders successfully with chainlink unavailable."""
    app = web.Application()
    register_routes(
        app, turns_log=tmp_path / "turns.jsonl", events_log=tmp_path / "events.jsonl",
        # home omitted → None
    )
    client = await aiohttp_client(app)
    resp = await client.get("/api/ops")
    assert resp.status == 200
    body = await resp.json()
    assert body["chainlink_issues"]["available"] is False
