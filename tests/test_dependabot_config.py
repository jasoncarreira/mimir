"""Dependabot must keep watching every ecosystem, and the protocol deps closely.

mcp 2.x shipped while the requirement had no upper bound and broke MCP tool
discovery for PyPI installs; only security updates were configured, so no PR
announced it. These tests keep the version-update coverage from being dropped.
"""

from __future__ import annotations

from pathlib import Path

import yaml


CONFIG = Path(__file__).resolve().parents[1] / ".github/dependabot.yml"
PROTOCOL_DEPS = {"mcp", "agent-client-protocol"}


def _updates() -> dict[str, dict]:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    assert config["version"] == 2
    entries = config["updates"]
    by_ecosystem = {entry["package-ecosystem"]: entry for entry in entries}
    assert len(by_ecosystem) == len(entries), "duplicate ecosystem entries"
    for entry in entries:
        assert entry["directory"] == "/"
        assert entry["schedule"]["interval"] in {"daily", "weekly"}
    return by_ecosystem


def test_dependabot_covers_every_ecosystem() -> None:
    assert set(_updates()) == {"github-actions", "uv", "npm", "docker"}


def test_protocol_deps_have_their_own_daily_group() -> None:
    uv = _updates()["uv"]
    assert uv["schedule"]["interval"] == "daily"
    groups = list(uv["groups"].items())
    # First matching group wins, so the protocol group must come first and
    # must not be limited to minor/patch updates.
    name, first = groups[0]
    assert name == "agent-protocols"
    assert PROTOCOL_DEPS <= set(first["patterns"])
    assert "update-types" not in first


def test_capped_and_pinned_requirements_still_produce_prs() -> None:
    uv = _updates()["uv"]
    # widen/increase-if-necessary both raise a PR when a release falls outside
    # `mcp<2` or the exact agent-client-protocol pin; lockfile-only would not.
    assert uv["versioning-strategy"] in {"increase-if-necessary", "widen"}
    allowed = uv.get("allow")
    if allowed is not None:
        types = {entry.get("dependency-type") for entry in allowed}
        assert "direct" in types or "all" in types


def test_acp_0_12_patches_ignored_but_1_0_still_surfaces() -> None:
    uv = _updates()["uv"]
    ignores = [entry for entry in uv.get("ignore", []) if entry.get("dependency-name") == "agent-client-protocol"]
    # Exactly one ignore, bounded below 1.0, so ACP 1.0 still opens a PR.
    assert ignores == [{"dependency-name": "agent-client-protocol", "versions": [">=0.12.1, <1.0.0"]}]
    assert "agent-client-protocol" in uv["groups"]["agent-protocols"]["patterns"]
    # No other protocol dependency is silenced.
    silenced = {entry.get("dependency-name") for entry in uv.get("ignore", [])}
    assert "mcp" not in silenced
