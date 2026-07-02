from __future__ import annotations

from datetime import UTC, datetime, timedelta
import subprocess
from pathlib import Path
from typing import Sequence

import pytest

from mimir.worklink.worktree import (
    SliceMergeConflict,
    SliceMergeSuccess,
    WorktreeLease,
    _assert_self_contained_checkout,
    cleanup_worktree,
    create_integration_branch,
    create_isolated_checkout,
    create_slice_worktree,
    create_worktree,
    merge_slice_into_integration,
    prune_attempt_worktrees,
)


def completed(args: Sequence[str], returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(args), returncode, stdout="", stderr="")


def test_create_worktree_uses_attempt_scoped_branch_and_path(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        if args[-1] == "refs/remotes/origin/main":
            return completed(args, returncode=1)
        return completed(args)

    lease = create_worktree(tmp_path, issue_id=439, attempt=2, runner=runner)

    assert lease.path == tmp_path / ".worklink" / "439-2"
    assert lease.branch == "issue/439-a2"
    assert lease.base_ref == "main"
    assert lease.local_base == "main"
    assert calls == [
        ["git", "-C", str(tmp_path), "fetch", "origin", "main"],
        [
            "git",
            "-C",
            str(tmp_path),
            "rev-parse",
            "--verify",
            "--quiet",
            "refs/remotes/origin/main",
        ],
        ["git", "-C", str(tmp_path), "rev-parse", "--verify", "--quiet", "refs/heads/main"],
        [
            "git",
            "-C",
            str(tmp_path),
            "worktree",
            "add",
            "--no-track",
            "-b",
            "issue/439-a2",
            str(lease.path),
            "main",
        ],
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
    assert old.exists()  # fake git runner did not remove it; real git would
    assert calls == [
        ["git", "-C", str(tmp_path), "worktree", "remove", "--force", str(old)],
        ["git", "-C", str(tmp_path), "branch", "-D", "issue/439-a1"],
    ]


def test_prune_attempt_worktrees_covers_relocated_isolated_checkouts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    root = tmp_path / ".worklink" / repo.name
    old = root / "613-1"
    young = root / "613-2"
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

    pruned = prune_attempt_worktrees(repo, older_than=timedelta(days=3), now=now, runner=runner)

    assert pruned == [old]
    assert not old.exists()
    assert young.exists()
    assert ignored.exists()
    assert calls == [["git", "-C", str(repo), "branch", "-D", "issue/613-a1"]]


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)
    return out.stdout.strip()


def _repo_with_main(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "checkout", "-q", "-b", "main")
    (repo / "shared.txt").write_text("base\n")
    _git(repo, "add", "shared.txt")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "push", "-q", "origin", "HEAD:main")
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text(exclude.read_text(encoding="utf-8") + "\n.worklink/\n", encoding="utf-8")
    return repo


def _repo_with_stale_local_main(tmp_path: Path) -> tuple[Path, Path, str, str]:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "checkout", "-q", "-b", "main")
    (repo / "a.txt").write_text("base\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "push", "-q", "origin", "HEAD:main")
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text(exclude.read_text(encoding="utf-8") + "\n.worklink/\n", encoding="utf-8")
    stale_sha = _git(repo, "rev-parse", "HEAD")

    updater = tmp_path / "updater"
    subprocess.run(["git", "clone", "-q", str(origin), str(updater)], check=True)
    _git(updater, "config", "user.email", "t@e.com")
    _git(updater, "config", "user.name", "t")
    _git(updater, "checkout", "-q", "main")
    (updater / "a.txt").write_text("base\nfresh\n")
    _git(updater, "commit", "-q", "-am", "fresh")
    _git(updater, "push", "-q", "origin", "HEAD:main")
    fresh_sha = _git(updater, "rev-parse", "HEAD")
    assert fresh_sha != stale_sha
    return origin, repo, stale_sha, fresh_sha


@pytest.mark.parametrize("isolated", [False, True])
def test_attempt_base_fetch_uses_fresh_origin_without_mutating_source(
    tmp_path: Path, isolated: bool
) -> None:
    _origin, repo, stale_sha, fresh_sha = _repo_with_stale_local_main(tmp_path)
    head_before = _git(repo, "rev-parse", "HEAD")
    branch_before = _git(repo, "branch", "--show-current")
    status_before = _git(repo, "status", "--short")

    if isolated:
        lease = create_isolated_checkout(repo, issue_id=521, attempt=1, base="main")
    else:
        lease = create_worktree(repo, issue_id=521, attempt=1, base="main")

    assert head_before == stale_sha
    assert _git(repo, "rev-parse", "HEAD") == head_before
    assert _git(repo, "branch", "--show-current") == branch_before
    assert _git(repo, "rev-parse", "refs/heads/main") == stale_sha
    assert _git(repo, "status", "--short") == status_before
    assert _git(repo, "rev-parse", "origin/main") == fresh_sha
    assert _git(lease.path, "rev-parse", "HEAD") == fresh_sha
    assert lease.local_base in ("origin/main", fresh_sha)


def test_base_fetch_failure_falls_back_to_local_base_and_logs_event(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    events: list[tuple[str, dict[str, object]]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        if args[:4] == ["git", "-C", str(tmp_path), "fetch"]:
            return subprocess.CompletedProcess(list(args), 128, stdout="", stderr="network down\n")
        return completed(args)

    def event_logger(event_type: str, **payload: object) -> None:
        events.append((event_type, payload))

    lease = create_worktree(
        tmp_path,
        issue_id=521,
        attempt=2,
        base="main",
        runner=runner,
        event_logger=event_logger,
    )

    assert lease.local_base == "main"
    assert calls[:3] == [
        ["git", "-C", str(tmp_path), "fetch", "origin", "main"],
        ["git", "-C", str(tmp_path), "rev-parse", "--verify", "--quiet", "refs/heads/main"],
        [
            "git",
            "-C",
            str(tmp_path),
            "worktree",
            "add",
            "--no-track",
            "-b",
            "issue/521-a2",
            str(lease.path),
            "main",
        ],
    ]
    assert events == [
        (
            "worklink_base_fetch_failed",
            {
                "repo": str(tmp_path),
                "base": "main",
                "returncode": 128,
                "stdout": "",
                "stderr": "network down",
            },
        )
    ]


def test_base_with_no_origin_counterpart_still_uses_local_branch(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "checkout", "-q", "-b", "main")
    (repo / "a.txt").write_text("base\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "local-only")
    (repo / "a.txt").write_text("local\n")
    _git(repo, "commit", "-q", "-am", "local")
    local_sha = _git(repo, "rev-parse", "local-only")

    lease = create_worktree(repo, issue_id=521, attempt=3, base="local-only")

    assert lease.local_base == "local-only"
    assert _git(lease.path, "rev-parse", "HEAD") == local_sha
    missing = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", "origin/local-only"],
        capture_output=True,
        text=True,
    )
    assert missing.returncode != 0


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


def test_create_integration_branch_uses_fresh_origin_base(tmp_path: Path) -> None:
    origin, repo, stale_sha, fresh_sha = _repo_with_stale_local_main(tmp_path)
    assert origin.exists()
    assert _git(repo, "rev-parse", "main") == stale_sha

    lease = create_integration_branch(
        repo,
        epic_id=771,
        base_ref="main",
        slug="Worktree Integration Branch Ops",
        epic_branch_prefix="integrated/",
    )

    assert lease.branch == "integrated/771-worktree-integration-branch-ops"
    assert lease.path == repo / ".worklink" / "epics" / "771-worktree-integration-branch-ops"
    assert lease.base_ref == "main"
    assert lease.local_base == "origin/main"
    assert _git(lease.path, "branch", "--show-current") == lease.branch
    assert _git(lease.path, "rev-parse", "HEAD") == fresh_sha
    assert _git(repo, "rev-parse", "main") == stale_sha


def test_create_slice_worktree_starts_at_current_integration_head(tmp_path: Path) -> None:
    repo = _repo_with_main(tmp_path)
    integration = create_integration_branch(repo, epic_id=771, base_ref="main")
    (integration.path / "dep.txt").write_text("dependency\n")
    _git(integration.path, "add", "dep.txt")
    _git(integration.path, "commit", "-q", "-m", "dependency")
    integration_head = _git(integration.path, "rev-parse", "HEAD")

    lease = create_slice_worktree(repo, slice_id=772, integration_branch=integration.branch)

    assert lease.branch == "issue/772-a1"
    assert lease.base_ref == integration.branch
    assert lease.local_base == integration_head
    assert _git(lease.path, "branch", "--show-current") == "issue/772-a1"
    assert _git(lease.path, "rev-parse", "HEAD") == integration_head
    assert (lease.path / "dep.txt").read_text() == "dependency\n"
    assert _git(repo, "branch", "--show-current") == "main"


def test_merge_slice_into_integration_records_merge_commit_and_cleans_worktree(
    tmp_path: Path,
) -> None:
    repo = _repo_with_main(tmp_path)
    integration = create_integration_branch(repo, epic_id=771, base_ref="main")
    lease = create_slice_worktree(repo, slice_id=772, integration_branch=integration.branch)
    (lease.path / "slice.txt").write_text("slice\n")
    _git(lease.path, "add", "slice.txt")
    _git(lease.path, "commit", "-q", "-m", "slice")
    slice_head = _git(lease.path, "rev-parse", "HEAD")

    result = merge_slice_into_integration(
        repo,
        slice_branch=lease.branch,
        integration_branch=integration.branch,
        strategy="ort",
    )

    assert isinstance(result, SliceMergeSuccess)
    assert result.merge_commit == _git(integration.path, "rev-parse", "HEAD")
    assert _git(integration.path, "rev-parse", "HEAD^2") == slice_head
    assert (integration.path / "slice.txt").read_text() == "slice\n"
    assert not lease.path.exists()
    assert _git(repo, "branch", "--show-current") == "main"


def test_merge_slice_into_integration_returns_conflict_result(tmp_path: Path) -> None:
    repo = _repo_with_main(tmp_path)
    integration = create_integration_branch(repo, epic_id=771, base_ref="main")
    (integration.path / "shared.txt").write_text("integration\n")
    _git(integration.path, "commit", "-q", "-am", "integration change")
    integration_head = _git(integration.path, "rev-parse", "HEAD")
    lease = create_slice_worktree(repo, slice_id=772, integration_branch=integration.branch)

    (integration.path / "shared.txt").write_text("integration again\n")
    _git(integration.path, "commit", "-q", "-am", "integration conflict side")
    pre_merge_head = _git(integration.path, "rev-parse", "HEAD")

    (lease.path / "shared.txt").write_text("slice\n")
    _git(lease.path, "commit", "-q", "-am", "slice conflict side")
    assert _git(lease.path, "rev-parse", "HEAD^") == integration_head

    result = merge_slice_into_integration(
        repo,
        slice_branch=lease.branch,
        integration_branch=integration.branch,
        strategy="ort",
    )

    assert isinstance(result, SliceMergeConflict)
    assert result.slice_branch == lease.branch
    assert _git(integration.path, "rev-parse", "HEAD") == pre_merge_head
    assert _git(integration.path, "status", "--short") == ""
    assert lease.path.exists()
    assert _git(repo, "branch", "--show-current") == "main"


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


def test_isolated_checkout_branch_pushes_from_checkout_not_parent(tmp_path: Path) -> None:
    # #518: the attempt branch + its commit live ONLY inside the isolated checkout
    # (own .git, origin already set). The PR push must run from the checkout, not
    # the parent repo — a parent-repo push fails "src refspec ... does not match any".
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

    lease = create_isolated_checkout(repo, issue_id=518, attempt=1, base="main")
    # Backend work + the worklink commit land inside the isolated checkout.
    (lease.path / "b.txt").write_text("work\n")
    _git(lease.path, "config", "user.email", "t@e.com")
    _git(lease.path, "config", "user.name", "t")
    _git(lease.path, "add", "b.txt")
    _git(lease.path, "commit", "-q", "-m", "work")

    # The bug: pushing the branch from the PARENT repo fails — it has no such ref.
    parent_push = subprocess.run(
        ["git", "-C", str(repo), "push", "-u", "origin", lease.branch],
        capture_output=True, text=True,
    )
    assert parent_push.returncode != 0
    assert "does not match any" in (parent_push.stderr + parent_push.stdout)

    # The fix: pushing from the checkout that owns the branch succeeds, and the
    # branch lands on the remote for the PR.
    checkout_push = subprocess.run(
        ["git", "-C", str(lease.path), "push", "-u", "origin", lease.branch],
        capture_output=True, text=True,
    )
    assert checkout_push.returncode == 0, checkout_push.stderr
    assert lease.branch in _git(repo, "ls-remote", "--heads", "origin")


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
