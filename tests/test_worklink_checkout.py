from __future__ import annotations

from datetime import UTC, datetime, timedelta
import errno
import inspect
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Sequence

import pytest

import mimir.worklink.checkout as checkout_module
from mimir.worklink.backends.feature_factory import DEFAULT_FACTORY_ENTRYPOINT
from mimir.worklink.checkout import (
    CheckoutAuthorization,
    CheckoutLease,
    _assert_self_contained_checkout,
    cleanup_checkout,
    create_isolated_checkout,
    create_worktree,
    prune_attempt_checkouts,
)


def completed(args: Sequence[str], returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(args), returncode, stdout="", stderr="")


def test_create_worktree_uses_attempt_scoped_branch_and_path(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        if args[-2:] == ["--verify", "origin/main"]:
            return subprocess.CompletedProcess(list(args), 0, stdout="main123\n", stderr="")
        if args[-1] == "refs/remotes/origin/main":
            return completed(args, returncode=1)
        return completed(args)

    lease = create_worktree(tmp_path, issue_id=439, attempt=2, runner=runner)

    assert lease.path == tmp_path / ".worklink" / "439-2"
    assert lease.branch == "issue/439-a2"
    assert lease.base_ref == "main"
    assert lease.local_base == "main"
    assert calls == [
        [
            "git", "-C", str(tmp_path), "status", "--porcelain=v1", "-z",
            "--untracked-files=all", "--ignored=no",
        ],
        ["git", "-C", str(tmp_path), "fetch", "origin", "main"],
        ["git", "-C", str(tmp_path), "rev-parse", "--verify", "origin/main"],
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
        ["git", "-C", str(tmp_path), "merge-base", "--is-ancestor", "main123", "main"],
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

    lease = CheckoutLease(439, 1, tmp_path, tmp_path / ".worklink" / "439-1", "issue/439-a1", "main")

    assert cleanup_checkout(lease, outcome="failed", runner=runner) is False
    assert calls == []
    assert cleanup_checkout(lease, outcome="completed", runner=runner) is True
    assert calls == [["git", "-C", str(tmp_path), "worktree", "remove", "--force", str(lease.path)]]


def test_prune_attempt_checkouts_is_conservative(tmp_path: Path) -> None:
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

    pruned = prune_attempt_checkouts(tmp_path, older_than=timedelta(days=3), now=now, runner=runner)

    assert pruned == [old]
    assert old.exists()  # fake git runner did not remove it; real git would
    assert calls == [
        ["git", "-C", str(tmp_path), "worktree", "remove", "--force", str(old)],
        ["git", "-C", str(tmp_path), "branch", "-D", "issue/439-a1"],
    ]


def test_prune_attempt_checkouts_skips_active(tmp_path: Path) -> None:
    # An over-TTL attempt whose is_active() reports True is NEVER reaped — this
    # guards a live detached-factory run whose top-level attempt-dir mtime froze
    # while it works in deep subdirs (the #840 worktree-loss race).
    import os

    root = tmp_path / ".worklink"
    live = root / "840-1"
    dead = root / "841-1"
    live.mkdir(parents=True)
    dead.mkdir()
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return completed(args)

    now = datetime.now(UTC)
    old_mtime = (now - timedelta(days=10)).timestamp()
    for path in (live, dead):
        os.utime(path, (old_mtime, old_mtime))

    pruned = prune_attempt_checkouts(
        tmp_path,
        older_than=timedelta(days=3),
        now=now,
        runner=runner,
        is_active=lambda child: child.name == "840-1",
    )

    assert pruned == [dead]  # only the inactive attempt is reaped
    assert all("840-1" not in " ".join(c) for c in calls)  # live attempt untouched


def test_prune_attempt_checkouts_covers_relocated_isolated_checkouts(tmp_path: Path) -> None:
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

    pruned = prune_attempt_checkouts(repo, older_than=timedelta(days=3), now=now, runner=runner)

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


def _base_refusal(repo: Path) -> tuple[str, dict[str, object]]:
    events: list[tuple[str, dict[str, object]]] = []
    with pytest.raises(RuntimeError) as raised:
        create_worktree(
            repo,
            issue_id=1459,
            attempt=1,
            event_logger=lambda name, **payload: events.append((name, payload)),
        )
    assert len(events) == 1
    return str(raised.value), events[0]


def test_clean_base_is_accepted_without_an_event(tmp_path: Path) -> None:
    repo = _repo_with_main(tmp_path)
    events: list[tuple[str, dict[str, object]]] = []

    lease = create_worktree(
        repo,
        issue_id=1459,
        attempt=1,
        event_logger=lambda name, **payload: events.append((name, payload)),
    )

    assert lease.path.exists()
    assert events == []


def test_staged_addition_refuses_base_and_names_path(tmp_path: Path) -> None:
    repo = _repo_with_main(tmp_path)
    (repo / "staged.txt").write_text("foreign\n", encoding="utf-8")
    _git(repo, "add", "staged.txt")

    message, event = _base_refusal(repo)

    assert "staged (1): 'staged.txt'" in message
    assert event == (
        "worklink_base_repo_refused",
        {
            "repo": str(repo),
            "reason": "dirty",
            "detail": "1 dirty path(s); staged (1): 'staged.txt'",
            "dirty_count": 1,
            "staged_count": 1,
            "staged_paths": ["staged.txt"],
            "unstaged_count": 0,
            "unstaged_paths": [],
            "untracked_count": 0,
            "untracked_paths": [],
            "sample_limit": 20,
        },
    )


def test_unstaged_modification_refuses_base_and_names_path(tmp_path: Path) -> None:
    repo = _repo_with_main(tmp_path)
    (repo / "shared.txt").write_text("foreign\n", encoding="utf-8")

    message, event = _base_refusal(repo)

    assert "unstaged (1): 'shared.txt'" in message
    assert event[1]["unstaged_paths"] == ["shared.txt"]


def test_ignored_only_untracked_content_is_accepted(tmp_path: Path) -> None:
    repo = _repo_with_main(tmp_path)
    (repo / ".gitignore").write_text(".venv/\n.worktrees/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore local directories")
    (repo / ".venv").mkdir()
    (repo / ".venv" / "python").write_text("ignored\n", encoding="utf-8")
    (repo / ".worktrees").mkdir()
    (repo / ".worktrees" / "attempt").write_text("ignored\n", encoding="utf-8")
    events: list[tuple[str, dict[str, object]]] = []

    lease = create_worktree(
        repo,
        issue_id=1459,
        attempt=1,
        event_logger=lambda name, **payload: events.append((name, payload)),
    )

    assert lease.path.exists()
    assert events == []


def test_git_status_ownership_failure_refuses_instead_of_reading_empty_stdout(
    tmp_path: Path,
) -> None:
    events: list[tuple[str, dict[str, object]]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        assert args[3] == "status"
        return subprocess.CompletedProcess(
            list(args),
            128,
            stdout="",
            stderr="fatal: detected dubious ownership in repository\n",
        )

    with pytest.raises(RuntimeError, match="status_failed.*dubious ownership"):
        create_worktree(
            tmp_path,
            issue_id=1459,
            attempt=1,
            runner=runner,
            event_logger=lambda name, **payload: events.append((name, payload)),
        )

    assert events[0][0] == "worklink_base_repo_refused"
    assert events[0][1]["reason"] == "status_failed"


def test_default_factory_entrypoint_resolves_outside_allocated_checkout(
    tmp_path: Path,
) -> None:
    lease = create_worktree(_repo_with_main(tmp_path), issue_id=1606, attempt=1)

    entrypoint = Path(DEFAULT_FACTORY_ENTRYPOINT).resolve()
    assert not entrypoint.is_relative_to(lease.path.resolve())


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


def test_base_fetch_failure_gates_build_and_logs_real_reason(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    events: list[tuple[str, dict[str, object]]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        if args[:4] == ["git", "-C", str(tmp_path), "fetch"]:
            return subprocess.CompletedProcess(list(args), 128, stdout="", stderr="network down\n")
        return completed(args)

    def event_logger(event_type: str, **payload: object) -> None:
        events.append((event_type, payload))

    with pytest.raises(RuntimeError, match="base repo fetch failed for origin/main"):
        create_worktree(
            tmp_path,
            issue_id=521,
            attempt=2,
            base="main",
            runner=runner,
            event_logger=event_logger,
        )

    # A failed fetch now also probes for refs left dangling by a reclaimed
    # alternate, because that residue makes every fetch fail until it is pruned
    # (2026-07-28: a leftover refs/remotes/origin/pr/1188 cost six worklink
    # attempts). Here the probe finds nothing, so the fetch must NOT be retried —
    # a genuine network failure should fail closed immediately, not double up.
    assert calls == [
        [
            "git", "-C", str(tmp_path), "status", "--porcelain=v1", "-z",
            "--untracked-files=all", "--ignored=no",
        ],
        ["git", "-C", str(tmp_path), "fetch", "origin", "main"],
        ["git", "-C", str(tmp_path), "for-each-ref", "--format=%(refname) %(objectname)"],
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


def test_base_with_no_origin_counterpart_fails_closed(tmp_path: Path) -> None:
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

    with pytest.raises(RuntimeError, match="base repo fetch failed for origin/local-only"):
        create_worktree(repo, issue_id=521, attempt=3, base="local-only")

    assert _git(repo, "rev-parse", "local-only") == local_sha
    missing = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", "origin/local-only"],
        capture_output=True,
        text=True,
    )
    assert missing.returncode != 0


def test_all_alternates_are_backed_up_and_pruned_before_fetch(tmp_path: Path) -> None:
    repo = _repo_with_main(tmp_path)
    alternates = repo / ".git" / "objects" / "info" / "alternates"
    live = tmp_path / "live-objects"
    live.mkdir()
    dead = tmp_path / "deleted-objects"
    original = f"{live}\n{dead}\n"
    alternates.write_text(original, encoding="utf-8")
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return subprocess.run(list(args), capture_output=True, text=True, check=False)

    lease = create_worktree(repo, issue_id=967, attempt=1, runner=runner)

    fetches = [call for call in calls if call[:4] == ["git", "-C", str(repo), "fetch"]]
    assert len(fetches) == 1
    assert not alternates.exists()
    backups = list(alternates.parent.glob("alternates.worklink-backup*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original
    assert _git(lease.path, "rev-parse", "HEAD") == _git(repo, "rev-parse", "origin/main")


def test_valid_alternate_is_repaired_before_isolated_clone(tmp_path: Path) -> None:
    repo = _repo_with_main(tmp_path)
    donor = tmp_path / "donor"
    subprocess.run(["git", "init", "-q", str(donor)], check=True)
    alternates = repo / ".git" / "objects" / "info" / "alternates"
    alternates.write_text(f"{donor / '.git' / 'objects'}\n", encoding="utf-8")
    events: list[tuple[str, dict[str, object]]] = []
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return subprocess.run(list(args), capture_output=True, text=True, check=False)

    lease = create_isolated_checkout(
        repo,
        issue_id=1033,
        attempt=1,
        event_logger=lambda name, **payload: events.append((name, payload)),
        runner=runner,
    )

    assert not alternates.exists()
    assert _git(lease.path, "rev-parse", "HEAD") == _git(repo, "rev-parse", "origin/main")
    repaired = next(payload for name, payload in events if name == "worklink_base_alternates_repaired")
    assert repaired["pruned"] == [str(donor / ".git" / "objects")]
    assert repaired["retained"] == []
    fsck_index = next(i for i, call in enumerate(calls) if "fsck" in call)
    prune_index = next(
        i for i, call in enumerate(calls) if call[-3:] == ["worktree", "prune", "--verbose"]
    )
    assert fsck_index < prune_index


def test_alternate_repair_recovers_probe_file_left_by_sigkill(tmp_path: Path) -> None:
    repo = _repo_with_main(tmp_path)
    donor = tmp_path / "donor"
    subprocess.run(["git", "init", "-q", str(donor)], check=True)
    alternates = repo / ".git" / "objects" / "info" / "alternates"
    original = f"{donor / '.git' / 'objects'}\n"
    alternates.write_text(original, encoding="utf-8")
    interrupted = alternates.with_name("alternates.worklink-check")
    alternates.replace(interrupted)
    events: list[tuple[str, dict[str, object]]] = []

    lease = create_isolated_checkout(
        repo,
        issue_id=1033,
        attempt=3,
        event_logger=lambda name, **payload: events.append((name, payload)),
    )

    assert not alternates.exists()
    assert not interrupted.exists()
    backups = list(alternates.parent.glob("alternates.worklink-backup*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original
    assert _git(lease.path, "rev-parse", "HEAD") == _git(repo, "rev-parse", "origin/main")
    recovered_index = next(
        i for i, (name, _payload) in enumerate(events)
        if name == "worklink_base_alternates_probe_recovered"
    )
    repaired_index = next(
        i for i, (name, _payload) in enumerate(events)
        if name == "worklink_base_alternates_repaired"
    )
    assert recovered_index < repaired_index
    recovered = events[recovered_index][1]
    assert recovered["restored_from"] == str(interrupted)
    assert recovered["restored_to"] == str(alternates)


def test_alternate_repair_refuses_ambiguous_interrupted_probe_files(tmp_path: Path) -> None:
    repo = _repo_with_main(tmp_path)
    alternates = repo / ".git" / "objects" / "info" / "alternates"
    first = alternates.with_name("alternates.worklink-check")
    second = alternates.with_name("alternates.worklink-check.1")
    first.write_text("/first/objects\n", encoding="utf-8")
    second.write_text("/second/objects\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="multiple interrupted probe files"):
        create_isolated_checkout(repo, issue_id=1033, attempt=4)

    assert not alternates.exists()
    assert first.read_text(encoding="utf-8") == "/first/objects\n"
    assert second.read_text(encoding="utf-8") == "/second/objects\n"


def test_alternate_repair_refuses_objects_available_only_from_alternate(tmp_path: Path) -> None:
    repo = _repo_with_main(tmp_path)
    donor = tmp_path / "donor"
    subprocess.run(["git", "init", "-q", str(donor)], check=True)
    _git(donor, "config", "user.email", "t@e.com")
    _git(donor, "config", "user.name", "t")
    _git(donor, "commit", "-q", "--allow-empty", "-m", "alternate only")
    unique_sha = _git(donor, "rev-parse", "HEAD")
    alternates = repo / ".git" / "objects" / "info" / "alternates"
    alternate_objects = donor / ".git" / "objects"
    alternates.write_text(f"{alternate_objects}\n", encoding="utf-8")
    _git(repo, "update-ref", "refs/heads/rescue-alternate", unique_sha)
    events: list[tuple[str, dict[str, object]]] = []

    with pytest.raises(RuntimeError, match=unique_sha):
        create_isolated_checkout(
            repo,
            issue_id=1033,
            attempt=2,
            event_logger=lambda name, **payload: events.append((name, payload)),
        )

    assert alternates.read_text(encoding="utf-8") == f"{alternate_objects}\n"
    assert _git(repo, "rev-parse", "refs/heads/rescue-alternate") == unique_sha
    refused = next(
        payload for name, payload in events
        if name == "worklink_base_alternates_repair_refused"
    )
    assert unique_sha in refused["at_risk_objects"]
    assert refused["retained"] == [str(alternate_objects)]
    assert refused["retained_refs"] == [f"refs/heads/rescue-alternate@{unique_sha}"]


def test_requested_base_ignores_first_fetch_head_entry_when_current(tmp_path: Path) -> None:
    fetch_head = tmp_path / ".git" / "FETCH_HEAD"
    fetch_head.parent.mkdir()
    fetch_head.write_text(
        "main123\t\tbranch 'main' of example.invalid/repo\n"
        "feature123\t\tbranch 'feature/acp' of example.invalid/repo\n",
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        if args[-2:] == ["--verify", "origin/feature/acp"]:
            return subprocess.CompletedProcess(list(args), 0, stdout="feature123\n", stderr="")
        return completed(args)

    lease = create_worktree(
        tmp_path,
        issue_id=1458,
        attempt=1,
        base="feature/acp",
        runner=runner,
    )

    assert lease.local_base == "origin/feature/acp"
    assert [
        "git", "-C", str(tmp_path), "merge-base", "--is-ancestor",
        "feature123", "origin/feature/acp",
    ] in calls
    assert not any("FETCH_HEAD" in call for call in calls)


@pytest.mark.parametrize("base,first_base", [("main", "feature/acp"), ("feature/acp", "main")])
def test_stale_remote_tracking_base_fails_with_named_ref_and_behind_count(
    tmp_path: Path,
    base: str,
    first_base: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote_ref = f"origin/{base}"
    fetch_head = tmp_path / ".git" / "FETCH_HEAD"
    fetch_head.parent.mkdir()
    fetch_head.write_text(
        f"other456\t\tbranch '{first_base}' of example.invalid/repo\n"
        f"origin456\t\tbranch '{base}' of example.invalid/repo\n",
        encoding="utf-8",
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(checkout_module, "_resolve_local_base", lambda *_args, **_kwargs: base)

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        if args[-3:] == ["--is-ancestor", "origin456", base]:
            return completed(args, returncode=1)
        if args[-2:] == ["--verify", remote_ref]:
            return subprocess.CompletedProcess(list(args), 0, stdout="origin456\n", stderr="")
        if args[-2:] == ["--verify", base]:
            return subprocess.CompletedProcess(list(args), 0, stdout="local123\n", stderr="")
        if args[-2:] == ["--count", f"{base}..origin456"]:
            return subprocess.CompletedProcess(list(args), 0, stdout="3\n", stderr="")
        return completed(args)

    with pytest.raises(
        RuntimeError,
        match=rf"stale base local123, {remote_ref} origin456, 3 commits behind",
    ):
        create_worktree(tmp_path, issue_id=967, attempt=2, base=base, runner=runner)

    assert not any(call[3:5] == ["worktree", "add"] for call in calls)
    assert not any("FETCH_HEAD" in call for call in calls)


def test_create_worktree_real_git_feature_acp_remote_base(tmp_path: Path) -> None:
    # Regression for #467: a feature base that exists only as a remote-tracking
    # branch (origin/feature/acp) must still yield the attempt-scoped
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
    _git(work, "checkout", "-q", "-b", "feature/acp")
    _git(work, "commit", "-q", "--allow-empty", "-m", "feature commit")
    _git(work, "push", "-q", "origin", "feature/acp")
    feature_sha = _git(work, "rev-parse", "HEAD")

    # Fresh clone: feature/acp exists only as origin/feature/acp.
    repo = tmp_path / "fresh"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
    _git(repo, "config", "user.email", "t@e.com")
    _git(repo, "config", "user.name", "t")

    lease = create_worktree(repo, issue_id=441, attempt=1, base="feature/acp")

    assert lease.branch == "issue/441-a1"
    assert lease.base_ref == "feature/acp"
    assert lease.local_base == "origin/feature/acp"
    # The worktree must be on the attempt branch, NOT the base branch (the DWIM bug).
    assert _git(lease.path, "branch", "--show-current") == "issue/441-a1"
    assert _git(lease.path, "rev-parse", "HEAD") == feature_sha
    # No stray local branch named after the base was created by DWIM.
    local_branches = _git(repo, "branch", "--format=%(refname:short)").split()
    assert "feature/acp" not in local_branches


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


def test_isolated_checkout_uses_explicit_effective_pushurl(tmp_path: Path) -> None:
    repo = _repo_with_main(tmp_path)
    push_target = tmp_path / "push-target.git"
    subprocess.run(["git", "init", "-q", "--bare", str(push_target)], check=True)
    _git(repo, "config", "remote.origin.pushurl", str(push_target))
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return subprocess.run(list(args), capture_output=True, text=True, check=False)

    lease = create_isolated_checkout(repo, issue_id=1125, attempt=1, runner=runner)

    assert _git(repo, "remote", "get-url", "--push", "origin") == str(push_target)
    assert _git(lease.path, "remote", "get-url", "--push", "origin") == str(push_target)
    parent_resolve = ["git", "-C", str(repo), "remote", "get-url", "--push", "origin"]
    checkout_resolve = [
        "git", "-C", str(lease.path), "remote", "get-url", "--push", "origin",
    ]
    branch_checkout = [
        "git", "-C", str(lease.path), "checkout", "-B", lease.branch, lease.local_base,
    ]
    assert calls.index(parent_resolve) < calls.index(checkout_resolve) < calls.index(branch_checkout)


def test_isolated_checkout_uses_push_instead_of_effective_target(tmp_path: Path) -> None:
    repo = _repo_with_main(tmp_path)
    fetch_target = _git(repo, "remote", "get-url", "origin")
    push_target = tmp_path / "rewritten-push.git"
    subprocess.run(["git", "init", "-q", "--bare", str(push_target)], check=True)
    _git(repo, "config", f"url.{push_target}.pushInsteadOf", fetch_target)

    effective_target = _git(repo, "remote", "get-url", "--push", "origin")
    assert effective_target == str(push_target)

    lease = create_isolated_checkout(repo, issue_id=1125, attempt=2)

    assert _git(lease.path, "remote", "get-url", "--push", "origin") == effective_target


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("set", "remote set denied"),
        ("verify", "push target lookup exploded"),
        ("mismatch", "wanted 'wanted-target', observed 'wrong-target'"),
    ],
)
def test_isolated_checkout_push_target_failures_clean_up_before_branch_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    message: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    path = tmp_path / ".worklink" / repo.name / "1125-3"
    calls: list[list[str]] = []

    def fake_clone(
        _repo: Path,
        clone_path: Path,
        **_kwargs: object,
    ) -> None:
        clone_path.mkdir(parents=True)

    monkeypatch.setattr(checkout_module, "_clone_attempt_checkout", fake_clone)

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        call = list(args)
        calls.append(call)
        if call[-2:] == ["--verify", "origin/main"]:
            return subprocess.CompletedProcess(call, 0, stdout="base-sha\n", stderr="")
        if call == ["git", "-C", str(repo), "remote", "get-url", "--push", "origin"]:
            return subprocess.CompletedProcess(call, 0, stdout="wanted-target\n", stderr="")
        if call[2] == str(path) and call[3:5] == ["remote", "set-url"] and failure == "set":
            return subprocess.CompletedProcess(call, 1, stdout="", stderr="remote set denied\n")
        if call[2] == str(path) and call[3:] == ["remote", "get-url", "--push", "origin"]:
            if failure == "verify":
                return subprocess.CompletedProcess(
                    call, 2, stdout="push target lookup exploded\n", stderr="",
                )
            return subprocess.CompletedProcess(
                call, 0, stdout=("wrong-target\n" if failure == "mismatch" else "wanted-target\n"), stderr="",
            )
        return completed(call)

    with pytest.raises(RuntimeError, match=message):
        create_isolated_checkout(repo, issue_id=1125, attempt=3, runner=runner)

    assert not path.exists()
    assert not any(call[3:5] == ["checkout", "-B"] for call in calls)


def test_cleanup_removes_successful_isolated_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    attempt = repo / ".worklink" / "517-1"
    attempt.mkdir(parents=True)
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return completed(args, returncode=1 if args[-2:] == ["-D", "issue/517-a1"] else 0)

    lease = CheckoutLease(517, 1, repo, attempt, "issue/517-a1", "main", isolated_checkout=True)

    assert cleanup_checkout(lease, outcome="completed", runner=runner) is True
    assert not attempt.exists()
    assert calls == [["git", "-C", str(repo), "branch", "-D", "issue/517-a1"]]


@pytest.mark.parametrize("worker_authorized", [False, True])
def test_isolated_cleanup_tolerates_checkout_removed_concurrently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker_authorized: bool,
) -> None:
    repo = tmp_path / "repo"
    attempt = repo / ".worklink" / "517-1" / "checkout"
    attempt.mkdir(parents=True)
    victim = attempt / "maintenance.lock"
    victim.write_text("lock\n")
    real_unlink = os.unlink
    raced = False

    def unlink(path: str | bytes, *, dir_fd: int | None = None) -> None:
        nonlocal raced
        if not raced and os.fsdecode(path) == victim.name:
            raced = True
            real_unlink(path, dir_fd=dir_fd)
        real_unlink(path, dir_fd=dir_fd)

    class SafeGit:
        def run(self, *_args: object, **_kwargs: object) -> None:
            return None

    monkeypatch.setattr(checkout_module.os, "unlink", unlink)
    lease = CheckoutLease(
        517,
        1,
        repo,
        attempt,
        "issue/517-a1",
        "main",
        isolated_checkout=True,
        worker_authorized=worker_authorized,
    )

    assert cleanup_checkout(
        lease,
        outcome="completed",
        runner=lambda args: completed(args, returncode=1),
        safe_git=SafeGit() if worker_authorized else None,
    ) is True
    assert raced
    assert not (attempt.parent if worker_authorized else attempt).exists()


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


def test_self_containment_assert_rejects_alternates_dependency(tmp_path: Path) -> None:
    repo = _repo_with_main(tmp_path)
    checkout = tmp_path / "referenced-checkout"
    subprocess.run(["git", "clone", "-q", "--shared", str(repo), str(checkout)], check=True)

    with pytest.raises(RuntimeError, match="alternates=True"):
        _assert_self_contained_checkout(
            checkout,
            runner=lambda args: subprocess.run(
                list(args), capture_output=True, text=True, check=False
            ),
        )


def _hardlink_failure(path: Path) -> subprocess.CompletedProcess[str]:
    """git's exact wording when it cannot hardlink an object into the clone."""
    return subprocess.CompletedProcess(
        args=[],
        returncode=128,
        stdout="",
        stderr=(
            f"fatal: failed to create link '{path}/.git/objects/dd/bb88b6dd951d45"
            "8666c3eab73a71648924ccdb': Operation not permitted\n"
        ),
    )


def test_clone_falls_back_to_object_copy_when_hardlinks_are_refused() -> None:
    """#1245 regression: one unlinkable object killed every build.

    A root-owned mode-444 object under ``fs.protected_hardlinks=1`` makes
    ``clone --local`` fail outright. The attempt must degrade to an object copy
    rather than dying, and must say so.
    """
    from mimir.worklink.checkout import _clone_attempt_checkout

    calls: list[Sequence[str]] = []
    events: list[tuple[str, dict]] = []
    target = Path("/tmp/attempt-1029-3")

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        if "--no-hardlinks" in args:
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        return _hardlink_failure(target)

    _clone_attempt_checkout(
        Path("/tmp/repo"), target,
        runner=runner,
        event_logger=lambda name, **fields: events.append((name, fields)),
    )

    assert len(calls) == 2
    assert "--no-hardlinks" not in calls[0]
    assert "--no-hardlinks" in calls[1]
    assert [name for name, _ in events] == ["worklink_checkout_hardlink_fallback"]
    assert "Operation not permitted" in events[0][1]["detail"]


def test_clone_does_not_retry_an_unrelated_failure() -> None:
    """A fallback that fires on any error would turn a real fault into 179 MB
    of copying and a confusing success. Only the hardlink error retries."""
    from mimir.worklink.checkout import _clone_attempt_checkout

    calls: list[Sequence[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return subprocess.CompletedProcess(
            args=[], returncode=128, stdout="",
            stderr="fatal: repository '/tmp/repo' does not exist\n",
        )

    with pytest.raises(RuntimeError, match="does not exist"):
        _clone_attempt_checkout(
            Path("/tmp/repo"), Path("/tmp/attempt"), runner=runner, event_logger=None,
        )

    assert len(calls) == 1


def test_clone_raises_when_the_object_copy_also_fails() -> None:
    """The fallback is a degradation, not a guarantee; it must still fail loud."""
    from mimir.worklink.checkout import _clone_attempt_checkout

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if "--no-hardlinks" in args:
            return subprocess.CompletedProcess(
                args=[], returncode=128, stdout="", stderr="fatal: out of disk\n",
            )
        return _hardlink_failure(Path("/tmp/attempt"))

    with pytest.raises(RuntimeError, match="out of disk"):
        _clone_attempt_checkout(
            Path("/tmp/repo"), Path("/tmp/attempt"), runner=runner, event_logger=None,
        )


def test_foreign_owned_git_objects_are_reported_with_path_uid_and_mode(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    object_path = repo / ".git" / "objects" / "aa" / "object"
    object_path.parent.mkdir(parents=True)
    object_path.write_bytes(b"object")
    object_path.chmod(0o444)
    events: list[tuple[str, dict]] = []

    foreign = checkout_module.report_foreign_owned_git_objects(
        repo,
        expected_uid=object_path.stat().st_uid + 1,
        event_logger=lambda name, **fields: events.append((name, fields)),
    )

    assert foreign == [object_path]
    assert events == [
        (
            "worklink_foreign_owned_git_object",
            {
                "repo": str(repo),
                "object_path": str(object_path),
                "owner_uid": object_path.stat().st_uid,
                "expected_owner_uid": object_path.stat().st_uid + 1,
                "mode": "0o444",
            },
        )
    ]


def test_git_object_ownership_check_stays_quiet_when_every_object_matches(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    object_path = repo / ".git" / "objects" / "bb" / "object"
    object_path.parent.mkdir(parents=True)
    object_path.write_bytes(b"object")
    events: list[tuple[str, dict]] = []

    foreign = checkout_module.report_foreign_owned_git_objects(
        repo,
        expected_uid=object_path.stat().st_uid,
        event_logger=lambda name, **fields: events.append((name, fields)),
    )

    assert foreign == []
    assert events == []


def test_git_object_ownership_check_is_not_reachable_from_clone_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner_name = checkout_module.report_foreign_owned_git_objects.__name__

    monkeypatch.setattr(
        checkout_module,
        scanner_name,
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("ownership check reached from clone path")
        ),
    )
    checkout_module._clone_attempt_checkout(
        Path("/repo"),
        Path("/checkout"),
        runner=lambda args: completed(args),
        event_logger=None,
    )

    assert scanner_name not in inspect.getsource(checkout_module._clone_attempt_checkout)
    assert scanner_name not in inspect.getsource(checkout_module.create_isolated_checkout)


@pytest.mark.parametrize("value", [None, "", "0", "false", "off"])
def test_disabled_coding_keeps_legacy_checkout_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    if value is None:
        monkeypatch.delenv("MIMIR_CODING_ENABLED", raising=False)
    else:
        monkeypatch.setenv("MIMIR_CODING_ENABLED", value)

    assert checkout_module._isolated_checkout_path(repo, ".worklink", 1410, 2) == (
        tmp_path / ".worklink" / "repo" / "1410-2"
    )


def test_enabled_coding_uses_bounded_hashed_checkout_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    root = tmp_path / "enabled"
    monkeypatch.setenv("MIMIR_CODING_ENABLED", "true")
    monkeypatch.setattr(checkout_module, "_ENABLED_CHECKOUT_ROOT", root)

    path = checkout_module._isolated_checkout_path(repo, ".worklink", 1410, 2)

    assert path.parent.parent.parent == root
    assert len(path.parent.parent.name) == 64
    assert path.parent.name == "1410-2"
    assert path.name == "checkout"


def test_enabled_clone_uses_no_hardlinks() -> None:
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return completed(args)

    checkout_module._clone_attempt_checkout(
        Path("/controller/repo"),
        Path("/var/lib/mimir-worklink/checkouts/repo/1410-1"),
        runner=runner,
        event_logger=None,
        no_hardlinks=True,
    )

    assert calls == [[
        "git", "clone", "--local", "--no-hardlinks", "--quiet",
        "/controller/repo", "/var/lib/mimir-worklink/checkouts/repo/1410-1",
    ]]


@pytest.mark.parametrize(
    "hardlinked_executable", [False, True], ids=["single-link", "hardlinked"]
)
def test_fd_normalization_breaks_hardlinks_and_sets_shared_modes(
    tmp_path: Path, hardlinked_executable: bool
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    source = tmp_path / "source"
    source.write_text("shared", encoding="utf-8")
    linked = checkout / "linked"
    linked.hardlink_to(source)
    executable_source = (
        tmp_path / "run-source" if hardlinked_executable else checkout / "run"
    )
    executable_source.write_text("#!/bin/sh\n", encoding="utf-8")
    executable_source.chmod(0o700)
    executable = checkout / "run"
    if hardlinked_executable:
        executable.hardlink_to(executable_source)
    external = tmp_path / "external"
    external.write_text("external", encoding="utf-8")
    (checkout / "link").symlink_to(external)
    original_inode = source.stat().st_ino
    executable_inode = executable.stat().st_ino
    assert executable.stat().st_nlink == (2 if hardlinked_executable else 1)
    checkout_fd = os.open(checkout, os.O_RDONLY | os.O_DIRECTORY)

    try:
        checkout_module._normalize_checkout_fd(
            checkout_fd, owner_uid=os.getuid(), group_gid=os.getgid()
        )
    finally:
        os.close(checkout_fd)

    assert linked.stat().st_ino != original_inode
    assert (executable.stat().st_ino != executable_inode) is hardlinked_executable
    assert stat.S_IMODE(checkout.stat().st_mode) == 0o2770
    assert stat.S_IMODE(linked.stat().st_mode) == 0o660
    assert stat.S_IMODE(executable.stat().st_mode) == 0o770
    assert all(
        stat.S_IMODE(path.stat(follow_symlinks=False).st_mode) & 0o007 == 0
        for path in (checkout, linked, executable)
    )
    assert external.read_text(encoding="utf-8") == "external"


def test_fd_normalization_rejects_special_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        checkout_module,
        "get_identities",
        lambda: (_ for _ in ()).throw(AssertionError("explicit identities ignored")),
    )
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    os.mkfifo(checkout / "fifo")
    checkout_fd = os.open(checkout, os.O_RDONLY | os.O_DIRECTORY)

    try:
        with pytest.raises(RuntimeError, match="special checkout entry refused"):
            checkout_module._normalize_checkout_fd(
                checkout_fd, owner_uid=os.getuid(), group_gid=os.getgid()
            )
    finally:
        os.close(checkout_fd)


def test_enabled_eligible_checkout_retains_exact_authorization_and_shared_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo_with_main(tmp_path)
    enabled_root = tmp_path / "enabled"
    calls: list[list[str]] = []
    monkeypatch.setenv("MIMIR_CODING_ENABLED", "true")
    monkeypatch.setattr(checkout_module, "_ENABLED_CHECKOUT_ROOT", enabled_root)
    monkeypatch.setattr(
        checkout_module,
        "get_identities",
        lambda: SimpleNamespace(mimir_uid=os.getuid(), worklink_gid=os.getgid()),
    )

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return subprocess.run(list(args), capture_output=True, text=True, check=False)

    lease = create_isolated_checkout(
        repo,
        issue_id=1410,
        attempt=1,
        runner=runner,
        worker_eligible=True,
    )
    try:
        assert lease.worker_authorized is True
        assert lease.authorization is not None
        assert lease.path.parent.parent.parent == enabled_root
        assert stat.S_IMODE(lease.path.parent.stat().st_mode) == 0o700
        lease.authorization.verify(lease.path)
        retained = lease.authorization.duplicate_fd()
        try:
            observed = os.fstat(retained)
            assert (observed.st_dev, observed.st_ino) == (
                lease.authorization.device,
                lease.authorization.inode,
            )
        finally:
            os.close(retained)
        assert stat.S_IMODE(lease.path.stat().st_mode) == 0o2770
        assert stat.S_IMODE((lease.path / ".git").stat().st_mode) == 0o2770
        clone = next(call for call in calls if call[:3] == ["git", "clone", "--local"])
        assert "--no-hardlinks" in clone
    finally:
        if lease.authorization is not None:
            lease.authorization.close()


def test_enabled_controller_only_checkout_preserves_legacy_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo_with_main(tmp_path)
    calls: list[list[str]] = []
    monkeypatch.setenv("MIMIR_CODING_ENABLED", "true")
    monkeypatch.setattr(checkout_module, "_ENABLED_CHECKOUT_ROOT", tmp_path / "enabled")

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return subprocess.run(list(args), capture_output=True, text=True, check=False)

    lease = create_isolated_checkout(
        repo,
        issue_id=1410,
        attempt=2,
        runner=runner,
        worker_eligible=False,
    )

    assert lease.path == tmp_path / ".worklink" / repo.name / "1410-2"
    assert lease.worker_authorized is False
    assert lease.authorization is None
    clone = next(call for call in calls if call[:3] == ["git", "clone", "--local"])
    assert "--no-hardlinks" not in clone


def test_normalization_preflight_leaves_valid_entries_unchanged_on_special_file(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    valid = checkout / "valid"
    valid.write_text("unchanged", encoding="utf-8")
    valid.chmod(0o600)
    before = valid.stat()
    os.mkfifo(checkout / "fifo")
    checkout_fd = os.open(checkout, os.O_RDONLY | os.O_DIRECTORY)

    try:
        with pytest.raises(RuntimeError, match="special checkout entry refused"):
            checkout_module._normalize_checkout_fd(
                checkout_fd, owner_uid=os.getuid(), group_gid=os.getgid()
            )
    finally:
        os.close(checkout_fd)

    after = valid.stat()
    assert (after.st_dev, after.st_ino, after.st_mode, after.st_nlink) == (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_nlink,
    )
    assert valid.read_text(encoding="utf-8") == "unchanged"


def test_authorized_cleanup_git_sink_inventory_is_closed() -> None:
    source = inspect.getsource(cleanup_checkout)
    authorized = source.split("if lease.worker_authorized:", 1)[1].split(
        "rmtree_missing_ok(lease.path.parent)", 1
    )[0]

    assert 'safe_git.run("update-ref"' in authorized
    assert "runner(" not in authorized
    assert "subprocess." not in authorized



def test_issued_checkout_open_is_root_relative_and_rejects_intermediate_symlink(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    issued = root / "repo-id" / "1410-1"
    issued.mkdir(parents=True)
    retained = checkout_module._open_issued_checkout(root, Path("repo-id/1410-1"))
    try:
        assert (os.fstat(retained).st_dev, os.fstat(retained).st_ino) == (
            issued.stat().st_dev,
            issued.stat().st_ino,
        )
    finally:
        os.close(retained)
    (root / "alias").symlink_to(root / "repo-id", target_is_directory=True)

    with pytest.raises((OSError, RuntimeError)):
        checkout_module._open_issued_checkout(root, Path("alias/1410-1"))

    source = inspect.getsource(checkout_module._open_issued_checkout)
    assert "libc.syscall" in source
    assert "0x02 | 0x04 | 0x08" in source


def test_linux_issued_checkout_fails_closed_without_openat2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    (root / "repo-id" / "1410-1").mkdir(parents=True)

    class MissingOpenAt2:
        @staticmethod
        def syscall(*args: object) -> int:
            return -1

    monkeypatch.setattr(checkout_module.sys, "platform", "linux")
    monkeypatch.setattr(
        checkout_module.ctypes, "CDLL", lambda *args, **kwargs: MissingOpenAt2()
    )
    monkeypatch.setattr(checkout_module.ctypes, "get_errno", lambda: errno.ENOSYS)

    with pytest.raises(RuntimeError, match="unavailable or unsafe"):
        checkout_module._open_issued_checkout(root, Path("repo-id/1410-1"))


@pytest.mark.parametrize("relative_path", [Path("../outside"), Path("/outside")])
def test_non_linux_issued_checkout_rejects_paths_outside_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(checkout_module.sys, "platform", "darwin")

    with pytest.raises(ValueError, match="beneath its trusted root"):
        checkout_module._open_issued_checkout(root, relative_path)
