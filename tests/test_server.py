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
