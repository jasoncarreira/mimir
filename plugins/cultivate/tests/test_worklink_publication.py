"""Publication fencing after an operator-stopped leaf attempt."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
import signal
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mimir_cultivate.worklink import control, orchestrator
from mimir_cultivate.worklink.backends.base import RawResult
from mimir_cultivate.worklink.backends.registry import WorklinkConfig
from mimir_cultivate.worklink.checkout import CheckoutLease
from mimir_cultivate.worklink.claims import ChainlinkClaims, ClaimRecord
from mimir_cultivate.worklink.compute import ComputeResult
from mimir_cultivate.worklink.evidence import EvidenceValidation, WorklinkEvidence
from mimir_cultivate.worklink.run_state import WorklinkRunState, save_run_state


ISSUE = 2037


def _evidence_dir(home: Path) -> Path:
    directory = home / "state/worklink/evidence"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest.mark.asyncio
async def test_stopped_attempt_does_not_fence_next_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use the operator command and both real finalize paths, including the fence."""
    started = datetime.now(UTC)
    claim = ClaimRecord(ISSUE, 1, "agent", started)
    state = WorklinkRunState(
        issue_id=ISSUE, attempt=1, backend="opencode", compute_name="local_subprocess",
        handle_substrate="local_subprocess", handle_identifier="1234", process_start_ticks=42,
        branch=f"issue/{ISSUE}-a1", base_ref="feature/plugins", local_base="feature/plugins",
        repo=str(tmp_path), repo_url="", test_command=None, started_at=started.isoformat(),
    )
    save_run_state(tmp_path, state)
    comments = [claim.to_comment()]

    def runner(args, **kwargs):
        if args[:3] == ["chainlink", "issue", "comment"]:
            comments.append(args[-1])
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(control, "process_is_alive", lambda state: True)
    monkeypatch.setattr(control, "process_identity_verified", lambda state: True)

    async def cancel(self, handle):
        assert control.operator_stop_requested(tmp_path, ISSUE, 1)

    monkeypatch.setattr(control.LocalSubprocessComputeBackend, "cancel", cancel)
    stopped = await asyncio.to_thread(control.stop_worklink, tmp_path, ISSUE, runner=runner)
    assert stopped.stopped

    claims = ChainlinkClaims(agent_id="agent", home_path=tmp_path, runner=runner)
    worker = orchestrator.WorklinkRunner(home=tmp_path, repo=tmp_path)
    issue = orchestrator.IssueContext(ISSUE, "title", "body", set())

    def terminal(attempt: int) -> orchestrator._TerminalClaimRelease:
        return orchestrator._TerminalClaimRelease(
            claims, home=tmp_path, issue_id=ISSUE, attempt=attempt,
        )

    stopped_result = await worker._finalize(
        issue=issue, claims=claims, claim_record=claim, attempt=1, config=None,
        backend=SimpleNamespace(name="opencode", interpret=Mock(side_effect=AssertionError("gate reached"))),
        compute=None, compute_result=ComputeResult(-signal.SIGTERM, "", ""),
        order=None, lease=CheckoutLease(ISSUE, 1, tmp_path, tmp_path, state.branch, "feature/plugins"),
        spec=None, started=started, test_cmd=None, root_dirty_before=(),
        runner=runner, terminal_release=terminal(1),
    )
    assert stopped_result.status == "stopped"
    prior = json.loads(stopped_result.evidence_path.read_text())
    assert prior["status"] == "stopped" and prior["pr_url"] is None

    async def interpret(order, compute_result):
        return RawResult(0, None, "success", None)

    async def observe_evidence(**kwargs):
        evidence = WorklinkEvidence(
            issue=ISSUE, attempt=2, backend="opencode", branch=f"issue/{ISSUE}-a2",
            checkout=str(tmp_path), started_at=started.isoformat(),
            finished_at=datetime.now(UTC).isoformat(), files_changed=["fix.py"],
            diff_stat="1 file changed", commands=[], tests=None, pr_url=None,
            status="completed", base_ref="feature/plugins", diff_observed=True,
        )
        return EvidenceValidation("completed", True, (), evidence)

    monkeypatch.setattr(orchestrator, "observe_evidence", observe_evidence)
    monkeypatch.setattr(orchestrator, "_with_outside_checkout_detection", lambda result, **kw: result)
    monkeypatch.setattr(orchestrator, "_commit_checkout_changes", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_ensure_clean_checkout", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_git_push", lambda *a, **kw: None)
    monkeypatch.setattr(orchestrator, "_with_head_sha", lambda result, *a, **kw: result)
    monkeypatch.setattr(orchestrator, "_cleanup_checkout_after_transition", lambda *a, **kw: None)
    opened = []

    def open_pr(*args, **kwargs):
        opened.append(args)
        return "https://example.test/pr/2037"

    monkeypatch.setattr(orchestrator, "_open_pr", open_pr)
    second = await worker._finalize(
        issue=issue, claims=claims, claim_record=ClaimRecord(ISSUE, 2, "agent", started),
        attempt=2, config=WorklinkConfig(), backend=SimpleNamespace(name="opencode", interpret=interpret),
        compute=None, compute_result=ComputeResult(0, "", ""), order=None,
        lease=CheckoutLease(ISSUE, 2, tmp_path, tmp_path, f"issue/{ISSUE}-a2", "feature/plugins"),
        spec=SimpleNamespace(backend_config={}), started=started, test_cmd=None,
        root_dirty_before=(), runner=runner, terminal_release=terminal(2),
    )
    assert second.status == "completed", second.reason
    assert second.pr_url == "https://example.test/pr/2037"
    assert len(opened) == 1
    assert json.loads(second.evidence_path.read_text())["pr_url"] == second.pr_url


@pytest.mark.parametrize("payload", [
    {"status": "unknown", "pr_url": None},
    {"pr_url": None},
    "not an object",
    "{not json",
    {"status": "stopped", "pr_url": "https://example.test/pr/1"},
    {"status": "stopped", "pr_url": ""},
    {"status": "completed", "pr_url": "https://example.test/pr/1"},
    {"status": "failed", "pr_url": "https://example.test/pr/1"},
    {"status": "blocked", "pr_url": "https://example.test/pr/1"},
])
def test_publication_fails_closed_on_prior_evidence(tmp_path: Path, payload: object) -> None:
    path = _evidence_dir(tmp_path) / f"{ISSUE}-1.json"
    path.write_text(payload if isinstance(payload, str) and payload.startswith("{not") else json.dumps(payload))
    with pytest.raises((orchestrator.WorklinkError, json.JSONDecodeError)):
        with orchestrator._leaf_publication(tmp_path, ISSUE, 2):
            pytest.fail("publication allowed with unsafe evidence")
    assert not (tmp_path / f"state/worklink/publications/{ISSUE}.json").exists()


def test_publication_fails_closed_on_existing_intent(tmp_path: Path) -> None:
    intent = tmp_path / f"state/worklink/publications/{ISSUE}.json"
    intent.parent.mkdir(parents=True)
    intent.write_text("{}")
    with pytest.raises(FileExistsError):
        with orchestrator._leaf_publication(tmp_path, ISSUE, 2):
            pytest.fail("existing intent was overwritten")
    assert intent.read_text() == "{}"
