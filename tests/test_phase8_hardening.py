"""Phase 8 — hardening: ResultMessage capture, SAGA retry, error-path coverage.

These tests verify that mimir survives the things production-like deployments
actually break on:

- SDK ``ResultMessage`` capture surfaces token usage + cost + stop_reason on
  every TurnRecord (resume detection + cost monitoring).
- ``SagaClient`` retries 5xx + transient ClientError, gives up cleanly on 4xx.
- Malformed tool-use blocks (Minimax sometimes drops args) don't crash the
  turn — the TurnRecord still lands.
- ``query()`` exception (timeout, connection drop) lands as ``error`` on the
  TurnRecord without wedging the dispatcher worker.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from aiohttp import web
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from mimir import server as mimir_server
from mimir.config import Config
from mimir.saga_client import _HttpSaga as SagaClient, SagaError


# ---- ResultMessage capture in TurnRecord -------------------------------


async def _fake_query_with_result(*, prompt, options, session_id="default", transport=None):
    """Fake SDK stream that emits a text reply followed by a ResultMessage."""
    yield AssistantMessage(content=[TextBlock(text="hello back")], model="claude-opus-4-7")
    yield ResultMessage(
        subtype="success",
        duration_ms=1234,
        duration_api_ms=1100,
        is_error=False,
        num_turns=1,
        session_id="fake-session",
        stop_reason="end_turn",
        total_cost_usd=0.0042,
        usage={
            "input_tokens": 1500,
            "output_tokens": 50,
            "cache_creation_input_tokens": 800,
            "cache_read_input_tokens": 700,
        },
        result="hello back",
    )


@pytest.mark.asyncio
async def test_turn_record_captures_result_message(tmp_path: Path):
    """A successful turn writes the ResultMessage's subtype, cost, usage, and
    stop_reason into the turns.jsonl record."""
    import os
    os.environ.update({
        "MIMIR_HOME": str(tmp_path),
        "ANTHROPIC_API_KEY": "test-key",
    })
    cfg = Config.from_env()
    cfg = replace(cfg, home=tmp_path, max_concurrent_turns=2, worker_idle_timeout_s=1)

    with patch("mimir.agent.query", new=_fake_query_with_result):
        from aiohttp.test_utils import TestClient, TestServer

        app = mimir_server.build_app(cfg)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={"trigger": "user_message", "channel_id": "bench-1", "content": "hi"},
            )
            assert resp.status == 200
            await app["dispatcher"].drain()

    record = json.loads((tmp_path / "logs" / "turns.jsonl").read_text().splitlines()[0])
    assert record["result_subtype"] == "success"
    assert record["result_is_error"] is False
    assert record["stop_reason"] == "end_turn"
    assert record["num_turns"] == 1
    assert record["total_cost_usd"] == 0.0042
    assert record["usage"]["input_tokens"] == 1500
    assert record["usage"]["output_tokens"] == 50
    assert record["usage"]["cache_read_input_tokens"] == 700


@pytest.mark.asyncio
async def test_turn_record_when_no_result_message(tmp_path: Path):
    """When query() crashes before a ResultMessage is emitted, the TurnRecord
    still lands — with ``error`` set and result_* fields None."""
    import os
    os.environ.update({
        "MIMIR_HOME": str(tmp_path),
        "ANTHROPIC_API_KEY": "test-key",
    })
    cfg = Config.from_env()
    cfg = replace(cfg, home=tmp_path, max_concurrent_turns=2, worker_idle_timeout_s=1)

    async def crash_query(*, prompt, options, session_id="default", transport=None):
        if False:
            yield None  # make this an async generator
        raise asyncio.TimeoutError("simulated SDK timeout")

    with patch("mimir.agent.query", new=crash_query):
        from aiohttp.test_utils import TestClient, TestServer

        app = mimir_server.build_app(cfg)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={"trigger": "user_message", "channel_id": "bench-1", "content": "hi"},
            )
            assert resp.status == 200
            await app["dispatcher"].drain()

    record = json.loads((tmp_path / "logs" / "turns.jsonl").read_text().splitlines()[0])
    assert record["error"] is not None
    assert "TimeoutError" in record["error"]
    assert record["result_subtype"] is None
    assert record["total_cost_usd"] is None


@pytest.mark.asyncio
async def test_malformed_tool_use_block_does_not_crash_turn(tmp_path: Path):
    """Minimax sometimes returns ``ToolUseBlock`` with empty/None ``input``.
    The turn must still complete and produce a TurnRecord."""
    import os
    os.environ.update({
        "MIMIR_HOME": str(tmp_path),
        "ANTHROPIC_API_KEY": "test-key",
    })
    cfg = Config.from_env()
    cfg = replace(cfg, home=tmp_path, max_concurrent_turns=2, worker_idle_timeout_s=1)

    async def malformed_query(*, prompt, options, session_id="default", transport=None):
        # A ToolUseBlock with input={} — Minimax tool-arg drop.
        yield AssistantMessage(
            content=[
                TextBlock(text="thinking..."),
                ToolUseBlock(id="tu_bad", name="mcp__mimir__file_search", input={}),
            ],
            model="claude-opus-4-7",
        )
        yield AssistantMessage(content=[TextBlock(text="recovered reply")], model="claude-opus-4-7")
        yield ResultMessage(
            subtype="success",
            duration_ms=10,
            duration_api_ms=5,
            is_error=False,
            num_turns=2,
            session_id="x",
            stop_reason="end_turn",
            total_cost_usd=None,
            usage={"input_tokens": 100, "output_tokens": 5},
            result="recovered reply",
        )

    with patch("mimir.agent.query", new=malformed_query):
        from aiohttp.test_utils import TestClient, TestServer

        app = mimir_server.build_app(cfg)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={"trigger": "user_message", "channel_id": "bench-1", "content": "hi"},
            )
            assert resp.status == 200
            await app["dispatcher"].drain()

    record = json.loads((tmp_path / "logs" / "turns.jsonl").read_text().splitlines()[0])
    # The malformed tool_call lands in the events stream but the turn finished.
    tool_calls = [e for e in record["events"] if e["type"] == "tool_call"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["args"] == {}
    assert "recovered reply" in record["output"]


# ---- SagaClient retry-with-backoff -------------------------------------


@pytest.mark.asyncio
async def test_saga_client_retries_5xx_then_succeeds(aiohttp_server, monkeypatch):
    """A 503 followed by a 200 succeeds without surfacing the failure."""
    # Speed up the test by collapsing the retry delays.
    monkeypatch.setattr("mimir.saga_client._RETRY_DELAYS_S", (0.0, 0.0, 0.0))

    call_count = 0

    async def flaky(request: web.Request) -> web.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return web.Response(status=503, text="upstream restarting")
        return web.json_response({"ok": True, "attempts": call_count})

    app = web.Application()
    app.router.add_post("/v1/feedback", flaky)
    server = await aiohttp_server(app)
    client = SagaClient(endpoint=str(server.make_url("/")).rstrip("/"))
    try:
        result = await client.feedback(["a1"], "response text")
        assert result == {"ok": True, "attempts": 2}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_saga_client_does_not_retry_4xx(aiohttp_server, monkeypatch):
    """A 400 is permanent — no retries, fails on the first attempt."""
    monkeypatch.setattr("mimir.saga_client._RETRY_DELAYS_S", (0.0, 0.0, 0.0))

    call_count = 0

    async def bad_request(request: web.Request) -> web.Response:
        nonlocal call_count
        call_count += 1
        return web.Response(status=400, text="bad atom_id format")

    app = web.Application()
    app.router.add_post("/v1/outcome", bad_request)
    server = await aiohttp_server(app)
    client = SagaClient(endpoint=str(server.make_url("/")).rstrip("/"))
    try:
        with pytest.raises(SagaError) as exc_info:
            await client.outcome(["bad-id"], feedback="positive")
        assert exc_info.value.status == 400
        assert call_count == 1, "4xx must not be retried"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_saga_client_gives_up_after_max_retries(aiohttp_server, monkeypatch):
    """Persistent 503s exhaust the retry budget and raise SagaError."""
    monkeypatch.setattr("mimir.saga_client._RETRY_DELAYS_S", (0.0, 0.0, 0.0))

    call_count = 0

    async def always_500(request: web.Request) -> web.Response:
        nonlocal call_count
        call_count += 1
        return web.Response(status=503, text="never recovers")

    app = web.Application()
    app.router.add_post("/v1/query", always_500)
    server = await aiohttp_server(app)
    client = SagaClient(endpoint=str(server.make_url("/")).rstrip("/"))
    try:
        with pytest.raises(SagaError) as exc_info:
            await client.query("anything")
        assert exc_info.value.status == 503
        assert call_count == 4, f"expected 4 attempts (1 + 3 retries), got {call_count}"
    finally:
        await client.close()


def test_retry_delay_clamps_to_last_entry():
    """Regression: ``_MAX_RETRIES`` and ``_RETRY_DELAYS_S`` are loosely
    coupled — today they line up (3 retries, 3 delays) but a future
    tuner who bumps ``_MAX_RETRIES`` past ``len(_RETRY_DELAYS_S)``
    would hit an ``IndexError`` mid-retry. The ``_retry_delay`` helper
    clamps the lookup to the last valid entry so the call sites stay
    safe regardless of how the constants drift.
    """
    from mimir.saga_client import _RETRY_DELAYS_S, _retry_delay

    # In-bounds attempts return the matching delay.
    for i, expected in enumerate(_RETRY_DELAYS_S):
        assert _retry_delay(i) == expected, f"attempt {i} should map to {expected}"

    # Out-of-bounds attempts clamp to the last entry rather than
    # IndexErroring. Cover ``len(...)`` (one past the last index) and
    # an arbitrary larger value (a hypothetical _MAX_RETRIES = 10).
    last = _RETRY_DELAYS_S[-1]
    assert _retry_delay(len(_RETRY_DELAYS_S)) == last
    assert _retry_delay(10) == last
    assert _retry_delay(100) == last
