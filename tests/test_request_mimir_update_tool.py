"""Update requests are version-checked and fulfilled only by operator replies."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from mimir import __version__, approval_requests
from mimir.identities import IdentityResolver
from mimir.models import AgentEvent
from mimir.tools import registry
from mimir.update_on_start import flag_path
from packaging.version import Version


@pytest.fixture
def setup_update(tmp_path, monkeypatch):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setitem(registry._STATE, "dispatcher", SimpleNamespace(
        _config=SimpleNamespace(operator_alert_channel="discord-1")))
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "identities.yaml").write_text("""people:
  - canonical: operator
    aliases: [discord-99]
    access: {roles: [admin]}
""")
    resolver = IdentityResolver(tmp_path)
    resolver.reload()
    return tmp_path, resolver


def reply(approval_id, *, decision="approve", source="discord", trigger="user_message", author="discord-99"):
    return AgentEvent(trigger=trigger, channel_id="discord-1",
                      content=f"{decision} {approval_id}", author=author, source=source)


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["not a version", "0.0.1"])
async def test_update_request_requires_valid_non_downgrade_version(setup_update, version):
    home, _ = setup_update
    result = await registry.request_mimir_update.ainvoke({"target_version": version})
    assert "refused" in result
    assert not flag_path(home).exists()


@pytest.mark.asyncio
async def test_newer_update_registers_without_flag(setup_update):
    home, _ = setup_update
    newer = f"{Version(__version__).major + 1}.0.0"
    result = await registry.request_mimir_update.ainvoke({"target_version": newer})
    assert "upd-" in result and newer in result
    assert not flag_path(home).exists()


@pytest.mark.asyncio
async def test_approved_update_only_writes_after_authenticated_reply(setup_update):
    home, identity = setup_update
    target = f"{Version(__version__).major + 1}.0.0rc1"
    result = await registry.request_mimir_update.ainvoke({"target_version": f"  {target}  ", "include_prereleases": True})
    approval_id = result.split("Update approval requested: ")[1].split(".")[0]
    assert not flag_path(home).exists()
    for untrusted in (reply(approval_id, source="web"), reply(approval_id, trigger="scheduled_tick"),
                      reply(approval_id, author="unknown")):
        assert approval_requests.resolve(untrusted, identity).status == "unauthenticated_operator"
        assert not flag_path(home).exists()
    assert approval_requests.resolve(reply(approval_id), identity).status == "granted"
    data = json.loads(flag_path(home).read_text())
    assert data["target_version"] == target
    assert data["include_prereleases"] is True
    assert data["approval_id"] == approval_id
    assert data["approved_by"] == "operator"


@pytest.mark.asyncio
async def test_decline_and_latest_never_write_without_approval(setup_update):
    home, identity = setup_update
    result = await registry.request_mimir_update.ainvoke({})
    approval_id = result.split("Update approval requested: ")[1].split(".")[0]
    assert not flag_path(home).exists()
    assert approval_requests.resolve(reply(approval_id, decision="decline"), identity).status == "declined"
    assert not flag_path(home).exists()


@pytest.mark.asyncio
async def test_missing_home_and_operator_channel_refuse(tmp_path, monkeypatch):
    monkeypatch.delenv("MIMIR_HOME", raising=False)
    assert "MIMIR_HOME" in await registry.request_mimir_update.ainvoke({})
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setitem(registry._STATE, "dispatcher", None)
    assert "refused" in await registry.request_mimir_update.ainvoke({})
    assert not flag_path(tmp_path).exists()


def test_tool_shape():
    assert asyncio.iscoroutinefunction(registry.request_mimir_update.coroutine)
    assert "request_mimir_update" in {t.name for t in registry.all_mimir_tools()}
