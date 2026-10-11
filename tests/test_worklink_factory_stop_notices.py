"""Closed inventory of agent-facing factory attempt endings."""

from __future__ import annotations

import ast
import asyncio
from dataclasses import replace
from datetime import UTC, datetime
import inspect

import pytest

from mimir.worklink import orchestrator
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
from tests.test_worklink_orchestrator import _factory_lifecycle_record, _factory_lifecycle_status, cp
from mimir.worklink.compute import LaunchHandle


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
