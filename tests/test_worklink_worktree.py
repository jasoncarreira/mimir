from __future__ import annotations

from datetime import UTC, datetime, timedelta
import subprocess
from pathlib import Path
from typing import Sequence

import pytest

from mimir.worklink.worktree import (
    WorktreeLease,
    _assert_self_contained_checkout,
    cleanup_worktree,
    create_isolated_checkout,
    create_worktree,
    prune_attempt_worktrees,
)


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


def test_create_isolated_checkout_has_real_git_dir_and_preserves_origin(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("base\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "push", "-q", "origin", "HEAD:main")

    lease = create_isolated_checkout(repo, issue_id=517, attempt=1, base="main")

    # #517: the isolated clone lives OUTSIDE the parent repo (a sibling), never
    # nested under repo/.worklink, so codex cannot walk up into the repo it was
    # cloned from and there is no clone-into-self.
    assert lease.path == repo.parent / ".worklink" / repo.name / "517-1"
    assert not lease.path.is_relative_to(repo)
    assert lease.branch == "issue/517-a1"
    assert lease.base_ref == "main"
    assert lease.isolated_checkout is True
    assert (lease.path / ".git").is_dir()
    assert _git(lease.path, "rev-parse", "--show-toplevel") == str(lease.path)
    assert _git(lease.path, "branch", "--show-current") == "issue/517-a1"
    assert _git(lease.path, "remote", "get-url", "origin") == str(origin)
    assert _git(lease.path, "rev-parse", "HEAD") == lease.local_base


def test_cleanup_removes_successful_isolated_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    attempt = repo / ".worklink" / "517-1"
    attempt.mkdir(parents=True)
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return completed(args, returncode=1 if args[-2:] == ["-D", "issue/517-a1"] else 0)

    lease = WorktreeLease(517, 1, repo, attempt, "issue/517-a1", "main", isolated_checkout=True)

    assert cleanup_worktree(lease, outcome="completed", runner=runner) is True
    assert not attempt.exists()
    assert calls == [["git", "-C", str(repo), "branch", "-D", "issue/517-a1"]]


def test_self_containment_assert_rejects_parent_pointing_checkout(tmp_path: Path) -> None:
    # A checkout whose git toplevel resolves to the PARENT (the #517 escape shape)
    # must be refused, not silently used.
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    parent = str(tmp_path / "repo")

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        proc = completed(args)
        if args[-1] == "--show-toplevel":
            proc.stdout = parent
        elif args[-1] == "--absolute-git-dir":
            proc.stdout = f"{parent}/.git"
        return proc

    with pytest.raises(RuntimeError, match="self-containment"):
        _assert_self_contained_checkout(attempt, runner=runner)


def test_self_containment_assert_accepts_sound_clone(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("base\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "push", "-q", "origin", "HEAD:main")

    lease = create_isolated_checkout(repo, issue_id=517, attempt=2, base="main")
    # A real clone passes the cheap assert and is rooted at itself, not the parent.
    _assert_self_contained_checkout(lease.path, runner=lambda a: subprocess.run(
        list(a), capture_output=True, text=True, check=False))
    assert _git(lease.path, "rev-parse", "--show-toplevel") == str(lease.path)
