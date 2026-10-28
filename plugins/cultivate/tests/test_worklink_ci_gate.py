"""Head-bound draft publication and repair, using fake forge and CI responses."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from mimir_cultivate.repository import RepositoryInventory
from mimir_cultivate.worklink import ci_gate, control, orchestrator
from mimir_cultivate.worklink.backends.base import RawResult
from mimir_cultivate.worklink.backends.base import WorkOrder
from mimir_cultivate.worklink.backends.registry import WorklinkConfig, WorklinkDefaults
from mimir_cultivate.worklink.checkout import CheckoutLease
from mimir_cultivate.worklink.claims import ChainlinkClaims, ClaimRecord
from mimir_cultivate.worklink.compute import ComputeResult
from mimir_cultivate.worklink.evidence import EvidenceValidation, WorklinkEvidence, validate_evidence


A, B, PR = "a" * 40, "b" * 40, "https://github.com/acme/widget/pull/1"


class Forge:
    def __init__(self, heads: dict[str, list[dict]], *, change_head: bool = False):
        self.heads = heads
        self.head = A
        self.current = {sha: 0 for sha in heads}
        self.body = "Closes chainlink-2050.\n\nWorklink evidence:\n- Gate mode: `ci`\n"
        self.ready = False
        self.ready_calls = 0
        self.reruns = 0
        self.change_head = change_head
        self.reads = 0

    def __call__(self, args, **kw):
        result: object = ""
        if args[:3] == ["gh", "pr", "view"]:
            if "headRefOid" in args[-1]:
                self.reads += 1
                if self.change_head and self.reads >= 2:
                    self.head = B
                result = {"headRefOid": self.head, "state": "OPEN", "isDraft": not self.ready}
            else:
                result = {"body": self.body}
        elif args[:2] == ["gh", "api"]:
            path = args[-1]
            if "check-runs?" in path:
                sha = path.split("/commits/")[1].split("/")[0]
                rows = self.heads[sha]
                index = min(self.current[sha], len(rows) - 1)
                check = rows[index]
                # A pair of reads (check-runs and statuses) constitutes one poll.
                result = [{"total_count": 1, "check_runs": [check]}]
            elif "/statuses?" in path:
                sha = path.split("/commits/")[1].split("/")[0]
                self.current[sha] += 1
                result = [[]]
            elif "/rerequest" in path:
                self.reruns += 1
                assert self.reruns <= 1, "infrastructure rerun cap exceeded"
                result = {}
        elif args[:3] == ["gh", "pr", "edit"]:
            self.body = args[-1]
        elif args[:3] == ["gh", "pr", "ready"]:
            self.ready_calls += 1
            self.ready = "--undo" not in args
        elif args[:2] == ["git", "-C"]:
            if "status" in args:
                result = ""
            elif "diff" in args:
                result = ""
            elif "remote.origin.url" in args:
                result = "https://github.com/acme/widget.git"
        return subprocess.CompletedProcess(args, 0, json.dumps(result) if isinstance(result, (dict, list)) else result, "")


def check(conclusion: str, *, output: str = "") -> dict:
    return {"id": 12, "name": "candidate (linux)", "status": "completed", "conclusion": conclusion,
            "output": {"summary": output}}


@pytest.mark.parametrize("scenario", ["green", "repair", "exhausted", "infrastructure", "timeout", "moved", "stopped", "quick_failure", "local"])
@pytest.mark.asyncio
async def test_ci_publication_scenarios(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str) -> None:
    started = datetime.now(UTC)
    if scenario == "infrastructure":
        heads = {A: [check("startup_failure"), check("startup_failure")]}
    elif scenario == "green":
        heads = {A: [check("success")]}
    elif scenario in ("repair", "exhausted"):
        heads = {A: [check("failure", output="test failed"), check("failure")], B: [check("success" if scenario == "repair" else "failure")]}
    elif scenario == "timeout":
        heads = {A: [{**check("failure"), "status": "in_progress", "conclusion": None}]}
    else:
        heads = {A: [check("success")], B: [check("success")]}
    forge = Forge(heads, change_head=scenario == "moved")
    claim = ClaimRecord(2050, 1, "agent", started)
    claims = ChainlinkClaims(agent_id="agent", home_path=tmp_path, runner=forge)
    worker = orchestrator.WorklinkRunner(home=tmp_path, repo=tmp_path)
    issue = orchestrator.IssueContext(2050, "Fix", "body", set())
    lease = CheckoutLease(2050, 1, tmp_path, tmp_path, "issue/2050-a1", "main")
    opened: list[bool] = []
    pushed: list[str] = []
    repair_prompts: list[str] = []

    async def interpret_result(order, result):
        return RawResult(0, None, "success", None)

    @dataclass(frozen=True)
    class Spec:
        repo_url: str = "https://github.com/acme/widget.git"
        base_ref: str = "main"
        timeout_s: int = 5
        backend_config: dict | None = None

    class Backend:
        name = "opencode"
        interpret = staticmethod(interpret_result)

        def work_spec(self, order, **kwargs):
            assert order.checkout == lease.path
            assert kwargs["attempt"] == 1 and kwargs["branch"] == lease.branch
            repair_prompts.append(order.prompt)
            return Spec(backend_config={"test_env": {}})

    class Compute:
        async def launch(self, spec):
            return SimpleNamespace(identifier="42")

        async def wait(self, handle, timeout):
            return ComputeResult(0, "", "")

        async def cleanup(self, handle):
            pass

    async def observe_evidence(**kwargs):
        evidence = WorklinkEvidence(
            issue=2050, attempt=1, backend="opencode", branch=lease.branch, checkout=str(tmp_path),
            started_at=started.isoformat(), finished_at=started.isoformat(), files_changed=["fix.py"],
            diff_stat="1 file", commands=[], tests=None, pr_url=None, status="completed",
            gate_mode=kwargs["gate_mode"], diff_observed=True,
        )
        if scenario == "quick_failure":
            from mimir_cultivate.worklink.evidence import TestResult
            evidence = replace(evidence, tests=TestResult("false", exit_code=1))
        elif scenario == "local":
            from mimir_cultivate.worklink.evidence import TestResult
            evidence = replace(evidence, tests=TestResult("true", exit_code=0))
        return validate_evidence(evidence)

    monkeypatch.setattr(orchestrator, "observe_evidence", observe_evidence)
    monkeypatch.setattr(orchestrator, "_with_outside_checkout_detection", lambda value, **kw: value)
    monkeypatch.setattr(orchestrator, "_commit_checkout_changes", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_ensure_clean_checkout", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_git_push", lambda *a, **kw: pushed.append(forge.head))
    monkeypatch.setattr(orchestrator, "_with_head_sha", lambda value, *a, **kw: replace(value, evidence=replace(value.evidence, head_sha=B if len(pushed) > 1 else A)))
    monkeypatch.setattr(orchestrator, "_open_pr", lambda *a, **kw: opened.append(kw.get("draft", False)) or PR)
    monkeypatch.setattr(orchestrator, "_cleanup_checkout_after_transition", lambda *a, **kw: None)
    if scenario in ("repair", "exhausted"):
        monkeypatch.setattr(orchestrator, "_persist_run_state", lambda *a, **kw: None)
        def push(*a, **kw):
            pushed.append(forge.head)
            if len(pushed) > 1:
                forge.head = B
        monkeypatch.setattr(orchestrator, "_git_push", push)
    if scenario == "stopped":
        monkeypatch.setattr(orchestrator, "operator_stop_requested", lambda *a: len(pushed) > 0)
    config = WorklinkConfig(defaults=WorklinkDefaults(max_ci_repair_rounds=1, ci_wait_timeout_s=1))
    result = await worker._finalize(
        issue=issue, claims=claims, claim_record=claim, attempt=1, config=config,
        backend=Backend(), compute=Compute(), compute_result=ComputeResult(0, "", ""),
        order=WorkOrder(issue_id=2050, checkout=tmp_path, prompt="fix", rules=None, timeout_s=5), lease=lease,
        spec=Spec(backend_config={"model": "test", "test_env": {}}),
        started=started, test_cmd="false" if scenario == "quick_failure" else None,
        root_dirty_before=(), runner=forge,
        terminal_release=orchestrator._TerminalClaimRelease(claims, home=tmp_path, issue_id=2050, attempt=1),
        gate_mode="local" if scenario == "local" else "ci",
    )
    expected = {"green": "completed", "repair": "completed", "local": "completed", "quick_failure": "failed",
                "stopped": "stopped"}.get(scenario, "blocked")
    assert result.status == expected, result.reason
    assert result.review_ready is (expected == "completed")
    assert opened == ([] if scenario == "quick_failure" else [scenario != "local"])
    assert forge.ready is (expected == "completed" and scenario != "local")
    evidence = json.loads(result.evidence_path.read_text())
    if scenario not in ("local", "quick_failure"):
        assert evidence["gate_mode"] == "ci"
    if scenario in ("repair", "exhausted"):
        assert evidence["ci_repair_rounds"] == 1
        assert len(repair_prompts) == 1
        assert "candidate (linux)" in repair_prompts[0]
    if scenario == "infrastructure":
        assert forge.reruns == 1
    if scenario == "moved":
        assert "moved" in result.reason


def test_ci_gate_inventory_and_defaults(tmp_path: Path) -> None:
    inventory = tmp_path / "repositories.yaml"
    row = {"slug": "acme/widget", "root": str(tmp_path), "origin": "https://github.com/acme/widget.git",
           "base_branch": "main", "mode": "rw"}
    import yaml
    inventory.write_text(yaml.safe_dump({"repositories": [row]}))
    assert RepositoryInventory.load(inventory).repositories[0].worklink_gate == "local"
    for mode, command in (("ci", None), ("ci", "ruff check ."), ("local", None)):
        inventory.write_text(yaml.safe_dump({"repositories": [{**row, "worklink_gate": mode, "quick_gate_command": command}]}))
        assert RepositoryInventory.load(inventory).repositories[0].quick_gate_command == command
    for bad in (-1, True, 1.5, "3"):
        with pytest.raises(ValueError, match="max_ci_repair_rounds"):
            WorklinkDefaults(max_ci_repair_rounds=bad)
    config_path = tmp_path / "worklink.yaml"
    config_path.write_text("defaults:\n  max_ci_repair_rounds: 0\n  ci_wait_timeout_s: 90\n")
    defaults = WorklinkConfig.load(config_path).defaults
    assert (defaults.max_ci_repair_rounds, defaults.ci_wait_timeout_s) == (0, 90)


@pytest.mark.asyncio
async def test_head_bound_and_rerun_cap(tmp_path: Path) -> None:
    forge = Forge({A: [check("success")], B: [check("failure")]}, change_head=True)
    result = await ci_gate.wait_for_ci(forge, "acme/widget", PR, A, timeout_s=1, stopped=lambda: False, poll_s=0)
    assert result.outcome == "moved"


@pytest.mark.asyncio
async def test_first_ci_read_refuses_a_temporarily_moved_head() -> None:
    class RevertedForge(Forge):
        def __call__(self, args, **kwargs):
            if args[:2] == ["gh", "api"] and "/statuses?" in args[-1]:
                self.head = A
            return super().__call__(args, **kwargs)

    forge = RevertedForge({A: [check("success")], B: [check("failure")]})
    forge.head = B
    result = await ci_gate.wait_for_ci(forge, "acme/widget", PR, A, timeout_s=1, stopped=lambda: False)
    assert result.outcome == "moved"


@pytest.mark.asyncio
async def test_absent_ci_checks_never_count_as_green() -> None:
    forge = Forge({A: [check("success")]})

    def empty_checks(args, **kwargs):
        if args[:2] == ["gh", "api"] and "check-runs?" in args[-1]:
            return subprocess.CompletedProcess(args, 0, json.dumps([{"total_count": 0, "check_runs": []}]), "")
        return forge(args, **kwargs)

    result = await ci_gate.wait_for_ci(empty_checks, "acme/widget", PR, A,
        timeout_s=0.01, stopped=lambda: False, poll_s=0)
    assert result.outcome == "timeout"


@pytest.mark.asyncio
async def test_ready_refuses_moved_head() -> None:
    forge = Forge({A: [check("success")], B: [check("failure")]}, change_head=True)
    forge.reads = 1
    with pytest.raises(RuntimeError, match="head moved"):
        await ci_gate.ready_pr(forge, PR, A)
    assert not forge.ready
    assert forge.ready_calls == 0


@pytest.mark.asyncio
async def test_rerun_refuses_moved_head() -> None:
    forge = Forge({A: [check("startup_failure")], B: [check("failure")]}, change_head=True)
    forge.reads = 1
    assert not await ci_gate.rerun_infrastructure(forge, "acme/widget", PR, A, (12,))
    assert forge.reruns == 0


@pytest.mark.asyncio
async def test_ready_does_not_accept_changed_head_after_transition() -> None:
    forge = Forge({A: [check("success")], B: [check("failure")]}, change_head=True)
    with pytest.raises(RuntimeError, match="head moved"):
        await ci_gate.ready_pr(forge, PR, A)
    assert not forge.ready


def test_stop_during_ci_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mimir_cultivate.worklink.run_state import WorklinkRunState, save_run_state
    import os
    from mimir_cultivate.worklink.run_state import process_start_ticks

    state = WorklinkRunState(2050, 1, "opencode", "local_subprocess", "controller", str(os.getpid()),
        "issue/2050-a1", "main", "main", str(tmp_path), "https://github.com/acme/widget.git",
        None, datetime.now(UTC).isoformat(), phase="ci_wait", process_start_ticks=process_start_ticks(os.getpid()))
    save_run_state(tmp_path, state)
    commands = []

    def run(args):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    result = control.stop_worklink(tmp_path, 2050, runner=run)
    assert result.stopped
    assert control.operator_stop_requested(tmp_path, 2050, 1)
    assert not commands  # controller owns the terminal transition and claim release
