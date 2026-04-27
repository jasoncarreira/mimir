"""MSAM HTTP client (SPEC §5.6). Uses an aiohttp test app — no real MSAM."""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp import web

from mimir.msam_client import MsamClient, MsamError


@pytest.fixture
async def msam_app(aiohttp_server):
    received: list[dict[str, Any]] = []

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def query(request: web.Request) -> web.Response:
        body = await request.json()
        received.append({"path": "/v1/query", "body": body, "headers": dict(request.headers)})
        return web.json_response(
            {
                "_raw_atoms": [
                    {"id": "a1", "stream": "semantic", "content": "alpha"},
                    {"id": "a2", "stream": "episodic", "content": "beta"},
                ]
            }
        )

    async def feedback(request: web.Request) -> web.Response:
        body = await request.json()
        received.append({"path": "/v1/feedback", "body": body})
        return web.json_response({"ok": True})

    async def end_session(request: web.Request) -> web.Response:
        body = await request.json()
        received.append({"path": "/v1/sessions/end", "body": body})
        return web.json_response({"atom_id": "atom-boundary-1", "session_id": body["session_id"]})

    async def boom(request: web.Request) -> web.Response:
        return web.Response(status=500, text="kaboom")

    app = web.Application()
    app.router.add_get("/v1/health", health)
    app.router.add_post("/v1/query", query)
    app.router.add_post("/v1/feedback", feedback)
    app.router.add_post("/v1/sessions/end", end_session)
    app.router.add_post("/v1/consolidate", boom)
    server = await aiohttp_server(app)
    return server, received


@pytest.mark.asyncio
async def test_health_reports_up(msam_app):
    server, _ = msam_app
    client = MsamClient(endpoint=str(server.make_url("/")).rstrip("/"))
    try:
        assert await client.health() is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_query_passes_session_id(msam_app):
    server, received = msam_app
    client = MsamClient(endpoint=str(server.make_url("/")).rstrip("/"))
    try:
        out = await client.query("hello", top_k=3, session_id="msam-x-1")
    finally:
        await client.close()

    assert "_raw_atoms" in out
    assert received[0]["path"] == "/v1/query"
    assert received[0]["body"]["session_id"] == "msam-x-1"
    assert received[0]["body"]["top_k"] == 3


@pytest.mark.asyncio
async def test_api_key_added_as_header(msam_app):
    server, received = msam_app
    client = MsamClient(
        endpoint=str(server.make_url("/")).rstrip("/"),
        api_key="secret-token",
    )
    try:
        await client.query("ping")
    finally:
        await client.close()
    headers = received[0]["headers"]
    assert headers.get("X-API-Key") == "secret-token"


@pytest.mark.asyncio
async def test_feedback_includes_session_id(msam_app):
    server, received = msam_app
    client = MsamClient(endpoint=str(server.make_url("/")).rstrip("/"))
    try:
        await client.feedback(
            ["a1", "a2"], "the response text", session_id="msam-x-1"
        )
    finally:
        await client.close()
    body = received[0]["body"]
    assert body["atom_ids"] == ["a1", "a2"]
    assert body["session_id"] == "msam-x-1"


@pytest.mark.asyncio
async def test_end_session_round_trip(msam_app):
    server, received = msam_app
    client = MsamClient(endpoint=str(server.make_url("/")).rstrip("/"))
    try:
        out = await client.end_session(
            "msam-x-1",
            "we discussed quantum",
            topics_discussed=["quantum"],
            decisions_made=None,
            unfinished=["follow up on entanglement"],
            emotional_state="curious",
        )
    finally:
        await client.close()
    assert out["session_id"] == "msam-x-1"
    body = received[0]["body"]
    assert body["topics_discussed"] == ["quantum"]
    assert body["unfinished"] == ["follow up on entanglement"]
    assert "decisions_made" not in body  # None drops on the wire


@pytest.mark.asyncio
async def test_500_response_raises_msam_error(msam_app):
    server, _ = msam_app
    client = MsamClient(endpoint=str(server.make_url("/")).rstrip("/"))
    try:
        with pytest.raises(MsamError) as exc_info:
            await client.consolidate()
    finally:
        await client.close()
    assert exc_info.value.status == 500
    assert "kaboom" in (exc_info.value.body or "")


@pytest.mark.asyncio
async def test_query_clamps_long_input(msam_app):
    """SQLite FTS5 caps expression depth at 1000; a probe with several hundred
    distinct tokens crashes MSAM's keyword path. Client truncates upstream."""
    server, received = msam_app
    client = MsamClient(endpoint=str(server.make_url("/")).rstrip("/"))
    big_query = " ".join(f"token{i}" for i in range(500))
    try:
        await client.query(big_query, top_k=5)
    finally:
        await client.close()
    sent = received[0]["body"]["query"]
    sent_tokens = sent.split()
    assert len(sent_tokens) <= 64, f"expected ≤64 tokens, got {len(sent_tokens)}"
    # First tokens preserved (clamping is head-truncate, not random sampling).
    assert sent_tokens[0] == "token0"


def test_clamp_query_short_input_unchanged():
    from mimir.msam_client import _clamp_query

    assert _clamp_query("just a few words") == "just a few words"
    assert _clamp_query("") == ""


def test_clamp_query_handles_no_whitespace():
    from mimir.msam_client import _clamp_query, _MAX_QUERY_CHARS

    huge = "x" * 5000
    out = _clamp_query(huge)
    assert len(out) <= _MAX_QUERY_CHARS
