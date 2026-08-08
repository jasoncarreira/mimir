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

    assert optional["acp"] == ["agent-client-protocol==0.12.0"]
    assert "agent-client-protocol==0.12.0" not in project["dependencies"]
    assert optional["dev"].count("agent-client-protocol==0.12.0") == 1
    assert dependency_groups["dev"].count("agent-client-protocol==0.12.0") == 1

    assert optional["mcp"] == ["mcp>=1.27"]
    assert optional["dev"].count("mcp>=1.27") == 1
    assert dependency_groups["dev"].count("mcp>=1.27") == 1


def test_wheel_guard_requires_complete_acp_seed() -> None:
    members = _required_wheel_members()
    acp_members = {member for member in members if member.startswith("mimir/acp/")}
    assert acp_members == {
        "mimir/acp/__init__.py",
        "mimir/acp/__main__.py",
        "mimir/acp/agent.py",
        "mimir/acp/bootstrap.py",
        "mimir/acp/composition.py",
        "mimir/acp/host.py",
        "mimir/acp/bridge.py",
        "mimir/acp/journal.py",
        "mimir/acp/session_store.py",
        "mimir/acp/updates.py",
        "mimir/acp/sdk.py",
        "mimir/acp/stdio.py",
    }


def test_wheel_guard_requires_bundled_acp_docs() -> None:
    assert "mimir/bundled_docs/docs/acp.md" in _required_wheel_members()


def test_acp_docs_are_force_included() -> None:
    wheel = _project_config()["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["mimir"]
    assert wheel["force-include"]["docs"] == "mimir/bundled_docs/docs"


def test_acp_docs_cover_remote_stdio_contract() -> None:
    docs = (ROOT / "docs" / "acp.md").read_text(encoding="utf-8")

    assert "ssh <host> mimir acp" in docs
    assert "ssh -T <host> mimir acp" in docs
    assert "docker exec -i <container> mimir acp" in docs
    assert (
        "Mimir provides ACP over stdio only. It does not open a network listener, "
        "socket, or port. The ACP client owns the SSH or Docker connection."
    ) in docs
    assert "stdin carries UTF-8 JSONL requests" in docs
    assert "stdout carries UTF-8 JSONL frames after Mimir starts" in docs
    assert "stderr carries diagnostics" in docs

    assert "SSH pseudo-TTY allocation" in docs
    assert "alter or interleave bytes" in docs
    assert "malformed-frame errors" in docs
    assert "use `ssh -T`" in docs
    assert "Docker `-t`" in docs
    assert "alter framing" in docs
    assert "without `-t`" in docs
    assert "MOTD, login banners, or shell startup/rc output" in docs
    assert "precede the first JSON frame" in docs
    assert "reject the first frame" in docs
    assert "silent noninteractive shell or wrapper" in docs
    assert "redirect diagnostics to stderr" in docs
    assert (
        "Mimir reserves stdout only after startup and cannot remove bytes already "
        "emitted by a parent shell, SSH daemon, or wrapper."
    ) in docs
    assert "does not strip banners or provide a network transport" in docs


def test_dockerfile_lists_acp_as_available_extra() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert '''# Available extras (see pyproject.toml):
#   anthropic, claude-code, openai, codex-plus  (model providers)
#   discord, slack                              (bridges)
#   mcp, acp                                    (Model Context Protocol)
#
ARG MIMIR_EXTRAS="anthropic,discord,slack,mcp"''' in dockerfile


def test_wheel_guard_matches_finite_acp_source_inventory() -> None:
    members = {member for member in _required_wheel_members() if member.startswith("mimir/acp/")}
    sources = {f"mimir/acp/{path.name}" for path in (ROOT / "mimir" / "acp").glob("*.py")}
    assert members == sources
