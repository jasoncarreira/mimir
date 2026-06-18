"""Per-user web auth + RBAC: middleware gate, /whoami, chat attribution (#726)."""

from __future__ import annotations

from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from mimir.bridges.web_chat import WebChatBridge
from mimir.identities import IdentityResolver
from mimir.identities_populator import issue_web_key
from mimir.server import _make_auth_middleware
from mimir.web_ui import _whoami_payload


async def _echo(request: web.Request) -> web.Response:
    ident = request.get("auth_identity")
    return web.json_response({
        "canonical": ident.canonical if ident else None,
        "is_master": bool(request.get("auth_is_master")),
        "is_admin": bool(request.get("auth_is_admin")),
    })


def _resolver(home: Path) -> IdentityResolver:
    r = IdentityResolver(home)
    r.reload()
    return r


def _app(home: Path, master_key: str) -> web.Application:
    app = web.Application(middlewares=[_make_auth_middleware(master_key)])
    app["identity_resolver"] = _resolver(home)
    app.router.add_get("/api/v1/turns", _echo)         # auth, non-admin
    app.router.add_get("/api/v1/admin/config", _echo)  # auth + admin
    app.router.add_get("/health", _echo)               # exempt
    return app


# ── middleware: the RBAC security gate ──────────────────────────────────


async def test_user_key_allowed_on_normal_route_but_403_on_admin(tmp_path: Path) -> None:
    user_key = issue_web_key(tmp_path, "alice", roles=["user"])
    async with TestClient(TestServer(_app(tmp_path, "master-secret"))) as c:
        r = await c.get("/api/v1/turns", headers={"X-API-Key": user_key})
        assert r.status == 200
        body = await r.json()
        assert body["canonical"] == "alice" and body["is_admin"] is False
        r2 = await c.get("/api/v1/admin/config", headers={"X-API-Key": user_key})
        assert r2.status == 403  # the RBAC boundary — server-side, not UI


async def test_admin_key_allowed_on_admin_route(tmp_path: Path) -> None:
    admin_key = issue_web_key(tmp_path, "ops", roles=["admin"])
    async with TestClient(TestServer(_app(tmp_path, "master-secret"))) as c:
        r = await c.get("/api/v1/admin/config", headers={"X-API-Key": admin_key})
        assert r.status == 200
        body = await r.json()
        assert body["canonical"] == "ops" and body["is_admin"] is True


async def test_master_key_is_admin_but_no_identity(tmp_path: Path) -> None:
    issue_web_key(tmp_path, "alice", roles=["user"])
    async with TestClient(TestServer(_app(tmp_path, "master-secret"))) as c:
        r = await c.get("/api/v1/admin/config", headers={"X-API-Key": "master-secret"})
        assert r.status == 200
        body = await r.json()
        assert body["is_master"] is True and body["is_admin"] is True
        assert body["canonical"] is None  # master key carries no user identity


async def test_unknown_and_missing_keys_rejected(tmp_path: Path) -> None:
    issue_web_key(tmp_path, "alice", roles=["user"])
    async with TestClient(TestServer(_app(tmp_path, "master-secret"))) as c:
        assert (await c.get("/api/v1/turns", headers={"X-API-Key": "nope"})).status == 401
        assert (await c.get("/api/v1/turns")).status == 401  # no key


async def test_exempt_route_needs_no_key(tmp_path: Path) -> None:
    async with TestClient(TestServer(_app(tmp_path, "master-secret"))) as c:
        assert (await c.get("/health")).status == 200


async def test_unauthorized_identity_rejected(tmp_path: Path) -> None:
    # A person with a key but NO roles is authenticated-but-unauthorized.
    key = issue_web_key(tmp_path, "stranger", roles=[])
    async with TestClient(TestServer(_app(tmp_path, "master-secret"))) as c:
        assert (await c.get("/api/v1/turns", headers={"X-API-Key": key})).status == 401


async def test_dev_open_mode_when_no_key_and_no_users(tmp_path: Path) -> None:
    # No master key + no web keys → legacy open path (localhost dev).
    async with TestClient(TestServer(_app(tmp_path, ""))) as c:
        r = await c.get("/api/v1/turns")
        assert r.status == 200
        assert (await r.json())["canonical"] is None  # no identity attached


async def test_failsafe_gate_active_when_users_exist_without_master(tmp_path: Path) -> None:
    # Web keys configured but MIMIR_API_KEY unset → gate must still activate,
    # so adding users can't leave the server wide open.
    user_key = issue_web_key(tmp_path, "alice", roles=["user"])
    async with TestClient(TestServer(_app(tmp_path, ""))) as c:
        assert (await c.get("/api/v1/turns")).status == 401  # no key → blocked
        ok = await c.get("/api/v1/turns", headers={"X-API-Key": user_key})
        assert ok.status == 200 and (await ok.json())["canonical"] == "alice"


# ── /whoami payload ─────────────────────────────────────────────────────


def test_whoami_payload_master() -> None:
    p = _whoami_payload(None, True)
    assert p["is_master"] and p["is_admin"] and p["roles"] == ["admin"] and p["canonical"] is None


def test_whoami_payload_user(tmp_path: Path) -> None:
    issue_web_key(tmp_path, "alice", roles=["user"])
    ident = _resolver(tmp_path).identity("alice")
    p = _whoami_payload(ident, False)
    assert p == {
        "canonical": "alice", "display_name": None,
        "roles": ["user"], "is_admin": False, "is_master": False,
    }


def test_whoami_payload_empty() -> None:
    assert _whoami_payload(None, False) == {
        "canonical": None, "display_name": None,
        "roles": [], "is_admin": False, "is_master": False,
    }


# ── web-chat trusted attribution ────────────────────────────────────────


class _FakeRequest(dict):
    """Minimal request: a MutableMapping (for request.get) + async json()."""

    def __init__(self, body: dict, **attrs) -> None:
        super().__init__()
        self.update(attrs)
        self._body = body

    async def json(self) -> dict:
        return self._body


def _bridge(tmp_path: Path) -> WebChatBridge:
    async def _noop(_event) -> bool:
        return True

    return WebChatBridge(enqueue=_noop, home=tmp_path)


async def test_chat_author_from_identity_not_client_body(tmp_path: Path) -> None:
    issue_web_key(tmp_path, "alice", roles=["user"])
    ident = _resolver(tmp_path).identity("alice")
    req = _FakeRequest(
        {"content": "hi", "author": "SPOOFED", "author_id": "SPOOFED"},
        auth_identity=ident,
        auth_is_master=False,
    )
    event, channel_id, err = await _bridge(tmp_path)._build_inbound_event(req)
    assert err is None and event is not None
    assert event.author_id == "alice"  # from the authenticated key, NOT spoofed
    assert event.author == "alice"
    assert channel_id == "web-default"


async def test_chat_master_key_rejected(tmp_path: Path) -> None:
    req = _FakeRequest({"content": "hi"}, auth_is_master=True)
    event, channel_id, err = await _bridge(tmp_path)._build_inbound_event(req)
    assert event is None and err is not None and err.status == 403


async def test_chat_dev_mode_falls_back_to_client_asserted(tmp_path: Path) -> None:
    # No auth attrs on the request (dev/open mode) → legacy client-asserted author.
    req = _FakeRequest({"content": "hi", "author": "alice", "author_id": "web-alice"})
    event, _channel, err = await _bridge(tmp_path)._build_inbound_event(req)
    assert err is None and event is not None
    assert event.author == "alice" and event.author_id == "web-alice"
