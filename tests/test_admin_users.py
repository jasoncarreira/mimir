"""Admin Users-page endpoints: list (no key material) + mint/rotate/revoke (#563)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from mimir import web_ui
from mimir.identities import IdentityResolver, hash_web_key
from mimir.identities_populator import issue_web_key
from mimir.server import _make_auth_middleware

MASTER = "master-secret"


class _Config:
    web_host = "127.0.0.1"


def _app(home: Path, master_key: str = MASTER) -> web.Application:
    app = web.Application(middlewares=[_make_auth_middleware(master_key)])
    app["api_key"] = master_key
    app["config"] = _Config()
    resolver = IdentityResolver(home)
    resolver.reload()
    app["identity_resolver"] = resolver
    web_ui.register_routes(
        app,
        turns_log=home / "t.jsonl",
        events_log=home / "e.jsonl",
        home=home,
        react_app_dist=home / "missing-dist",
    )
    return app


def _data(body: dict) -> dict:
    return body.get("data", body)


async def test_admin_users_routes_require_admin(tmp_path: Path) -> None:
    user_key = issue_web_key(tmp_path, "alice", roles=["user"])
    admin_key = issue_web_key(tmp_path, "ops", roles=["admin"])
    async with TestClient(TestServer(_app(tmp_path))) as c:
        # non-admin user → 403 on every admin-users route
        assert (await c.get("/api/v1/admin/users", headers={"X-API-Key": user_key})).status == 403
        assert (await c.post("/api/v1/admin/users/key", headers={"X-API-Key": user_key},
                             json={"canonical": "bob"})).status == 403
        assert (await c.post("/api/v1/admin/users/revoke", headers={"X-API-Key": user_key},
                             json={"canonical": "alice"})).status == 403
        # no key → 401
        assert (await c.get("/api/v1/admin/users")).status == 401
        # admin user → 200
        assert (await c.get("/api/v1/admin/users", headers={"X-API-Key": admin_key})).status == 200
        # master key (admin) → 200
        assert (await c.get("/api/v1/admin/users", headers={"X-API-Key": MASTER})).status == 200


async def test_list_never_returns_key_material(tmp_path: Path) -> None:
    key = issue_web_key(tmp_path, "alice", roles=["user"], label="laptop")
    second = issue_web_key(tmp_path, "alice", label="phone")
    async with TestClient(TestServer(_app(tmp_path))) as c:
        body = await (await c.get("/api/v1/admin/users", headers={"X-API-Key": MASTER})).json()
        data = _data(body)
        blob = json.dumps(data)
        assert key not in blob  # no raw key
        assert hash_web_key(key) not in blob  # no hash
        assert "webkey:" not in blob  # no alias material at all
        assert second not in blob and hash_web_key(second) not in blob
        alice = next(u for u in data["users"] if u["canonical"] == "alice")
        assert alice["has_web_key"] is True
        assert sorted(alice["web_keys"], key=lambda key: key["label"]) == [
            {"label": "laptop", "present": True}, {"label": "phone", "present": True}
        ]
        assert alice["roles"] == ["user"] and alice["is_admin"] is False


async def test_issue_key_returns_raw_once_and_authenticates(tmp_path: Path) -> None:
    async with TestClient(TestServer(_app(tmp_path))) as c:
        r = await c.post("/api/v1/admin/users/key", headers={"X-API-Key": MASTER},
                         json={"canonical": "bob", "role": "user"})
        assert r.status == 200
        key = _data(await r.json())["key"]
        assert key  # raw key returned once
        # the minted key authenticates immediately (resolver reloaded live)
        who = await c.get("/api/v1/whoami", headers={"X-API-Key": key})
        wd = _data(await who.json())
        assert wd["canonical"] == "bob" and wd["is_admin"] is False
        # the list shows has_web_key but never the key itself
        lst = _data(await (await c.get("/api/v1/admin/users", headers={"X-API-Key": MASTER})).json())
        bob = next(u for u in lst["users"] if u["canonical"] == "bob")
        assert bob["has_web_key"] is True
        assert key not in json.dumps(lst)


async def test_issue_admin_role_grants_admin(tmp_path: Path) -> None:
    async with TestClient(TestServer(_app(tmp_path))) as c:
        r = await c.post("/api/v1/admin/users/key", headers={"X-API-Key": MASTER},
                         json={"canonical": "newadmin", "role": "admin"})
        key = _data(await r.json())["key"]
        wd = _data(await (await c.get("/api/v1/whoami", headers={"X-API-Key": key})).json())
        assert wd["is_admin"] is True and "admin" in wd["roles"]


async def test_rotate_invalidates_old_key(tmp_path: Path) -> None:
    k1 = issue_web_key(tmp_path, "carol", roles=["user"])
    other = issue_web_key(tmp_path, "carol", label="phone")
    async with TestClient(TestServer(_app(tmp_path))) as c:
        r = await c.post("/api/v1/admin/users/key", headers={"X-API-Key": MASTER},
                         json={"canonical": "carol", "rotate": True})
        k2 = _data(await r.json())["key"]
        assert k1 != k2
        assert (await c.get("/api/v1/whoami", headers={"X-API-Key": k1})).status == 401  # old dead
        assert (await c.get("/api/v1/whoami", headers={"X-API-Key": other})).status == 401
        assert (await c.get("/api/v1/whoami", headers={"X-API-Key": k2})).status == 200


async def test_revoke_key_blocks_auth(tmp_path: Path) -> None:
    key = issue_web_key(tmp_path, "alice", roles=["user"])
    async with TestClient(TestServer(_app(tmp_path))) as c:
        assert (await c.get("/api/v1/whoami", headers={"X-API-Key": key})).status == 200
        r = await c.post("/api/v1/admin/users/revoke", headers={"X-API-Key": MASTER},
                         json={"canonical": "alice"})
        assert _data(await r.json())["revoked"] is True
        # key is dead immediately (resolver reloaded)
        assert (await c.get("/api/v1/whoami", headers={"X-API-Key": key})).status == 401


async def test_add_and_revoke_label_preserves_other_keys(tmp_path: Path) -> None:
    original = issue_web_key(tmp_path, "alice", roles=["user"], label="laptop")
    async with TestClient(TestServer(_app(tmp_path))) as c:
        response = await c.post(
            "/api/v1/admin/users/key", headers={"X-API-Key": MASTER},
            json={"canonical": "alice", "label": "phone"},
        )
        assert response.status == 200
        assert "no-store" in response.headers["Cache-Control"]
        data = _data(await response.json())
        assert set(data) == {"canonical", "key"}
        added = data["key"]
        for key in (original, added):
            who = await c.get("/api/v1/whoami", headers={"X-API-Key": key})
            assert who.status == 200
            assert _data(await who.json())["roles"] == ["user"]
        response = await c.post(
            "/api/v1/admin/users/revoke", headers={"X-API-Key": MASTER},
            json={"canonical": "alice", "label": "phone"},
        )
        assert response.status == 200
        assert _data(await response.json())["revoked"] is True
        assert (await c.get("/api/v1/whoami", headers={"X-API-Key": added})).status == 401
        assert (await c.get("/api/v1/whoami", headers={"X-API-Key": original})).status == 200
        listing = _data(await (await c.get("/api/v1/admin/users", headers={"X-API-Key": MASTER})).json())
        assert listing["users"][0]["web_keys"] == [{"label": "laptop", "present": True}]
        assert listing["users"][0]["has_web_key"] is True
        response = await c.post(
            "/api/v1/admin/users/revoke", headers={"X-API-Key": MASTER},
            json={"canonical": "alice"},
        )
        assert response.status == 200
        listing = _data(await (await c.get("/api/v1/admin/users", headers={"X-API-Key": MASTER})).json())
        assert listing["users"][0]["web_keys"] == []
        assert listing["users"][0]["has_web_key"] is False


@pytest.mark.parametrize("endpoint", ["key", "revoke"])
@pytest.mark.parametrize("body", [
    [], ["alice"], "alice", None, 42,
    {"canonical": ["alice"]}, {"canonical": 42},
    {"canonical": "alice", "label": []},
    {"canonical": "alice", "label": {}},
    {"canonical": "alice", "label": False},
    {"canonical": "alice", "label": 123},
    {"canonical": "alice", "label": ""},
    {"canonical": "alice", "label": "  "},
])
async def test_invalid_key_request_does_not_mutate(tmp_path: Path, endpoint: str, body, monkeypatch) -> None:
    from unittest.mock import Mock

    key = issue_web_key(tmp_path, "alice", roles=["admin"])
    before = (tmp_path / "state" / "identities.yaml").read_bytes()
    mutate = Mock(side_effect=AssertionError("invalid request reached key mutation"))
    monkeypatch.setattr(f"mimir.identities_populator.{'issue_web_key' if endpoint == 'key' else 'revoke_web_key'}", mutate)
    async with TestClient(TestServer(_app(tmp_path))) as c:
        response = await c.post(
            f"/api/v1/admin/users/{endpoint}", headers={"X-API-Key": MASTER},
            data=json.dumps(body),
        )
        assert response.status == 400
        mutate.assert_not_called()
        assert (tmp_path / "state" / "identities.yaml").read_bytes() == before
        assert (await c.get("/api/v1/whoami", headers={"X-API-Key": key})).status == 200


@pytest.mark.parametrize("rotate", [None, "false", "true", 0, 1, [], {}])
async def test_invalid_rotate_does_not_mutate(tmp_path: Path, rotate, monkeypatch) -> None:
    from unittest.mock import Mock

    key = issue_web_key(tmp_path, "alice", roles=["admin"])
    before = (tmp_path / "state" / "identities.yaml").read_bytes()
    mutate = Mock(side_effect=AssertionError("invalid request reached key mutation"))
    monkeypatch.setattr("mimir.identities_populator.issue_web_key", mutate)
    async with TestClient(TestServer(_app(tmp_path))) as c:
        response = await c.post(
            "/api/v1/admin/users/key", headers={"X-API-Key": MASTER},
            json={"canonical": "alice", "rotate": rotate},
        )
        assert response.status == 400
        mutate.assert_not_called()
        assert (tmp_path / "state" / "identities.yaml").read_bytes() == before
        assert (await c.get("/api/v1/whoami", headers={"X-API-Key": key})).status == 200


@pytest.mark.parametrize("label", [None, "admin-device"])
async def test_revoke_last_web_key_without_master_is_refused(tmp_path: Path, label) -> None:
    key = issue_web_key(tmp_path, "ops", roles=["admin"], label="admin-device")
    async with TestClient(TestServer(_app(tmp_path, master_key=""))) as c:
        response = await c.post(
            "/api/v1/admin/users/revoke",
            headers={"X-API-Key": key},
            json={"canonical": "ops", "label": label},
        )
        assert response.status == 409
        body = await response.json()
        assert body["error"]["code"] == "last_web_key"
        assert "MIMIR_API_KEY is unset" in body["error"]["message"]

        # The rejected mutation never reaches disk or the live resolver.
        assert (await c.get("/api/v1/admin/users", headers={"X-API-Key": key})).status == 200


async def test_issue_key_bad_role_400(tmp_path: Path) -> None:
    async with TestClient(TestServer(_app(tmp_path))) as c:
        r = await c.post("/api/v1/admin/users/key", headers={"X-API-Key": MASTER},
                         json={"canonical": "x", "role": "superuser"})
        assert r.status == 400
        r2 = await c.post("/api/v1/admin/users/key", headers={"X-API-Key": MASTER}, json={})
        assert r2.status == 400  # canonical required


async def test_user_prefs_endpoint_persists_skin_and_refreshes_whoami(tmp_path: Path) -> None:
    key = issue_web_key(tmp_path, "alice", roles=["user"])
    async with TestClient(TestServer(_app(tmp_path))) as c:
        r = await c.post(
            "/api/v1/user/prefs",
            headers={"X-API-Key": key},
            json={"prefs": {"skin": "cosmic-nebula"}},
        )
        assert r.status == 200
        data = _data(await r.json())
        assert data["canonical"] == "alice"
        assert data["prefs"] == {"skin": "cosmic-nebula"}

        who = _data(await (await c.get("/api/v1/whoami", headers={"X-API-Key": key})).json())
        assert who["prefs"] == {"skin": "cosmic-nebula"}
        listing = _data(await (await c.get("/api/v1/admin/users", headers={"X-API-Key": MASTER})).json())
        alice = next(u for u in listing["users"] if u["canonical"] == "alice")
        assert alice["prefs"] == {"skin": "cosmic-nebula"}


async def test_user_prefs_endpoint_requires_user_identity(tmp_path: Path) -> None:
    async with TestClient(TestServer(_app(tmp_path))) as c:
        r = await c.post(
            "/api/v1/user/prefs",
            headers={"X-API-Key": MASTER},
            json={"prefs": {"skin": "cosmic-nebula"}},
        )
        assert r.status == 403
