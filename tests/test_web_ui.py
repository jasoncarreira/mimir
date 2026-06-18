"""Turn viewer + log API routes (SPEC §11)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from mimir import web_ui
from mimir.web_contracts import (
    render_typescript_contracts,
    validate_api_envelope,
    validate_list_meta,
)


@pytest.fixture
def app(tmp_path: Path) -> tuple[web.Application, Path, Path]:
    turns_log = tmp_path / "turns.jsonl"
    events_log = tmp_path / "events.jsonl"
    a = web.Application()
    web_ui.register_routes(a, turns_log=turns_log, events_log=events_log)
    return a, turns_log, events_log


def test_generated_typescript_contracts_are_current():
    generated = Path("frontend/src/api/generated/contracts.ts").read_text(
        encoding="utf-8"
    )
    assert generated == render_typescript_contracts()


@pytest.mark.asyncio
async def test_turns_page_serves_html(app):
    a, _, _ = app
    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/turns")
        assert resp.status == 200
        assert resp.content_type == "text/html"
        body = await resp.text()
        assert "mimir turns" in body  # header title (renamed from "Turn Viewer")
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
async def test_api_v1_turns_returns_envelope_and_list_metadata(app):
    a, turns_log, _ = app
    rows = [{"turn_id": f"t{i}"} for i in range(5)]
    turns_log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/v1/turns?limit=2")
        body = await resp.json()

    assert resp.status == 200
    validate_api_envelope(body, expect_ok=True)
    validate_list_meta(body["meta"])
    assert [t["turn_id"] for t in body["data"]["turns"]] == ["t3", "t4"]
    assert body["meta"] == {
        "cursor": "t4",
        "limit": 2,
        "total": 5,
        "truncated": True,
    }


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
async def test_api_turns_limit_returns_newest_page(app):
    """Progressive loading: ?limit=N returns the newest N turns (file tail)."""
    a, turns_log, _ = app
    rows = [{"turn_id": f"t{i}"} for i in range(5)]
    turns_log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/turns?limit=2")
        body = await resp.json()
    # Newest 2 (file is oldest-first; tail is t3, t4).
    assert [t["turn_id"] for t in body["turns"]] == ["t3", "t4"]


@pytest.mark.asyncio
async def test_api_turns_before_returns_older_page(app):
    """Progressive loading: ?before=<id>&limit=N returns up to N turns
    immediately OLDER than the cursor (scroll-back page)."""
    a, turns_log, _ = app
    rows = [{"turn_id": f"t{i}"} for i in range(6)]  # t0..t5
    turns_log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    async with TestClient(TestServer(a)) as client:
        # Two turns older than t4 -> t2, t3.
        resp = await client.get("/api/turns?before=t4&limit=2")
        body = await resp.json()
        # Unknown cursor -> empty (treated as "no older page").
        resp2 = await client.get("/api/turns?before=nope&limit=2")
        body2 = await resp2.json()
    assert [t["turn_id"] for t in body["turns"]] == ["t2", "t3"]
    assert body2["turns"] == []


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
async def test_api_v1_events_returns_envelope_and_list_metadata(app):
    a, _, events_log = app
    rows = [
        {"timestamp": "2026-01-01T00:00:00Z", "type": "turn_started"},
        {"timestamp": "2026-01-01T00:00:01Z", "type": "tool_call"},
        {"timestamp": "2026-01-01T00:00:02Z", "type": "tool_call"},
    ]
    events_log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/v1/events?type=tool_call&limit=1")
        body = await resp.json()

    assert resp.status == 200
    validate_api_envelope(body, expect_ok=True)
    assert [e["timestamp"] for e in body["data"]["events"]] == ["2026-01-01T00:00:02Z"]
    assert body["meta"] == {
        "cursor": "2026-01-01T00:00:02Z",
        "limit": 1,
        "total": 2,
        "truncated": True,
    }


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


@pytest.mark.asyncio
async def test_react_app_serves_built_index_and_assets(tmp_path: Path):
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text(
        '<div id="root"></div><script src="/app/assets/app.js"></script>',
        encoding="utf-8",
    )
    (assets / "app.js").write_text("console.log('mimir app')", encoding="utf-8")

    a = web.Application()
    web_ui.register_routes(
        a,
        turns_log=tmp_path / "t.jsonl",
        events_log=tmp_path / "e.jsonl",
        react_app_dist=dist,
    )

    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/app")
        assert resp.status == 200
        assert resp.content_type == "text/html"
        assert resp.headers["Cache-Control"].startswith("no-store")
        assert "/app/assets/app.js" in await resp.text()

        asset_resp = await client.get("/app/assets/app.js")
        assert asset_resp.status == 200
        assert await asset_resp.text() == "console.log('mimir app')"

        fallback_resp = await client.get("/app/turns/42")
        assert fallback_resp.status == 200
        assert fallback_resp.headers["Cache-Control"].startswith("no-store")
        assert "/app/assets/app.js" in await fallback_resp.text()


@pytest.mark.asyncio
async def test_web_bootstrap_is_no_store_and_secret_free(tmp_path: Path):
    class _Config:
        web_host = "0.0.0.0"

    a = web.Application()
    a["api_key"] = "super-secret"
    a["config"] = _Config()
    web_ui.register_routes(
        a,
        turns_log=tmp_path / "t.jsonl",
        events_log=tmp_path / "e.jsonl",
        react_app_dist=tmp_path / "missing-dist",
    )

    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/web/bootstrap")
        body_text = await resp.text()
        body = json.loads(body_text)

    assert resp.status == 200
    assert resp.headers["Cache-Control"].startswith("no-store")
    assert "super-secret" not in body_text
    assert body["auth"]["required"] is True
    assert body["server"]["public_bind"] is True
    assert body["stream_auth"]["shape"] == "fetch-event-stream"
    assert body["stream_auth"]["native_eventsource_supported_when_auth_required"] is False


@pytest.mark.asyncio
async def test_api_v1_web_bootstrap_is_enveloped_no_store_and_secret_free(tmp_path: Path):
    class _Config:
        web_host = "0.0.0.0"

    a = web.Application()
    a["api_key"] = "super-secret"
    a["config"] = _Config()
    web_ui.register_routes(
        a,
        turns_log=tmp_path / "t.jsonl",
        events_log=tmp_path / "e.jsonl",
        react_app_dist=tmp_path / "missing-dist",
    )

    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/v1/web/bootstrap")
        body_text = await resp.text()
        body = json.loads(body_text)

    assert resp.status == 200
    assert resp.headers["Cache-Control"].startswith("no-store")
    assert "super-secret" not in body_text
    validate_api_envelope(body, expect_ok=True)
    assert body["data"]["auth"]["required"] is True
    assert body["data"]["server"]["public_bind"] is True


@pytest.mark.asyncio
async def test_api_v1_web_bootstrap_auth_exempt_with_middleware(tmp_path: Path):
    from mimir.server import _make_auth_middleware

    class _Config:
        web_host = "0.0.0.0"

    a = web.Application(middlewares=[_make_auth_middleware("super-secret")])
    a["api_key"] = "super-secret"
    a["config"] = _Config()
    web_ui.register_routes(
        a,
        turns_log=tmp_path / "t.jsonl",
        events_log=tmp_path / "e.jsonl",
        react_app_dist=tmp_path / "missing-dist",
    )

    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/v1/web/bootstrap")
        body = await resp.json()

    assert resp.status == 200
    validate_api_envelope(body, expect_ok=True)
    assert body["data"]["auth"]["required"] is True


@pytest.mark.asyncio
async def test_api_v1_ops_errors_use_stable_envelope(app):
    a, _, _ = app
    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/v1/ops?days=bad")
        body = await resp.json()

    assert resp.status == 400
    validate_api_envelope(body, expect_ok=False)
    assert body["error"]["code"] == "invalid_days"


@pytest.mark.asyncio
async def test_shared_web_auth_script_served_no_store(tmp_path: Path):
    a = web.Application()
    web_ui.register_routes(
        a,
        turns_log=tmp_path / "t.jsonl",
        events_log=tmp_path / "e.jsonl",
        react_app_dist=tmp_path / "missing-dist",
    )

    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/app/auth.js")
        body = await resp.text()

    assert resp.status == 200
    assert resp.headers["Cache-Control"].startswith("no-store")
    assert "window.MimirAuth" in body
    assert "api_key=" not in body


@pytest.mark.asyncio
async def test_react_app_missing_build_returns_503(tmp_path: Path):
    a = web.Application()
    web_ui.register_routes(
        a,
        turns_log=tmp_path / "t.jsonl",
        events_log=tmp_path / "e.jsonl",
        react_app_dist=tmp_path / "missing-dist",
    )

    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/app")
        assert resp.status == 503
        assert "npm run build" in await resp.text()


def _make_min_saga_db(path: Path) -> None:
    """Minimal saga DB with just the tables build_db_stats_payload reads."""
    import sqlite3
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE atoms (id TEXT, tombstoned INTEGER DEFAULT 0);"
        "CREATE TABLE sessions (id TEXT);"
        "CREATE TABLE triples (id TEXT, tombstoned INTEGER DEFAULT 0);"
        "CREATE TABLE schema_version (version INTEGER);"
        "INSERT INTO atoms (id, tombstoned) VALUES ('a1', 0);"
        "INSERT INTO schema_version (version) VALUES (1);"
    )
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_saga_db_fallback_uses_dot_mimir_not_state(tmp_path: Path):
    """Regression (saga page 'db not found or unreadable'): with home set
    and no explicit saga_db, the /saga dashboard must read
    <home>/.mimir/saga.db (saga's canonical default), not the stale
    <home>/state/saga.db that no longer exists."""
    _make_min_saga_db(tmp_path / ".mimir" / "saga.db")
    a = web.Application()
    web_ui.register_routes(
        a, turns_log=tmp_path / "t.jsonl", events_log=tmp_path / "e.jsonl",
        home=tmp_path,  # no saga_db → exercises the fallback
    )
    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/saga?view=stats")
        assert resp.status == 200
        payload = await resp.json()
        assert payload.get("ready") is True, payload
        assert payload["db_path"].replace("\\", "/").endswith("/.mimir/saga.db"), \
            payload["db_path"]


@pytest.mark.asyncio
async def test_saga_db_explicit_kwarg_wins(tmp_path: Path):
    """server.py passes the saga.toml-resolved path as saga_db=; it must
    take precedence over the home-derived fallback."""
    db = tmp_path / "custom" / "saga.db"
    _make_min_saga_db(db)
    a = web.Application()
    web_ui.register_routes(
        a, turns_log=tmp_path / "t.jsonl", events_log=tmp_path / "e.jsonl",
        home=tmp_path, saga_db=db,
    )
    async with TestClient(TestServer(a)) as client:
        resp = await client.get("/api/saga?view=stats")
        payload = await resp.json()
        assert payload.get("ready") is True, payload
        assert payload["db_path"].replace("\\", "/").endswith("/custom/saga.db"), \
            payload["db_path"]
