"""Startup warning claims checked against real HTTP auth and route handlers."""

from __future__ import annotations

import ast
import inspect
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from mimir import server
from mimir.bridges.web_chat import WebChatBridge


# Each concrete claim must appear in BOTH emitted warnings and hold over HTTP.
_ACCESS_CLAIMS = [
    ("POST", "/event", "user_message", 200, "POST /event (user_message): 200 when queued"),
    ("POST", "/event", "saga_end_session", 403, "POST /event (saga_end_session): 403"),
    ("GET", "/api/ops", None, 401, "GET /api/ops: 401"),
    ("GET", "/api/memory", None, 401, "GET /api/memory: 401"),
    ("POST", "/chat", None, 401, "POST /chat: 401"),
    ("GET", "/chat/stream", None, 401, "GET /chat/stream: 401"),
]


def _emit_build_app_warnings(*, api_key="", allow_unauthenticated=False):
    """Run the two actual startup blocks without booting unrelated services.

    Compile the owned source, rather than duplicating its logging conditions.
    HTTP tests below exercise production middleware and handlers separately.
    """
    tree = ast.parse(inspect.getsource(server.build_app))
    blocks = [
        node for node in tree.body[0].body
        if isinstance(node, ast.If) and ast.unparse(node.test) == "not config.api_key"
    ]
    assert len(blocks) == 2
    namespace = {
        **vars(server),
        "config": SimpleNamespace(
            api_key=api_key, allow_unauthenticated=allow_unauthenticated,
        ),
    }
    exec(compile(ast.Module(body=blocks, type_ignores=[]), server.__file__, "exec"), namespace)


def _route_app(tmp_path, *, api_key=""):
    app = web.Application(middlewares=[
        server._make_auth_middleware(api_key, web_host="127.0.0.1"),
    ])
    dispatcher = SimpleNamespace(enqueue=AsyncMock(return_value=True))
    app["dispatcher"] = dispatcher
    app.router.add_post("/event", server._handle_event)
    server.web_ui.register_routes(
        app, turns_log=tmp_path / "turns.jsonl",
        events_log=tmp_path / "events.jsonl", home=tmp_path,
    )
    bridge = WebChatBridge(enqueue=dispatcher.enqueue, home=tmp_path)
    bridge.register_routes(app)
    return app, dispatcher, bridge


@pytest.mark.parametrize("method,path,trigger,status,claim", _ACCESS_CLAIMS)
async def test_warning_claim_matches_route(
    tmp_path, caplog, method, path, trigger, status, claim,
):
    with caplog.at_level(logging.WARNING, logger="mimir.server"):
        _emit_build_app_warnings()
    warnings = [r.getMessage() for r in caplog.records if r.name == "mimir.server"]
    assert len(warnings) == 2
    for warning in warnings:
        assert claim in warning
        assert "no per-user web keys configured or previously loaded" in warning
        assert "not an authentication boundary" in warning
        assert "local processes can still inject messages" in warning
        assert "every route accepts unauthenticated" not in warning

    app, dispatcher, bridge = _route_app(tmp_path)
    async with TestClient(TestServer(app)) as client:
        response = await client.request(method, path, json={
            "channel_id": "test", "content": "hello", "trigger": trigger,
        })
        assert response.status == status
        body = await response.json()
        if status == 200:
            assert body["ok"] is True
            dispatcher.enqueue.assert_awaited_once()
            assert dispatcher.enqueue.call_args.args[0].trigger == "user_message"
        else:
            assert "error" in body
            dispatcher.enqueue.assert_not_awaited()
        assert not bridge._subscribers


@pytest.mark.parametrize("api_key,allow_unauthenticated,levels", [
    ("", False, [logging.WARNING, logging.WARNING]),
    ("", True, [logging.DEBUG, logging.WARNING]),
    ("secret", False, []),
    ("secret", True, []),
])
def test_actual_startup_warning_levels(caplog, api_key, allow_unauthenticated, levels):
    with caplog.at_level(logging.DEBUG, logger="mimir.server"):
        _emit_build_app_warnings(
            api_key=api_key, allow_unauthenticated=allow_unauthenticated,
        )
    records = [r for r in caplog.records if r.name == "mimir.server"]
    assert [r.levelno for r in records] == levels


@pytest.mark.parametrize("api_key,headers,trigger,status,error", [
    ("secret", {}, "user_message", 401, "unauthorized"),
    ("secret", {"X-API-Key": "wrong"}, "user_message", 401, "unauthorized"),
    ("secret", {"X-API-Key": "secret"}, "saga_end_session", 200, None),
    ("", {"X-API-Key": "invented"}, "saga_end_session", 403,
     "trigger not permitted for non-admin HTTP callers"),
    ("", {"Origin": "https://attacker.example"}, "user_message", 403,
     "cross_site_request"),
    ("", {"Sec-Fetch-Site": "cross-site"}, "user_message", 403,
     "cross_site_request"),
    ("", {"Host": "attacker.example"}, "user_message", 403, "invalid_host"),
])
async def test_warning_boundary_controls(tmp_path, api_key, headers, trigger, status, error):
    app, dispatcher, _ = _route_app(tmp_path, api_key=api_key)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/event", headers=headers, json={
            "channel_id": "test", "content": "hello", "trigger": trigger,
        })
        assert response.status == status
        body = await response.json()
        if error is not None:
            assert body["error"] == error
            dispatcher.enqueue.assert_not_awaited()
        else:
            assert body["ok"] is True
            dispatcher.enqueue.assert_awaited_once()
            assert dispatcher.enqueue.call_args.args[0].trigger == trigger
