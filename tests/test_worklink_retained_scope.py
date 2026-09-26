from __future__ import annotations

from dataclasses import replace
import fcntl
import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from mimir import access_control as ac
from mimir.contained_execution import CollectedExecutionResult
from mimir.models import RetainedFactoryScope
from mimir.project_tests import ProjectTestResult, RepoProjectTests
from mimir.readonly_backend import RetainedCheckoutFilesystemBackend
from mimir.worklink.compute import LaunchHandle
from mimir.worklink.backends.feature_factory import FactoryStatus
from mimir.worklink.dispatch_failures import dispatch_failure_state_dir, record_failure
from mimir.worklink.factory_state import (
    FactoryRunRecord,
    factory_issue_resource_lock,
    save_factory_record,
)
from mimir.worklink.retained_scope import (
    derive_retained_factory_scope,
    retained_factory_effect_lease,
)


@pytest.fixture
def retained_incident(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from mimir.worklink import worker_client

    home = tmp_path / "home"
    root = tmp_path / "worklink"
    checkout = root / "mimir" / "1810-1" / "checkout"
    sandbox = checkout / ".factory-sandboxes" / "chainlink-1810"
    sandbox.mkdir(parents=True)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setattr(worker_client, "WORKLINK_CHECKOUT_ROOT", root)
    incident = record_failure(
        dispatch_failure_state_dir(home), issue_id=1810, attempt=1,
        exit_status=1, error="factory failed", log_path="run.log",
        run_id="chainlink-1810", work_path=str(sandbox),
    )
    record = FactoryRunRecord(
        run_id="chainlink-1810", issue_id=1810, attempt=1,
        repository="owner/mimir", base_ref="main", branch="feature/chainlink-1810",
        launcher="/opt/factory.js", sandbox=str(sandbox), session="session-1",
        handle=LaunchHandle("local_subprocess", "99999999", 1), status=None,
        observed_at=None, controller_phase="failed",
    )
    save_factory_record(home, record)
    item = {
        "issue_id": 1810,
        "error_signature": incident["signature"],
        "failure_occurrence_id": incident["occurrence_id"],
        "delivery_key": (
            f"worklink-run-failure:1810:{incident['signature']}:{incident['occurrence_id']}"
        ),
    }
    event = SimpleNamespace(
        trigger="poller", service_principal="poller:worklink-ready-queue",
        extra={"poller_name": "worklink-ready-queue", "items": [item]},
    )
    service = SimpleNamespace(
        canonical="poller:worklink-ready-queue", trigger="poller", authority_profile="github",
    )
    return SimpleNamespace(
        home=home, root=root, sandbox=sandbox, record=record,
        incident=incident, item=item, event=event, service=service,
    )


def test_scope_is_exact_ephemeral_and_current(retained_incident) -> None:
    case = retained_incident
    before = sorted(str(path.relative_to(case.home)) for path in case.home.rglob("*"))
    result = derive_retained_factory_scope(case.event, case.service)
    after = sorted(str(path.relative_to(case.home)) for path in case.home.rglob("*"))

    assert result.refusal_reason is None
    assert result.scope == RetainedFactoryScope(
        issue_id=1810, signature=case.incident["signature"],
        occurrence_id=case.incident["occurrence_id"], run_id="chainlink-1810",
        attempt=1, session="session-1", repository="owner/mimir",
        branch="feature/chainlink-1810", sandbox=str(case.sandbox),
    )
    assert result.scope.resolve_path(case.sandbox / "new.py") == case.sandbox / "new.py"
    assert result.scope.resolve_path(case.sandbox.parent / "sibling.py") is None
    assert before == after


def test_retained_file_effects_are_in_all_central_policy_inventories() -> None:
    from mimir.tools import budget_gate

    for tool in ("write_file", "edit_file"):
        assert ac.get_sink_category(tool) is ac.SinkCategory.FILE
        assert ac.get_tool_flow_direction(tool) is ac.ToolFlowDirection.SINK
        assert ac.TRIGGER_CAPABILITY_TIERS[tool] is ac.CapabilityTier.SCOPE_CONTAINED
        assert tool in budget_gate._REMEDIATION_EFFECT_TOOLS
        assert (
            ac.TOOL_DESCRIPTORS[tool].result_origin
            & ac.ResultOriginKind.NON_INGESTING
        )


def test_worklink_resume_is_exact_spawn_capability() -> None:
    from mimir.tools import budget_gate

    assert ac.get_sink_category("worklink_resume") is ac.SinkCategory.SPAWN
    assert ac.get_tool_flow_direction("worklink_resume") is ac.ToolFlowDirection.BOTH
    assert ac.TRIGGER_CAPABILITY_TIERS["worklink_resume"] is ac.CapabilityTier.CODE_EXECUTION
    assert "worklink_resume" in budget_gate._REMEDIATION_EFFECT_TOOLS


@pytest.mark.asyncio
async def test_worklink_resume_dispatches_exact_scope_detached(
    retained_incident, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.tools import registry
    from mimir.worklink import autonomy, detached_dispatch, factory_state

    case = retained_incident
    scope = derive_retained_factory_scope(case.event, case.service).scope
    assert scope is not None
    parked = replace(case.record, status=FactoryStatus(
        run_id=case.record.run_id, valid=True, sandbox_path=case.record.sandbox,
        status="needs-human",
    ))
    save_factory_record(case.home, parked)
    monkeypatch.setenv("WORKLINK_REPO", str(case.sandbox.parent))
    monkeypatch.setattr(factory_state, "factory_process_is_alive", lambda record: False)
    monkeypatch.setattr(factory_state, "factory_process_is_verified_dead", lambda record: True)
    monkeypatch.setattr(
        autonomy, "make_claims",
        lambda home: SimpleNamespace(_active_worklink_lock_ids_for_scope=lambda **kwargs: set()),
    )
    launches: list[dict[str, object]] = []
    monkeypatch.setattr(
        detached_dispatch,
        "launch_detached_worklink",
        lambda **kwargs: launches.append(kwargs) or detached_dispatch.DetachedWorklinkProcess(
            4321, dispatch_failure_state_dir(case.home) / "run-epic-1810.log",
        ),
    )

    output = await registry.worklink_resume.coroutine(runtime=_retained_runtime(scope))

    assert "recovery dispatched" in output
    assert "run_id=chainlink-1810" in output
    assert "retained_attempt=1" in output
    assert "pid=4321" in output
    assert "child may still refuse" in output
    assert "only a successful child claim consumes" in output
    assert launches[0]["recovery"] == detached_dispatch.FactoryRecoveryIdentity(
        scope.signature, scope.occurrence_id, scope.run_id, scope.attempt, scope.session,
    )


@pytest.mark.asyncio
async def test_worklink_resume_refuses_leaf_without_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    from mimir.tools import registry
    from mimir.worklink import detached_dispatch

    monkeypatch.setattr(
        detached_dispatch, "launch_detached_worklink",
        Mock(side_effect=AssertionError("leaf incident launched")),
    )
    runtime = SimpleNamespace(context=SimpleNamespace(
        retained_factory_scope=None,
        retained_factory_scope_refusal="incident is not a retained factory run",
    ))
    output = await registry.worklink_resume.coroutine(runtime=runtime)
    assert "refused" in output
    assert "leaf incidents must be diagnosed and reported" in output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("stale", "incident occurrence is not current"),
        ("missing", "record is missing"),
        ("ambiguous", "record is ambiguous"),
        ("replaced", "target was replaced"),
        ("replaced_branch", "target was replaced"),
        ("replaced_repository", "target was replaced"),
        ("replaced_sandbox", "target was replaced"),
        ("status", "not needs-human"),
        ("alive", "process is alive"),
        ("unverified", "death cannot be verified"),
        ("same_issue", "another run for this issue is live"),
        ("cap", "concurrency cap reached"),
    ],
)
async def test_worklink_resume_preflight_refuses_changed_state_without_launch(
    retained_incident, monkeypatch: pytest.MonkeyPatch, mutation: str, reason: str,
) -> None:
    from mimir.tools import registry
    from mimir.worklink import autonomy, detached_dispatch, dispatch_failures, factory_state

    case = retained_incident
    scope = derive_retained_factory_scope(case.event, case.service).scope
    assert scope is not None
    parked = replace(case.record, status=FactoryStatus(
        run_id=case.record.run_id, valid=True, sandbox_path=case.record.sandbox,
        status="needs-human",
    ))
    records = [parked]
    if mutation == "missing":
        records = []
    elif mutation == "ambiguous":
        records = [parked, parked]
    elif mutation == "replaced":
        records = [replace(parked, session="replacement")]
    elif mutation == "replaced_branch":
        records = [replace(parked, branch="replacement")]
    elif mutation == "replaced_repository":
        records = [replace(parked, repository="other/repository")]
    elif mutation == "replaced_sandbox":
        replacement = str(case.sandbox.parent.parent / "replacement" / case.sandbox.name)
        records = [replace(
            parked,
            sandbox=replacement,
            status=replace(parked.status, sandbox_path=replacement),
        )]
    elif mutation == "status":
        records = [replace(parked, status=replace(parked.status, status="running"))]
    monkeypatch.setenv("WORKLINK_REPO", str(case.sandbox.parent))
    monkeypatch.setattr(factory_state, "load_factory_records_for_issue", lambda *args: records)
    monkeypatch.setattr(
        dispatch_failures, "current_failure_record",
        (
            lambda *args: {**case.incident, "occurrence_id": "replacement-occurrence"}
            if mutation == "stale" else case.incident
        ),
    )
    monkeypatch.setattr(
        factory_state, "factory_process_is_alive", lambda record: mutation == "alive",
    )
    monkeypatch.setattr(
        factory_state, "factory_process_is_verified_dead",
        lambda record: mutation not in {"alive", "unverified"},
    )
    active = (
        {scope.issue_id} if mutation == "same_issue"
        else {999} if mutation == "cap" else set()
    )
    monkeypatch.setattr(
        autonomy, "make_claims",
        lambda home: SimpleNamespace(_active_worklink_lock_ids_for_scope=lambda **kwargs: active),
    )
    monkeypatch.setattr(autonomy, "factory_max_concurrent", lambda: 1)
    launch = Mock(side_effect=AssertionError("refused recovery launched"))
    monkeypatch.setattr(detached_dispatch, "launch_detached_worklink", launch)

    output = await registry.worklink_resume.coroutine(runtime=_retained_runtime(scope))

    assert reason in output
    launch.assert_not_called()


def test_central_policy_grants_only_five_retained_repo_tools(retained_incident) -> None:
    scope = derive_retained_factory_scope(
        retained_incident.event, retained_incident.service,
    ).scope
    assert scope is not None
    allowed = {"repo_status", "repo_diff", "repo_test", "repo_stage", "repo_commit"}
    service = ac.ServicePrincipal(
        canonical="poller:worklink-ready-queue", trigger="poller",
        authority_profile="github", capabilities=tuple(sorted(allowed)),
        readable_domains=("repository",),
        sink_destinations=("bound_pull_request",),
    )
    for tool in ac._REPO_TOOL_ACTIONS:
        decision = ac.authorize_repo_pr_tool(
            tool, scope, service_principal=service, enforce=True,
            flow_direction=ac.get_tool_flow_direction(tool),
        )
        assert decision.allowed is (tool in allowed)
        assert decision.repo_pr_action_scope is scope
        assert decision.result_integrity == ("trusted" if tool in allowed else "untrusted")

    wrong_service = replace(service, canonical="poller:other")
    wrong_trigger = replace(service, trigger="scheduled_tick")
    wrong_profile = replace(service, authority_profile="custom")
    missing_capability = replace(
        service, capabilities=tuple(sorted(allowed - {"repo_commit"})),
    )
    for principal in (
        wrong_service, wrong_trigger, wrong_profile, missing_capability,
    ):
        assert ac.authorize_repo_pr_tool(
            "repo_commit", scope, service_principal=principal, enforce=True,
            flow_direction=ac.ToolFlowDirection.SINK,
        ).allowed is False


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("prompt", "ready-queue prompt is not an incident"),
        ("service", "service is not the Worklink ready queue"),
        ("stale", "incident occurrence is stale"),
        ("inactive", "incident occurrence is stale"),
        ("missing", "retained factory record is missing"),
        ("ambiguous", "retained factory record is ambiguous"),
        ("replaced", "retained factory target was replaced"),
        ("leaf", "incident is not a retained factory run"),
        ("unverifiable", "process death cannot be verified"),
    ],
)
def test_scope_refusal_cases(retained_incident, mutation: str, reason: str) -> None:
    case = retained_incident
    if mutation == "prompt":
        case.event.extra = {"poller_name": "worklink-ready-queue", "items": []}
    elif mutation == "service":
        case.service.canonical = "poller:other"
    elif mutation == "stale":
        case.item["failure_occurrence_id"] = "superseded"
        case.item["delivery_key"] = (
            f"worklink-run-failure:1810:{case.item['error_signature']}:superseded"
        )
    elif mutation == "inactive":
        state = dispatch_failure_state_dir(case.home) / "dispatch_failures.json"
        payload = __import__("json").loads(state.read_text())
        payload["issues"]["1810"]["active"] = False
        state.write_text(__import__("json").dumps(payload))
    elif mutation == "missing":
        (case.home / "state/worklink/factory-runs/chainlink-1810.json").unlink()
    elif mutation == "ambiguous":
        save_factory_record(case.home, replace(case.record, run_id="1810"))
    elif mutation == "replaced":
        save_factory_record(case.home, replace(case.record, attempt=2))
    elif mutation == "leaf":
        state = dispatch_failure_state_dir(case.home) / "dispatch_failures.json"
        payload = __import__("json").loads(state.read_text())
        payload["issues"]["1810"]["run_id"] = None
        state.write_text(__import__("json").dumps(payload))
    else:
        save_factory_record(case.home, replace(case.record, handle=None))

    result = derive_retained_factory_scope(case.event, case.service)
    assert result.scope is None
    assert reason in (result.refusal_reason or "")


def test_effect_lease_revalidates_and_refuses_busy_resource(
    retained_incident, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = retained_incident
    scope = derive_retained_factory_scope(case.event, case.service).scope
    assert scope is not None
    monkeypatch.setattr(
        "mimir.worklink.retained_scope._factory_session_lock_is_fresh", lambda _record: False,
    )
    with retained_factory_effect_lease(case.home, scope) as lease:
        assert lease.scope == scope
        with factory_issue_resource_lock(case.home, 1810) as acquired:
            assert acquired is False

    with factory_issue_resource_lock(case.home, 1810) as acquired:
        assert acquired is True
        with retained_factory_effect_lease(case.home, scope) as lease:
            assert lease.scope is None
            assert lease.refusal_reason == "factory issue resource lock unavailable"


def test_legacy_run_id_uses_the_same_issue_resource_lock(retained_incident) -> None:
    case = retained_incident
    legacy_sandbox = case.sandbox.with_name("1810")
    case.sandbox.rename(legacy_sandbox)
    canonical_record = case.home / "state/worklink/factory-runs/chainlink-1810.json"
    canonical_record.unlink()
    save_factory_record(
        case.home, replace(case.record, run_id="1810", sandbox=str(legacy_sandbox)),
    )
    state = dispatch_failure_state_dir(case.home) / "dispatch_failures.json"
    payload = __import__("json").loads(state.read_text())
    payload["issues"]["1810"].update({"run_id": "1810", "work_path": str(legacy_sandbox)})
    state.write_text(__import__("json").dumps(payload))
    scope = derive_retained_factory_scope(case.event, case.service).scope
    assert scope is not None and scope.run_id == "1810"

    with factory_issue_resource_lock(case.home, 1810) as acquired:
        assert acquired
        with retained_factory_effect_lease(case.home, scope) as lease:
            assert lease.scope is None
            assert lease.refusal_reason == "factory issue resource lock unavailable"


@pytest.mark.parametrize(
    "changes",
    [
        {"valid": False},
        {"lock": None},
        {"dead_lock": None},
        {"lock": "absent", "lock_session": "foreign"},
        {"lock": "stale", "lock_session": "foreign"},
    ],
)
def test_session_lock_reconciliation_fails_closed(
    retained_incident, monkeypatch: pytest.MonkeyPatch, changes: dict[str, object],
) -> None:
    case = retained_incident
    status = {
        "valid": True, "run_id": case.record.run_id,
        "sandbox_path": case.record.sandbox, "lock": "stale",
        "dead_lock": False, "lock_session": case.record.session,
    }
    status.update(changes)
    monkeypatch.setattr(
        "mimir.worklink.backends.feature_factory.FeatureFactoryBackend.status",
        lambda *args, **kwargs: SimpleNamespace(**status),
    )
    from mimir.worklink.retained_scope import _factory_session_lock_is_fresh

    with pytest.raises(RuntimeError, match="status identity changed"):
        _factory_session_lock_is_fresh(case.record)


def test_retained_backend_dispatches_both_effects_under_lease(
    retained_incident, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = retained_incident
    scope = derive_retained_factory_scope(case.event, case.service).scope
    assert scope is not None
    backend = RetainedCheckoutFilesystemBackend(case.root)
    target = case.sandbox / "fix.py"
    monkeypatch.setattr(
        backend, "_binding", lambda _path: (SimpleNamespace(), scope, target),
    )
    monkeypatch.setattr(
        "mimir.worklink.retained_scope._factory_session_lock_is_fresh", lambda _record: False,
    )
    calls: list[tuple[str, str, dict[str, object]]] = []

    class Client:
        def factory_file_operation(self, operation: str, path: str, **arguments: object):
            calls.append((operation, path, arguments))
            return {
                "status": "ok", "path": path,
                **({"occurrences": 1} if operation == "edit_file" else {}),
            }

    monkeypatch.setattr(
        "mimir.worklink.worker_client.WorkerClient.for_factory_checkout",
        lambda *args, **kwargs: Client(),
    )
    assert backend.write(str(target), "new\n").error is None
    assert backend.edit(str(target), "new", "fixed").error is None
    assert [call[0] for call in calls] == ["write_file", "edit_file"]
    assert all(call[1] == ".factory-sandboxes/chainlink-1810/fix.py" for call in calls)


@pytest.mark.parametrize(
    "race", ["stale", "replaced", "legacy", "alive", "unverifiable", "fresh", "interlock"],
)
def test_effect_lease_refuses_operation_time_races(
    retained_incident, monkeypatch: pytest.MonkeyPatch, race: str,
) -> None:
    case = retained_incident
    scope = derive_retained_factory_scope(case.event, case.service).scope
    assert scope is not None
    monkeypatch.setattr(
        "mimir.worklink.retained_scope._factory_session_lock_is_fresh",
        lambda _record: race == "fresh",
    )
    monkeypatch.setattr(
        "mimir.worklink.retained_scope.factory_process_is_alive",
        lambda _record: race == "alive",
    )
    monkeypatch.setattr(
        "mimir.worklink.retained_scope.factory_process_is_verified_dead",
        lambda _record: race != "unverifiable",
    )
    if race == "stale":
        state = dispatch_failure_state_dir(case.home) / "dispatch_failures.json"
        payload = __import__("json").loads(state.read_text())
        payload["issues"]["1810"]["occurrence_id"] = "replacement"
        state.write_text(__import__("json").dumps(payload))
    elif race == "replaced":
        save_factory_record(case.home, replace(case.record, branch="replacement"))
    elif race == "legacy":
        (case.home / "state/worklink/factory-runs/chainlink-1810.json").unlink()
        save_factory_record(case.home, replace(case.record, run_id="1810"))

    if race == "interlock":
        from mimir.worklink.factory_state import factory_checkout_interlock

        outer = factory_checkout_interlock(case.home, pruning=True)
    else:
        outer = __import__("contextlib").nullcontext(True)
    with outer as acquired:
        assert acquired
        with retained_factory_effect_lease(case.home, scope) as lease:
            assert lease.scope is None
            expected = {
                "stale": "incident occurrence is stale",
                "replaced": "retained factory target was replaced",
                "legacy": "retained factory target was replaced",
                "alive": "retained factory process is alive",
                "unverifiable": "retained factory process death cannot be verified",
                "fresh": "factory session lock is fresh",
                "interlock": "factory checkout interlock unavailable",
            }[race]
            assert lease.refusal_reason == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["write", "edit", "test", "stage", "commit"])
@pytest.mark.parametrize("race", ["replaced", "live", "interlock", "legacy"])
async def test_each_retained_effect_refuses_operation_time_race_without_mutation(
    retained_incident,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    race: str,
) -> None:
    """Every mutating seam reacquires the exact lease before its downstream effect."""
    from mimir.tools import repo as repo_module
    from mimir.worklink import worker_client

    case = retained_incident
    _initialize_retained_repository(case)
    scope = derive_retained_factory_scope(case.event, case.service).scope
    assert scope is not None
    monkeypatch.setattr(
        "mimir.worklink.retained_scope._factory_session_lock_is_fresh", lambda _record: False,
    )
    monkeypatch.setattr(
        "mimir.worklink.retained_scope.factory_process_is_alive",
        lambda _record: race == "live",
    )
    monkeypatch.setattr(
        "mimir.worklink.retained_scope.factory_process_is_verified_dead", lambda _record: True,
    )
    if race == "replaced":
        save_factory_record(case.home, replace(case.record, branch="replacement"))
    elif race == "legacy":
        (case.home / "state/worklink/factory-runs/chainlink-1810.json").unlink()
        save_factory_record(case.home, replace(case.record, run_id="1810"))

    files_before = {
        path.relative_to(case.sandbox).as_posix(): (path.stat().st_mode, path.read_bytes())
        for path in case.sandbox.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(case.sandbox).parts
    }
    index_before = (case.sandbox / ".git" / "index").read_bytes()
    refs_before = _git(case.sandbox, "show-ref")
    head_before = _git(case.sandbox, "rev-parse", "HEAD")
    owner_rpc = Mock(side_effect=AssertionError("refused effect reached owner RPC"))
    file_rpc = Mock(side_effect=AssertionError("refused effect reached file RPC"))
    project_test = AsyncMock(side_effect=AssertionError("refused effect launched tests"))
    monkeypatch.setattr(worker_client, "run_factory_control", owner_rpc)
    monkeypatch.setattr(worker_client.WorkerClient, "for_factory_checkout", file_rpc)
    monkeypatch.setattr(repo_module.RepoProjectTests, "execute", project_test)

    if race == "interlock":
        from mimir.worklink.factory_state import factory_checkout_interlock

        outer = factory_checkout_interlock(case.home, pruning=True)
    else:
        outer = __import__("contextlib").nullcontext(True)
    expected = {
        "replaced": "retained factory target was replaced",
        "live": "retained factory process is alive",
        "interlock": "factory checkout interlock unavailable",
        "legacy": "retained factory target was replaced",
    }[race]
    backend = RetainedCheckoutFilesystemBackend(case.root)
    target = case.sandbox / "fix.txt"
    monkeypatch.setattr(
        backend, "_binding", lambda _path: (SimpleNamespace(), scope, target),
    )
    runtime = _retained_runtime(scope)

    with outer as acquired:
        assert acquired
        if operation == "write":
            outcome = backend.write(str(case.sandbox / "blocked.txt"), "blocked\n")
            assert expected in (outcome.error or "")
        elif operation == "edit":
            outcome = backend.edit(str(target), "before", "after")
            assert expected in (outcome.error or "")
        else:
            tool = getattr(repo_module, f"repo_{operation}")
            arguments = {
                "stage": (("fix.txt",),),
                "commit": (("fix.txt",), "must not commit"),
            }.get(operation, ())
            with pytest.raises(Exception, match=expected):
                if operation == "test":
                    await tool.coroutine(scope.repository, scope.issue_id, runtime=runtime)
                else:
                    tool.func(scope.repository, scope.issue_id, *arguments, runtime=runtime)

    files_after = {
        path.relative_to(case.sandbox).as_posix(): (path.stat().st_mode, path.read_bytes())
        for path in case.sandbox.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(case.sandbox).parts
    }
    assert files_after == files_before
    assert (case.sandbox / ".git" / "index").read_bytes() == index_before
    assert _git(case.sandbox, "show-ref") == refs_before
    assert _git(case.sandbox, "rev-parse", "HEAD") == head_before
    owner_rpc.assert_not_called()
    file_rpc.assert_not_called()
    project_test.assert_not_awaited()


@pytest.mark.parametrize(
    "unsafe", ["ancestor", "symlink", "hardlink", "directory", "fifo", "mode", "owner"],
)
def test_resource_lock_hardening_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str,
) -> None:
    if unsafe == "ancestor":
        target = tmp_path / "target"
        target.mkdir()
        (tmp_path / "state").symlink_to(target, target_is_directory=True)
        with factory_issue_resource_lock(tmp_path, 1810) as acquired:
            assert acquired is False
        assert not (target / "worklink").exists()
        return
    directory = tmp_path / "state" / "worklink"
    directory.mkdir(parents=True)
    lock = directory / "factory-issue-1810.lock"
    if unsafe == "symlink":
        lock.symlink_to(tmp_path / "elsewhere")
    elif unsafe == "hardlink":
        source = tmp_path / "source"
        source.touch(mode=0o600)
        os.link(source, lock)
    elif unsafe == "directory":
        lock.mkdir()
    elif unsafe == "fifo":
        os.mkfifo(lock)
    elif unsafe == "mode":
        lock.touch(mode=0o644)
    else:
        lock.touch(mode=0o600)
    inode = lock.lstat().st_ino
    if unsafe == "owner":
        original = os.fstat

        def foreign_owner(fd: int):
            value = original(fd)
            if value.st_ino == inode:
                values = {
                    name: getattr(value, name)
                    for name in dir(value)
                    if name.startswith("st_")
                }
                values["st_uid"] = value.st_uid + 1
                return SimpleNamespace(**values)
            return value

        from mimir.worklink import factory_state

        monkeypatch.setattr(factory_state.os, "fstat", foreign_owner)
    with factory_issue_resource_lock(tmp_path, 1810) as acquired:
        assert acquired is False
    assert lock.lstat().st_ino == inode


def _git(path: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *arguments],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _retained_runtime(scope: RetainedFactoryScope) -> SimpleNamespace:
    return SimpleNamespace(context=SimpleNamespace(
        retained_factory_scope=scope,
        canonical_principal="poller:worklink-ready-queue",
        is_service=True,
    ))


def _initialize_retained_repository(case: SimpleNamespace) -> None:
    _git(case.sandbox, "init", "-q")
    _git(case.sandbox, "config", "user.name", "untrusted")
    _git(case.sandbox, "config", "user.email", "untrusted@example.invalid")
    _git(case.sandbox, "checkout", "-q", "-b", case.record.branch)
    (case.sandbox / ".gitignore").write_text(".factory/\n", encoding="utf-8")
    (case.sandbox / "fix.txt").write_text("before\n", encoding="utf-8")
    _git(case.sandbox, "add", ".gitignore", "fix.txt")
    _git(case.sandbox, "commit", "-q", "-m", "seed")


@pytest.mark.asyncio
async def test_retained_repo_tools_complete_real_multistep_flow_under_owner_control(
    retained_incident, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.git_bootstrap import DEFAULT_USER_EMAIL, DEFAULT_USER_NAME
    from mimir.tools import repo as repo_module
    from mimir.worklink import worker_client

    case = retained_incident
    _initialize_retained_repository(case)
    nested = case.sandbox / ".factory" / case.record.run_id / "worktrees" / "slice"
    nested.mkdir(parents=True)
    _git(nested, "init", "-q")
    scope = derive_retained_factory_scope(case.event, case.service).scope
    assert scope is not None
    monkeypatch.setattr(
        "mimir.worklink.retained_scope._factory_session_lock_is_fresh", lambda _record: False,
    )
    calls: list[tuple[str, ...]] = []

    def owner_control(checkout, argv, *, env, timeout, output_limit):
        assert checkout == case.sandbox
        calls.append(tuple(argv))
        return subprocess.run(
            argv,
            env={**env, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"},
            capture_output=True,
            check=False,
        )

    monkeypatch.setattr(worker_client, "run_factory_control", owner_control)
    runtime = _retained_runtime(scope)
    marker = case.sandbox / "controller-executed"
    helper = case.sandbox / "malicious.sh"
    helper.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n", encoding="utf-8")
    helper.chmod(0o755)
    hooks = case.sandbox / "hooks"
    hooks.mkdir()
    (hooks / "pre-commit").write_text(helper.read_text(encoding="utf-8"), encoding="utf-8")
    (hooks / "pre-commit").chmod(0o755)
    _git(case.sandbox, "config", "core.hooksPath", str(hooks))
    _git(case.sandbox, "config", "core.fsmonitor", str(helper))
    _git(case.sandbox, "config", "filter.evil.clean", str(helper))
    (case.sandbox / ".gitattributes").write_text("fix.txt filter=evil\n", encoding="utf-8")
    (case.sandbox / "fix.txt").write_text("after\n", encoding="utf-8")

    status = repo_module.repo_status.func(scope.repository, scope.issue_id, runtime=runtime)
    diff = repo_module.repo_diff.func(scope.repository, scope.issue_id, runtime=runtime)
    assert "fix.txt" in status["stdout"]
    assert "+after" in diff["stdout"]

    async def passing_test(self, selectors, *, suite):
        assert self._retained_scope == scope
        return ProjectTestResult(True, "tests_passed", 0)

    monkeypatch.setattr(repo_module.RepoProjectTests, "execute", passing_test)
    tested = await repo_module.repo_test.coroutine(
        scope.repository, scope.issue_id, runtime=runtime,
    )
    assert tested["ok"] is True
    repo_module.repo_stage.func(
        scope.repository, scope.issue_id, ("fix.txt",), runtime=runtime,
    )
    committed = repo_module.repo_commit.func(
        scope.repository, scope.issue_id, ("fix.txt",), "retained fix", runtime=runtime,
    )

    assert committed["provenance"] == {
        "kind": "retained_factory_scope", "scope_id": scope.scope_id,
        "repository": scope.repository, "issue_id": scope.issue_id,
        "run_id": scope.run_id,
    }
    assert _git(case.sandbox, "branch", "--show-current") == scope.branch
    assert _git(case.sandbox, "show", "-s", "--format=%an <%ae>") == (
        f"{DEFAULT_USER_NAME} <{DEFAULT_USER_EMAIL}>"
    )
    assert (case.sandbox / "fix.txt").read_text(encoding="utf-8") == "after\n"
    assert not marker.exists()
    assert calls and all("core.hooksPath=/dev/null" in argv for argv in calls)


@pytest.mark.asyncio
async def test_retained_repo_test_runs_source_git_through_owner_control(
    retained_incident, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir import contained_checkout, contained_snapshot, project_tests
    from mimir.worklink import worker_client

    case = retained_incident
    _initialize_retained_repository(case)
    scope = derive_retained_factory_scope(case.event, case.service).scope
    assert scope is not None
    (case.home / "worklink.yaml").write_text(
        "defaults:\n  test_command: /usr/bin/true\n", encoding="utf-8",
    )
    marker = case.sandbox / "controller-executed"
    helper = case.sandbox / "malicious.sh"
    helper.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n", encoding="utf-8")
    helper.chmod(0o755)
    hooks = case.sandbox / "hooks"
    hooks.mkdir()
    (hooks / "pre-commit").write_text(helper.read_text(encoding="utf-8"), encoding="utf-8")
    (hooks / "pre-commit").chmod(0o755)
    _git(case.sandbox, "config", "core.hooksPath", str(hooks))
    _git(case.sandbox, "config", "core.fsmonitor", str(helper))
    _git(case.sandbox, "config", "filter.evil.clean", str(helper))
    (case.sandbox / ".gitattributes").write_text("fix.txt filter=evil\n", encoding="utf-8")
    _git(case.sandbox, "add", ".gitattributes", "hooks/pre-commit", "malicious.sh")
    _git(case.sandbox, "commit", "-q", "--no-verify", "-m", "adversarial config fixture")
    marker.unlink(missing_ok=True)

    calls: list[tuple[str, ...]] = []
    controller_git_calls: list[tuple[str, ...]] = []
    subprocess_run = subprocess.run

    def owner_control(checkout, argv, *, env, timeout, output_limit):
        assert checkout == case.sandbox
        calls.append(tuple(argv))
        return subprocess_run(
            argv,
            env={**env, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"},
            capture_output=True,
            check=False,
        )

    def controller_subprocess(argv, *args, **kwargs):
        controller_git_calls.append(tuple(os.fsdecode(part) for part in argv))
        return subprocess_run(argv, *args, **kwargs)

    monkeypatch.setattr(worker_client, "run_factory_control", owner_control)
    monkeypatch.setattr(contained_snapshot.subprocess, "run", controller_subprocess)
    monkeypatch.setattr(
        project_tests.RepoGitTools, "validated_checkout_root",
        lambda _self: case.sandbox,
    )
    checkout_root = case.home / "repo-test-checkouts"
    checkout_root.mkdir()
    monkeypatch.setattr(contained_checkout, "REPO_TEST_CHECKOUT_ROOT", checkout_root)
    monkeypatch.setattr(
        contained_checkout, "_open_repo_test_checkout",
        lambda relative: os.open(checkout_root / relative, os.O_RDONLY | os.O_DIRECTORY),
    )
    monkeypatch.setattr(contained_checkout.os, "chown", lambda *_a, **_k: None)
    monkeypatch.setattr(contained_checkout.os, "fchown", lambda *_a, **_k: None)
    monkeypatch.setattr(contained_checkout, "_normalize_checkout_fd", lambda *_a, **_k: None)

    async def runner(*args, **kwargs):
        capability = args[1]
        assert stat.S_IMODE(capability.path.parent.stat().st_mode) == 0o700
        return CollectedExecutionResult(0, b"passed", b"", False, False, 0, 0)

    result = await RepoProjectTests(retained_scope=scope, runner=runner).execute()

    assert result.ok is True
    assert not marker.exists()
    source_calls = [argv for argv in calls if argv[:3] == ("git", "-C", str(case.sandbox))]
    assert any("ls-files" in argv for argv in source_calls)
    assert any("rev-parse" in argv for argv in source_calls)
    assert any("bundle" in argv and "create" in argv for argv in source_calls)
    assert all(argv[0:3] == ("git", "-C", str(case.sandbox)) for argv in source_calls)
    assert not any(
        len(argv) >= 3 and argv[:3] == ("git", "-C", str(case.sandbox))
        for argv in controller_git_calls
    )
    assert not tuple(case.sandbox.glob(".mimir-repo-test-*.bundle"))


@pytest.mark.asyncio
async def test_retained_repo_test_excludes_only_factory_control_plane(
    retained_incident, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir import project_tests

    case = retained_incident
    _initialize_retained_repository(case)
    scope = derive_retained_factory_scope(case.event, case.service).scope
    assert scope is not None
    (case.home / "worklink.yaml").write_text(
        "defaults:\n  test_command: /usr/bin/true\n", encoding="utf-8",
    )
    checkout = SimpleNamespace(
        path=case.sandbox,
        capability=SimpleNamespace(path=case.sandbox),
        close=lambda: None,
    )
    factory_arguments: list[dict[str, object]] = []

    class GitTools:
        def __init__(self, *args, **kwargs):
            pass

        def validated_checkout_root(self):
            return case.sandbox

    def checkout_factory(source, **kwargs):
        assert source == case.sandbox
        factory_arguments.append(kwargs)
        return checkout

    async def runner(*args, **kwargs):
        return CollectedExecutionResult(0, b"passed", b"", False, False, 0, 0)

    monkeypatch.setattr(project_tests, "RepoGitTools", GitTools)
    result = await RepoProjectTests(
        retained_scope=scope, runner=runner, checkout_factory=checkout_factory,
    ).execute()

    assert result.ok is True
    assert factory_arguments == [{
        "scope_id": scope.scope_id,
        "pr_number": scope.issue_id,
        "known_sensitive": (),
        "excluded_prefixes": (b".factory",),
        "source_git_runner": project_tests.retained_factory_subprocess_runner,
        "bundle_provider": project_tests.retained_factory_snapshot_bundle,
    }]


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("repo_checkout", ()), ("repo_fetch", ()), ("repo_push", ()),
        ("repo_merge", ()), ("repo_rebase", ()),
        ("repo_revert", ("a" * 40,)),
    ],
)
def test_retained_scope_refuses_excluded_repo_operations(
    retained_incident, monkeypatch: pytest.MonkeyPatch, name: str, arguments: tuple[str, ...],
) -> None:
    from mimir.tools import repo as repo_module

    case = retained_incident
    scope = derive_retained_factory_scope(case.event, case.service).scope
    assert scope is not None
    tool = getattr(repo_module, name)
    with pytest.raises(Exception, match="retained factory scope does not grant"):
        tool.func(scope.repository, scope.issue_id, *arguments, runtime=_retained_runtime(scope))


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["status", "diff", "test", "stage", "commit"])
async def test_each_retained_repo_operation_refuses_stale_occurrence_before_execution(
    retained_incident, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    from mimir.tools import repo as repo_module
    from mimir.worklink import worker_client

    case = retained_incident
    scope = derive_retained_factory_scope(case.event, case.service).scope
    assert scope is not None
    state = dispatch_failure_state_dir(case.home) / "dispatch_failures.json"
    payload = __import__("json").loads(state.read_text())
    payload["issues"]["1810"]["occurrence_id"] = "replacement"
    state.write_text(__import__("json").dumps(payload))
    control = Mock()
    monkeypatch.setattr(worker_client, "run_factory_control", control)
    runtime = _retained_runtime(scope)
    tool = getattr(repo_module, f"repo_{operation}")
    arguments = {
        "stage": (("fix.txt",),),
        "commit": (("fix.txt",), "message"),
    }.get(operation, ())

    with pytest.raises(Exception, match="incident occurrence is stale"):
        if operation == "test":
            await tool.coroutine(scope.repository, scope.issue_id, runtime=runtime)
        else:
            tool.func(scope.repository, scope.issue_id, *arguments, runtime=runtime)
    control.assert_not_called()
