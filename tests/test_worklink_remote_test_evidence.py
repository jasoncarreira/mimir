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
    backend_completed,
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


# ─── backend_completed gate predicate ──────────────────────────────────

def test_backend_completed_normalizes_synonyms_and_excludes_failures():
    assert backend_completed("completed") is True
    assert backend_completed("SUCCESS") is True
    assert backend_completed(" succeeded ") is True
    assert backend_completed("failed") is False
    assert backend_completed("blocked") is False
    assert backend_completed("needs_human") is False


# ─── fold_remote_test_evidence ─────────────────────────────────────────

def test_fold_passing_test_job_on_completed_run_makes_review_ready(tmp_path: Path):
    folded = fold_remote_test_evidence(
        _remote_validation(tmp_path), "echo ok", 0, backend_status="completed"
    )
    assert folded.review_ready is True
    assert folded.status == "completed"
    assert folded.evidence.tests.observed is True
    assert folded.evidence.tests.exit_code == 0


def test_fold_failing_test_job_blocks_review(tmp_path: Path):
    folded = fold_remote_test_evidence(
        _remote_validation(tmp_path), "echo ok", 1, backend_status="completed"
    )
    assert folded.review_ready is False
    assert folded.status == "failed"
    assert "tests_failed" in folded.reasons
    assert folded.evidence.tests.observed is True  # observed, but failed


def test_fold_failing_test_job_carries_failure_tail_into_summary(tmp_path: Path):
    """chainlink #815 remote trusted gate: the test job's failure detail must
    survive into the folded evidence so retry feedback can act on it."""
    folded = fold_remote_test_evidence(
        _remote_validation(tmp_path),
        "pytest -q",
        1,
        backend_status="completed",
        failure_tail="command: pytest -q\nexit: 1\nFAILED tests/test_a.py::t - boom",
    )
    assert folded.review_ready is False
    assert "FAILED tests/test_a.py::t - boom" in folded.evidence.tests.summary
    assert "remote sandboxed test job: exit 1" in folded.evidence.tests.summary

    # A PASSING fold PRESERVES the tail too (PR #1018 review): a
    # retried-then-passed job carries its retry marker there.
    passing = fold_remote_test_evidence(
        _remote_validation(tmp_path), "pytest -q", 0, backend_status="completed",
        failure_tail="trusted job retried (first: exit 1, retry: exit 0)",
    )
    assert passing.review_ready is True
    assert "trusted job retried (first: exit 1" in passing.evidence.tests.summary


def test_gate_failure_detail_prefers_stdout_then_folded_evidence(tmp_path: Path):
    from mimir.worklink.worker import gate_failure_detail

    folded = fold_remote_test_evidence(
        _remote_validation(tmp_path), "pytest -q", 1, backend_status="completed",
        failure_tail="FAILED tests/test_a.py::t - boom",
    )
    # No section in the implementation job's stdout → falls back to the folded
    # remote-gate evidence.
    assert "FAILED tests/test_a.py::t - boom" in gate_failure_detail(folded, "worklink worker: completed")
    # A stdout section (worker's local gate) wins when present.
    stdout = "WORKLINK_TESTS_TAIL_BEGIN\nlocal gate detail\nWORKLINK_TESTS_TAIL_END\n"
    assert gate_failure_detail(folded, stdout) == "local gate detail"
    # Review-ready evidence with passing tests yields nothing.
    passing = fold_remote_test_evidence(
        _remote_validation(tmp_path), "pytest -q", 0, backend_status="completed"
    )
    assert gate_failure_detail(passing, "worklink worker: completed") is None


def test_fold_cannot_launder_a_failed_run_even_when_tests_pass(tmp_path: Path):
    # Defense in depth: even if fold is reached on a non-completed backend run
    # (e.g. a partial diff got pushed), a passing test job must NOT flip it to
    # review-ready — the original backend status governs.
    folded = fold_remote_test_evidence(
        _remote_validation(tmp_path), "echo ok", 0, backend_status="failed"
    )
    assert folded.review_ready is False
    assert folded.status == "failed"


def test_fold_does_not_make_a_blocked_run_review_ready(tmp_path: Path):
    folded = fold_remote_test_evidence(
        _remote_validation(tmp_path), "echo ok", 0, backend_status="blocked"
    )
    assert folded.review_ready is False  # never review-ready off a non-completed run
    assert folded.status != "completed"


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
async def test_run_test_only_failure_emits_tests_tail_section(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """chainlink #815: the remote trusted gate's failure output must survive the
    test container via stdout — otherwise the retry loop stays blind whenever the
    worker's internal gate passed but the controller's fresh job failed."""
    failure_output = "\n".join(
        [f"line-{index}" for index in range(1, 81)]
        + ["FAILED tests/test_remote.py::test_gate - AssertionError", "1 failed in 2.2s"]
    )

    def runner(args, *, cwd=None):
        if args == "pytest -q":
            return _cp(args, returncode=1, stdout=failure_output)
        return _cp(args)

    code = await _run_test_only(_test_only_payload(tmp_path, "pytest -q"), runner=runner)

    assert code == 1
    out = capsys.readouterr().out
    assert "WORKLINK_TESTS_TAIL_BEGIN" in out
    section = out.split("WORKLINK_TESTS_TAIL_BEGIN", 1)[1].split("WORKLINK_TESTS_TAIL_END", 1)[0]
    assert "command: pytest -q" in section
    assert "exit: 1" in section
    assert "FAILED tests/test_remote.py::test_gate" in section
    assert "line-1\n" not in section  # tail, not head
    # The machine-readable JSON status line is still the FINAL line.
    assert out.rstrip().splitlines()[-1].startswith('{"test_only": true')


@pytest.mark.asyncio
async def test_run_test_only_success_emits_no_tail_section(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    def runner(args, *, cwd=None):
        if args == "echo ok":
            return _cp(args, returncode=0, stdout="all good")
        return _cp(args)

    code = await _run_test_only(_test_only_payload(tmp_path), runner=runner)
    assert code == 0
    assert "WORKLINK_TESTS_TAIL" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_run_test_only_setup_failure_returns_sentinel(tmp_path: Path):
    def runner(args, *, cwd=None):
        if isinstance(args, list) and args[:2] == ["git", "clone"]:
            return _cp(args, returncode=128, stdout="fatal: repo not found")
        return _cp(args)

    code = await _run_test_only(_test_only_payload(tmp_path), runner=runner)
    assert code == 70  # distinct setup-failure sentinel (still non-zero → fails closed)


def test_fold_passing_retry_keeps_flaky_marker_review_ready(tmp_path: Path):
    """PR #1018 review blocker: exit_code=0 + retry note must persist the note
    (the accepted flaky-pass case) while remaining review-ready."""
    folded = fold_remote_test_evidence(
        _remote_validation(tmp_path), "env -u MIMIR_MODEL_SPEC uv run pytest -q", 0,
        backend_status="completed",
        failure_tail="trusted job retried (first: exit 1, retry: exit 0)\nFAILED tests/test_a.py::t",
    )
    assert folded.review_ready is True
    assert folded.evidence.tests.exit_code == 0
    assert "remote sandboxed test job: exit 0" in folded.evidence.tests.summary
    assert "trusted job retried" in folded.evidence.tests.summary
