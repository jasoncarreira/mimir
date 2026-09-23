"""Worklink remediation reads retained checkouts without gaining shell or writes."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mimir import access_control as ac
from mimir._context import reset_current_turn, set_current_turn
from mimir.models import InformationFlowLabels, InformationFlowState, SourceLabel
from mimir.read_policy import resolve_non_admin_read_target
from mimir.readonly_backend import FileToolRouter, WriteGuardBackend, build_file_tool_routes


@pytest.fixture
def retained_reader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from mimir.worklink import worker_client

    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    retained = tmp_path / "worklink"
    sandbox = retained / "mimir" / "1806-1" / "checkout" / ".factory-sandboxes" / "run-1"
    sandbox.mkdir(parents=True)
    target = sandbox / "issue.txt"
    target.write_text("retained needle\n", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    external_target = external / "input.txt"
    external_target.write_text("external instructions\n", encoding="utf-8")
    monkeypatch.setattr(worker_client, "WORKLINK_CHECKOUT_ROOT", retained)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", f"{external}:ro")

    manifest = json.loads((
        Path(__file__).parents[1]
        / "mimir/optional-skills/chainlink-orchestrator/pollers.json"
    ).read_text(encoding="utf-8"))["pollers"][0]
    service = ac.build_trigger_service_principal(
        canonical="poller:worklink-ready-queue",
        trigger="poller",
        profile="github",
        tier=ac.CapabilityTier.CODE_EXECUTION,
        capabilities=tuple(manifest["authority"]["capabilities"]),
        creation_path="mimir.pollers.run_poller:chainlink-orchestrator/pollers.json",
    )
    initial = InformationFlowLabels().with_source(SourceLabel(
        principal="worklink", domain="poller_payload", resource_id="incident:1806",
        bridge_instance="service:poller:worklink-ready-queue",
        sensitivity="internal", integrity="trusted", integrity_effect="active_ingest",
    ))
    state = InformationFlowState(labels=initial)
    auth = SimpleNamespace(
        canonical_principal=service.canonical,
        is_service=True,
        service_authority=service,
        event_ingress=None,
        trigger="poller",
        origin_trigger="github-poller:worklink-ready-queue",
        channel_id=service.canonical,
        bridge_instance=f"service:{service.canonical}",
        ifc_state=state,
        ifc_labels=initial,
        roles=("service",),
    )
    backend = FileToolRouter(
        default=WriteGuardBackend(home, ["state"], guard_outside_root=True),
        routes=build_file_tool_routes([
            (str(external), "ro"),
            (str(ac.worklink_retained_checkout_root()), "ro"),
        ]),
    )
    token = set_current_turn(SimpleNamespace(turn_id="retained-reader", auth_context=auth))
    try:
        yield SimpleNamespace(
            home=home, retained=retained, sandbox=sandbox, target=target,
            external=external, external_target=external_target,
            service=service, auth=auth, state=state, backend=backend,
        )
    finally:
        reset_current_turn(token)


def _authorization(tool_name: str = "read_file") -> ac.ToolAuthorization:
    return ac.ToolAuthorization(
        tool_name=tool_name,
        decision=ac.OperationDecision.RESOURCE_SCOPED,
        allowed=True,
    )


def _read_labels(reader, path: Path) -> InformationFlowLabels:
    capture = ac.begin_protected_result_capture()
    try:
        result = reader.backend.read(str(path))
    finally:
        provenance = ac.end_protected_result_capture(capture)
    assert result.error is None
    labels = ac.classify_protected_result(
        "read_file", {"file_path": str(path)}, reader.auth,
        _authorization(), result=result, provenance=provenance,
    )
    assert labels is not None
    return labels


@pytest.mark.asyncio
async def test_retained_route_reads_nested_factory_sandbox_without_configured_root(
    retained_reader,
) -> None:
    reader = retained_reader
    assert str(reader.retained) not in str(reader.external)
    assert reader.retained in ac.service_filesystem_read_roots(
        reader.service, auth_context=reader.auth,
    )
    registry = ac.ToolRegistry()
    calls = (
        ("read_file", {"file_path": str(reader.target)}),
        ("ls", {"path": str(reader.sandbox)}),
        ("grep", {"path": str(reader.sandbox), "pattern": "needle"}),
    )
    for tool, arguments in calls:
        decision = registry.authorize_tool(
            tool, reader.auth, enforce=True, arguments=arguments,
        )
        assert decision.allowed, decision

    assert reader.backend.read(str(reader.target)).file_data["content"] == "retained needle\n"
    assert str(reader.target) in {
        entry["path"] for entry in reader.backend.ls(str(reader.sandbox)).entries
    }
    assert {match["path"] for match in reader.backend.grep(
        "needle", str(reader.sandbox),
    ).matches} == {str(reader.target)}
    assert (await reader.backend.aread(str(reader.target))).error is None


def test_retained_read_preserves_ifc_while_external_read_blocks_it(retained_reader) -> None:
    reader = retained_reader
    retained_labels = reader.state.merge(_read_labels(reader, reader.target))
    retained_decision = ac.SinkGate.check_sink_flow(
        "shell_exec", "git status --short", retained_labels, reader.auth, enforce=True,
    )
    assert retained_labels.has_untrusted_active_ingest is False
    assert retained_decision.allowed is False
    assert not retained_decision.reason.startswith("ifc_label_blocked:")
    assert retained_decision.reason == "service_sink_destination_denied"

    external_labels = reader.state.merge(_read_labels(reader, reader.external_target))
    external_decision = ac.SinkGate.check_sink_flow(
        "shell_exec", "git status --short", external_labels, reader.auth, enforce=True,
    )
    assert external_labels.has_untrusted_active_ingest is True
    assert external_decision.reason == "ifc_label_blocked:shell_process"


@pytest.mark.parametrize("escape", ["symlink", "dotdot", "undeclared"])
def test_retained_read_scope_refuses_escapes(retained_reader, escape: str) -> None:
    reader = retained_reader
    outside = reader.retained.parent / "undeclared.txt"
    outside.write_text("outside\n", encoding="utf-8")
    if escape == "symlink":
        candidate = reader.sandbox / "escape.txt"
        candidate.symlink_to(outside)
    elif escape == "dotdot":
        candidate = reader.retained / ".." / outside.name
    else:
        candidate = outside
    decision = ac.ToolRegistry().authorize_tool(
        "read_file", reader.auth, enforce=True,
        arguments={"file_path": str(candidate)},
    )
    assert decision.allowed is False
    assert resolve_non_admin_read_target(str(candidate), scan_file=True) is None


def test_retained_root_and_anchor_are_exclusive_to_reserved_service(retained_reader) -> None:
    reader = retained_reader
    other_poller = SimpleNamespace(**vars(reader.auth))
    other_service = ac.build_trigger_service_principal(
        canonical="poller:other-github", trigger="poller", profile="github",
        tier=ac.CapabilityTier.SCOPE_CONTAINED,
        capabilities=("read_file",), creation_path="test",
    )
    other_poller.service_authority = other_service
    other_poller.canonical_principal = other_service.canonical
    non_poller_service = ac.build_trigger_service_principal(
        canonical="scheduled:test", trigger="scheduled_tick", profile="github",
        tier=ac.CapabilityTier.SCOPE_CONTAINED,
        capabilities=("read_file",), creation_path="test",
    )
    non_poller = SimpleNamespace(**vars(reader.auth))
    non_poller.service_authority = non_poller_service
    non_poller.canonical_principal = non_poller_service.canonical
    non_poller.trigger = "scheduled_tick"

    for auth, service in ((other_poller, other_service), (non_poller, non_poller_service)):
        assert reader.retained not in ac.service_filesystem_read_roots(
            service, auth_context=auth,
        )
        source = ac.protected_result_source(
            auth, principal="filesystem", domain="filesystem",
            resource_id=str(reader.target), bridge_instance="filesystem",
        )
        assert (source.integrity, source.integrity_effect) == (
            "untrusted", "active_ingest",
        )


def test_retained_root_does_not_expand_shell_or_write_authority(
    retained_reader, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.tools.budget_gate import _resolve_service_shell_cwd

    reader = retained_reader
    monkeypatch.setattr(
        "mimir.read_policy.configured_non_admin_read_roots",
        lambda: (reader.external,),
    )
    assert reader.retained not in ac.service_shell_filesystem_read_roots(
        reader.service, auth_context=reader.auth,
    )
    resolved, refusal = _resolve_service_shell_cwd(
        str(reader.sandbox), reader.service,
    )
    assert resolved is None
    assert "outside" in refusal
    assert ac._service_shell_read_operand_refusal(
        reader.service, ["cat", str(reader.target)], auth_context=reader.auth,
    ) == "a filesystem operand is outside the read roots or withheld"
    assert ac._service_shell_read_operand_refusal(
        reader.service, ["grep", "-r", "needle", str(reader.sandbox)],
        auth_context=reader.auth,
    ) == "a filesystem operand is outside the read roots or withheld"

    new_file = reader.sandbox / "new.txt"
    assert reader.backend.write(str(new_file), "new").error
    assert reader.backend.edit(str(reader.target), "needle", "changed").error
    assert not new_file.exists()
    assert reader.target.read_text(encoding="utf-8") == "retained needle\n"
