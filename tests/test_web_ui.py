"""Turn viewer + log API routes (SPEC §11)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from mimir import web_ui


@pytest.fixture
def app(tmp_path: Path) -> tuple[web.Application, Path, Path]:
    turns_log = tmp_path / "turns.jsonl"
    events_log = tmp_path / "events.jsonl"
    a = web.Application()
    web_ui.register_routes(a, turns_log=turns_log, events_log=events_log)
    return a, turns_log, events_log


@pytest.mark.asyncio
async def test_turns_page_serves_html(app):
    a, _, _ = app
    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/turns")
        assert resp.status == 200
        assert resp.content_type == "text/html"
        body = await resp.text()
        assert "Turn Viewer" in body
        assert "/api/turns" in body  # the page polls this endpoint


@pytest.mark.asyncio
async def test_api_turns_returns_records(app):
    a, turns_log, _ = app
    rows = [
        {"turn_id": "t1", "channel_id": "c1", "output": "hi"},
        {"turn_id": "t2", "channel_id": "c1", "output": "ok"},
    ]
    turns_log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/turns")
        body = await resp.json()
    assert [t["turn_id"] for t in body["turns"]] == ["t1", "t2"]


@pytest.mark.asyncio
async def test_api_turns_with_after_filter(app):
    a, turns_log, _ = app
    rows = [{"turn_id": f"t{i}"} for i in range(5)]
    turns_log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/turns?after=t2")
        body = await resp.json()
    # Strictly after t2 — t3, t4.
    assert [t["turn_id"] for t in body["turns"]] == ["t3", "t4"]


@pytest.mark.asyncio
async def test_api_turns_handles_missing_file(app):
    a, _, _ = app
    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/turns")
        body = await resp.json()
    assert body == {"turns": []}


@pytest.mark.asyncio
async def test_api_events_filters_by_type_and_limit(app):
    a, _, events_log = app
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "turn_started"},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "tool_call"},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "tool_call"},
        {"timestamp": "2026-01-01T00:00:03Z", "type": "turn_finished"},
    ]
    events_log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/events?type=tool_call")
        body = await resp.json()
        assert all(e["type"] == "tool_call" for e in body["events"])
        assert len(body["events"]) == 2

        # Multiple types via repeated query param.
        resp = await client.get("/api/events?type=turn_started&type=turn_finished")
        body = await resp.json()
        assert {e["type"] for e in body["events"]} == {"turn_started", "turn_finished"}

        # Comma-joined form should work too.
        resp = await client.get("/api/events?type=turn_started,turn_finished")
        body = await resp.json()
        assert {e["type"] for e in body["events"]} == {"turn_started", "turn_finished"}

        # Limit returns the tail.
        resp = await client.get("/api/events?limit=2")
        body = await resp.json()
        assert [e["type"] for e in body["events"]] == ["tool_call", "turn_finished"]

        # since= drops anything before the timestamp.
        resp = await client.get("/api/events?since=2026-01-01T00:00:02Z")
        body = await resp.json()
        assert [e["type"] for e in body["events"]] == ["tool_call", "turn_finished"]


@pytest.mark.asyncio
async def test_read_jsonl_caps_at_max_records(app):
    """Pattern A (2026-05-10): ``_read_jsonl`` is bounded by
    ``max_records`` (default 5000). Pre-2026-05-10 it forward-read
    the entire file synchronously per HTTP request — combined with
    the turn-viewer polling every 5s, the loop got pinned re-parsing
    hundreds of MB on a hot file. The cap means older records past
    the limit are silently dropped from the response."""
    from mimir.web_ui import _read_jsonl

    a, _, events_log = app
    # Write 50 records but cap at 10.
    rows = [{"i": i, "type": "x"} for i in range(50)]
    events_log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    out = _read_jsonl(events_log, max_records=10)
    # Output is chronological — most recent 10 records (i=40..49).
    assert [r["i"] for r in out] == list(range(40, 50))


@pytest.mark.asyncio
async def test_read_jsonl_under_cap_returns_all(app):
    """When the file has fewer records than the cap, all are returned
    in chronological order (no silent dropping)."""
    from mimir.web_ui import _read_jsonl

    a, _, events_log = app
    rows = [{"i": i, "type": "x"} for i in range(7)]
    events_log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    out = _read_jsonl(events_log, max_records=100)
    assert [r["i"] for r in out] == list(range(7))


@pytest.mark.asyncio
async def test_register_routes_is_idempotent(app):
    """Calling register_routes twice (e.g. server rebuild) doesn't crash."""
    a, turns_log, events_log = app
    web_ui.register_routes(a, turns_log=turns_log, events_log=events_log)
    # Should still work.
    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/health" if False else "/api/turns")
        assert resp.status == 200
