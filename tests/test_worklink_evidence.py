from __future__ import annotations

from datetime import UTC, datetime
import subprocess
from pathlib import Path
from typing import Sequence

from mimir.worklink.evidence import (
    BLOCKED_SENTINEL,
    TestResult,
    WorklinkEvidence,
    observe_evidence,
    validate_evidence,
)


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


def test_blocked_with_reason_stays_blocked_and_not_review_ready() -> None:
    result = validate_evidence(
        base_evidence(status="blocked", blocked_reason="acceptance criteria contradict #438")
    )

    assert result.status == "blocked"
    assert result.review_ready is False
    assert result.evidence.blocked_reason == "acceptance criteria contradict #438"


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


def _seed_repo(repo: Path) -> None:
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)


def test_observe_evidence_detects_blocked_sentinel(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _seed_repo(repo)
    # The backend partially edited a file, then concluded it cannot proceed and
    # wrote the blocked sentinel. The "completed" backend_status must not win.
    (repo / "partial.py").write_text("# half-done\n")
    (repo / BLOCKED_SENTINEL).write_text(
        "Acceptance criteria contradict issue #438; needs planner review.\n"
    )

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

    assert result.status == "blocked"
    assert result.review_ready is False
    assert result.evidence.blocked_reason == (
        "Acceptance criteria contradict issue #438; needs planner review."
    )
    # The sentinel is a control signal, never a deliverable in the changed set.
    assert BLOCKED_SENTINEL not in result.evidence.files_changed
    assert "partial.py" in result.evidence.files_changed


def test_observe_evidence_ignores_empty_blocked_sentinel(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _seed_repo(repo)
    (repo / "new_module.py").write_text("print('ok')\n")
    (repo / BLOCKED_SENTINEL).write_text("   \n")  # whitespace only == no signal

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

    assert result.status == "completed"
    assert result.review_ready is True
    assert result.evidence.blocked_reason is None
    assert BLOCKED_SENTINEL not in result.evidence.files_changed
    assert result.evidence.files_changed == ["new_module.py"]
