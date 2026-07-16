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

import logging
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
    _handle_health,
    _handle_event,
    _handle_root,
)


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
