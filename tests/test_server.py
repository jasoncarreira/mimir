"""End-to-end smoke test of the HTTP surface with a mocked SDK driver."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from claude_agent_sdk import AssistantMessage, TextBlock

from mimir import server as mimir_server
from mimir.config import Config


async def _fake_query_factory(reply_text: str):
    async def _fake_query(*, prompt, options, session_id="default", transport=None):
        yield AssistantMessage(content=[TextBlock(text=reply_text)], model="claude-opus-4-7")
    return _fake_query


@pytest.mark.asyncio
async def test_event_drives_full_pipeline(tmp_path: Path):
    # Minimal config — point everything at tmp.
    os.environ.update({
        "MIMIR_HOME": str(tmp_path),
        "ANTHROPIC_API_KEY": "test-key",
    })
    cfg = Config.from_env()
    cfg = replace(cfg, home=tmp_path, max_concurrent_turns=2, worker_idle_timeout_s=1)

    fake = await _fake_query_factory("hello back")

    with patch("mimir.agent.query", new=fake):
        # Build the app the way main() would, but with our patched query.
        app = mimir_server.build_app(cfg)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={
                    "trigger": "user_message",
                    "channel_id": "bench-1",
                    "content": "hello",
                    "author": "tester",
                },
            )
            assert resp.status == 200

            # Drain the dispatcher so the turn definitely lands on disk.
            await app["dispatcher"].drain()

    # Verify turns.jsonl
    turns_lines = [
        json.loads(l) for l in (tmp_path / "logs" / "turns.jsonl").read_text().splitlines()
    ]
    assert len(turns_lines) == 1
    rec = turns_lines[0]
    assert rec["channel_id"] == "bench-1"
    assert rec["trigger"] == "user_message"
    assert rec["output"] == "hello back"
    assert rec["error"] is None

    # Verify events.jsonl has the expected milestones
    event_types = [
        json.loads(l)["type"]
        for l in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()
    ]
    assert "app_started" in event_types
    assert "event_queued" in event_types
    assert "turn_started" in event_types
    assert "turn_finished" in event_types


@pytest.mark.asyncio
async def test_event_endpoint_validates_channel_id(tmp_path: Path):
    cfg = Config.from_env()
    cfg = replace(cfg, home=tmp_path)
    fake = await _fake_query_factory("noop")
    with patch("mimir.agent.query", new=fake):
        app = mimir_server.build_app(cfg)
        async with TestClient(TestServer(app)) as client:
            r = await client.post("/event", json={"content": "hi"})
            assert r.status == 400
            r = await client.get("/health")
            assert r.status == 200
            data = await r.json()
            assert data == {"ok": True}
            await app["dispatcher"].drain()


@pytest.mark.asyncio
async def test_event_endpoint_requires_api_key_when_configured(tmp_path: Path):
    cfg = Config.from_env()
    cfg = replace(cfg, home=tmp_path, api_key="secret-token")
    fake = await _fake_query_factory("noop")
    with patch("mimir.agent.query", new=fake):
        app = mimir_server.build_app(cfg)
        async with TestClient(TestServer(app)) as client:
            # No header → 401, body never parsed.
            r = await client.post("/event", json={"channel_id": "bench-1", "content": "x"})
            assert r.status == 401

            # Wrong header → 401.
            r = await client.post(
                "/event",
                json={"channel_id": "bench-1", "content": "x"},
                headers={"X-API-Key": "wrong"},
            )
            assert r.status == 401

            # Right header → goes through (validates channel etc. as normal).
            r = await client.post(
                "/event",
                json={"channel_id": "bench-1", "content": "x"},
                headers={"X-API-Key": "secret-token"},
            )
            assert r.status == 200

            # /health stays open regardless of auth state.
            r = await client.get("/health")
            assert r.status == 200

            await app["dispatcher"].drain()


@pytest.mark.asyncio
async def test_event_endpoint_open_when_api_key_unset(tmp_path: Path):
    """Default config has empty api_key — auth disabled, requests pass.
    This is the dev / localhost-only mode."""
    cfg = Config.from_env()
    cfg = replace(cfg, home=tmp_path, api_key="")
    fake = await _fake_query_factory("noop")
    with patch("mimir.agent.query", new=fake):
        app = mimir_server.build_app(cfg)
        async with TestClient(TestServer(app)) as client:
            r = await client.post(
                "/event",
                json={"channel_id": "bench-1", "content": "x"},
            )
            assert r.status == 200
            await app["dispatcher"].drain()


# ─── Pattern B: middleware-level auth on every non-exempt route ───────


@pytest.mark.asyncio
async def test_auth_middleware_gates_api_routes_when_key_set(tmp_path: Path):
    """Pattern B (CR2-#2): every JSON / data route requires X-API-Key
    when MIMIR_API_KEY is set. Pre-2026-05-10 only POST /event was
    gated; /api/turns, /api/events, /api/ops, /chat were all open.
    Now the gate is at the app middleware so new routes inherit
    protection by default."""
    cfg = Config.from_env()
    cfg = replace(cfg, home=tmp_path, api_key="secret-token")
    fake = await _fake_query_factory("noop")
    with patch("mimir.agent.query", new=fake):
        app = mimir_server.build_app(cfg)
        async with TestClient(TestServer(app)) as client:
            # Each of these returns 401 without auth, 200 with.
            for path in ("/api/turns", "/api/events", "/api/ops"):
                r = await client.get(path)
                assert r.status == 401, f"{path} should require auth"
                r = await client.get(
                    path, headers={"X-API-Key": "secret-token"},
                )
                assert r.status == 200, f"{path} with key should pass"

            # /chat POST requires auth.
            r = await client.post("/chat", json={"content": "hi"})
            assert r.status == 401
            r = await client.post(
                "/chat", json={"content": "hi"},
                headers={"X-API-Key": "secret-token"},
            )
            assert r.status == 200

            await app["dispatcher"].drain()


@pytest.mark.asyncio
async def test_auth_middleware_exempts_html_shells_and_health(tmp_path: Path):
    """The HTML shells (/turns, /ops) AND /health are exempt — the
    shells contain JS that prompts for an API key and uses it for
    subsequent /api/* fetches; health is for orchestrators that
    poll without credentials."""
    cfg = Config.from_env()
    cfg = replace(cfg, home=tmp_path, api_key="secret-token")
    fake = await _fake_query_factory("noop")
    with patch("mimir.agent.query", new=fake):
        app = mimir_server.build_app(cfg)
        async with TestClient(TestServer(app)) as client:
            for path in ("/turns", "/ops", "/health"):
                r = await client.get(path)
                assert r.status == 200, f"{path} should be exempt"

            await app["dispatcher"].drain()


@pytest.mark.asyncio
async def test_auth_middleware_accepts_query_param_fallback(tmp_path: Path):
    """SSE / EventSource clients can't set custom headers natively.
    The middleware accepts ``?api_key=`` as a fallback so /chat/stream
    and similar SSE endpoints can be gated. Header is preferred when
    both are present (header wins on equal validity)."""
    cfg = Config.from_env()
    cfg = replace(cfg, home=tmp_path, api_key="secret-token")
    fake = await _fake_query_factory("noop")
    with patch("mimir.agent.query", new=fake):
        app = mimir_server.build_app(cfg)
        async with TestClient(TestServer(app)) as client:
            # No auth at all → 401.
            r = await client.get("/api/ops")
            assert r.status == 401
            # ?api_key= → 200.
            r = await client.get("/api/ops?api_key=secret-token")
            assert r.status == 200
            # Wrong query value → 401.
            r = await client.get("/api/ops?api_key=wrong")
            assert r.status == 401

            await app["dispatcher"].drain()


@pytest.mark.asyncio
async def test_auth_middleware_open_when_api_key_unset(tmp_path: Path):
    """When MIMIR_API_KEY is unset, the middleware passes through
    unconditionally — every route stays open. Operator gets a startup
    warning explaining the unsafe state."""
    cfg = Config.from_env()
    cfg = replace(cfg, home=tmp_path, api_key="")
    fake = await _fake_query_factory("noop")
    with patch("mimir.agent.query", new=fake):
        app = mimir_server.build_app(cfg)
        async with TestClient(TestServer(app)) as client:
            for path in ("/api/turns", "/api/events", "/api/ops"):
                r = await client.get(path)
                assert r.status == 200, (
                    f"{path} should pass when api_key unset"
                )
            await app["dispatcher"].drain()
