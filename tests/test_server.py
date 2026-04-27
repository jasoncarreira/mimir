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
    async def _fake_query(*, prompt, options, transport=None):
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
