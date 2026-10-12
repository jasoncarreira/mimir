"""Closed inventory of agent-facing factory attempt endings."""

from __future__ import annotations

import ast
import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import inspect
import hashlib
import textwrap
from pathlib import Path

import pytest

from mimir.worklink import orchestrator
from mimir.worklink import evidence as evidence_module
from mimir.worklink import claims as claims_module
from mimir.worklink import dispatch_failures
from mimir.worklink.backends.feature_factory import FactoryStatus
from mimir.worklink.claims import ClaimRecord
from mimir.worklink.dispatch_failures import (
    FACTORY_STOP_KINDS,
    dispatch_failure_state_dir,
    load_failure_state,
    pending_failure_alerts,
    record_failure,
)
from mimir.worklink.orchestrator import IssueContext, WorklinkRunner
from tests.test_worklink_orchestrator import (
    _factory_lifecycle_record,
    _factory_lifecycle_status,
    _run_factory_preflight_case,
    cp,
)
from mimir.worklink.compute import ComputeResult, LaunchHandle
from mimir.worklink.poller_hooks import report_live_delivery_receipts


ENDING_NOTICES = {
    "success": "factory_success",
    "factory_decided_park": "factory_needs_human",
    "budget_park": "factory_needs_human",
    "idle_park": "factory_needs_human",
    "lock_refusal_park": "factory_needs_human",
    "blocked": "factory_blocked",
    "partial": "factory_partial",
    "factory_exception": "incident",
    "factory_interruption": "incident",
    "factory_recovery": "incident",
    "factory_stall": "incident",
    "factory_wrapper": "incident",
    "operator_stop": None,  # Explicit operator instruction, not an agent intervention.
}

# This inventory covers the leaf result statuses AND pre/post-claim endings.
# A new leaf status or exit needs an explicit delivery mapping or exemption.
LEAF_ENDING_NOTICES = {
    "completed": None,  # Completed evidence becomes review_ready; the PR is the notice.
    "review_ready": None,  # Review is operator-owned.
    "blocked": "leaf_blocked",
    "failed": "incident",  # leaf_result and reattach incidents already prompt.
    "stopped": None,  # Explicit operator stop.
    "quota_hold": None,  # Scheduled quota retry, no decision needed.
    "attempts_exhausted": "leaf_attempts_exhausted",
    "template_invalid": "leaf_template_invalid",
    "claim_reaped": "leaf_claim_reaped",
}


def test_leaf_ending_inventory_is_closed():
    from mimir.worklink.dispatch_failures import LEAF_TRANSITION_KINDS

    source = inspect.getsource(WorklinkRunner._finalize)
    tree = ast.parse(textwrap.dedent(source))
    returned = {
        arg.value for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "WorklinkRunResult"
        for arg in node.args[2:3] if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    }
    # The evidence validator supplies the dynamic statuses used by _finalize.
    evidence_tree = ast.parse(inspect.getsource(evidence_module.validate_evidence))
    evidence_statuses = {
        item.value for node in ast.walk(evidence_tree) if isinstance(node, ast.Set)
        for item in node.elts if isinstance(item, ast.Constant) and isinstance(item.value, str)
    }
    dynamic = {"review_ready" if status == "completed" else status for status in evidence_statuses}
    final_statuses = returned | dynamic | {
        node.value.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
        for target in node.targets if isinstance(target, ast.Name) and target.id == "transition_status"
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
    }
    assert evidence_statuses == {"completed", "blocked", "failed"}
    assert final_statuses == {"review_ready", "blocked", "failed", "stopped", "quota_hold"}
    assert set(LEAF_ENDING_NOTICES) == final_statuses | {
        "completed", "attempts_exhausted", "template_invalid", "claim_reaped",
    }
    assert set(LEAF_ENDING_NOTICES.values()) - {None, "incident"} == LEAF_TRANSITION_KINDS
    assert 'kind="leaf_blocked"' in source
    assert '"leaf_attempts_exhausted"' in inspect.getsource(claims_module.ChainlinkClaims._attempts_exhausted)
    assert '"leaf_claim_reaped"' in inspect.getsource(claims_module.ChainlinkClaims.reap_stale_claims)
    assert 'kind="leaf_template_invalid"' in inspect.getsource(orchestrator._demote_template_invalid_ready_leaf)


def test_factory_ending_inventory_is_closed():
    tree = ast.parse(inspect.getsource(orchestrator))
    producers = {
        keyword.value.value
        for node in ast.walk(tree) if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "producer" and isinstance(keyword.value, ast.Constant)
        and isinstance(keyword.value.value, str) and keyword.value.value.startswith("factory_")
    }
    expected = (
        {"success", "factory_decided_park", "budget_park", "idle_park",
         "lock_refusal_park", "operator_stop"}
        | (FactoryStatus.TERMINAL_STATUSES - {"completed"}) | producers
    )
    assert set(ENDING_NOTICES) == expected
    assert set(ENDING_NOTICES.values()) - {None, "incident", "factory_success"} == FACTORY_STOP_KINDS
    # Each independent parked exit must continue recording, even outside _finish_factory_070.
    source = inspect.getsource(WorklinkRunner._supervise_factory_070)
    assert source.count("return await park_for_reason(") == 5
    assert 'kind="factory_needs_human"' in source  # lock refusal
    assert inspect.getsource(WorklinkRunner._finish_factory_070).count("_record_factory_milestone(") == 2


@pytest.mark.parametrize("ending", list(ENDING_NOTICES))
def test_every_factory_ending_has_one_terminal_agent_notice(tmp_path, monkeypatch, ending):
    state_dir = dispatch_failure_state_dir(tmp_path)
    expected = ENDING_NOTICES[ending]
    if ending in {"factory_exception", "factory_interruption", "factory_recovery",
                  "factory_stall", "factory_wrapper"}:
        record_failure(state_dir, issue_id=700, attempt=1, exit_status=1,
                       error=f"{ending} stopped", log_path=None, run_id="700")
        _, alerts = pending_failure_alerts(state_dir)
        assert len(alerts) == 1 and "prompt" in alerts[0] and "signal" not in alerts[0]
        assert not load_failure_state(state_dir).get("factory_transitions")
        return
    if ending == "operator_stop":
        from mimir.worklink.control import _mark_operator_stop

        _mark_operator_stop(tmp_path, 700, 1)
        orchestrator._record_run_failure(home=tmp_path, issue_id=700, attempt=1,
                                         error="operator stopped", exit_status=1, autonomous=True)
        assert not load_failure_state(state_dir).get("factory_transitions")
        assert pending_failure_alerts(state_dir)[1] == []
        return

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    factory_status = "needs-human" if ending.endswith("park") else (
        "completed" if ending == "success" else ending
    )
    reason = f"{ending} requires a decision"
    status = replace(_factory_lifecycle_status(sandbox, status=factory_status),
                     terminal_result={"reason": reason})
    record = _factory_lifecycle_record(sandbox, LaunchHandle("local_subprocess", "123", 456))
    record = record.observed(status, datetime.now(UTC).isoformat())
    if ending in {"budget_park", "idle_park", "lock_refusal_park"}:
        record = replace(record, controller_error=reason, controller_phase="parked")
    class Claims:
        def transition_issue(self, *args, **kwargs):
            assert kwargs["status"] == ("review" if ending == "success" else "blocked")

    async def verify(**kwargs):
        return tmp_path / "evidence.json", "https://github.com/owner/repo/pull/42"

    monkeypatch.setattr(orchestrator, "_verify_factory_completion", verify)
    result = asyncio.run(WorklinkRunner(home=tmp_path, repo=tmp_path)._finish_factory_070(
        issue=IssueContext(700, "epic", "build", {"worklink:epic"}),
        claim_record=ClaimRecord(700, 1, "agent", datetime.now(UTC)),
        claims=Claims(), backend=object(), compute=object(), factory_record=record,
        test_cmd="pytest -q", runner=lambda args: cp(args), started_at=datetime.now(UTC),
    ))
    if ending == "success":
        orchestrator._record_factory_milestone(
            tmp_path, kind="factory_success", issue=IssueContext(700, "epic", "build", set()),
            run_id="700", attempt=1, pr_url=result.pr_url,
        )
    entries = list(load_failure_state(state_dir)["factory_transitions"].values())
    assert len(entries) == 1 and entries[0]["kind"] == expected
    assert pending_failure_alerts(state_dir)[1] == []


@pytest.mark.parametrize("status", ["needs-human", "blocked", "partial"])
def test_factory_stop_ledger_failure_preserves_result_and_labels(tmp_path, monkeypatch, status):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    record = _factory_lifecycle_record(sandbox, LaunchHandle("local_subprocess", "123", 456))
    record = record.observed(_factory_lifecycle_status(sandbox, status=status),
                             datetime.now(UTC).isoformat())
    transitions = []

    class Claims:
        def transition_issue(self, issue_id, **kwargs):
            transitions.append((issue_id, kwargs))

    def fail(*args, **kwargs):
        raise OSError("ledger unavailable")

    monkeypatch.setattr(dispatch_failures, "record_factory_transition", fail)
    result = asyncio.run(WorklinkRunner(home=tmp_path, repo=tmp_path)._finish_factory_070(
        issue=IssueContext(700, "epic", "build", {"worklink:epic"}),
        claim_record=ClaimRecord(700, 1, "agent", datetime.now(UTC)),
        claims=Claims(), backend=object(), compute=object(), factory_record=record,
        test_cmd="pytest -q", runner=lambda args: cp(args), started_at=datetime.now(UTC),
    ))
    assert result.status == status
    assert len(transitions) == 1 and transitions[0][1]["status"] == "blocked"
    assert transitions[0][1]["review_ready"] is False
    assert not load_failure_state(dispatch_failure_state_dir(tmp_path)).get("factory_transitions")


@pytest.mark.parametrize("autonomous", [False, True])
@pytest.mark.parametrize("ending", ["needs-human", "blocked", "partial"])
def test_real_factory_stop_is_live_receipt_for_manual_and_autonomous_runs(
    tmp_path, monkeypatch, autonomous, ending,
):
    """Use the public epic path, real supervisor/ending/writer, and receipt reader."""
    original_supervise = WorklinkRunner._supervise_factory_070
    lifecycle = []

    async def supervise_actual(_supervisor, **kwargs):
        current = kwargs["factory_record"]
        status = replace(
            _factory_lifecycle_status(Path(current.sandbox), status=ending),
            run_id=current.run_id, branch=current.branch,
            terminal_result={"reason": f"{ending} needs a decision"},
        )
        stopped = asyncio.Event()

        class Compute:
            async def wait(self, handle, timeout_s):
                await stopped.wait()
                return ComputeResult(-15, "", "cancelled", handle=handle)

            async def cancel(self, handle):
                lifecycle.append("cancel")
                stopped.set()

            async def cleanup(self, handle):
                lifecycle.append("cleanup")

            def job_alive(self, handle):
                return not stopped.is_set()

        class Backend:
            poll_interval_s = 1

            def status(self, *args, **kwargs):
                return status

            def heartbeat(self, *args, **kwargs):
                pass

        kwargs.update(backend=Backend(), compute=Compute())
        return await original_supervise(_supervisor, **kwargs)

    result, launched, _, _ = _run_factory_preflight_case(
        tmp_path, monkeypatch, credentials={"GITHUB_TOKEN": "github-token"},
        autonomous=autonomous, outcome=ending, terminal=supervise_actual,
        sync_entrypoint=True,
    )
    assert result.status == ending
    assert len(launched) == 1
    assert lifecycle == ["cancel", "cleanup"]
    state_dir = dispatch_failure_state_dir(tmp_path)
    entries = list(load_failure_state(state_dir)["factory_transitions"].values())
    stop_kind = {
        "needs-human": "factory_needs_human",
        "blocked": "factory_blocked",
        "partial": "factory_partial",
    }[ending]
    # Stop notices intentionally include operator-launched (non-autonomous) runs;
    # only the start/success informational milestones have the autonomous gate.
    assert [entry["kind"] for entry in entries] == (
        (["factory_start"] if autonomous else []) + [stop_kind]
    )
    # Keep an unrelated live incident in the same ledger: a malformed stop
    # would abort the whole reader and lose both this and all milestones.
    incident = record_failure(
        state_dir, issue_id=701, attempt=1, exit_status=1,
        error="unrelated live failure", log_path=None, run_id="701",
    )
    incident_key = (
        f"worklink-run-failure:701:{incident['signature']}:{incident['occurrence_id']}"
    )
    expected_live = {
        hashlib.sha256(key.encode()).hexdigest()
        for key in [incident_key, *(entry["delivery_key"] for entry in entries)]
    }
    reported = []
    report_live_delivery_receipts(state_dir, tmp_path, lambda live: reported.append(set(live)))
    assert reported == [expected_live]
    assert entries[-1]["status"] == ending
    assert isinstance(entries[-1]["sandbox"], str)
    assert isinstance(entries[-1]["record_path"], str)


@pytest.mark.parametrize("kind", sorted(FACTORY_STOP_KINDS))
@pytest.mark.parametrize("projection", ["missing_record", "missing_status", "stale_status"])
def test_stop_writer_normalizes_optional_or_stale_projection(tmp_path, kind, projection):
    record = _factory_lifecycle_record(
        tmp_path / "sandbox", LaunchHandle("local_subprocess", "123", 456),
    )
    # FactoryRunRecord requires an absolute sandbox; the no-record fallback
    # still needs a string rather than None in the delivery ledger.
    if projection == "missing_record":
        record = None
    elif projection == "stale_status":
        record = record.observed(
            _factory_lifecycle_status(tmp_path / "sandbox", status="running"),
            datetime.now(UTC).isoformat(),
        )
    orchestrator._record_factory_milestone(
        tmp_path, kind=kind, issue=IssueContext(700, "epic", "build", set()),
        run_id="700", attempt=1, factory_record=record,
    )
    state_dir = dispatch_failure_state_dir(tmp_path)
    entry, = load_failure_state(state_dir)["factory_transitions"].values()
    assert entry["status"] == {
        "factory_needs_human": "needs-human",
        "factory_blocked": "blocked",
        "factory_partial": "partial",
    }[kind]
    assert entry["sandbox"] == ("" if record is None else record.sandbox)
    assert isinstance(entry["record_path"], str)
    reported = []
    report_live_delivery_receipts(state_dir, tmp_path, lambda live: reported.append(set(live)))
    assert reported == [{hashlib.sha256(entry["delivery_key"].encode()).hexdigest()}]
