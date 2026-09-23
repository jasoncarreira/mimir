from __future__ import annotations

from dataclasses import replace
import fcntl
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from mimir import access_control as ac
from mimir.models import RetainedFactoryScope
from mimir.readonly_backend import RetainedCheckoutFilesystemBackend
from mimir.worklink.compute import LaunchHandle
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
        assert tool in ac._NON_INGESTING_RESULT_TOOLS


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
    "race", ["stale", "replaced", "alive", "unverifiable", "fresh", "interlock"],
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
                "alive": "retained factory process is alive",
                "unverifiable": "retained factory process death cannot be verified",
                "fresh": "factory session lock is fresh",
                "interlock": "factory checkout interlock unavailable",
            }[race]
            assert lease.refusal_reason == expected


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
