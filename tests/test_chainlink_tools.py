"""The live tracker reader is bounded, source-labelled and read-only."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from mimir import access_control
from mimir.access_control import CapabilityTier, ToolFlowDirection
from mimir.models import AuthContext
from mimir.tools import chainlink_issue_show


def test_chainlink_issue_show_reads_only_exact_issue_and_bounds_content(tmp_path, monkeypatch):
    (tmp_path / ".chainlink").mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr("mimir.tools.chainlink.chainlink_bin", lambda: "/usr/local/bin/chainlink")
    calls = []

    def run(argv, *, cwd, stdout, stderr, timeout, check):
        calls.append((argv, cwd))
        stdout.write(json.dumps({
            "id": 42, "title": "Live issue", "status": "open",
            "labels": ["worklink:ready"], "parent_id": 7,
            "description": "x" * 20_000,
            "comments": [{"content": "z" * 3_000, "author": "bot"} for _ in range(8)],
        }).encode())
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("mimir.tools.chainlink.subprocess.run", run)
    result = chainlink_issue_show.func(42)
    assert result == {
        "id": 42, "title": "Live issue", "status": "open", "labels": ["worklink:ready"],
        "parent": 7, "description_excerpt": "x" * 16_384,
        "recent_comments": [{"body": "z" * 2_048, "author": "bot", "created_at": ""}] * 5,
    }
    assert calls == [(["/usr/local/bin/chainlink", "issue", "show", "42", "--json"], str(tmp_path))]


def test_chainlink_issue_show_missing_and_invalid(tmp_path, monkeypatch):
    (tmp_path / ".chainlink").mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr("mimir.tools.chainlink.chainlink_bin", lambda: "chainlink")
    monkeypatch.setattr("mimir.tools.chainlink.subprocess.run",
                        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1))
    assert chainlink_issue_show.func(42) == {"error": "issue_not_found", "issue_id": 42}
    assert chainlink_issue_show.func(0) == {"error": "invalid_issue_id"}


def test_chainlink_issue_show_fails_closed_on_bad_tracker_data(tmp_path, monkeypatch):
    (tmp_path / ".chainlink").mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr("mimir.tools.chainlink.chainlink_bin", lambda: "chainlink")

    def run(argv, *, stdout, **kwargs):
        stdout.write(json.dumps({
            "id": 99, "title": "Other issue", "status": "open",
            "description": "Other content", "labels": [], "comments": [],
        }).encode())
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("mimir.tools.chainlink.subprocess.run", run)
    assert chainlink_issue_show.func(42) == {"error": "chainlink_read_failed"}


def test_chainlink_issue_show_requires_tracker_and_bounded_output(tmp_path, monkeypatch):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))

    def run(argv, *, stdout, **kwargs):
        stdout.write(json.dumps({
            "id": 42, "title": "Large", "status": "open", "labels": [],
            "parent_id": None, "description": "x" * 131_073, "comments": [],
        }).encode())
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("mimir.tools.chainlink.chainlink_bin", lambda: "chainlink")
    monkeypatch.setattr("mimir.tools.chainlink.subprocess.run", run)
    assert chainlink_issue_show.func(42) == {"error": "chainlink_tracker_unavailable"}
    (tmp_path / ".chainlink").mkdir()
    assert chainlink_issue_show.func(42) == {"error": "chainlink_read_failed"}


def test_chainlink_issue_show_authorization_and_provenance():
    assert access_control._TOOL_FLOW_MAP["chainlink_issue_show"] is ToolFlowDirection.SOURCE
    assert access_control.TRIGGER_CAPABILITY_TIERS["chainlink_issue_show"] is CapabilityTier.SCOPE_CONTAINED
    assert access_control._OPERATION_READABLE_DOMAIN["chainlink_issue_show"] == "chainlink"
    assert access_control.get_tool_descriptor("chainlink_issue_show").sink_destination is None
    ctx = AuthContext(
        principal="bot", canonical_principal="bot", roles=("service",),
        event_ingress=None, trigger="poller", channel_id="poller:test", interactivity=None,
        enforcement_enabled=True,
    )
    auth = access_control.ToolAuthorization(
        tool_name="chainlink_issue_show", decision="resource_scoped", allowed=True,
    )
    labels = access_control.classify_protected_result(
        "chainlink_issue_show", {"issue_id": 42}, ctx, auth, result={"id": 42},
    )
    source, = labels.sources
    assert (source.domain, source.resource_id, source.integrity, source.integrity_effect) == (
        "chainlink", "issue/42", "untrusted", "informational",
    )
    invalid = access_control.classify_protected_result(
        "chainlink_issue_show", {"issue_id": "42"}, ctx, auth, result={"id": 42},
    )
    assert invalid.has_untrusted_active_ingest


@pytest.mark.parametrize("name", ["chainlink-orchestrator", "github-poller"])
def test_service_manifests_offer_typed_reader(name):
    base = Path(__file__).resolve().parents[1] / "mimir/optional-skills" / name
    manifest = json.loads((base / "pollers.json").read_text())
    capabilities = manifest["pollers"][0]["authority"]["capabilities"]
    assert "chainlink_issue_show" in capabilities
    service = access_control.build_trigger_service_principal(
        canonical=f"poller:{manifest['pollers'][0]['name']}", trigger="poller",
        profile="github", tier=CapabilityTier.CODE_EXECUTION,
        capabilities=tuple(capabilities), creation_path="test",
    )
    ctx = AuthContext(
        principal=f"service:{service.canonical}", canonical_principal=service.canonical,
        roles=("service",), event_ingress=None, trigger="poller",
        channel_id="poller:test", interactivity=None, is_service=True,
        service_authority=service, enforcement_enabled=True,
    )
    assert service.can_read_domain("chainlink")
    assert access_control.ToolRegistry().authorize_tool(
        "chainlink_issue_show", ctx, enforce=True, arguments={"issue_id": 42},
    ).allowed
    ungranted = replace(ctx, service_authority=replace(
        service, capabilities=tuple(cap for cap in capabilities if cap != "chainlink_issue_show"),
    ))
    assert not access_control.ToolRegistry().authorize_tool(
        "chainlink_issue_show", ungranted, enforce=True, arguments={"issue_id": 42},
    ).allowed


def test_profile_skills_name_typed_reads():
    base = Path(__file__).resolve().parents[1] / "mimir/skills"
    for skill in ("chainlink", "github", "review"):
        assert "chainlink_issue_show" in (base / skill / "SKILL.md").read_text()
