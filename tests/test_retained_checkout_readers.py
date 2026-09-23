"""Worklink remediation reads retained checkouts without gaining shell or writes."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from mimir import access_control as ac
from mimir._context import reset_current_turn, set_current_turn
from mimir.agent import _create_turn_auth_context, _initialize_ifc_labels
from mimir.event_logger import init_logger
from mimir.models import (
    InformationFlowLabels, InformationFlowState, RetainedFactoryScope, SourceLabel,
    TurnContext,
)
from mimir.pollers import discover_pollers, run_poller
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
    initial = InformationFlowLabels().with_channel(service.canonical).with_source(SourceLabel(
        principal="worklink", domain="poller_payload", resource_id="incident:1806",
        bridge_instance="service:poller:worklink-ready-queue",
        sensitivity="internal", integrity="trusted", integrity_effect="informational",
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
        retained_factory_scope=RetainedFactoryScope(
            issue_id=1806, signature="signature", occurrence_id="occurrence",
            run_id="run-1", attempt=1, session="session-1",
            repository="owner/mimir", branch="feature/run-1", sandbox=str(sandbox),
        ),
    )
    backend = FileToolRouter(
        default=WriteGuardBackend(home, ["state"], guard_outside_root=True),
        routes=build_file_tool_routes([
            (str(external), "ro"),
            (str(ac.worklink_retained_checkout_root()), "retained"),
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


class _CapturingEnqueue:
    def __init__(self) -> None:
        self.events = []

    async def __call__(self, event, **_kwargs) -> bool:
        self.events.append(event)
        return True


@pytest.mark.asyncio
async def test_real_worklink_failure_turn_reads_retained_checkout_with_ifc_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.worklink import dispatch_failures as failures
    from mimir.worklink.compute import LaunchHandle
    from mimir.worklink.factory_state import FactoryRunRecord, save_factory_record
    from mimir.worklink import worker_client

    home = tmp_path / "home"
    state_root = home / "state" / "pollers"
    state_root.mkdir(parents=True)
    init_logger(home / "logs" / "events.jsonl", session_id="retained-real-turn")
    repo = tmp_path / "repo"
    repo.mkdir()
    retained = tmp_path / "worklink"
    checkout = retained / "mimir" / "1806-1" / "checkout"
    checkout.mkdir(parents=True)
    sandbox = checkout / ".factory-sandboxes" / "chainlink-1806"
    sandbox.mkdir(parents=True)
    retained_target = sandbox / "issue.txt"
    retained_target.write_text("retained needle\n", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    external_target = external / "input.txt"
    external_target.write_text("external instructions\n", encoding="utf-8")
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("WORKLINK_REPO", str(repo))
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", f"{external}:ro")
    monkeypatch.setattr(worker_client, "WORKLINK_CHECKOUT_ROOT", retained)

    skills = Path(__file__).parents[1] / "mimir" / "optional-skills"
    ready = next(
        config for config in discover_pollers(skills, state_root=state_root)
        if config.name == "worklink-ready-queue"
    )
    failures.record_failure(
        failures.dispatch_failure_state_dir(home),
        issue_id=1806,
        attempt=1,
        exit_status=1,
        error="retained checkout requires remediation",
        log_path="run.log",
        run_id="chainlink-1806",
        work_path=str(sandbox),
    )
    save_factory_record(home, FactoryRunRecord(
        run_id="chainlink-1806", issue_id=1806, attempt=1,
        repository="owner/mimir", base_ref="main", branch="feature/chainlink-1806",
        launcher="/opt/factory.js", sandbox=str(sandbox), session="session-1",
        handle=LaunchHandle("local_subprocess", "99999999", 1), status=None,
        observed_at=None, controller_phase="failed",
    ))
    ready = replace(
        ready,
        command=f"{sys.executable} scripts/poller.py",
    )
    enqueued = _CapturingEnqueue()

    emitted = await run_poller(ready, enqueue=enqueued, home=home)
    event_log = home / "logs" / "events.jsonl"
    assert emitted == 1, event_log.read_text(encoding="utf-8")
    [event] = enqueued.events
    labels = _initialize_ifc_labels(event)
    auth = _create_turn_auth_context(
        event, None, policy_version=None, enforce=True, ifc_labels=labels,
    )
    turn = TurnContext(
        turn_id="real-worklink-remediation",
        session_id=event.channel_id,
        trigger=event.trigger,
        channel_id=event.channel_id,
        started_at=0.0,
        auth_context=auth,
        ifc_labels=labels,
    )
    token = set_current_turn(turn)
    try:
        service = ac.get_trusted_service_from_auth_context(auth)
        assert service is not None
        assert service.canonical == "poller:worklink-ready-queue"
        assert sandbox in ac.service_filesystem_read_roots(
            service, auth_context=auth,
        )
        decision = ac.ToolRegistry().authorize_tool(
            "read_file", auth, enforce=True,
            arguments={"file_path": str(retained_target)},
        )
        assert decision.allowed, (decision.reason, decision.refusal_detail)
        for tool in ("write_file", "edit_file"):
            write_target = sandbox / "fix.py"
            write_decision = ac.ToolRegistry().authorize_tool(
                tool, auth, enforce=True, target_channel=str(write_target),
                arguments={"file_path": str(write_target)},
            )
            assert write_decision.allowed, write_decision.reason
            sibling = sandbox.parent / "chainlink-1807" / "fix.py"
            assert not ac.ToolRegistry().authorize_tool(
                tool, auth, enforce=True, target_channel=str(sibling),
                arguments={"file_path": str(sibling)},
            ).allowed
        backend = FileToolRouter(
            default=WriteGuardBackend(home, ["state"], guard_outside_root=True),
            routes=build_file_tool_routes([
                (str(external), "ro"),
                (str(retained), "retained"),
            ]),
        )
        reader = SimpleNamespace(backend=backend, auth=auth, state=auth.ifc_state)
        retained_labels = auth.ifc_state.merge(_read_labels(reader, retained_target))
        retained_sink = ac.SinkGate.check_sink_flow(
            "shell_exec", "git status --short", retained_labels, auth, enforce=True,
        )
        assert retained_labels.has_untrusted_active_ingest is False
        assert not retained_sink.reason.startswith("ifc_label_blocked:")

        external_labels = auth.ifc_state.merge(_read_labels(reader, external_target))
        external_sink = ac.SinkGate.check_sink_flow(
            "shell_exec", "git status --short", external_labels, auth, enforce=True,
        )
        assert external_labels.has_untrusted_active_ingest is True
        assert external_sink.reason == "ifc_label_blocked:shell_process"
    finally:
        reset_current_turn(token)


@pytest.mark.asyncio
async def test_retained_route_reads_nested_factory_sandbox_without_configured_root(
    retained_reader,
) -> None:
    reader = retained_reader
    assert str(reader.retained) not in str(reader.external)
    assert reader.sandbox in ac.service_filesystem_read_roots(
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
        assert decision.allowed, (decision.reason, decision.refusal_detail)

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
        assert reader.sandbox not in ac.service_filesystem_read_roots(
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
    assert reader.sandbox not in ac.service_shell_filesystem_read_roots(
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


def test_retained_scope_centrally_authorizes_only_exact_unprotected_writes(
    retained_reader,
) -> None:
    reader = retained_reader
    registry = ac.ToolRegistry()
    inside = reader.sandbox / "src" / "fix.py"
    sibling = reader.sandbox.parent / "other-run" / "fix.py"
    escaped = reader.sandbox / ".." / "other-run" / "fix.py"
    protected = reader.sandbox / ".git" / "config"

    for tool in ("write_file", "edit_file"):
        policy = reader.service.sink_policy_for(tool)
        assert policy is not None
        assert ac._target_within_trigger_service_write_roots(
            str(inside), policy.destination, auth_context=reader.auth,
        ), policy.destination
        decision = registry.authorize_tool(
            tool, reader.auth, enforce=True, target_channel=str(inside),
            arguments={"file_path": str(inside)}, ifc_labels=InformationFlowLabels(),
        )
        assert decision.allowed, (decision.reason, decision.refusal_detail)
        for target in (sibling, escaped, protected):
            assert not registry.authorize_tool(
                tool, reader.auth, enforce=True, target_channel=str(target),
                arguments={"file_path": str(target)}, ifc_labels=InformationFlowLabels(),
            ).allowed
