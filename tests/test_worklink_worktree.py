from __future__ import annotations

from datetime import UTC, datetime, timedelta
import subprocess
from pathlib import Path
from typing import Sequence

from mimir.worklink.worktree import cleanup_worktree, create_worktree, prune_attempt_worktrees, WorktreeLease


def completed(args: Sequence[str], returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(args), returncode, stdout="", stderr="")


def test_create_worktree_uses_attempt_scoped_branch_and_path(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return completed(args)

    lease = create_worktree(tmp_path, issue_id=439, attempt=2, runner=runner)

    assert lease.path == tmp_path / ".worklink" / "439-2"
    assert lease.branch == "issue/439-a2"
    assert lease.base_ref == "main"
    assert lease.local_base == "main"
    assert calls == [
        ["git", "-C", str(tmp_path), "rev-parse", "--verify", "--quiet", "refs/heads/main"],
        ["git", "-C", str(tmp_path), "worktree", "add", "--no-track", "-b", "issue/439-a2", str(lease.path), "main"],
    ]


def test_cleanup_removes_only_successful_worktrees(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return completed(args)

    lease = WorktreeLease(439, 1, tmp_path, tmp_path / ".worklink" / "439-1", "issue/439-a1", "main")

    assert cleanup_worktree(lease, outcome="failed", runner=runner) is False
    assert calls == []
    assert cleanup_worktree(lease, outcome="completed", runner=runner) is True
    assert calls == [["git", "-C", str(tmp_path), "worktree", "remove", "--force", str(lease.path)]]


def test_prune_attempt_worktrees_is_conservative(tmp_path: Path) -> None:
    root = tmp_path / ".worklink"
    old = root / "439-1"
    young = root / "439-2"
    ignored = root / "notes"
    old.mkdir(parents=True)
    young.mkdir()
    ignored.mkdir()
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return completed(args)

    now = datetime.now(UTC)
    old_mtime = (now - timedelta(days=10)).timestamp()
    young_mtime = now.timestamp()
    for path, mtime in [(old, old_mtime), (young, young_mtime), (ignored, old_mtime)]:
        path.touch()
        import os

        os.utime(path, (mtime, mtime))

    pruned = prune_attempt_worktrees(tmp_path, older_than=timedelta(days=3), now=now, runner=runner)

    assert pruned == [old]
    assert calls == [
        ["git", "-C", str(tmp_path), "worktree", "remove", "--force", str(old)],
        ["git", "-C", str(tmp_path), "branch", "-D", "issue/439-a1"],
    ]


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)
    return out.stdout.strip()


def test_create_worktree_real_git_slash_named_remote_base(tmp_path: Path) -> None:
    # Regression for #467: a feature base that exists only as a remote-tracking
    # branch (origin/integration/worklink) must still yield the attempt-scoped
    # branch. With a bare `worktree add -b ... <base>`, git's DWIM ignores -b and
    # checks out the base branch instead. Verified here with real git.
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git(work, "config", "user.email", "t@e.com")
    _git(work, "config", "user.name", "t")
    _git(work, "commit", "-q", "--allow-empty", "-m", "main commit")
    _git(work, "push", "-q", "origin", "HEAD:main")
    _git(work, "checkout", "-q", "-b", "integration/worklink")
    _git(work, "commit", "-q", "--allow-empty", "-m", "feature commit")
    _git(work, "push", "-q", "origin", "integration/worklink")

    # Fresh clone: integration/worklink exists only as origin/integration/worklink.
    repo = tmp_path / "fresh"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "t")

    lease = create_worktree(repo, issue_id=441, attempt=1, base="integration/worklink")

    assert lease.branch == "issue/441-a1"
    assert lease.base_ref == "integration/worklink"
    assert lease.local_base == "origin/integration/worklink"
    # The worktree must be on the attempt branch, NOT the base branch (the DWIM bug).
    assert _git(lease.path, "branch", "--show-current") == "issue/441-a1"
    assert _git(lease.path, "rev-parse", "HEAD") == _git(repo, "rev-parse", "origin/integration/worklink")
    # No stray local branch named after the base was created by DWIM.
    local_branches = _git(repo, "branch", "--format=%(refname:short)").split()
    assert "integration/worklink" not in local_branches
