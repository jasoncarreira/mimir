from __future__ import annotations

from datetime import UTC, datetime
import subprocess
from pathlib import Path
from typing import Sequence

from mimir.worklink.evidence import TestResult, WorklinkEvidence, observe_evidence, validate_evidence


def base_evidence(**overrides: object) -> WorklinkEvidence:
    values = dict(
        issue=439,
        attempt=1,
        backend="codex",
        branch="issue/439-a1",
        worktree=".worklink/439-1",
        started_at="2026-06-11T05:00:00+00:00",
        finished_at="2026-06-11T05:05:00+00:00",
        files_changed=["mimir/worklink/evidence.py"],
        diff_stat="1 file changed, 10 insertions(+)",
        commands=[],
        tests=TestResult("pytest", 0, "passed"),
        pr_url="https://github.com/example/repo/pull/1",
        status="completed",
        blocked_reason=None,
        transcript=None,
        diff_observed=True,
    )
    values.update(overrides)
    return WorklinkEvidence(**values)  # type: ignore[arg-type]


def test_completed_empty_diff_demotes_to_failed() -> None:
    result = validate_evidence(base_evidence(files_changed=[]))

    assert result.status == "failed"
    assert result.review_ready is False
    assert "completed_empty_diff" in result.reasons


def test_review_rejects_unobserved_fabricated_tests() -> None:
    result = validate_evidence(base_evidence(tests=TestResult("pytest", 0, "backend says passed", observed=False)))

    assert result.status == "failed"
    assert result.review_ready is False
    assert "tests_not_observed" in result.reasons


def test_explicit_skipped_tests_can_be_review_ready() -> None:
    result = validate_evidence(base_evidence(tests=TestResult(None, skipped_reason="docs only")))

    assert result.status == "completed"
    assert result.review_ready is True
    assert result.reasons == ()


def test_blocked_requires_reason() -> None:
    result = validate_evidence(base_evidence(status="blocked", blocked_reason=""))

    assert result.status == "failed"
    assert "blocked_missing_reason" in result.reasons


def test_blocked_reason_does_not_require_diff_or_tests() -> None:
    result = validate_evidence(
        base_evidence(
            status="blocked",
            blocked_reason="planner contradiction",
            files_changed=[],
            tests=None,
            diff_observed=False,
        )
    )

    assert result.status == "blocked"
    assert result.review_ready is False
    assert result.reasons == ()


def test_observe_evidence_carries_backend_blocked_reason(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    (repo / "a.txt").write_text("old\n")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)

    result = observe_evidence(
        issue=466,
        attempt=1,
        backend="codex",
        branch="issue/466-a1",
        worktree=repo,
        started_at=datetime(2026, 6, 13, 12, tzinfo=UTC),
        base_ref="main",
        backend_status="blocked",
        blocked_reason="Acceptance criteria conflict with review criteria",
        test_command=None,
    )

    assert result.status == "blocked"
    assert result.review_ready is False
    assert result.reasons == ()
    assert result.evidence.blocked_reason == "Acceptance criteria conflict with review criteria"


def test_gate_test_summary_keeps_output_tail_not_head() -> None:
    """chainlink #815: pytest prints the failure list LAST — the evidence test
    summary must keep the tail so retries can act on it."""
    from mimir.worklink.evidence import _summarize_test_output

    lines = [f"noise-{index}" for index in range(1, 101)] + [
        "FAILED tests/test_z.py::test_gate - AssertionError",
        "1 failed, 9 passed in 3.16s",
    ]
    result = subprocess.CompletedProcess(
        ["pytest"], 1, stdout="\n".join(lines), stderr="warning: deprecation"
    )

    summary = _summarize_test_output(result)

    assert "1 failed, 9 passed in 3.16s" in summary
    assert "FAILED tests/test_z.py::test_gate" in summary
    assert "warning: deprecation" in summary
    assert "noise-1\n" not in summary
    assert len(summary) <= 6000


def test_gate_command_not_found_is_not_tests_failed() -> None:
    """chainlink #820: exit 127 = the gate command itself cannot run — an
    environment error, distinct from failing tests."""
    result = validate_evidence(base_evidence(tests=TestResult("pytest -q", 127, "pytest: not found")))

    assert result.review_ready is False
    assert "gate_command_not_found" in result.reasons
    assert "tests_failed" not in result.reasons


def test_completed_requires_tests_or_skipped_reason() -> None:
    result = validate_evidence(base_evidence(tests=None))

    assert result.status == "failed"
    assert result.review_ready is False
    assert "tests_missing" in result.reasons


def test_completed_requires_passing_tests() -> None:
    result = validate_evidence(base_evidence(tests=TestResult("pytest", 1, "failed")))

    assert result.status == "failed"
    assert result.review_ready is False
    assert "tests_failed" in result.reasons


def test_completed_requires_observed_diff() -> None:
    result = validate_evidence(base_evidence(diff_observed=False))

    assert result.status == "failed"
    assert result.review_ready is False
    assert "diff_not_observed" in result.reasons


def test_observe_evidence_uses_executor_diff_and_test_results(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    (repo / "a.txt").write_text("old\n")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)
    (repo / "a.txt").write_text("new\n")

    result = observe_evidence(
        issue=439,
        attempt=1,
        backend="codex",
        branch="issue/439-a1",
        worktree=repo,
        started_at=datetime(2026, 6, 11, 5, tzinfo=UTC),
        base_ref="main",
        backend_status="completed",
        test_command="test -f a.txt",
    )

    assert result.review_ready is True
    assert result.evidence.files_changed == ["a.txt"]
    assert result.evidence.tests is not None
    assert result.evidence.tests.exit_code == 0


def test_observe_evidence_sees_untracked_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)
    (repo / "new_module.py").write_text("print('new')\n")

    result = observe_evidence(
        issue=439,
        attempt=1,
        backend="codex",
        branch="issue/439-a1",
        worktree=repo,
        started_at=datetime(2026, 6, 11, 5, tzinfo=UTC),
        base_ref="main",
        backend_status="completed",
        test_command="python -c 'import sys; sys.exit(0)'",
    )

    assert result.review_ready is True
    assert result.evidence.files_changed == ["new_module.py"]


def test_observe_evidence_sees_committed_backend_work(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)
    subprocess.run(["git", "switch", "-q", "-c", "issue/439-a1"], cwd=repo, check=True)
    (repo / "new_test.py").write_text("def test_new():\n    assert True\n")
    subprocess.run(["git", "add", "new_test.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "backend work"], cwd=repo, check=True)

    result = observe_evidence(
        issue=439,
        attempt=1,
        backend="codex",
        branch="issue/439-a1",
        worktree=repo,
        started_at=datetime(2026, 6, 11, 5, tzinfo=UTC),
        base_ref="main",
        backend_status="completed",
        test_command="python -c 'import sys; sys.exit(0)'",
    )

    assert result.review_ready is True
    assert result.evidence.files_changed == ["new_test.py"]
