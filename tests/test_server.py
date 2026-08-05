"""Dedicated coverage for mimir/server.py (chainlink #247, slice 3/5).

Pins behaviour of the components that aren't covered by the three
existing focused files (test_server_auth_warning, test_server_bind_security,
test_server_consolidate):

- ``_safe_str_eq``         — constant-time string comparison helper
- ``_make_auth_middleware`` — key-header gate + exempt-route bypass
- ``_AUTH_EXEMPT``         — correct set membership
- ``_MaskApiKeyInAccessLog`` — access-log filter redaction
- ``_handle_health``       — liveness endpoint
- ``_handle_event``        — event injection endpoint (valid + error paths)

All tests exercise these units without standing up the full ``build_app``
wiring (dispatcher, scheduler, saga, bridges) — each test builds a
minimal ``aiohttp.web.Application`` with only the routes and state
needed to prove the behaviour under test.
"""
from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from mimir.server import (
    _AUTH_EXEMPT,
    _AUTH_EXEMPT_PREFIXES,
    _MaskApiKeyInAccessLog,
    _is_auth_exempt,
    _make_auth_middleware,
    _safe_str_eq,
    _start_mcp_servers,
    _handle_health,
    _handle_event,
    _handle_root,
)


def _production_call_sites(call_name: str) -> set[str]:
    root = Path(__file__).resolve().parent.parent
    sites: set[str] = set()
    for path in (root / "mimir").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if isinstance(function, ast.Name) and function.id == call_name:
                sites.add(path.relative_to(root).as_posix())
            elif isinstance(function, ast.Attribute) and function.attr == call_name:
                sites.add(path.relative_to(root).as_posix())
    return sites


def test_runtime_is_only_production_agent_constructor() -> None:
    assert _production_call_sites("Agent") == {"mimir/runtime.py"}


def test_runtime_is_only_production_global_buffer_writer() -> None:
    assert _production_call_sites("set_global_buffer") == {"mimir/runtime.py"}


def test_runtime_is_only_production_mcp_tools_writer() -> None:
    assert _production_call_sites("set_mcp_tools") == {"mimir/runtime.py"}


def test_production_global_writers_are_confined() -> None:
    root = Path(__file__).resolve().parent.parent
    setter_names = {
        "set_indexer",
        "set_index_generator",
        "set_turns_log_path",
        "set_channel_registry",
        "set_identity_resolver",
        "set_dispatcher",
        "set_scheduler",
        "set_commitments_store",
        "set_spawn_config",
        "set_shell_job_registry",
    }
    sites: set[str] = set()
    for path in (root / "mimir").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"tools", "web_tools"}
                and node.func.attr in setter_names | {"set_home"}
            ):
                sites.add(path.relative_to(root).as_posix())
    assert sites == {"mimir/runtime.py"}

    server_tree = ast.parse(
        (root / "mimir" / "server.py").read_text(encoding="utf-8")
    )
    module_assignments = [
        node
        for node in server_tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
    ]
    assert not any(
        target.id == "_STARTUP_BACKGROUND_TASKS"
        for node in module_assignments
        for target in (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
        )
        if isinstance(target, ast.Name)
    )


# ──────────────────────────────────────────────────────────────────────────────
# MCP production startup wiring
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_mcp_servers_returns_tools_and_policy_attention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace
    from types import SimpleNamespace

    from mimir.mcp_client import MCPProvenance, MCPServerConfig

    config = MCPServerConfig(
        name="demo",
        command="demo-server",
        args=[],
        server_config_id="demo-production",
        policy_version="policy-v2",
    )
    provenance = replace(
        MCPProvenance.create(
            config,
            "read_item",
            {},
            server_config_id=config.server_config_id,
        ),
        classification="resource_scoped",
        adapter_name="demo-owner",
        adapter_version="adapter-v1",
        approval_version="approval-v1",
        policy_version="policy-v1",
    )
    tool = SimpleNamespace(name="mcp_demo_read_item", mcp_provenance=provenance)
    manager = MagicMock()
    manager.start_servers = AsyncMock(return_value=[tool])
    manager.shutdown = AsyncMock()
    events: list[tuple[str, dict[str, Any]]] = []

    async def capture(kind: str, **fields: Any) -> None:
        events.append((kind, fields))

    monkeypatch.setattr("mimir.server.log_event", capture)
    returned_manager, tools = await _start_mcp_servers(manager, [config])

    manager.start_servers.assert_awaited_once_with([config])
    assert tools == [tool]
    assert returned_manager is manager
    assert events[0] == (
        "mcp_servers_ready",
        {"count": 1, "tool_names": ["mcp_demo_read_item"]},
    )
    attention = next(fields for kind, fields in events if kind == "mcp_policy_attention_required")
    assert attention["count"] >= 1
    assert any(issue.get("actual_policy") == "policy-v1" for issue in attention["issues"])


@pytest.mark.asyncio
async def test_start_mcp_servers_failure_shuts_down_and_returns_no_manager() -> None:
    manager = MagicMock()
    manager.start_servers = AsyncMock(side_effect=RuntimeError("start failed"))
    manager.shutdown = AsyncMock()

    returned_manager, tools = await _start_mcp_servers(manager, [object()])

    assert returned_manager is None
    assert tools == []
    manager.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_mcp_servers_retains_manager_when_failure_shutdown_fails() -> None:
    manager = MagicMock()
    manager.start_servers = AsyncMock(side_effect=RuntimeError("start failed"))
    manager.shutdown = AsyncMock(side_effect=RuntimeError("shutdown failed"))

    returned_manager, tools = await _start_mcp_servers(manager, [object()])

    assert returned_manager is manager
    assert tools == []


def test_runtime_field_proxies_delegate_and_fail_closed() -> None:
    from types import SimpleNamespace

    from mimir.server import _RuntimeFieldProxy, _RuntimeSlot

    slot = _RuntimeSlot()
    proxy = _RuntimeFieldProxy(slot, "turn_event_bus")
    with pytest.raises(RuntimeError, match="agent runtime is not initialized"):
        proxy.subscribe("channel")

    bus = SimpleNamespace(subscribe=lambda channel: f"queue:{channel}")
    slot.bundle = SimpleNamespace(turn_event_bus=bus)
    assert proxy.subscribe("channel") == "queue:channel"


@pytest.mark.asyncio
async def test_pairing_notifier_aclose_is_idempotent_and_clears_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from mimir.server import _PairingNotifier

    monkeypatch.setattr("mimir.server.log_event", AsyncMock())
    channels = MagicMock()
    channels.send = AsyncMock()
    config = SimpleNamespace(
        operator_alert_channel="ops",
        pairing_operator_digest_delay_seconds=60.0,
        pairing_dm_auto_reply_enabled=True,
        pairing_dm_auto_reply_interval_seconds=60.0,
        pairing_dm_auto_reply_text="pending",
        pairing_pending_max=100,
    )
    notifier = _PairingNotifier(config, channels)
    await notifier.notify_operator(
        canonical="alice",
        display="Alice",
        platform="slack",
        channel_id="dm-alice",
        delivery="dm",
    )
    await notifier.maybe_reply_dm(canonical="alice", dm_channel_id="dm-alice")
    await asyncio.sleep(0)
    operator_task = notifier._operator_task
    dm_task = notifier._dm_reply_task

    await notifier.aclose()
    await notifier.aclose()

    assert notifier._operator_task is None
    assert notifier._dm_reply_task is None
    assert notifier._operator_pending == []
    assert notifier._dm_reply_queue.empty()
    assert operator_task is not None and operator_task.cancelled()
    assert dm_task is not None and dm_task.done()


# ──────────────────────────────────────────────────────────────────────────────
# _safe_str_eq
# ──────────────────────────────────────────────────────────────────────────────


class TestSafeStrEq:
    def test_equal_strings_returns_true(self) -> None:
        assert _safe_str_eq("abc123", "abc123") is True

    def test_unequal_strings_returns_false(self) -> None:
        assert _safe_str_eq("abc123", "abc124") is False

    def test_empty_equal(self) -> None:
        assert _safe_str_eq("", "") is True

    def test_empty_vs_non_empty(self) -> None:
        assert _safe_str_eq("", "x") is False

    def test_different_length(self) -> None:
        assert _safe_str_eq("short", "much-longer-string") is False

    def test_unicode_equal(self) -> None:
        assert _safe_str_eq("kéy-🔑", "kéy-🔑") is True

    def test_unicode_unequal(self) -> None:
        assert _safe_str_eq("kéy-🔑", "key-🔑") is False


# ──────────────────────────────────────────────────────────────────────────────
# _AUTH_EXEMPT
# ──────────────────────────────────────────────────────────────────────────────


class TestAuthExemptSet:
    def test_health_get_is_exempt(self) -> None:
        assert ("GET", "/health") in _AUTH_EXEMPT

    def test_react_app_get_is_exempt(self) -> None:
        assert ("GET", "/app") in _AUTH_EXEMPT

    def test_browser_auth_bootstrap_is_exempt(self) -> None:
        assert ("GET", "/app/auth.js") in _AUTH_EXEMPT
        assert ("GET", "/api/web/bootstrap") in _AUTH_EXEMPT
        assert ("GET", "/api/v1/web/bootstrap") in _AUTH_EXEMPT

    def test_skill_auto_update_event_reports_failures_without_drift(self) -> None:
        from mimir.server import _skill_auto_update_event
        from mimir.skill_install import AutoSkillUpdateResult

        event = _skill_auto_update_event(AutoSkillUpdateResult(
            failed={"github-poller": ["poller.py"]},
        ))

        assert event is not None
        kind, fields = event
        assert kind == "skills_auto_update_failed"
        assert fields["failed"] == {"github-poller": ["poller.py"]}

    def test_skill_auto_update_event_reports_remaining_drift_as_non_failed(self) -> None:
        from mimir.server import _skill_auto_update_event
        from mimir.skill_install import AutoSkillUpdateResult

        event = _skill_auto_update_event(AutoSkillUpdateResult(
            remaining_drift={"github-poller": {"extra": ["local-note.md"]}},
        ))

        assert event is not None
        kind, fields = event
        assert kind == "skills_auto_update"
        assert fields["remaining_drift"] == {
            "github-poller": {"extra": ["local-note.md"]}
        }

    def test_react_assets_get_are_prefix_exempt(self) -> None:
        assert ("GET", "/app/") in _AUTH_EXEMPT_PREFIXES
        assert _is_auth_exempt("GET", "/app/assets/index.js") is True

    def test_turns_get_is_exempt(self) -> None:
        assert ("GET", "/turns") in _AUTH_EXEMPT

    def test_ops_get_is_exempt(self) -> None:
        assert ("GET", "/ops") in _AUTH_EXEMPT

    def test_saga_get_is_exempt(self) -> None:
        assert ("GET", "/saga") in _AUTH_EXEMPT

    def test_state_get_is_exempt(self) -> None:
        assert ("GET", "/state") in _AUTH_EXEMPT  # renamed from /memory

    def test_root_get_is_exempt(self) -> None:
        assert ("GET", "/") in _AUTH_EXEMPT

    def test_event_post_is_not_exempt(self) -> None:
        assert ("POST", "/event") not in _AUTH_EXEMPT

    def test_health_post_is_not_exempt(self) -> None:
        """A hypothetical POST /health must NOT inherit the GET exemption."""
        assert ("POST", "/health") not in _AUTH_EXEMPT

    def test_turns_post_is_not_exempt(self) -> None:
        assert ("POST", "/turns") not in _AUTH_EXEMPT

    def test_react_prefix_does_not_exempt_post(self) -> None:
        assert _is_auth_exempt("POST", "/app/assets/index.js") is False

    def test_is_frozenset_of_tuples(self) -> None:
        assert isinstance(_AUTH_EXEMPT, frozenset)
        for item in _AUTH_EXEMPT:
            assert isinstance(item, tuple)
            assert len(item) == 2


# ──────────────────────────────────────────────────────────────────────────────
# _MaskApiKeyInAccessLog
# ──────────────────────────────────────────────────────────────────────────────


class TestMaskApiKeyInAccessLog:
    def _make_record(self, msg: Any, args: tuple = ()) -> logging.LogRecord:
        record = logging.LogRecord(
            name="aiohttp.access",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=msg,
            args=args,
            exc_info=None,
        )
        return record

    def test_masks_query_param_in_msg(self) -> None:
        filt = _MaskApiKeyInAccessLog()
        record = self._make_record("GET /?api_key=supersecret HTTP/1.1")
        result = filt.filter(record)
        assert result is True
        assert "supersecret" not in record.msg
        assert "REDACTED" in record.msg

    def test_masks_mid_query_string(self) -> None:
        filt = _MaskApiKeyInAccessLog()
        record = self._make_record("/path?foo=bar&api_key=s3cr3t&baz=qux")
        filt.filter(record)
        assert "s3cr3t" not in record.msg
        assert "REDACTED" in record.msg
        # Other params survive
        assert "foo=bar" in record.msg
        assert "baz=qux" in record.msg

    def test_masks_in_args_tuple(self) -> None:
        filt = _MaskApiKeyInAccessLog()
        record = self._make_record(
            "method=%s url=%s",
            ("GET", "/?api_key=exposed"),
        )
        filt.filter(record)
        for arg in record.args:
            assert "exposed" not in str(arg)

    def test_non_string_args_left_alone(self) -> None:
        """Non-string elements in args (numbers, None) are passed through."""
        filt = _MaskApiKeyInAccessLog()
        record = self._make_record("code=%s time=%s", (200, 0.001))
        filt.filter(record)
        assert record.args == (200, 0.001)

    def test_non_string_msg_not_crashed(self) -> None:
        """A non-string msg (aiohttp may pass structured objects) must not crash."""
        filt = _MaskApiKeyInAccessLog()
        record = self._make_record(42)  # int msg
        result = filt.filter(record)
        assert result is True
        assert record.msg == 42  # untouched

    def test_clean_record_unchanged(self) -> None:
        filt = _MaskApiKeyInAccessLog()
        record = self._make_record("GET /health HTTP/1.1")
        filt.filter(record)
        assert record.msg == "GET /health HTTP/1.1"

    def test_case_insensitive_match(self) -> None:
        filt = _MaskApiKeyInAccessLog()
        record = self._make_record("GET /?API_KEY=secret HTTP/1.1")
        filt.filter(record)
        assert "secret" not in record.msg

    def test_always_returns_true(self) -> None:
        """filter() must return True to keep the record in the log stream."""
        filt = _MaskApiKeyInAccessLog()
        record = self._make_record("irrelevant")
        assert filt.filter(record) is True


# ──────────────────────────────────────────────────────────────────────────────
# _handle_health
# ──────────────────────────────────────────────────────────────────────────────


def _health_app() -> web.Application:
    """Minimal app with only the /health route."""
    app = web.Application()
    app.router.add_get("/health", _handle_health)
    return app


def _root_app() -> web.Application:
    """Minimal app with only the / route."""
    app = web.Application()
    app.router.add_get("/", _handle_root)
    return app


class TestRootRedirect:
    async def test_root_redirects_to_react_app(self) -> None:
        async with TestClient(TestServer(_root_app())) as client:
            resp = await client.get("/", allow_redirects=False)
            assert resp.status == 302
            assert resp.headers["Location"] == "/app"


class TestHandleHealth:
    @pytest.mark.asyncio
    async def test_health_returns_200(self) -> None:
        async with TestClient(TestServer(_health_app())) as client:
            resp = await client.get("/health")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_health_returns_ok_true(self) -> None:
        async with TestClient(TestServer(_health_app())) as client:
            resp = await client.get("/health")
            body = await resp.json()
        assert body == {"ok": True}

    @pytest.mark.asyncio
    async def test_health_is_json(self) -> None:
        async with TestClient(TestServer(_health_app())) as client:
            resp = await client.get("/health")
        assert "application/json" in resp.headers.get("Content-Type", "")


# ──────────────────────────────────────────────────────────────────────────────
# _make_auth_middleware
# ──────────────────────────────────────────────────────────────────────────────


async def _ok_handler(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def _auth_app(expected_key: str) -> web.Application:
    """Minimal app wiring the auth middleware around a simple route."""
    app = web.Application(middlewares=[_make_auth_middleware(expected_key)])
    app.router.add_get("/protected", _ok_handler)
    app.router.add_post("/protected", _ok_handler)
    # Register all exempt paths so we can hit them in tests
    app.router.add_get("/health", _handle_health)
    app.router.add_get("/turns", _ok_handler)
    app.router.add_get("/ops", _ok_handler)
    app.router.add_get("/saga", _ok_handler)
    app.router.add_get("/state", _ok_handler)
    app.router.add_get("/api/web/bootstrap", _ok_handler)
    app.router.add_get("/api/v1/web/bootstrap", _ok_handler)
    return app


class TestAuthMiddlewareNoKey:
    """When no key is configured the middleware is a no-op pass-through."""

    @pytest.mark.asyncio
    async def test_no_key_allows_any_request(self) -> None:
        async with TestClient(TestServer(_auth_app(""))) as client:
            resp = await client.get("/protected")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_no_key_allows_without_header(self) -> None:
        async with TestClient(TestServer(_auth_app(""))) as client:
            resp = await client.get("/protected")
        assert resp.status == 200


class TestAuthMiddlewareWithKey:
    """When a key IS configured the middleware gates every non-exempt route."""

    @pytest.mark.asyncio
    async def test_correct_header_key_passes(self) -> None:
        async with TestClient(TestServer(_auth_app("my-secret"))) as client:
            resp = await client.get(
                "/protected", headers={"X-API-Key": "my-secret"}
            )
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_wrong_header_key_rejected(self) -> None:
        async with TestClient(TestServer(_auth_app("my-secret"))) as client:
            resp = await client.get(
                "/protected", headers={"X-API-Key": "wrong"}
            )
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_missing_key_header_rejected(self) -> None:
        async with TestClient(TestServer(_auth_app("my-secret"))) as client:
            resp = await client.get("/protected")
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_401_body_has_error_field(self) -> None:
        async with TestClient(TestServer(_auth_app("my-secret"))) as client:
            resp = await client.get("/protected")
            body = await resp.json()
        assert body.get("error") == "unauthorized"

    @pytest.mark.asyncio
    async def test_query_param_api_key_is_rejected(self) -> None:
        """API keys in URLs are rejected; browsers use header-based fetch."""
        async with TestClient(TestServer(_auth_app("my-secret"))) as client:
            resp = await client.get("/protected?api_key=my-secret")
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_query_param_wrong_key_is_rejected(self) -> None:
        async with TestClient(TestServer(_auth_app("my-secret"))) as client:
            resp = await client.get("/protected?api_key=nope")
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_header_takes_precedence_over_query_when_both_present(
        self,
    ) -> None:
        """Header auth still works if a non-auth query string is present."""
        async with TestClient(TestServer(_auth_app("my-secret"))) as client:
            resp = await client.get(
                "/protected?api_key=garbage",
                headers={"X-API-Key": "my-secret"},
            )
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_empty_header_does_not_fall_back_to_query(self) -> None:
        async with TestClient(TestServer(_auth_app("my-secret"))) as client:
            resp = await client.get(
                "/protected?api_key=my-secret",
                headers={"X-API-Key": ""},
            )
        assert resp.status == 401


class TestAuthMiddlewareExemptRoutes:
    """_AUTH_EXEMPT routes bypass the gate even when a key is configured."""

    @pytest.mark.asyncio
    async def test_health_is_exempt(self) -> None:
        async with TestClient(TestServer(_auth_app("secret"))) as client:
            resp = await client.get("/health")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_turns_is_exempt(self) -> None:
        async with TestClient(TestServer(_auth_app("secret"))) as client:
            resp = await client.get("/turns")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_ops_is_exempt(self) -> None:
        async with TestClient(TestServer(_auth_app("secret"))) as client:
            resp = await client.get("/ops")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_saga_is_exempt(self) -> None:
        async with TestClient(TestServer(_auth_app("secret"))) as client:
            resp = await client.get("/saga")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_state_is_exempt(self) -> None:
        async with TestClient(TestServer(_auth_app("secret"))) as client:
            resp = await client.get("/state")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_v1_web_bootstrap_is_exempt(self) -> None:
        async with TestClient(TestServer(_auth_app("secret"))) as client:
            resp = await client.get("/api/v1/web/bootstrap")
        assert resp.status == 200


# ──────────────────────────────────────────────────────────────────────────────
# _handle_event
# ──────────────────────────────────────────────────────────────────────────────


def _event_app(
    *,
    enqueue_returns: bool = True,
) -> tuple[web.Application, MagicMock]:
    """Minimal app wiring the /event route with a stub dispatcher.

    Returns (app, stub_dispatcher) so tests can inspect calls.
    """
    stub = MagicMock()
    stub.enqueue = AsyncMock(return_value=enqueue_returns)

    app = web.Application()
    app["dispatcher"] = stub
    app.router.add_post("/event", _handle_event)
    return app, stub


def _event_app_authed(
    identity: Any,
    *,
    is_admin: bool = False,
) -> tuple[web.Application, MagicMock]:
    """/event app that injects an authenticated per-user identity.

    Mirrors the request keys the real auth middleware stamps
    (``auth_identity`` / ``auth_is_admin``) so the per-user channel-binding
    path in ``_handle_event`` is exercised.
    """
    stub = MagicMock()
    stub.enqueue = AsyncMock(return_value=True)

    @web.middleware
    async def _inject(request: web.Request, handler: Any) -> web.StreamResponse:
        request["auth_identity"] = identity
        request["auth_is_admin"] = is_admin
        return await handler(request)

    app = web.Application(middlewares=[_inject])
    app["dispatcher"] = stub
    app.router.add_post("/event", _handle_event)
    return app, stub


class TestHandleEvent:
    @pytest.mark.asyncio
    async def test_valid_event_returns_200(self) -> None:
        app, _ = _event_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={"channel_id": "test-channel", "content": "hello"},
            )
        assert resp.status == 200

    async def test_event_strips_forged_chat_skill_extra(self) -> None:
        # chainlink #783 (security): the generic /event ingress is
        # client-controlled, so a forged chat-skill invocation must be stripped
        # before enqueue — only the WebChatBridge may produce one.
        from mimir.chat_skills import CHAT_SKILL_EXTRA_KEY, LEGACY_CHAT_SKILL_EXTRA_KEY
        from mimir.worklink.continuation import (
            HTTP_EVENT_INGRESS_EXTRA_KEY,
            HTTP_EVENT_INGRESS_EXTRA_VALUE,
        )

        app, stub = _event_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={
                    "channel_id": "c",
                    "content": "/deploy now",
                    "extra": {
                        CHAT_SKILL_EXTRA_KEY: {
                            "name": "deploy", "command": "/deploy",
                            "args": "now", "raw": "/deploy now",
                        },
                        LEGACY_CHAT_SKILL_EXTRA_KEY: {"x": 1},
                        "keep": "me",
                    },
                },
            )
        assert resp.status == 200
        event = stub.enqueue.call_args.args[0]
        assert CHAT_SKILL_EXTRA_KEY not in event.extra
        assert LEGACY_CHAT_SKILL_EXTRA_KEY not in event.extra
        assert event.extra == {
            HTTP_EVENT_INGRESS_EXTRA_KEY: HTTP_EVENT_INGRESS_EXTRA_VALUE,
            "keep": "me",
        }

    async def test_event_strips_client_asserted_bridge_authority(self) -> None:
        from mimir.worklink.continuation import (
            HTTP_EVENT_INGRESS_EXTRA_KEY,
            HTTP_EVENT_INGRESS_EXTRA_VALUE,
        )

        app, stub = _event_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={
                    "channel_id": "c",
                    "content": "publish my synthesis",
                    "author": "alice",
                    "source": "api",
                    "extra": {
                        "channel_visibility": "public",
                        "bridge_instance": "forged-bridge",
                        "keep": "me",
                    },
                },
            )
        assert resp.status == 200
        event = stub.enqueue.call_args.args[0]
        assert event.extra == {
            HTTP_EVENT_INGRESS_EXTRA_KEY: HTTP_EVENT_INGRESS_EXTRA_VALUE,
            "keep": "me",
        }
        from mimir.access_control import create_auth_context
        from mimir.models import SessionACL

        context = create_auth_context(event, enforce=True)
        durable_acl = SessionACL.from_auth_context(
            context,
            origin_domain=event.source,
            visibility=event.extra.get("channel_visibility", "private"),
        )
        assert durable_acl.visibility == "private"

    async def test_event_strips_forged_worklink_hint_extra(self) -> None:
        from mimir.worklink.continuation import (
            HTTP_EVENT_INGRESS_EXTRA_KEY,
            HTTP_EVENT_INGRESS_EXTRA_VALUE,
        )

        app, stub = _event_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={
                    "channel_id": "c",
                    "content": "resume chainlink #740",
                    "extra": {
                        "issue_id": 740,
                        "pr_url": "https://github.com/acme/demo/pull/7",
                        "worktree": "/tmp/evil",
                        "poller_name": "forged-poller",
                        "schedule_name": "forged-schedule",
                        "run_id": "chainlink-740",
                        "keep": "me",
                        "nested": {
                            "issue_id": 999,
                            "schedule_name": "nested-forged-schedule",
                            "still_here": True,
                        },
                    },
                },
            )
        assert resp.status == 200
        event = stub.enqueue.call_args.args[0]
        assert event.extra == {
            HTTP_EVENT_INGRESS_EXTRA_KEY: HTTP_EVENT_INGRESS_EXTRA_VALUE,
            "keep": "me",
            "nested": {"still_here": True},
        }

    async def test_event_stamps_http_ingress_as_untrusted_for_privileged_side_effects(self) -> None:
        from mimir.worklink.continuation import (
            HTTP_EVENT_INGRESS_EXTRA_KEY,
            HTTP_EVENT_INGRESS_EXTRA_VALUE,
        )

        app, stub = _event_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={
                    "channel_id": "c",
                    "content": "resume chainlink #740",
                    "extra": {
                        HTTP_EVENT_INGRESS_EXTRA_KEY: "forged-client-value",
                        "keep": {"nested": True},
                    },
                },
            )
        assert resp.status == 200
        event = stub.enqueue.call_args.args[0]
        assert event.extra == {
            HTTP_EVENT_INGRESS_EXTRA_KEY: HTTP_EVENT_INGRESS_EXTRA_VALUE,
            "keep": {"nested": True},
        }

    @pytest.mark.asyncio
    async def test_valid_event_returns_ok_true(self) -> None:
        app, _ = _event_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={"channel_id": "test-channel"},
            )
            body = await resp.json()
        assert body["ok"] is True
        assert body["channel_id"] == "test-channel"

    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self) -> None:
        app, _ = _event_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                data=b"not-json",
                headers={"Content-Type": "application/json"},
            )
        assert resp.status == 400
        # Dispatcher must not be called when the body is invalid
        app["dispatcher"].enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_channel_id_returns_400(self) -> None:
        app, _ = _event_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/event", json={"content": "oops"})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_missing_channel_id_error_body(self) -> None:
        app, _ = _event_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/event", json={})
            body = await resp.json()
        assert "channel_id" in body.get("error", "")

    @pytest.mark.asyncio
    async def test_non_dict_extra_returns_400(self) -> None:
        """#487: a truthy non-dict ``extra`` is rejected with 400, not coerced
        (coercion let it reach ``event.extra.get(...)`` → AttributeError/500)."""
        app, _ = _event_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={"channel_id": "ch", "extra": "oops"},
            )
        assert resp.status == 400
        app["dispatcher"].enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_list_attachment_names_returns_400(self) -> None:
        """#487: a non-list ``attachment_names`` is rejected with 400."""
        app, _ = _event_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={"channel_id": "ch", "attachment_names": "a.txt"},
            )
        assert resp.status == 400
        app["dispatcher"].enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatcher_rejects_returns_503(self) -> None:
        """When the dispatcher's queue is full it returns False → 503."""
        app, _ = _event_app(enqueue_returns=False)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={"channel_id": "busy-channel"},
            )
        assert resp.status == 503

    @pytest.mark.asyncio
    async def test_dispatcher_rejects_body_has_channel_id(self) -> None:
        app, _ = _event_app(enqueue_returns=False)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={"channel_id": "busy-channel"},
            )
            body = await resp.json()
        assert body.get("channel_id") == "busy-channel"

    @pytest.mark.asyncio
    async def test_non_admin_key_cannot_target_another_users_channel(self) -> None:
        # Security: the request-body channel_id is spoofable. A per-user web key
        # must not be able to run a turn on another user's channel (which would
        # pull that user's private history into context / steer its egress).
        from types import SimpleNamespace

        from mimir.web_channels import web_channel_for_identity

        bob = SimpleNamespace(canonical="bob", display_name="Bob")
        app, stub = _event_app_authed(bob, is_admin=False)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={
                    "channel_id": web_channel_for_identity("alice"),
                    "content": "read alice's history",
                },
            )
        assert resp.status == 403
        stub.enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_admin_key_may_target_own_channel(self) -> None:
        from types import SimpleNamespace

        from mimir.web_channels import web_channel_for_identity

        bob = SimpleNamespace(canonical="bob", display_name="Bob")
        own = web_channel_for_identity("bob")
        app, stub = _event_app_authed(bob, is_admin=False)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={"channel_id": own, "content": "hi"},
            )
        assert resp.status == 200
        event = stub.enqueue.call_args.args[0]
        assert event.channel_id == own
        assert event.author == "bob"

    @pytest.mark.asyncio
    async def test_admin_key_may_target_any_channel(self) -> None:
        # Admins (and the master key, which has identity=None) target any
        # channel for operator/automation use.
        from types import SimpleNamespace

        admin = SimpleNamespace(canonical="op", display_name="Op")
        app, stub = _event_app_authed(admin, is_admin=True)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                json={"channel_id": "web-alice", "content": "ops broadcast"},
            )
        assert resp.status == 200
        stub.enqueue.assert_called_once()

    @pytest.mark.asyncio
    async def test_trigger_defaults_to_user_message(self) -> None:
        """A body with no ``trigger`` field should default to ``user_message``."""
        app, stub = _event_app()
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/event",
                json={"channel_id": "ch"},
            )
        call_args = stub.enqueue.call_args
        event = call_args.args[0]
        assert event.trigger == "user_message"

    @pytest.mark.asyncio
    async def test_explicit_trigger_forwarded(self) -> None:
        app, stub = _event_app()
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/event",
                json={"channel_id": "ch", "trigger": "scheduled_tick"},
            )
        event = stub.enqueue.call_args.args[0]
        assert event.trigger == "scheduled_tick"

    @pytest.mark.asyncio
    async def test_content_forwarded(self) -> None:
        app, stub = _event_app()
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/event",
                json={"channel_id": "ch", "content": "ping"},
            )
        event = stub.enqueue.call_args.args[0]
        assert event.content == "ping"

    @pytest.mark.asyncio
    async def test_author_forwarded(self) -> None:
        app, stub = _event_app()
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/event",
                json={"channel_id": "ch", "author": "alice", "author_id": "u123"},
            )
        event = stub.enqueue.call_args.args[0]
        assert event.author == "alice"
        assert event.author_id == "u123"

    @pytest.mark.asyncio
    async def test_per_user_key_ignores_spoofed_admin_and_service_authority(
        self, tmp_path,
    ) -> None:
        from mimir.access_control import create_auth_context
        from mimir.identities import IdentityResolver
        from mimir.identities_populator import issue_web_key
        from mimir.web_channels import web_channel_for_identity

        user_key = issue_web_key(tmp_path, "alice", roles=["user"])
        issue_web_key(tmp_path, "operator", roles=["admin"])
        resolver = IdentityResolver(tmp_path)
        resolver.reload()

        stub = MagicMock()
        stub.enqueue = AsyncMock(return_value=True)
        app = web.Application(middlewares=[_make_auth_middleware("master-secret")])
        app["dispatcher"] = stub
        app["identity_resolver"] = resolver
        app.router.add_post("/event", _handle_event)

        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/event",
                headers={"X-API-Key": user_key},
                json={
                    # A non-admin per-user key may only target its own channel;
                    # the spoofed author/service_principal below must still be
                    # stripped regardless.
                    "channel_id": web_channel_for_identity("alice"),
                    "author": "operator",
                    "author_id": "operator",
                    "trigger": "scheduled_tick",
                    "source": "api",
                    "service_principal": "scheduler",
                },
            )

        assert resp.status == 200
        event = stub.enqueue.call_args.args[0]
        assert event.author == "alice"
        assert event.author_id == "alice"
        assert event.service_principal is None
        auth_context = create_auth_context(event, resolver, enforce=True)
        assert auth_context.roles == ("user",)
        assert auth_context.is_service is False

    @pytest.mark.asyncio
    async def test_empty_body_returns_400(self) -> None:
        """A totally empty JSON object has no channel_id → 400."""
        app, _ = _event_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/event", json={})
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_event_routed_through_auth_middleware(self) -> None:
        """POST /event is NOT in _AUTH_EXEMPT → gated when a key is configured."""
        stub = MagicMock()
        stub.enqueue = AsyncMock(return_value=True)

        app = web.Application(middlewares=[_make_auth_middleware("gatekey")])
        app["dispatcher"] = stub
        app.router.add_post("/event", _handle_event)

        async with TestClient(TestServer(app)) as client:
            # No key → 401
            resp = await client.post(
                "/event", json={"channel_id": "ch"}
            )
        assert resp.status == 401
        stub.enqueue.assert_not_called()

    @pytest.mark.asyncio
    async def test_source_forwarded_but_http_ingress_marker_added(self) -> None:
        """chainlink #890: client-supplied source is forwarded but the HTTP
        ingress marker is added so the dispatcher knows it's untrusted."""
        from mimir.worklink.continuation import (
            HTTP_EVENT_INGRESS_EXTRA_KEY,
            HTTP_EVENT_INGRESS_EXTRA_VALUE,
        )

        app, stub = _event_app()
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/event",
                json={"channel_id": "ch", "source": "api", "author": "unknown"},
            )
        event = stub.enqueue.call_args.args[0]
        assert event.source == "api"
        assert event.extra.get(HTTP_EVENT_INGRESS_EXTRA_KEY) == HTTP_EVENT_INGRESS_EXTRA_VALUE
