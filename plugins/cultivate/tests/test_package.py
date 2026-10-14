"""Built distributions and activation boundary for Cultivate."""

from __future__ import annotations

import os
import subprocess
import sys
import tarfile
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest


ROOT = Path(__file__).resolve().parents[3]
PLUGIN = ROOT / "plugins/cultivate"


@pytest.fixture(scope="module")
def built_packages(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    dest = tmp_path_factory.mktemp("cultivate-packages")
    for name in ("mimir-agent", "mimir-cultivate"):
        subprocess.run(["uv", "build", "--package", name, "--out-dir", str(dest)],
                       cwd=ROOT, check=True, capture_output=True, text=True)
    return {path.name: path for path in dest.iterdir()}


def test_workspace_metadata_and_manifest() -> None:
    root = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project = tomllib.loads((PLUGIN / "pyproject.toml").read_text())
    manifest = tomllib.loads((PLUGIN / "mimir-plugin.toml").read_text())
    version = root["project"]["version"]
    assert "plugins/cultivate" in root["tool"]["uv"]["workspace"]["members"]
    assert "coding" not in root["project"]["optional-dependencies"]
    assert project["project"]["version"] == manifest["version"] == version
    assert project["project"]["dependencies"] == [f"mimir-agent=={version}"]
    assert project["project"]["license"] == manifest["license"] == "BUSL-1.1"
    assert project["project"]["entry-points"]["mimir.plugins"] == {
        "cultivate": "mimir_cultivate:provider"}
    assert manifest["name"] == "cultivate"
    assert manifest["requires_mimir"] == f"=={version}"
    assert set(manifest) == {"name", "version", "license", "requires_mimir"}
    assert (PLUGIN / "brand/LICENSE").read_bytes() == (ROOT / "brand/LICENSE").read_bytes()


def test_built_distribution_boundaries(built_packages: dict[str, Path]) -> None:
    for name, path in built_packages.items():
        if name.startswith("mimir_agent-") and name.endswith(".whl"):
            with ZipFile(path) as archive:
                assert not any(member.startswith(("plugins/", "mimir_cultivate/", "mimir_cultivate-"))
                               for member in archive.namelist())
        elif name.startswith("mimir_agent-") and name.endswith(".tar.gz"):
            with tarfile.open(path) as archive:
                assert not any(member.name.split("/", 1)[-1].startswith("plugins/")
                               for member in archive.getmembers())
        elif name.startswith("mimir_cultivate-") and name.endswith(".whl"):
            with ZipFile(path) as archive:
                members = set(archive.namelist())
                for source, packaged in (
                    ("LICENSE", "mimir_cultivate/LICENSE"),
                    ("brand/LICENSE", "mimir_cultivate/brand/LICENSE"),
                    ("mimir-plugin.toml", "mimir_cultivate/mimir-plugin.toml"),
                ):
                    assert archive.read(packaged) == (PLUGIN / source).read_bytes()
                assert "mimir_cultivate/registration.py" in members
                assert not any(member.startswith("mimir/") for member in members)
        elif name.startswith("mimir_cultivate-") and name.endswith(".tar.gz"):
            with tarfile.open(path) as archive:
                members = archive.getnames()
                assert any(member.endswith("/brand/LICENSE") for member in members)
                assert any(member.endswith("/mimir-plugin.toml") for member in members)


def test_disabled_provider_is_not_imported() -> None:
    env = os.environ.copy()
    env.pop("MIMIR_PLUGINS", None)
    result = subprocess.run([
        sys.executable, "-c",
        "import sys; from mimir.plugins.manager import load_plugins; "
        "assert not load_plugins(enable='').plugins; "
        "assert not any(n == 'mimir_cultivate' or n.startswith('mimir_cultivate.') "
        "for n in sys.modules)",
    ], cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_selected_provider_registers_no_future_resources() -> None:
    from mimir.plugins.manager import load_plugins

    manager = load_plugins(enable="cultivate")
    assert [plugin.candidate.name for plugin in manager.plugins] == ["cultivate"]
    assert not any(tool.owner == "cultivate" for tool in manager.policy.tools.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("named", [True, False])
async def test_selected_empty_provider_preserves_authenticated_approval_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, named: bool,
) -> None:
    from mimir import approval_requests
    from mimir.models import AgentEvent
    from tests.test_agent import _resolver
    from tests.test_turn_prompt_assembly import _make_agent

    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setenv("MIMIR_PLUGINS", "cultivate")
    agent = _make_agent(tmp_path)
    assert [plugin.candidate.name for plugin in agent._config.plugin_manager.plugins] == ["cultivate"]
    agent._identity_resolver = _resolver(tmp_path, """people:
  - canonical: operator
    aliases: [discord-99]
    access: {roles: [admin]}
""")
    channel = f"cultivate-approval-{tmp_path.name}"
    entry = approval_requests.register(
        kind="update", channel_id=channel,
        description="test update", expires_at=time.monotonic() + 60,
        resolver=lambda *_args: "granted", inject_into_turn=False,
    )
    try:
        def reject_turn_setup(*_args: object, **_kwargs: object) -> None:
            pytest.fail("approval entered ordinary turn setup")

        monkeypatch.setattr("mimir.agent._initialize_ifc_labels", reject_turn_setup)
        result = await agent.run_turn(AgentEvent(
            trigger="user_message", channel_id=channel, source="discord",
            author="discord-99", content=(f"approve {entry.approval_id}" if named else "approve"),
        ))
        assert result.kind == "operator_approval"
        assert result.output == f"granted {entry.approval_id}"
        assert approval_requests.pending(channel) == ()
    finally:
        approval_requests.cancel(entry.approval_id)


@pytest.mark.asyncio
async def test_selected_empty_provider_does_not_bypass_invalid_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir import approval_requests
    from mimir.models import AgentEvent
    from tests.test_agent import _resolver
    from tests.test_turn_prompt_assembly import _make_agent

    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setenv("MIMIR_PLUGINS", "cultivate")
    agent = _make_agent(tmp_path)
    agent._identity_resolver = _resolver(tmp_path, """people:
  - canonical: operator
    aliases: [discord-99]
    access: {roles: [admin]}
""")
    channel = f"cultivate-invalid-{tmp_path.name}"
    entry = approval_requests.register(
        kind="update", channel_id=channel,
        description="test update", expires_at=time.monotonic() + 60,
        resolver=lambda *_args: pytest.fail("unauthenticated approval resolved"),
        inject_into_turn=False,
    )
    try:
        def ordinary_setup(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("ordinary turn reached")

        monkeypatch.setattr("mimir.agent._initialize_ifc_labels", ordinary_setup)
        with pytest.raises(RuntimeError, match="ordinary turn reached"):
            await agent.run_turn(AgentEvent(
                trigger="user_message", channel_id=channel, source="discord",
                author="discord-98", content=f"approve {entry.approval_id}",
            ))
        assert approval_requests.pending(channel) == (entry,)
    finally:
        approval_requests.cancel(entry.approval_id)


@pytest.mark.asyncio
async def test_selected_empty_provider_keeps_core_health_response() -> None:
    import json

    from mimir.plugins.manager import load_plugins
    from mimir.server import _handle_health

    manager = load_plugins(enable="cultivate")
    assert len(manager.plugins) == 1
    response = await _handle_health(SimpleNamespace(app={
        "plugin_manager": manager, "check_worker_executor_health": False,
    }))
    assert json.loads(response.text) == {"ok": True}
