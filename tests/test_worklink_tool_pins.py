from __future__ import annotations

from pathlib import Path
import re

from mimir.worklink.backends.feature_factory import FACTORY_VERSION
from mimir.worklink.tool_pins import (
    OPENCODE_VERSION,
    default_tool_pins,
)


def test_default_tool_pin_inventory_covers_distinct_executable_risk_surfaces() -> None:
    pins = {pin.name: pin for pin in default_tool_pins()}

    expected = {
        "chainlink", "mermaid-cli", "osv-scanner", "gogcli",
        "opencode", "feature-factory", "opencode-feature-factory", "opencode-project-memory",
        "opencode-openai-codex-auth", "opencode-anthropic-auth",
    }
    assert set(pins) == expected
    assert pins["chainlink"].category == "issue-cli"
    assert pins["mermaid-cli"].category == "renderer"
    assert pins["gogcli"].category == "integration-cli"
    assert pins["osv-scanner"].category == "security-scanner"
    assert pins["opencode"].category == "coding-cli"
    assert pins["opencode"].pin == OPENCODE_VERSION
    assert pins["feature-factory"].pin == FACTORY_VERSION
    assert pins["opencode-feature-factory"].pin == FACTORY_VERSION
    assert pins["opencode-feature-factory"].category == "coding-plugin"
    assert pins["opencode-project-memory"].category == "coding-plugin"
    assert pins["opencode-openai-codex-auth"].category == "coding-plugin"
    assert pins["opencode-anthropic-auth"].category == "coding-plugin"
    for pin in pins.values():
        assert pin.pin
        assert pin.smoke
        assert pin.source
        assert pin.install
        assert pin.risk


def test_worklink_docs_describe_current_tool_pin_inventory() -> None:
    root = Path(__file__).resolve().parents[1]
    docs = (root / "docs/internal/WORKLINK.md").read_text(encoding="utf-8")
    inventory_summary = docs.split(
        "The seed covers pinned external executables", 1
    )[1].split("Each entry records", 1)[0]

    for expected in (
        "chainlink",
        "mermaid-cli",
        "osv-scanner",
        "gogcli",
        "OpenCode CLI and plugins",
    ):
        assert expected in inventory_summary
    assert "Codex CLI" not in inventory_summary

    tool_pins_example = docs.split("```yaml\ntool_pins:", 1)[1].split("```", 1)[0]
    assert "name: opencode" in tool_pins_example
    assert 'smoke: "opencode --version"' in tool_pins_example
    assert 'package: "opencode-ai"' in tool_pins_example
    assert "name: codex" not in tool_pins_example
    assert 'smoke: "codex --version"' not in tool_pins_example
    assert 'package: "@openai/codex"' not in tool_pins_example


def test_default_tool_pin_inventory_matches_shipped_installs() -> None:
    pins = {pin.name: pin for pin in default_tool_pins()}
    root = Path(__file__).resolve().parents[1]
    install_paths = (
        "Dockerfile",
        "mimir/scaffold_docker.py",
        "mimir/skills/chainlink/dockerfile.fragment",
        "mimir/optional-skills/gmail-poller/dockerfile.fragment",
        "mimir/optional-skills/dependency-advisory-watch/dockerfile.fragment",
    )
    install_sources = {
        relpath: (root / relpath).read_text(encoding="utf-8")
        for relpath in install_paths
    }
    install_text = "\n".join(install_sources.values())

    assert f"--tag {pins['chainlink'].pin}" in install_text
    assert f"@mermaid-js/mermaid-cli@{pins['mermaid-cli'].pin}" in install_text
    assert f"github.com/steipete/gogcli/cmd/gog@{pins['gogcli'].pin}" in install_text
    assert pins["osv-scanner"].pin in install_text
    assert f"opencode-ai@{pins['opencode'].pin}" in install_text
    assert pins["feature-factory"].pin == pins["opencode-feature-factory"].pin
    dockerfile = install_sources["Dockerfile"]
    factory_arg = re.search(r"(?m)^ARG FACTORY_VERSION=([^\s]+)$", dockerfile)
    assert factory_arg is not None
    assert FACTORY_VERSION == pins["feature-factory"].pin == factory_arg.group(1)
    assert "feature-factory@${FACTORY_VERSION}" in dockerfile
    assert "opencode-feature-factory@${FACTORY_VERSION}" in dockerfile
    scaffold = install_sources["mimir/scaffold_docker.py"]
    assert "feature-factory@{FACTORY_VERSION}" in scaffold
    assert "opencode-feature-factory@{FACTORY_VERSION}" in scaffold
    assert f"opencode-project-memory@{pins['opencode-project-memory'].pin}" in install_text
    assert f"opencode-openai-codex-auth@{pins['opencode-openai-codex-auth'].pin}" in install_text
    assert f"opencode-anthropic-auth@{pins['opencode-anthropic-auth'].pin}" in install_text
