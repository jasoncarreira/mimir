from __future__ import annotations

import ast
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
WHEEL_ASSERTION = ROOT / ".github" / "assert_wheel_contents.py"


def _project_config() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _required_wheel_members() -> tuple[str, ...]:
    tree = ast.parse(WHEEL_ASSERTION.read_text(encoding="utf-8"))
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "REQUIRED_MEMBERS"
            for target in node.targets
        )
    ]
    assert len(assignments) == 1
    members = ast.literal_eval(assignments[0].value)
    assert isinstance(members, tuple)
    return members


def test_acp_and_mcp_dependency_declarations() -> None:
    config = _project_config()
    project = config["project"]
    optional = project["optional-dependencies"]
    dependency_groups = config["dependency-groups"]

    assert optional["acp"] == []
    assert project["dependencies"].count("agent-client-protocol==0.12.0") == 1
    assert project["dependencies"].count("keyring==25.7.0") == 1
    assert optional["dev"].count("agent-client-protocol==0.12.0") == 0
    assert dependency_groups["dev"].count("agent-client-protocol==0.12.0") == 0
    assert optional["dev"].count("keyring==25.7.0") == 0
    assert dependency_groups["dev"].count("keyring==25.7.0") == 0

    assert optional["mcp"] == ["mcp>=1.27"]
    assert optional["dev"].count("mcp>=1.27") == 1
    assert dependency_groups["dev"].count("mcp>=1.27") == 1



def test_both_console_scripts_use_the_early_entrypoint() -> None:
    scripts = _project_config()["project"]["scripts"]
    assert scripts == {
        "mimir": "mimir.entrypoint:main",
        "mimir-agent": "mimir.entrypoint:main",
    }

def test_wheel_guard_requires_complete_intermediate_surface() -> None:
    members = _required_wheel_members()
    acp_members = {member for member in members if member.startswith("mimir/acp/")}
    assert acp_members == {
        "mimir/acp/__init__.py",
        "mimir/acp/__main__.py",
        "mimir/acp/agent.py",
        "mimir/acp/bootstrap.py",
        "mimir/acp/credentials.py",
        "mimir/acp/daemon.py",
        "mimir/acp/host.py",
        "mimir/acp/profiles.py",
        "mimir/acp/proxy.py",
        "mimir/acp/relay.py",
        "mimir/acp/ssh.py",
        "mimir/acp/bridge.py",
        "mimir/acp/journal.py",
        "mimir/acp/session_store.py",
        "mimir/acp/updates.py",
        "mimir/acp/sdk.py",
        "mimir/acp/stdio.py",
        "mimir/acp/transport.py",
    }


def test_wheel_guard_requires_bundled_acp_docs() -> None:
    assert "mimir/bundled_docs/docs/acp.md" in _required_wheel_members()


def test_acp_docs_are_force_included() -> None:
    wheel = _project_config()["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["mimir"]
    assert wheel["force-include"]["docs"] == "mimir/bundled_docs/docs"


def test_acp_docs_cover_client_contract() -> None:
    docs = (ROOT / "docs" / "acp.md").read_text(encoding="utf-8")
    assert "mimir acp credential add" in docs
    assert "mimir-agent acp relay --home" in docs
    assert "stdout" in docs and "JSONL" in docs
    assert "no plaintext" in docs
    assert "credential-mutation-uncertain" in docs
    assert "12 seconds" in docs and "5 seconds" in docs

def test_wheel_guard_matches_finite_acp_source_inventory() -> None:
    members = {member for member in _required_wheel_members() if member.startswith("mimir/acp/")}
    sources = {f"mimir/acp/{path.name}" for path in (ROOT / "mimir" / "acp").glob("*.py")}
    assert members == sources
