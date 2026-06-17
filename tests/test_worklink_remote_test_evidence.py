"""Remote test-evidence re-derivation (chainlink #538).

A non-shared (docker-sibling/ecs) run can't run the worker's untrusted branch on
the controller, so the controller used to stub tests ``observed=false`` and the
gate failed closed. Instead the controller now runs a fresh sandboxed test job on
the pushed branch (worker ``test_only`` mode) and folds its exit code in. These
cover the two new pieces in isolation: the worker test-only path and the fold.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence

import pytest

from mimir.worklink.compute import WorkSpec
from mimir.worklink.evidence import (
    EvidenceValidation,
    fold_remote_test_evidence,
    observe_remote_evidence,
)
from mimir.worklink.worker import WorkerPayload, _run_test_only


def _cp(args, returncode=0, stdout="") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")


def _remote_validation(tmp_path: Path, *, name_only="remote.txt\n") -> EvidenceValidation:
    """A diff-observed remote validation with tests still stubbed unobserved —
    exactly what observe_remote_evidence returns before the test job runs."""
    from datetime import datetime, timezone

    def runner(args, *, cwd=None):
        if isinstance(args, list) and "diff" in args and "--name-only" in args:
            return _cp(args, stdout=name_only)
        if isinstance(args, list) and "diff" in args and "--stat" in args:
            return _cp(args, stdout=" remote.txt | 1 +\n")
        return _cp(args)

    return observe_remote_evidence(
        issue=441, attempt=1, backend="fake", branch="issue/441-a1",
        worktree=tmp_path / "wt", started_at=datetime.now(timezone.utc), base_ref="main",
        backend_status="completed", test_command="echo ok", runner=runner,
    )


# ─── fold_remote_test_evidence ─────────────────────────────────────────

def test_fold_passing_test_job_makes_review_ready(tmp_path: Path):
    folded = fold_remote_test_evidence(_remote_validation(tmp_path), "echo ok", 0)
    assert folded.review_ready is True
    assert folded.status == "completed"
    assert folded.evidence.tests.observed is True
    assert folded.evidence.tests.exit_code == 0


def test_fold_failing_test_job_blocks_review(tmp_path: Path):
    folded = fold_remote_test_evidence(_remote_validation(tmp_path), "echo ok", 1)
    assert folded.review_ready is False
    assert folded.status == "failed"
    assert "tests_failed" in folded.reasons
    assert folded.evidence.tests.observed is True  # observed, but failed


# ─── worker test_only path ─────────────────────────────────────────────

def _test_only_payload(tmp_path: Path, test_command: str = "echo ok") -> WorkerPayload:
    spec = WorkSpec(
        issue_id=441, attempt=1, repo_url="git@github.com:o/r.git", base_ref="main",
        branch="issue/441-a1", prompt="p", rules=None, test_command=test_command,
        backend="codex", timeout_s=60, test_only=True,
    )
    return WorkerPayload(spec=spec, repo_dir=tmp_path / "repo", evidence_path=tmp_path / "e.json")


@pytest.mark.asyncio
async def test_run_test_only_checks_out_pushed_branch_and_returns_exit_code(tmp_path: Path):
    calls: list[Sequence[str] | str] = []

    def runner(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess:
        calls.append(args)
        if args == "echo ok":
            return _cp(args, returncode=0)  # tests pass
        return _cp(args)  # git ops succeed

    code = await _run_test_only(_test_only_payload(tmp_path), runner=runner)
    assert code == 0
    # Clones, fetches the PUSHED branch, checks it out detached, then runs tests.
    assert ["git", "clone", "git@github.com:o/r.git", str(tmp_path / "repo")] in calls
    assert ["git", "-C", str(tmp_path / "repo"), "fetch", "origin",
            "+issue/441-a1:refs/remotes/origin/issue/441-a1"] in calls
    assert ["git", "-C", str(tmp_path / "repo"), "checkout", "--detach", "origin/issue/441-a1"] in calls
    assert "echo ok" in calls  # the test command ran (cwd=repo)


@pytest.mark.asyncio
async def test_run_test_only_propagates_test_failure_exit_code(tmp_path: Path):
    def runner(args, *, cwd=None):
        if args == "pytest -q":
            return _cp(args, returncode=1)
        return _cp(args)

    code = await _run_test_only(_test_only_payload(tmp_path, "pytest -q"), runner=runner)
    assert code == 1  # non-zero → caller blocks the gate


@pytest.mark.asyncio
async def test_run_test_only_setup_failure_returns_sentinel(tmp_path: Path):
    def runner(args, *, cwd=None):
        if isinstance(args, list) and args[:2] == ["git", "clone"]:
            return _cp(args, returncode=128, stdout="fatal: repo not found")
        return _cp(args)

    code = await _run_test_only(_test_only_payload(tmp_path), runner=runner)
    assert code == 70  # distinct setup-failure sentinel (still non-zero → fails closed)
