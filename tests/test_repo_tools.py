from __future__ import annotations

import asyncio
import base64
from dataclasses import asdict, replace
import json
import functools
import os
from pathlib import Path
import shutil
import socket
import select
import signal
import stat
import struct
import sys
from types import SimpleNamespace
import subprocess
import traceback
import uuid

import pytest
from langchain_core.tools import ToolException

from mimir.contained_execution import CollectedExecutionResult
from mimir.contained_snapshot import SnapshotCredentialsRefused, create_git_snapshot
from mimir.models import RepoPRAction, RepoPRActionScope, RepoReviewState
from mimir.pr_checkout_lease import (
    PRCheckoutLease,
    cleanup_pr_checkout_lease,
    create_pr_checkout_lease,
)
from mimir.project_tests import (
    ProjectTestRefusal,
    RepoProjectTests,
)
from mimir.repo_tools import (
    GitCommit,
    GitDiff,
    GitFetch,
    GitMerge,
    GitMergeAbort,
    GitProcessResult,
    GitPush,
    GitRebase,
    GitRebaseAbort,
    GitRefusal,
    GitRevert,
    GitRevertAbort,
    GitStage,
    GitStatus,
    GitUnmerged,
    RepoGitTools,
    _bounded_subprocess_runner,
    was_agent_push,
)
from mimir.tools.refusals import ToolPolicyRefusal
from mimir.worklink.identities import get_identities
from mimir.worklink.worker_client import StaleWorkerExecutorError


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _repo_scope_and_state(tmp_path: Path) -> tuple[Path, Path, RepoPRActionScope, RepoReviewState]:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    source = tmp_path / "source"
    subprocess.run(["git", "clone", "-q", str(origin), str(source)], check=True)
    _git(source, "config", "user.name", "test")
    _git(source, "config", "user.email", "test@example.com")
    _git(source, "checkout", "-q", "-b", "main")
    (source / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(source, "add", "tracked.txt")
    _git(source, "commit", "-q", "-m", "base")
    base = _git(source, "rev-parse", "HEAD")
    _git(source, "push", "-q", "origin", "HEAD:main")
    _git(source, "checkout", "-q", "-b", "worklink/7")
    (source / "tracked.txt").write_text("pull request\n", encoding="utf-8")
    _git(source, "commit", "-qam", "pull request")
    head = _git(source, "rev-parse", "HEAD")
    _git(source, "push", "-q", "origin", "HEAD:worklink/7")
    scope = RepoPRActionScope(
        provenance="poller_payload",
        canonical_repo="owner/repo",
        canonical_root=str(source.resolve()),
        canonical_origin=str(origin.resolve()),
        principal="mimir-bot",
        event_type="pr_changes_requested_stale",
        allowed_operations=frozenset(action.value for action in RepoPRAction),
        pr_number=7,
        head_repo="owner/repo",
        head_remote="origin",
        destination_ref="refs/heads/worklink/7",
        observed_head_sha=head,
        base_ref="main",
        observed_base_sha=base,
    )
    leases = tmp_path / "leases"
    leases.mkdir()
    state = RepoReviewState(scope)
    create_pr_checkout_lease(scope, owner="mimir-bot", lease_root=leases, review_state=state)
    return origin, source, scope, state


@pytest.fixture
def repo_tools(tmp_path: Path) -> tuple[Path, Path, RepoPRActionScope, RepoReviewState, RepoGitTools]:
    origin, source, scope, state = _repo_scope_and_state(tmp_path)
    return origin, source, scope, state, RepoGitTools(state)


def test_inspection_uses_pinned_hardened_argv_and_sanitized_environment(
    repo_tools, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _origin, _source, _scope, state, _tools = repo_tools
    lease = state.checkout_lease
    assert lease is not None
    marker = lease.path / "executed"
    helper = lease.path / "helper.sh"
    helper.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    helper.chmod(0o755)
    (lease.path / ".gitattributes").write_text("tracked.txt diff=evil filter=evil\n", encoding="utf-8")
    _git(lease.path, "config", "diff.evil.textconv", str(helper))
    _git(lease.path, "config", "filter.evil.clean", str(helper))
    _git(lease.path, "config", "core.pager", str(helper))
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", str(helper))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "alias.status")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", f"!{helper}")
    monkeypatch.setenv("PAGER", str(helper))
    calls: list[tuple[tuple[str, ...], dict[str, str], float, int]] = []

    def recording_runner(argv, *, env, timeout, output_limit):
        calls.append((argv, env, timeout, output_limit))
        return _bounded_subprocess_runner(
            argv, env=env, timeout=timeout, output_limit=output_limit,
        )

    tools = RepoGitTools(state, runner=recording_runner)
    assert tools.execute(GitStatus()).ok
    assert tools.execute(GitDiff(mode="base")).ok

    assert not marker.exists()
    assert calls
    for argv, env, timeout, output_limit in calls:
        assert argv[0] == str(Path("/usr/bin/git").resolve())
        assert argv[1:3] == ("-C", str(lease.path.resolve()))
        assert ("-c", "core.hooksPath=/dev/null") == argv[5:7]
        assert "credential.helper=" in argv
        assert "http.extraHeader=" in argv
        assert "http.proxy=" in argv
        assert "http.followRedirects=false" in argv
        assert "protocol.allow=never" in argv
        assert "--no-pager" in argv
        assert "--no-optional-locks" in argv
        assert timeout == 20.0
        assert output_limit == 1_048_576
        assert "GIT_EXTERNAL_DIFF" not in env
        assert "GIT_CONFIG_COUNT" not in env
        assert "PAGER" not in env
        assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    diff_argv = next(argv for argv, *_ in calls if "diff" in argv and "--no-color" in argv)
    assert "--no-ext-diff" in diff_argv
    assert "--no-textconv" in diff_argv
    assert not any("protocol.file.allow=always" in argv for argv, *_ in calls)


def test_fetch_is_typed_networked_and_uses_only_bound_refs(repo_tools) -> None:
    _origin, _source, scope, state, _tools = repo_tools
    calls: list[tuple[str, ...]] = []

    def recording_runner(argv, *, env, timeout, output_limit):
        calls.append(argv)
        return _bounded_subprocess_runner(
            argv, env=env, timeout=timeout, output_limit=output_limit,
        )

    result = RepoGitTools(state, runner=recording_runner).execute(GitFetch())
    fetches = [argv for argv in calls if "fetch" in argv]
    assert result.ok
    assert len(fetches) == 2
    assert [argv[-1] for argv in fetches] == [scope.destination_ref, f"refs/heads/{scope.base_ref}"]
    assert all(scope.canonical_origin in argv for argv in fetches)
    assert all("protocol.file.allow=always" in argv for argv in fetches)
    assert all("fetch" not in argv for argv in calls if "status" in argv or "diff" in argv)


def test_tracked_file_query_uses_index_membership_and_refuses_untracked_or_symlink(
    repo_tools, tmp_path: Path,
) -> None:
    _origin, _source, _scope, state, tools = repo_tools
    lease = state.checkout_lease
    tracked = lease.path / "tracked.txt"
    tracked.write_text("modified but still tracked\n", encoding="utf-8")
    untracked = lease.path / "untracked.txt"
    untracked.write_text("not published\n", encoding="utf-8")
    ignored = lease.path / "ignored.txt"
    ignored.write_text("not published\n", encoding="utf-8")
    (lease.path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    escape = lease.path / "escape.txt"
    escape.symlink_to(outside)

    assert tools.is_tracked_file(tracked) is True
    assert tools.is_tracked_file(untracked) is False
    assert tools.is_tracked_file(ignored) is False
    assert tools.is_tracked_file(escape) is False
    assert tools.is_tracked_file(outside) is False


def test_commit_stages_only_explicit_paths_and_preserves_dirty_out_of_scope_file(repo_tools) -> None:
    _origin, _source, _scope, state, tools = repo_tools
    lease = state.checkout_lease
    (lease.path / "tracked.txt").write_text("remediated\n", encoding="utf-8")
    (lease.path / "outside.txt").write_text("do not commit\n", encoding="utf-8")

    result = tools.execute(GitCommit(("tracked.txt",), "remediate review"))

    assert result.ok
    assert _git(lease.path, "show", "--format=", "--name-only", "HEAD") == "tracked.txt"
    assert (lease.path / "outside.txt").read_text(encoding="utf-8") == "do not commit\n"
    assert "outside.txt" in _git(lease.path, "status", "--porcelain")


def test_commit_disables_repo_controlled_hooks_and_clean_filters(repo_tools) -> None:
    _origin, _source, _scope, state, tools = repo_tools
    lease = state.checkout_lease
    marker = lease.path / "helper-executed"
    helper = lease.path / "malicious-helper.sh"
    helper.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n", encoding="utf-8")
    helper.chmod(0o755)
    hooks = lease.path / "hooks"
    hooks.mkdir()
    (hooks / "pre-commit").symlink_to(helper)
    _git(lease.path, "config", "core.hooksPath", str(hooks))
    _git(lease.path, "config", "filter.evil.clean", str(helper))
    (lease.path / ".gitattributes").write_text("tracked.txt filter=evil\n", encoding="utf-8")
    (lease.path / "tracked.txt").write_text("safe content\n", encoding="utf-8")

    assert tools.execute(GitCommit(("tracked.txt",), "safe commit")).ok

    assert not marker.exists()
    assert _git(lease.path, "show", "HEAD:tracked.txt") == "safe content"


def test_commit_refuses_pre_staged_out_of_scope_paths(repo_tools) -> None:
    _origin, _source, _scope, state, tools = repo_tools
    lease = state.checkout_lease
    (lease.path / "tracked.txt").write_text("wanted\n", encoding="utf-8")
    (lease.path / "outside.txt").write_text("staged\n", encoding="utf-8")
    _git(lease.path, "add", "outside.txt")

    with pytest.raises(GitRefusal, match="outside") as refusal:
        tools.execute(GitCommit(("tracked.txt",), "scoped"))

    assert refusal.value.code == "dirty_out_of_scope"
    assert _git(lease.path, "diff", "--cached", "--name-only") == "outside.txt"


@pytest.mark.parametrize("paths", [(), ("-A",), ("../escape",), ("/tmp/escape",), (":(glob)*",)])
def test_stage_refuses_empty_add_all_and_path_injection(repo_tools, paths: tuple[str, ...]) -> None:
    tools = repo_tools[-1]
    expected = "explicit_paths_required" if not paths else "invalid_path"
    with pytest.raises(GitRefusal) as refusal:
        tools.execute(GitStage(paths))
    assert refusal.value.code == expected


def test_conflict_resolution_stage_requires_unmerged_index_proof(repo_tools, monkeypatch) -> None:
    tools = repo_tools[-1]
    monkeypatch.setattr(tools, "_unmerged_paths", lambda: {"conflict.txt"})

    with pytest.raises(GitRefusal) as refusal:
        tools.execute(GitStage(("tracked.txt",)))

    assert refusal.value.code == "unproven_conflict_path"


def test_invalid_revert_ancestry_is_refused(repo_tools) -> None:
    _origin, _source, scope, _state, tools = repo_tools
    with pytest.raises(GitRefusal) as refusal:
        tools.execute(GitRevert(scope.observed_base_sha))
    assert refusal.value.code == "invalid_revert_ancestry"


def test_merge_conflict_names_unmerged_path_and_recovery_operations(tmp_path: Path) -> None:
    origin, source, scope, _old_state = _repo_scope_and_state(tmp_path)
    _git(source, "checkout", "-q", "main")
    (source / "tracked.txt").write_text("advanced base\n", encoding="utf-8")
    _git(source, "commit", "-qam", "advance base")
    advanced_base = _git(source, "rev-parse", "HEAD")
    _git(source, "push", "-q", "origin", "HEAD:main")
    scope = replace(scope, observed_base_sha=advanced_base)
    leases = tmp_path / "merge-leases"
    leases.mkdir()
    state = RepoReviewState(scope)
    create_pr_checkout_lease(scope, owner="mimir-bot", lease_root=leases, review_state=state)
    tools = RepoGitTools(state)

    with pytest.raises(GitRefusal) as conflict:
        tools.execute(GitMerge())

    assert conflict.value.code == "merge_conflict"
    assert "tracked.txt" in str(conflict.value)
    assert "merge conflict" in str(conflict.value)
    assert "repo_unmerged" in str(conflict.value)
    assert "repo_merge_abort" in str(conflict.value)
    assert "CONFLICT (content)" in str(conflict.value)
    assert tools.execute(GitMergeAbort()).ok


def test_merge_non_conflict_failure_preserves_git_stderr(repo_tools) -> None:
    _origin, _source, _scope, state, _tools = repo_tools
    unknown_sha = "f" * 40
    object.__setattr__(
        state,
        "checkout_lease",
        replace(state.checkout_lease, base_sha=unknown_sha),
    )

    with pytest.raises(GitRefusal) as refusal:
        RepoGitTools(state).execute(GitMerge())

    assert refusal.value.code == "git_failed"
    assert str(refusal.value) != "Git operation failed"
    assert unknown_sha in str(refusal.value)
    assert "repo_unmerged" not in str(refusal.value)
    assert "repo_merge_abort" not in str(refusal.value)


def test_checked_failure_reports_redacted_stdout_and_names_silent_exit(repo_tools) -> None:
    _origin, _source, _scope, state, _tools = repo_tools
    secret = "stdout-secret"

    def failed_runner(argv, *, env, timeout, output_limit):
        if "status" in argv:
            return GitProcessResult(1, stdout=f"diagnostic {secret}")
        if "show" in argv:
            return GitProcessResult(1)
        return _bounded_subprocess_runner(
            argv, env=env, timeout=timeout, output_limit=output_limit,
        )

    tools = RepoGitTools(state, runner=failed_runner)
    with pytest.raises(GitRefusal) as refusal:
        tools._command(("status",), sensitive_values=(secret,))

    assert str(refusal.value) == "diagnostic [REDACTED]"

    with pytest.raises(GitRefusal) as silent:
        tools._checked(("show",))
    assert str(silent.value) == "Git exited with status 1 without diagnostic output"


@pytest.mark.parametrize(
    ("failed_command", "operation", "stream", "condition"),
    [
        (
            "add",
            GitStage(("tracked.txt",)),
            "stderr",
            "index.lock: File exists",
        ),
        (
            "commit",
            GitCommit(("tracked.txt",), "remediate review"),
            "stdout",
            "nothing to commit, working tree clean",
        ),
    ],
)
def test_stage_and_commit_failures_report_named_redacted_git_condition(
    repo_tools,
    failed_command: str,
    operation,
    stream: str,
    condition: str,
) -> None:
    _origin, _source, _scope, state, _tools = repo_tools
    secret_url = "https://agent:super-secret-password@example.invalid/owner/repo.git"
    (state.checkout_lease.path / "tracked.txt").write_text("remediated\n", encoding="utf-8")

    def failed_runner(argv, *, env, timeout, output_limit):
        if failed_command in argv:
            return GitProcessResult(
                1,
                **{stream: f"{condition}; remote {secret_url}"},
            )
        return _bounded_subprocess_runner(
            argv, env=env, timeout=timeout, output_limit=output_limit,
        )

    with pytest.raises(GitRefusal) as failure:
        RepoGitTools(state, runner=failed_runner).execute(operation)

    detail = str(failure.value)
    assert failure.value.code == "git_failed"
    assert condition in detail
    assert detail != "Git operation failed"
    assert "super-secret-password" not in detail
    assert "https://[REDACTED]@example.invalid/owner/repo.git" in detail


def test_rebase_conflict_has_separately_modeled_working_abort(tmp_path: Path) -> None:
    origin, source, scope, _old_state = _repo_scope_and_state(tmp_path)
    _git(source, "checkout", "-q", "main")
    (source / "tracked.txt").write_text("advanced base\n", encoding="utf-8")
    _git(source, "commit", "-qam", "advance base")
    advanced_base = _git(source, "rev-parse", "HEAD")
    _git(source, "push", "-q", "origin", "HEAD:main")
    scope = replace(scope, observed_base_sha=advanced_base)
    leases = tmp_path / "new-leases"
    leases.mkdir()
    state = RepoReviewState(scope)
    create_pr_checkout_lease(scope, owner="mimir-bot", lease_root=leases, review_state=state)
    tools = RepoGitTools(state)

    with pytest.raises(GitRefusal) as conflict:
        tools.execute(GitRebase())
    assert conflict.value.code == "git_failed"
    assert (state.checkout_lease.path / ".git" / "rebase-merge").is_dir()

    unmerged = tools.execute(GitUnmerged())
    records = [record for record in unmerged.stdout.split("\x00") if record]
    assert {record.partition("\t")[2] for record in records} == {"tracked.txt"}
    assert {
        record.partition("\t")[0].rsplit(" ", 1)[1]
        for record in records
    } == {"1", "2", "3"}
    status = tools.execute(GitStatus())
    assert "tracked.txt" in status.stdout
    assert tools.execute(GitDiff()).ok

    for operation in (
        GitCommit(("tracked.txt",), "must not commit a conflicted rebase"),
        GitPush(),
    ):
        with pytest.raises(GitRefusal) as mutation:
            tools.execute(operation)
        assert mutation.value.code == "git_failed"

    assert tools.execute(GitRebaseAbort()).ok
    assert _git(state.checkout_lease.path, "symbolic-ref", "--short", "HEAD") == scope.head_ref
    assert _git(state.checkout_lease.path, "rev-parse", "HEAD") == scope.observed_head_sha
    assert _git(origin, "rev-parse", scope.destination_ref) == scope.observed_head_sha


def _two_sided_conflict_tools(
    tmp_path: Path,
) -> tuple[Path, RepoPRActionScope, RepoReviewState, RepoGitTools]:
    origin, source, scope, old_state = _repo_scope_and_state(tmp_path)
    _git(source, "checkout", "-q", "main")
    (source / "tracked.txt").write_text("base property\n", encoding="utf-8")
    (source / "base-test").write_text(
        "#!/bin/sh\ngrep -q 'base property' tracked.txt\n", encoding="utf-8",
    )
    (source / "head-test").write_text(
        "#!/bin/sh\ngrep -q 'head property' tracked.txt\n", encoding="utf-8",
    )
    (source / "suite").write_text(
        "#!/bin/sh\n./base-test && ./head-test\n", encoding="utf-8",
    )
    for path in ("base-test", "head-test", "suite"):
        (source / path).chmod(0o755)
    _git(source, "add", "tracked.txt", "base-test", "head-test", "suite")
    _git(source, "commit", "-q", "-m", "advance base with pinned property")
    advanced_base = _git(source, "rev-parse", "HEAD")
    _git(source, "push", "-q", "origin", "HEAD:main")
    conflict_scope = replace(
        scope,
        event_type="pr_mergeability_conflicting",
        observed_base_sha=advanced_base,
    )
    state = RepoReviewState(conflict_scope)
    create_pr_checkout_lease(
        conflict_scope,
        owner="mimir-bot",
        lease_root=old_state.checkout_lease.lease_root,
        review_state=state,
    )
    return origin, conflict_scope, state, RepoGitTools(state)


@pytest.mark.parametrize("dropped_side", ["base", "head"])
def test_conflict_resolution_dropping_either_test_pinned_side_is_not_pushed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dropped_side: str,
) -> None:
    origin, scope, state, tools = _two_sided_conflict_tools(tmp_path)
    lease = state.checkout_lease
    with pytest.raises(GitRefusal):
        tools.execute(GitRebase())
    chosen = "head property\n" if dropped_side == "base" else "base property\n"
    (lease.path / "tracked.txt").write_text(chosen, encoding="utf-8")
    tools.execute(GitStage(("tracked.txt",)))
    assert tools.execute(GitRebase(
        "base property", "base-test",
        "head property", "head-test",
    )).ok

    home = tmp_path / "home"
    _configure_worklink_test(home, str(lease.path / "suite"))
    monkeypatch.setenv("MIMIR_HOME", str(home))
    result = _execute_project_tests(state)
    assert result.ok is False

    with pytest.raises(GitRefusal) as refusal:
        tools.execute(GitPush())
    assert refusal.value.code == "full_test_required"
    assert _git(origin, "rev-parse", scope.destination_ref) == scope.observed_head_sha


def test_conflict_resolution_preserves_both_sides_records_evidence_and_pushes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin, scope, state, tools = _two_sided_conflict_tools(tmp_path)
    lease = state.checkout_lease
    with pytest.raises(GitRefusal):
        tools.execute(GitRebase())
    (lease.path / "tracked.txt").write_text(
        "base property\nhead property\n", encoding="utf-8",
    )
    tools.execute(GitStage(("tracked.txt",)))
    assert tools.execute(GitRebase(
        "base property", "base-test checks the rebased file",
        "head property", "head-test checks the rebased file",
    )).ok
    message = _git(lease.path, "log", "-1", "--format=%B")
    assert "Base property: base property" in message
    assert "Base verification: base-test checks the rebased file" in message
    assert "Head property: head property" in message
    assert "Head verification: head-test checks the rebased file" in message

    with pytest.raises(GitRefusal) as untested:
        tools.execute(GitPush())
    assert untested.value.code == "full_test_required"
    home = tmp_path / "home"
    _configure_worklink_test(home, str(lease.path / "suite"))
    monkeypatch.setenv("MIMIR_HOME", str(home))
    assert _execute_project_tests(state).ok
    assert tools.execute(GitPush()).ok
    assert _git(origin, "rev-parse", scope.destination_ref) == _git(
        lease.path, "rev-parse", "HEAD",
    )


def test_revert_conflict_has_separately_authorized_abort(repo_tools) -> None:
    _origin, _source, scope, state, tools = repo_tools
    lease = state.checkout_lease
    (lease.path / "tracked.txt").write_text("later remediation\n", encoding="utf-8")
    tools.execute(GitCommit(("tracked.txt",), "later remediation"))

    with pytest.raises(GitRefusal) as conflict:
        tools.execute(GitRevert(scope.observed_head_sha))
    assert conflict.value.code == "git_failed"
    assert (lease.path / ".git" / "REVERT_HEAD").exists()

    assert tools.execute(GitRevertAbort()).ok
    assert _git(lease.path, "status", "--porcelain") == ""


def test_stale_remote_head_returns_named_refusal_and_never_pushes(repo_tools) -> None:
    origin, source, _scope, state, _tools = repo_tools
    _git(source, "checkout", "-q", "worklink/7")
    (source / "remote.txt").write_text("advanced\n", encoding="utf-8")
    _git(source, "add", "remote.txt")
    _git(source, "commit", "-q", "-m", "advance remote")
    _git(source, "push", "-q", "origin", "HEAD:worklink/7")
    calls: list[tuple[str, ...]] = []

    def recording_runner(argv, *, env, timeout, output_limit):
        calls.append(argv)
        return _bounded_subprocess_runner(
            argv, env=env, timeout=timeout, output_limit=output_limit,
        )

    result = RepoGitTools(state, runner=recording_runner).execute(GitPush())

    assert result.ok is False
    assert result.code == "stale_scope"
    assert any("ls-remote" in argv for argv in calls)
    assert not any("push" in argv for argv in calls)
    assert _git(origin, "rev-parse", "refs/heads/worklink/7") != state.checkout_lease.head_sha


def test_push_argv_has_only_bound_non_force_non_delete_branch_form(repo_tools) -> None:
    _origin, _source, scope, state, _tools = repo_tools
    (state.checkout_lease.path / "push.txt").write_text("push me\n", encoding="utf-8")
    _tools.execute(GitCommit(("push.txt",), "push mutation"))
    calls: list[tuple[str, ...]] = []

    def recording_runner(argv, *, env, timeout, output_limit):
        calls.append(argv)
        return _bounded_subprocess_runner(
            argv, env=env, timeout=timeout, output_limit=output_limit,
        )

    assert RepoGitTools(state, runner=recording_runner).execute(GitPush()).ok
    pushed_head = _git(state.checkout_lease.path, "rev-parse", "HEAD")
    assert was_agent_push(
        scope.canonical_repo, scope.pr_number, scope.observed_head_sha, pushed_head,
    )
    push = next(argv for argv in calls if "push" in argv)
    assert push[-4:] == ("push", "--porcelain", scope.canonical_origin, f"HEAD:{scope.destination_ref}")
    assert not any(arg in push for arg in ("--force", "--force-with-lease", "--delete", "--tags", "--mirror", "--all"))
    ls_remote_index = next(index for index, argv in enumerate(calls) if "ls-remote" in argv)
    push_index = calls.index(push)
    assert push_index == ls_remote_index + 1
    verification_fetches = [
        argv for argv in calls
        if "fetch" in argv and scope.destination_ref in argv
    ]
    assert len(verification_fetches) == 1
    assert calls.index(verification_fetches[0]) == push_index + 1
    assert _git(Path(scope.canonical_origin), "rev-parse", scope.destination_ref) == _git(
        state.checkout_lease.path, "rev-parse", "HEAD",
    )


def _clean_rebase_tools(
    tmp_path: Path,
    *,
    event_type: str = "pr_mergeability_rebase",
) -> tuple[Path, Path, RepoPRActionScope, RepoReviewState]:
    origin, source, scope, old_state = _repo_scope_and_state(tmp_path)
    original_author = _git(source, "show", "-s", "--format=%an <%ae>", scope.observed_head_sha)
    _git(source, "checkout", "-q", "main")
    (source / "base-only.txt").write_text("advanced base\n", encoding="utf-8")
    _git(source, "add", "base-only.txt")
    _git(source, "commit", "-q", "-m", "advance base")
    advanced_base = _git(source, "rev-parse", "HEAD")
    _git(source, "push", "-q", "origin", "HEAD:main")
    rebase_scope = replace(
        scope,
        event_type=event_type,
        observed_base_sha=advanced_base,
    )
    state = RepoReviewState(rebase_scope)
    create_pr_checkout_lease(
        rebase_scope,
        owner="mimir-bot",
        lease_root=old_state.checkout_lease.lease_root,
        review_state=state,
    )
    assert RepoGitTools(state).execute(GitRebase()).ok
    assert _git(state.checkout_lease.path, "show", "-s", "--format=%an <%ae>") == original_author
    return origin, source, rebase_scope, state


def test_mergeability_rebase_push_uses_exact_head_lease_and_preserves_author(
    tmp_path: Path,
) -> None:
    origin, _source, scope, state = _clean_rebase_tools(tmp_path)
    calls: list[tuple[str, ...]] = []

    def recording_runner(argv, *, env, timeout, output_limit):
        calls.append(argv)
        return _bounded_subprocess_runner(
            argv, env=env, timeout=timeout, output_limit=output_limit,
        )

    assert RepoGitTools(state, runner=recording_runner).execute(GitPush()).ok
    push = next(argv for argv in calls if "push" in argv)
    assert (
        f"--force-with-lease={scope.destination_ref}:{scope.observed_head_sha}"
        in push
    )
    assert _git(origin, "rev-parse", scope.destination_ref) == _git(
        state.checkout_lease.path, "rev-parse", "HEAD",
    )
    assert cleanup_pr_checkout_lease(state.checkout_lease, review_state=state) is True


def test_changes_requested_rebase_push_uses_exact_head_lease(tmp_path: Path) -> None:
    origin, _source, scope, state = _clean_rebase_tools(
        tmp_path, event_type="pr_changes_requested_stale",
    )
    calls: list[tuple[str, ...]] = []

    def recording_runner(argv, *, env, timeout, output_limit):
        calls.append(argv)
        return _bounded_subprocess_runner(
            argv, env=env, timeout=timeout, output_limit=output_limit,
        )

    assert RepoGitTools(state, runner=recording_runner).execute(GitPush()).ok
    push = next(argv for argv in calls if "push" in argv)
    assert (
        f"--force-with-lease={scope.destination_ref}:{scope.observed_head_sha}"
        in push
    )
    assert _git(origin, "rev-parse", scope.destination_ref) == _git(
        state.checkout_lease.path, "rev-parse", "HEAD",
    )


def test_head_accepted_by_rewritten_push_is_accepted_by_cleanup_when_commit_skipped(
    tmp_path: Path,
) -> None:
    origin, source, scope, old_state = _repo_scope_and_state(tmp_path)
    _git(source, "checkout", "-q", "main")
    (source / "tracked.txt").write_text("pull request\n", encoding="utf-8")
    _git(source, "commit", "-qam", "apply PR content upstream")
    advanced_base = _git(source, "rev-parse", "HEAD")
    _git(source, "push", "-q", "origin", "HEAD:main")
    rebase_scope = replace(
        scope,
        event_type="pr_mergeability_rebase",
        observed_base_sha=advanced_base,
    )
    state = RepoReviewState(rebase_scope)
    lease = create_pr_checkout_lease(
        rebase_scope,
        owner="mimir-bot",
        lease_root=old_state.checkout_lease.lease_root,
        review_state=state,
    )

    assert RepoGitTools(state).execute(GitRebase()).ok
    assert _git(lease.path, "rev-parse", "HEAD") == advanced_base
    assert RepoGitTools(state).execute(GitPush()).ok
    assert _git(origin, "rev-parse", scope.destination_ref) == advanced_base
    assert cleanup_pr_checkout_lease(lease, review_state=state) is True


def test_mergeability_rebase_stale_lease_refuses_concurrent_push(tmp_path: Path) -> None:
    origin, source, scope, state = _clean_rebase_tools(tmp_path)
    _git(source, "checkout", "-q", "worklink/7")
    (source / "concurrent.txt").write_text("concurrent\n", encoding="utf-8")
    _git(source, "add", "concurrent.txt")
    _git(source, "commit", "-q", "-m", "concurrent push")
    concurrent_head = _git(source, "rev-parse", "HEAD")
    _git(source, "push", "-q", "origin", "HEAD:worklink/7")

    result = RepoGitTools(state).execute(GitPush())

    assert result.ok is False
    assert result.code == "stale_scope"
    assert _git(origin, "rev-parse", scope.destination_ref) == concurrent_head


def test_changes_requested_rebase_stale_lease_refuses_concurrent_push(
    tmp_path: Path,
) -> None:
    origin, source, scope, state = _clean_rebase_tools(
        tmp_path, event_type="pr_changes_requested_stale",
    )
    _git(source, "checkout", "-q", "worklink/7")
    (source / "concurrent.txt").write_text("concurrent\n", encoding="utf-8")
    _git(source, "add", "concurrent.txt")
    _git(source, "commit", "-q", "-m", "concurrent push")
    concurrent_head = _git(source, "rev-parse", "HEAD")
    _git(source, "push", "-q", "origin", "HEAD:worklink/7")
    calls: list[tuple[str, ...]] = []

    def recording_runner(argv, *, env, timeout, output_limit):
        calls.append(argv)
        return _bounded_subprocess_runner(
            argv, env=env, timeout=timeout, output_limit=output_limit,
        )

    result = RepoGitTools(state, runner=recording_runner).execute(GitPush())

    assert result.ok is False
    assert result.code == "stale_scope"
    assert any("ls-remote" in argv for argv in calls)
    assert not any("push" in argv for argv in calls)
    assert _git(origin, "rev-parse", scope.destination_ref) == concurrent_head


def test_push_refuses_when_successful_command_leaves_remote_unchanged(repo_tools) -> None:
    origin, _source, scope, state, tools = repo_tools
    lease = state.checkout_lease
    (lease.path / "push.txt").write_text("push me\n", encoding="utf-8")
    tools.execute(GitCommit(("push.txt",), "stranded mutation"))
    expected = _git(lease.path, "rev-parse", "HEAD")
    observed = _git(origin, "rev-parse", scope.destination_ref)

    def no_op_push(argv, *, env, timeout, output_limit):
        if "push" in argv:
            return GitProcessResult(0, stdout="To origin\n\tup to date\n")
        return _bounded_subprocess_runner(
            argv, env=env, timeout=timeout, output_limit=output_limit,
        )

    with pytest.raises(GitRefusal) as refusal:
        RepoGitTools(state, runner=no_op_push).execute(GitPush())

    assert refusal.value.code == "push_not_applied"
    assert expected in str(refusal.value)
    assert observed in str(refusal.value)
    assert f"local commit {expected} remains unpushed" in str(refusal.value)
    assert _git(origin, "rev-parse", scope.destination_ref) == observed


def test_push_succeeds_if_remote_advances_on_top_before_verification(repo_tools) -> None:
    origin, source, scope, state, tools = repo_tools
    lease = state.checkout_lease
    (lease.path / "push.txt").write_text("push me\n", encoding="utf-8")
    tools.execute(GitCommit(("push.txt",), "push mutation"))
    expected = _git(lease.path, "rev-parse", "HEAD")

    def concurrent_push(argv, *, env, timeout, output_limit):
        result = _bounded_subprocess_runner(
            argv, env=env, timeout=timeout, output_limit=output_limit,
        )
        if "push" in argv and result.returncode == 0:
            _git(source, "fetch", "-q", "origin", scope.destination_ref)
            _git(source, "checkout", "-q", "-B", "concurrent", "FETCH_HEAD")
            (source / "concurrent.txt").write_text("later work\n", encoding="utf-8")
            _git(source, "add", "concurrent.txt")
            _git(source, "commit", "-q", "-m", "concurrent advance")
            _git(source, "push", "-q", "origin", f"HEAD:{scope.destination_ref}")
        return result

    result = RepoGitTools(state, runner=concurrent_push).execute(GitPush())
    remote_head = _git(origin, "rev-parse", scope.destination_ref)

    assert result.ok
    assert remote_head != expected
    assert _git(origin, "merge-base", "--is-ancestor", expected, remote_head) == ""


@pytest.mark.parametrize("push_fails", [False, True])
def test_https_push_uses_invocation_scoped_auth_without_credential_leak(
    repo_tools, monkeypatch: pytest.MonkeyPatch, push_fails: bool,
) -> None:
    origin, _source, scope, old_state, _tools = repo_tools
    https_scope = replace(scope, canonical_origin="https://github.com/owner/repo.git")
    lease = replace(
        old_state.checkout_lease,
        canonical_origin=https_scope.canonical_origin,
        scope_id=https_scope.scope_id,
    )
    _git(lease.path, "remote", "set-url", "origin", https_scope.canonical_origin)
    state = RepoReviewState(https_scope)
    state.attach_checkout_lease(lease)
    state.record_git_head(https_scope.scope_id, scope.observed_head_sha)
    (lease.path / "push.txt").write_text("push me\n", encoding="utf-8")
    token = "never-expose-this-token"
    monkeypatch.setenv("GITHUB_TOKEN", token)
    monkeypatch.setattr(
        "mimir.forge.github.confirm_github_identity",
        lambda principal, credential: principal,
    )
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def runner(argv, *, env, timeout, output_limit):
        calls.append((argv, env))
        if "push" in argv and push_fails:
            return GitProcessResult(1, stderr=f"upstream rejected {token}")
        local_argv = tuple(
            str(origin)
            if arg == https_scope.canonical_origin
            else "protocol.file.allow=always"
            if arg == "protocol.allow=never"
            else arg
            for arg in argv
        )
        return _bounded_subprocess_runner(
            local_argv, env=env, timeout=timeout, output_limit=output_limit,
        )

    tools = RepoGitTools(state, runner=runner)
    tools.execute(GitCommit(("push.txt",), "push mutation"))
    error = ""
    events: list[str] = []
    try:
        result = tools.execute(GitPush())
        assert push_fails is False
        assert result.ok
        events.append(repr(result))
    except GitRefusal as exc:
        assert push_fails is True
        error = str(exc)
        events.append(error)
        assert exc.code == "git_failed"
        assert "upstream rejected [REDACTED]" in error

    network_calls = [
        (argv, env) for argv, env in calls
        if "ls-remote" in argv or "push" in argv or "fetch" in argv
    ]
    assert [
        next(command for command in ("ls-remote", "push", "fetch") if command in argv)
        for argv, _env in network_calls
    ] == (["ls-remote", "push"] if push_fails else ["ls-remote", "push", "fetch"])
    authorization = "Authorization: Basic " + base64.b64encode(
        f"x-access-token:{token}".encode(),
    ).decode()
    for argv, env in network_calls:
        assert https_scope.canonical_origin in argv
        assert token not in "\0".join(argv)
        assert "credential.helper=" in argv
        assert "http.followRedirects=false" in argv
        assert env["GIT_CONFIG_COUNT"] == "1"
        assert env["GIT_CONFIG_KEY_0"] == (
            f"http.{https_scope.canonical_origin}.extraheader"
        )
        assert env["GIT_CONFIG_VALUE_0"] == authorization
        assert env["GIT_ASKPASS"] == "/bin/false"
    assert all(
        "GIT_CONFIG_COUNT" not in env
        for argv, env in calls
        if "ls-remote" not in argv and "push" not in argv and "fetch" not in argv
    )
    assert token not in error
    assert all(token not in event for event in events)
    assert "credential" not in _git(lease.path, "config", "--local", "--list")
    if push_fails:
        assert _git(origin, "rev-parse", https_scope.destination_ref) != _git(
            lease.path, "rev-parse", "HEAD",
        )
    else:
        assert _git(origin, "rev-parse", https_scope.destination_ref) == _git(
            lease.path, "rev-parse", "HEAD",
        )


def test_https_push_refuses_when_acting_identity_is_unverified(
    repo_tools, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _origin, _source, scope, old_state, _tools = repo_tools
    https_scope = replace(scope, canonical_origin="https://github.com/owner/repo.git")
    lease = replace(
        old_state.checkout_lease,
        canonical_origin=https_scope.canonical_origin,
        scope_id=https_scope.scope_id,
    )
    state = RepoReviewState(https_scope)
    state.attach_checkout_lease(lease)
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")

    def refuse(_principal, _token):
        raise ForgeError("github identity verification cache is empty")

    from mimir.forge import ForgeError

    monkeypatch.setattr("mimir.forge.github.confirm_github_identity", refuse)
    with pytest.raises(GitRefusal) as refusal:
        RepoGitTools(state)._push_remote()

    assert refusal.value.code == "push_identity_unverified"
    assert "cache is empty" in str(refusal.value)


def test_push_failure_reports_and_preserves_stranded_local_commit(repo_tools) -> None:
    _origin, _source, _scope, state, tools = repo_tools
    lease = state.checkout_lease
    (lease.path / "push.txt").write_text("push me\n", encoding="utf-8")
    tools.execute(GitCommit(("push.txt",), "stranded mutation"))
    stranded_head = _git(lease.path, "rev-parse", "HEAD")

    def failed_push(argv, *, env, timeout, output_limit):
        if "push" in argv:
            return GitProcessResult(1, stderr="upstream diagnostic could contain sensitive data")
        return _bounded_subprocess_runner(
            argv, env=env, timeout=timeout, output_limit=output_limit,
        )

    with pytest.raises(GitRefusal) as refusal:
        RepoGitTools(state, runner=failed_push).execute(GitPush())

    assert refusal.value.code == "git_failed"
    assert str(refusal.value) == (
        "push failed: upstream diagnostic could contain sensitive data; "
        f"local commit {stranded_head} remains unpushed in preserved "
        f"checkout lease {lease.path.resolve()}"
    )
    assert "upstream diagnostic" in str(refusal.value)
    assert lease.is_active
    assert _git(lease.path, "rev-parse", "HEAD") == stranded_head


@pytest.mark.parametrize(
    "origin",
    [
        "https://github.com/other/repo.git",
        "https://example.com/owner/repo.git",
        "https://user@github.com/owner/repo.git",
        "https://github.com:443/owner/repo.git",
        "https://github.com/owner/repo.git?redirect=attacker",
    ],
)
def test_push_credential_is_available_only_for_exact_canonical_github_origin(
    repo_tools, monkeypatch: pytest.MonkeyPatch, origin: str,
) -> None:
    _local_origin, _source, scope, old_state, _tools = repo_tools
    altered = replace(scope, canonical_origin=origin)
    lease = replace(
        old_state.checkout_lease,
        canonical_origin=origin,
        scope_id=altered.scope_id,
    )
    state = RepoReviewState(altered)
    state.attach_checkout_lease(lease)
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")

    with pytest.raises(GitRefusal) as refusal:
        RepoGitTools(state)._push_remote()

    assert refusal.value.code == "push_auth_unavailable"


def test_unrelated_event_force_push_refuses_before_any_network_call(
    repo_tools, monkeypatch,
) -> None:
    _origin, _source, scope, old_state, _tools = repo_tools
    unrelated_scope = replace(scope, event_type="pr_review_requested")
    state = RepoReviewState(unrelated_scope)
    state.attach_checkout_lease(replace(
        old_state.checkout_lease, scope_id=unrelated_scope.scope_id,
    ))
    tools = RepoGitTools(state, enforce=False)
    original_raw = tools._raw
    network_seen = False

    def non_ancestor(arguments, **kwargs):
        nonlocal network_seen
        if arguments[:2] == ("merge-base", "--is-ancestor"):
            return GitProcessResult(1)
        if "ls-remote" in arguments or "push" in arguments:
            network_seen = True
        return original_raw(arguments, **kwargs)

    monkeypatch.setattr(tools, "_raw", non_ancestor)
    with pytest.raises(GitRefusal) as refusal:
        tools.execute(GitPush())
    assert refusal.value.code == "force_push_refused"
    assert network_seen is False


@pytest.mark.parametrize("branch", ["main", "master"])
def test_protected_branch_force_push_refuses_from_remediation_scope(
    repo_tools, monkeypatch, branch: str,
) -> None:
    _origin, _source, scope, old_state, _tools = repo_tools
    protected_scope = replace(scope, destination_ref=f"refs/heads/{branch}")
    lease = replace(
        old_state.checkout_lease,
        scope_id=protected_scope.scope_id,
        destination_ref=protected_scope.destination_ref,
    )
    _git(lease.path, "checkout", "-q", "-B", branch)
    state = RepoReviewState(protected_scope)
    state.attach_checkout_lease(lease)
    tools = RepoGitTools(state, enforce=False)
    original_raw = tools._raw
    network_seen = False

    def non_ancestor(arguments, **kwargs):
        nonlocal network_seen
        if arguments[:2] == ("merge-base", "--is-ancestor"):
            return GitProcessResult(1)
        if "ls-remote" in arguments or "push" in arguments:
            network_seen = True
        return original_raw(arguments, **kwargs)

    monkeypatch.setattr(tools, "_raw", non_ancestor)
    with pytest.raises(GitRefusal) as refusal:
        tools.execute(GitPush())
    assert refusal.value.code == "force_push_refused"
    assert network_seen is False


def test_cross_pr_checkout_is_refused(repo_tools) -> None:
    _origin, _source, _scope, state, _tools = repo_tools
    tools = RepoGitTools(state, enforce=False)
    _git(state.checkout_lease.path, "checkout", "-q", "-b", "other-pr")
    with pytest.raises(GitRefusal) as refusal:
        tools.execute(GitStatus())
    assert refusal.value.code == "cross_pr_checkout"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("destination_ref", "refs/heads/worklink/7:refs/tags/pwned"),
        ("destination_ref", "--upload-pack=evil"),
        ("base_ref", "../pwned"),
        ("head_remote", "attacker"),
    ],
)
def test_scope_ref_and_remote_injection_is_refused(repo_tools, field: str, value: str) -> None:
    _origin, _source, scope, old_state, _tools = repo_tools
    altered = replace(scope, **{field: value})
    old_lease = old_state.checkout_lease
    lease = replace(old_lease, scope_id=altered.scope_id, destination_ref=altered.destination_ref)
    state = RepoReviewState(altered)
    state.attach_checkout_lease(lease)
    with pytest.raises(GitRefusal) as refusal:
        RepoGitTools(state)
    assert refusal.value.code == "invalid_scope"


@pytest.mark.parametrize(
    ("field", "value"),
    [("observed_head_sha", "abc"), ("observed_base_sha", "f" * 39)],
)
def test_scope_requires_full_bound_commit_ids(repo_tools, field: str, value: str) -> None:
    _origin, _source, scope, old_state, _tools = repo_tools
    altered = replace(scope, **{field: value})
    lease = replace(old_state.checkout_lease, scope_id=altered.scope_id)
    state = RepoReviewState(altered)
    state.attach_checkout_lease(lease)
    with pytest.raises(GitRefusal) as refusal:
        RepoGitTools(state)
    assert refusal.value.code == "invalid_scope"


@pytest.mark.parametrize("kind", ["missing", "scope", "owner", "revoked"])
def test_checkout_lease_identity_checks_are_each_pinned(repo_tools, kind: str) -> None:
    _origin, _source, _scope, state, _tools = repo_tools
    lease = state.checkout_lease
    candidate = {
        "missing": None,
        "scope": replace(lease, scope_id="other"),
        "owner": replace(lease, owner="other"),
        "revoked": replace(lease, revoked=True),
    }[kind]
    object.__setattr__(state, "checkout_lease", candidate)
    with pytest.raises(GitRefusal) as refusal:
        RepoGitTools(state)
    assert refusal.value.code == "inactive_checkout"


def test_checkout_symlink_and_root_escape_are_refused(repo_tools, tmp_path: Path) -> None:
    _origin, _source, _scope, state, _tools = repo_tools
    lease = state.checkout_lease
    alias = lease.lease_root / "alias"
    alias.symlink_to(lease.path, target_is_directory=True)
    object.__setattr__(state, "checkout_lease", replace(lease, path=alias))
    with pytest.raises(GitRefusal) as symlink_refusal:
        RepoGitTools(state, enforce=False)
    assert symlink_refusal.value.code == "invalid_checkout"

    outside = tmp_path / "outside-checkout"
    outside.mkdir()
    object.__setattr__(state, "checkout_lease", replace(lease, path=outside))
    with pytest.raises(GitRefusal) as escape_refusal:
        RepoGitTools(state, enforce=False)
    assert escape_refusal.value.code == "invalid_checkout"


def test_repo_url_rewrite_config_is_refused_before_network(repo_tools) -> None:
    _origin, _source, _scope, state, tools = repo_tools
    _git(state.checkout_lease.path, "config", "url.file:///tmp/attacker.insteadOf", "unused:")
    with pytest.raises(GitRefusal) as refusal:
        tools.execute(GitFetch())
    assert refusal.value.code == "unsafe_repo_config"


def test_repo_url_rewrite_config_is_refused_before_authenticated_push(
    repo_tools, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _origin, _source, scope, old_state, _tools = repo_tools
    altered = replace(scope, canonical_origin="https://github.com/owner/repo.git")
    lease = replace(
        old_state.checkout_lease,
        canonical_origin=altered.canonical_origin,
        scope_id=altered.scope_id,
    )
    _git(lease.path, "remote", "set-url", "origin", altered.canonical_origin)
    _git(lease.path, "config", "url.https://attacker.invalid/.insteadOf", altered.canonical_origin)
    state = RepoReviewState(altered)
    state.attach_checkout_lease(lease)
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")

    with pytest.raises(GitRefusal) as refusal:
        RepoGitTools(state).execute(GitPush())

    assert refusal.value.code == "unsafe_repo_config"


def test_unsupported_bound_remote_transport_is_refused(repo_tools) -> None:
    _origin, _source, scope, old_state, _tools = repo_tools
    altered = replace(scope, canonical_origin="ftp://example.invalid/repo")
    lease = replace(
        old_state.checkout_lease,
        canonical_origin=altered.canonical_origin,
        scope_id=altered.scope_id,
    )
    _git(lease.path, "remote", "set-url", "origin", altered.canonical_origin)
    state = RepoReviewState(altered)
    state.attach_checkout_lease(lease)
    with pytest.raises(GitRefusal) as refusal:
        RepoGitTools(state).execute(GitFetch())
    assert refusal.value.code == "unsupported_transport"


def test_malformed_unmerged_index_output_is_refused(repo_tools, monkeypatch) -> None:
    tools = repo_tools[-1]
    original = tools._command

    def malformed(arguments, **kwargs):
        if arguments[:3] == ("ls-files", "--unmerged", "-z"):
            return GitProcessResult(0, "missing-tab-record\x00")
        return original(arguments, **kwargs)

    monkeypatch.setattr(tools, "_command", malformed)
    with pytest.raises(GitRefusal) as refusal:
        tools.execute(GitStage(("tracked.txt",)))
    assert refusal.value.code == "invalid_git_output"


def test_fetch_ref_mismatch_is_named_stale_scope(repo_tools, monkeypatch) -> None:
    tools = RepoGitTools(repo_tools[-2], enforce=False)
    original = tools._command

    def stale(arguments, **kwargs):
        result = original(arguments, **kwargs)
        if arguments[:3] == ("rev-parse", "--verify", "FETCH_HEAD^{commit}"):
            return replace(result, stdout="f" * 40 + "\n")
        return result

    monkeypatch.setattr(tools, "_command", stale)
    with pytest.raises(GitRefusal) as refusal:
        tools.execute(GitFetch())
    assert refusal.value.code == "stale_scope"


def test_fetch_refuses_base_advanced_mid_remediation_with_actionable_reason(repo_tools) -> None:
    _origin, source, _scope, state, tools = repo_tools
    checked_out_base = state.checkout_lease.base_sha
    _git(source, "checkout", "-q", "main")
    (source / "advanced.txt").write_text("new base work\n", encoding="utf-8")
    _git(source, "add", "advanced.txt")
    _git(source, "commit", "-q", "-m", "advance base")
    advanced_base = _git(source, "rev-parse", "HEAD")
    _git(source, "push", "-q", "origin", "HEAD:main")

    with pytest.raises(GitRefusal) as refusal:
        tools.execute(GitFetch())

    assert refusal.value.code == "base_advanced"
    assert checked_out_base in str(refusal.value)
    assert advanced_base in str(refusal.value)
    assert "restart checkout before rebasing" in str(refusal.value)


def test_fetch_refuses_rewritten_base_mid_remediation_with_both_shas(repo_tools) -> None:
    _origin, source, _scope, state, tools = repo_tools
    checked_out_base = state.checkout_lease.base_sha
    _git(source, "checkout", "-q", "--orphan", "replacement-main")
    _git(source, "rm", "-q", "-rf", ".")
    (source / "replacement.txt").write_text("replacement\n", encoding="utf-8")
    _git(source, "add", "replacement.txt")
    _git(source, "commit", "-q", "-m", "rewrite base")
    rewritten_base = _git(source, "rev-parse", "HEAD")
    _git(source, "push", "-q", "--force", "origin", "HEAD:main")

    with pytest.raises(GitRefusal) as refusal:
        tools.execute(GitFetch())

    assert refusal.value.code == "base_history_rewritten"
    assert checked_out_base in str(refusal.value)
    assert rewritten_base in str(refusal.value)


def test_shape_message_and_commit_id_validations_are_pinned(repo_tools) -> None:
    tools = repo_tools[-1]
    cases = (
        (GitStage(["tracked.txt"]), "invalid_shape"),  # type: ignore[arg-type]
        (GitStage(("tracked.txt", "tracked.txt")), "invalid_paths"),
        (GitDiff(mode="attacker"), "invalid_shape"),  # type: ignore[arg-type]
        (GitCommit(("tracked.txt",), ""), "invalid_message"),
        (GitCommit(("tracked.txt",), "x" * (64 * 1024 + 1)), "invalid_message"),
        (GitRevert("HEAD"), "invalid_commit"),
        (object(), "invalid_shape"),
    )
    for operation, code in cases:
        with pytest.raises(GitRefusal) as refusal:
            tools.execute(operation)  # type: ignore[arg-type]
        assert refusal.value.code == code


def test_commit_requires_at_least_one_matching_staged_change(repo_tools) -> None:
    tools = repo_tools[-1]
    with pytest.raises(GitRefusal) as refusal:
        tools.execute(GitCommit(("tracked.txt",), "empty commit refused"))
    assert refusal.value.code == "dirty_out_of_scope"


@pytest.mark.parametrize("change", ["origin", "head"])
def test_checkout_origin_and_expected_head_checks_are_each_pinned(repo_tools, change: str) -> None:
    _origin, _source, _scope, state, tools = repo_tools
    lease = state.checkout_lease
    if change == "origin":
        _git(lease.path, "remote", "set-url", "origin", "/tmp/other-origin")
    else:
        (lease.path / "unexpected.txt").write_text("unexpected\n", encoding="utf-8")
        _git(lease.path, "add", "unexpected.txt")
        _git(lease.path, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-q", "-m", "unexpected")
    with pytest.raises(GitRefusal) as refusal:
        tools.execute(GitStatus())
    assert refusal.value.code == "cross_pr_checkout"


def test_constructor_refuses_unbounded_execution_and_untrusted_git_pins(tmp_path: Path, repo_tools) -> None:
    state = repo_tools[-2]
    with pytest.raises(ValueError, match="positive"):
        RepoGitTools(state, timeout=0)
    with pytest.raises(ValueError, match="positive"):
        RepoGitTools(state, output_limit=0)
    with pytest.raises(ValueError, match="absolute"):
        RepoGitTools(state, git_executable=Path("git"))
    with pytest.raises(ValueError, match="missing"):
        RepoGitTools(state, git_executable=tmp_path / "missing-git")
    not_executable = tmp_path / "not-executable"
    not_executable.write_text("not git\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not executable"):
        RepoGitTools(state, git_executable=not_executable)


def _configure_worklink_test(home: Path, command: str = "/usr/bin/true -q") -> None:
    home.mkdir(exist_ok=True)
    (home / "worklink.yaml").write_text(
        f"defaults:\n  test_command: {command}\n",
        encoding="utf-8",
    )


def _test_checkout_factory(source: Path, **_kwargs):
    return SimpleNamespace(path=source, capability=SimpleNamespace(path=source), close=lambda: None)


def _snapshot_checkout_factory(root: Path, issued: list[Path]):
    def factory(source: Path, **_kwargs):
        boundary = root / f"issued-{len(issued)}"
        destination = boundary / "checkout"
        create_git_snapshot(source, destination)
        issued.append(destination)

        def close() -> None:
            shutil.rmtree(boundary)

        return SimpleNamespace(
            path=destination,
            capability=SimpleNamespace(path=destination),
            close=close,
        )

    return factory


def _recursive_metadata(root: Path) -> dict[str, tuple[int, int, int]]:
    return {
        path.relative_to(root).as_posix(): (
            path.stat(follow_symlinks=False).st_uid,
            path.stat(follow_symlinks=False).st_gid,
            stat.S_IMODE(path.stat(follow_symlinks=False).st_mode),
        )
        for path in (root, *root.rglob("*"))
    }


def _repo_test_conftest_payload(canary: Path) -> bytes:
    return (
        "from pathlib import Path\n"
        "import json, os\n"
        "def pytest_sessionstart(session):\n"
        "    marker = Path('snapshot-executed.json')\n"
        "    details = {'uid': os.getuid(), 'home': os.environ['HOME'], 'attacked': True}\n"
        "    try:\n"
        f"        Path(*{list(canary.parts)!r}).write_text('altered')\n"
        "    except OSError:\n"
        "        details['attacked'] = False\n"
        "    details['marker'] = 'executed'\n"
        "    marker.write_text(json.dumps(details))\n"
        "    print('MIMIR_PAYLOAD=' + json.dumps(details), flush=True)\n"
    ).encode()


def _plant_repo_test_payload(
    lease: Path, conftest_payload: bytes, python_executable: str,
) -> None:
    (lease / "conftest.py").write_bytes(conftest_payload)
    (lease / "test_payload.py").write_text("def test_payload():\n    assert True\n", encoding="utf-8")
    command = (
        'namespace={}; exec(compile(open("conftest.py").read(), '
        '"conftest.py", "exec"), namespace); '
        'namespace["pytest_sessionstart"](None)'
    )
    (lease / "run-tests.sh").write_text(
        f"#!/bin/sh\nexec {python_executable} -c {command!r} \"$@\"\n",
        encoding="utf-8",
    )


async def _local_contained_runner(argv, directory, worker_env, projections, **_kwargs):
    completed = await asyncio.to_thread(
        subprocess.run, argv, cwd=directory.path, env=dict(worker_env), capture_output=True, check=False,
    )
    return CollectedExecutionResult(
        completed.returncode, completed.stdout, completed.stderr, False, False, 0, 0,
    )


def _execute_project_tests(state: RepoReviewState):
    return asyncio.run(RepoProjectTests(
        state, runner=_local_contained_runner, checkout_factory=_test_checkout_factory,
    ).execute())


@pytest.mark.asyncio
async def test_project_tests_use_snapshot_collected_result_and_worker_environment(
    repo_tools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = repo_tools[-2]
    home = tmp_path / "home"
    _configure_worklink_test(home)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("GITHUB_TOKEN", "never-forwarded")
    calls = []

    def factory(source, **kwargs):
        capability = SimpleNamespace(path=tmp_path / "snapshot")
        return SimpleNamespace(path=capability.path, capability=capability, close=lambda: None)

    async def runner(argv, directory, env, projections, **kwargs):
        calls.append((argv, directory, env, projections, kwargs))
        return CollectedExecutionResult(0, b"ok", b"", False, False, 0, 0)

    result = await RepoProjectTests(state, runner=runner, checkout_factory=factory).execute(
        ("tracked.txt::case",)
    )
    assert result.ok is True
    argv, directory, env, projections, kwargs = calls[0]
    assert argv == ("/usr/bin/true", "-q", "tracked.txt::case")
    assert directory.path == tmp_path / "snapshot"
    assert projections == ()
    assert "HOME" not in env
    assert "GITHUB_TOKEN" not in env
    assert "MIMIR_HOME" not in env
    assert env["CI"] == "1"
    assert kwargs["stdout_limit"] == kwargs["stderr_limit"] == 64 * 1024
    assert kwargs["timeout_s"] == 300.0


@pytest.mark.asyncio
async def test_project_tests_scrub_checkout_home_and_sensitive_output(
    repo_tools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = repo_tools[-2]
    home = tmp_path / "controller-home"
    _configure_worklink_test(home, "/usr/bin/false")
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    secret = "ghp_abcdefghijklmnopqrstuvwxyz"

    async def runner(*_args, **_kwargs):
        payload = f"{state.checkout_lease.path} {home} {secret}".encode()
        return CollectedExecutionResult(3, payload, payload, False, False, 0, 0)

    result = await RepoProjectTests(
        state, runner=runner, checkout_factory=_test_checkout_factory,
    ).execute()
    assert result.code == "tests_failed"
    assert str(home) not in result.stdout + result.stderr
    assert str(state.checkout_lease.path) not in result.stdout + result.stderr
    assert secret not in result.stdout + result.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", ["--flag", "../outside", "tracked.txt;id", "/tmp/test"])
async def test_project_test_selector_injection_is_refused(
    repo_tools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selector: str,
) -> None:
    state = repo_tools[-2]
    home = tmp_path / "home"
    _configure_worklink_test(home)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    with pytest.raises(ProjectTestRefusal) as refusal:
        await RepoProjectTests(state).execute((selector,))
    assert refusal.value.code in {"test_selector_invalid", "test_selector_outside_checkout"}


@pytest.mark.asyncio
async def test_project_test_scope_and_active_lease_guards(
    repo_tools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _origin, _source, scope, state, _tools = repo_tools
    home = tmp_path / "home"
    _configure_worklink_test(home)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    denied = RepoReviewState(replace(
        scope, allowed_operations=scope.allowed_operations - {RepoPRAction.TEST.value},
    ))
    with pytest.raises(ProjectTestRefusal, match="scope does not grant"):
        await RepoProjectTests(denied).execute()
    object.__setattr__(state, "checkout_lease", replace(state.checkout_lease, revoked=True))
    with pytest.raises(ProjectTestRefusal) as inactive:
        await RepoProjectTests(state).execute()
    assert inactive.value.code == "inactive_checkout"


@pytest.mark.asyncio
async def test_project_test_snapshot_credentials_are_named_refusal(
    repo_tools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = repo_tools[-2]
    home = tmp_path / "home"
    _configure_worklink_test(home)
    monkeypatch.setenv("MIMIR_HOME", str(home))

    def refused(*_args, **_kwargs):
        raise SnapshotCredentialsRefused(1)

    with pytest.raises(ProjectTestRefusal) as refusal:
        await RepoProjectTests(state, checkout_factory=refused).execute()
    assert refusal.value.code == "test_snapshot_credentials_refused"


@pytest.mark.asyncio
async def test_public_repo_test_credential_refusal_persists_no_sensitive_material(
    repo_tools,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import mimir.event_logger as event_logger
    from mimir.tools import repo as repo_module

    state = repo_tools[-2]
    home = tmp_path / "controller-config"
    _configure_worklink_test(home)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    credential_path = state.checkout_lease.path / "nested" / "credentials.json"
    credential_path.parent.mkdir()
    credential_bytes = b"arbitrary-credential\x00\xff\x10-not-token-shaped"
    credential_path.write_bytes(credential_bytes)
    issued: list[Path] = []
    event_path = tmp_path / "persisted" / "events.jsonl"
    previous_logger = event_logger._logger
    event_logger.init_logger(event_path, "repo-test-containment")
    caplog.set_level("DEBUG")
    monkeypatch.setattr(repo_module, "_state", lambda *_args: state)
    monkeypatch.setattr(
        repo_module,
        "RepoProjectTests",
        lambda review_state: RepoProjectTests(
            review_state,
            runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("credential refusal must happen before launch")
            ),
            checkout_factory=_snapshot_checkout_factory(tmp_path / "snapshots", issued),
        ),
    )
    assert repo_module.repo_test.coroutine is not None
    try:
        with pytest.raises(ToolPolicyRefusal) as refusal:
            await repo_module.repo_test.coroutine(
                repository="owner/repo",
                pull_request=7,
                selectors=(),
                runtime=None,
            )
    finally:
        event_logger._logger = previous_logger

    persisted_events = event_path.read_bytes()
    event_records = [json.loads(line) for line in persisted_events.splitlines()]
    assert [(record["type"], record["reason_code"]) for record in event_records] == [
        ("repo_test_containment_refused", "snapshot_credentials")
    ]
    assert event_records[0]["repository"] == "owner/repo"
    assert event_records[0]["pull_request"] == 7
    outward_sinks = (
        str(refusal.value).encode(),
        persisted_events,
        caplog.text.encode(),
    )
    for sink in outward_sinks:
        assert credential_bytes not in sink
        assert os.fsencode(credential_path) not in sink
        assert os.fsencode(home) not in sink
    assert issued == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        OSError("socket unavailable"),
        PermissionError("peer rejected"),
        ValueError("checkout fd admission rejected"),
        RuntimeError("worker launch rejected"),
    ],
    ids=("socket", "peer", "fd-admission", "launch"),
)
async def test_project_test_containment_unavailable_fails_closed(
    repo_tools,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    from mimir import project_tests as project_tests_module

    state = repo_tools[-2]
    home = tmp_path / "home"
    _configure_worklink_test(home)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    marker = tmp_path / "must-not-execute"
    events: list[tuple[str, dict[str, object]]] = []

    async def record_event(name: str, **fields: object) -> None:
        events.append((name, fields))

    async def unavailable(*_args, **_kwargs):
        assert not marker.exists()
        raise failure

    monkeypatch.setattr(project_tests_module, "safe_log_event", record_event)
    with pytest.raises(ProjectTestRefusal) as refusal:
        await RepoProjectTests(
            state, runner=unavailable, checkout_factory=_test_checkout_factory,
        ).execute()
    assert refusal.value.code == "test_containment_unavailable"
    assert str(failure) not in str(refusal.value)
    assert events == [("repo_test_containment_refused", {
        "reason_code": "containment_unavailable",
        "repository": "owner/repo",
        "pull_request": 7,
    })]
    assert not marker.exists()


@pytest.mark.asyncio
async def test_project_test_names_stale_root_executor_and_rebuild_action(
    repo_tools,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir import project_tests as project_tests_module

    state = repo_tools[-2]
    home = tmp_path / "home"
    _configure_worklink_test(home)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    events: list[tuple[str, dict[str, object]]] = []

    async def record_event(name: str, **fields: object) -> None:
        events.append((name, fields))

    async def stale(*_args, **_kwargs):
        raise StaleWorkerExecutorError(
            "stale root executor image; rebuild the image and restart the container"
        )

    monkeypatch.setattr(project_tests_module, "safe_log_event", record_event)
    with pytest.raises(ProjectTestRefusal) as refusal:
        await RepoProjectTests(
            state, runner=stale, checkout_factory=_test_checkout_factory,
        ).execute()

    assert refusal.value.code == "test_stale_root_executor"
    assert "rebuild the image and restart the container" in str(refusal.value)
    assert events == [("repo_test_containment_refused", {
        "reason_code": "stale_root_executor",
        "repository": "owner/repo",
        "pull_request": 7,
    })]


@pytest.mark.asyncio
async def test_project_test_permission_refusal_names_path_metadata_and_identity(
    repo_tools,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir import project_tests as project_tests_module

    state = repo_tools[-2]
    home = tmp_path / "home"
    _configure_worklink_test(home)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    boundary = tmp_path / "unreadable"
    checkout_path = boundary / "checkout"
    checkout_path.mkdir(parents=True)
    failed_path = checkout_path / "uv.toml"
    failed_path.write_text("file-content-must-not-be-logged", encoding="utf-8")
    events: list[tuple[str, dict[str, object]]] = []

    async def record_event(name: str, **fields: object) -> None:
        events.append((name, fields))

    async def denied(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied", failed_path)

    def close() -> None:
        boundary.chmod(0o700)

    boundary.chmod(0)
    monkeypatch.setattr(project_tests_module, "safe_log_event", record_event)
    checkout = SimpleNamespace(
        path=checkout_path,
        capability=SimpleNamespace(path=checkout_path),
        close=close,
    )
    with pytest.raises(ProjectTestRefusal) as refusal:
        await RepoProjectTests(
            state,
            runner=denied,
            checkout_factory=lambda *_args, **_kwargs: checkout,
        ).execute()

    assert refusal.value.code == "test_path_permission_denied"
    identities = get_identities()
    message = str(refusal.value)
    assert f"path={failed_path}" in message
    assert "path_mode=0o000" in message
    assert f"path_uid={os.geteuid()}" in message
    assert f"path_gid={os.getegid()}" in message
    assert f"runner_effective_uid={identities.worklink_uid}" in message
    assert f"runner_effective_gid={identities.worklink_gid}" in message
    assert f"traversal_failed={boundary}" in message
    assert "file-content-must-not-be-logged" not in message
    assert events == [("repo_test_containment_refused", {
        "reason_code": "path_permission_denied",
        "repository": "owner/repo",
        "pull_request": 7,
        "path": str(failed_path),
        "path_mode": "0o000",
        "path_uid": os.geteuid(),
        "path_gid": os.getegid(),
        "runner_effective_uid": identities.worklink_uid,
        "runner_effective_gid": identities.worklink_gid,
        "traversal_failed": str(boundary),
    })]
    assert "file-content-must-not-be-logged" not in repr(events)


@pytest.mark.asyncio
async def test_uv_config_search_is_bounded_at_unsearchable_snapshot_boundary(
    repo_tools,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pins the production property that RepoProjectTests sets UV_NO_CONFIG=1 for
    # uv commands, so uv cannot search above an fd-entered checkout for ambient
    # config. Deliberately independent of three things this previously depended
    # on by accident: the real uv's release behaviour, ``/proc`` (absent on
    # macOS), and running as non-root -- root bypasses the directory mode, which
    # silently voided the premise when the suite ran as root in a container.
    state = repo_tools[-2]
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_uv = bin_dir / "uv"  # must be named `uv`: production keys on command[0].
    fake_uv.write_text(
        "#!/bin/sh\n"
        'if [ "$UV_NO_CONFIG" = "1" ]; then exit 0; fi\n'
        'echo "ambient uv config discovery was not disabled" >&2\n'
        "exit 2\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    _configure_worklink_test(home, f"{fake_uv} run --offline /usr/bin/true")
    monkeypatch.setenv("MIMIR_HOME", str(home))
    boundary = tmp_path / "snapshot-boundary"
    checkout_path = boundary / "checkout"
    checkout_path.mkdir(parents=True)
    checkout_fd = os.open(checkout_path, os.O_RDONLY | os.O_DIRECTORY)
    boundary.chmod(0)

    # Negative control: without the production environment, the command fails.
    observed = subprocess.run(
        [str(fake_uv), "run", "--offline", "/usr/bin/true"],
        env={"PATH": os.environ["PATH"]},
        cwd=str(tmp_path),
        capture_output=True,
        check=False,
    )
    assert observed.returncode == 2
    assert b"ambient uv config discovery was not disabled" in observed.stderr

    # The boundary is unsearchable by pathname, which is why production enters
    # the checkout by fd. Root ignores the mode, so only assert where the OS
    # enforces it; the fd path below is what production relies on either way.
    if os.geteuid() != 0:
        with pytest.raises(PermissionError):
            os.listdir(checkout_path)

    observed_env: dict[str, str] = {}

    async def runner(argv, _directory, env, _projections, **_kwargs):
        observed_env.update(env)
        completed = await asyncio.to_thread(
            functools.partial(
                subprocess.run,
                list(argv),
                env=env,
                preexec_fn=lambda: os.fchdir(checkout_fd),
                capture_output=True,
                check=False,
            ),
        )
        return CollectedExecutionResult(
            completed.returncode,
            completed.stdout,
            completed.stderr,
            False,
            False,
            0,
            0,
        )

    def close() -> None:
        boundary.chmod(0o700)
        os.close(checkout_fd)

    checkout = SimpleNamespace(
        path=checkout_path,
        capability=SimpleNamespace(path=checkout_path),
        close=close,
    )
    result = await RepoProjectTests(
        state,
        runner=runner,
        checkout_factory=lambda *_args, **_kwargs: checkout,
    ).execute(("tracked.txt",))

    # Deleting the production ``environment["UV_NO_CONFIG"] = "1"`` line makes
    # the fake uv exit 2, which fails both assertions below.
    assert result.ok is True
    assert observed_env.get("UV_NO_CONFIG") == "1"
    # Cleanup restored the boundary before the fd was closed.
    assert stat.S_IMODE(boundary.stat().st_mode) == 0o700


@pytest.mark.asyncio
@pytest.mark.parametrize("runner_fails", [False, True])
async def test_project_test_snapshot_is_removed_after_execution(
    repo_tools,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner_fails: bool,
) -> None:
    state = repo_tools[-2]
    home = tmp_path / "home"
    _configure_worklink_test(home)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    issued: list[Path] = []

    async def runner(*_args, **_kwargs):
        if runner_fails:
            raise OSError("launch failed")
        return CollectedExecutionResult(0, b"", b"", False, False, 0, 0)

    service = RepoProjectTests(
        state,
        runner=runner,
        checkout_factory=_snapshot_checkout_factory(tmp_path / "snapshots", issued),
    )
    if runner_fails:
        with pytest.raises(ProjectTestRefusal) as refusal:
            await service.execute(("tracked.txt",))
        assert refusal.value.code == "test_containment_unavailable"
    else:
        assert (await service.execute(("tracked.txt",))).ok
    assert len(issued) == 1
    assert not issued[0].exists()


@pytest.mark.asyncio
async def test_project_test_snapshot_cleanup_failure_is_not_silent(
    repo_tools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir import project_tests as project_tests_module

    state = repo_tools[-2]
    home = tmp_path / "home"
    _configure_worklink_test(home)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    events: list[tuple[str, dict[str, object]]] = []

    async def record_event(name: str, **fields: object) -> None:
        events.append((name, fields))

    monkeypatch.setattr(project_tests_module, "safe_log_event", record_event)
    checkout = SimpleNamespace(
        path=state.checkout_lease.path,
        capability=SimpleNamespace(path=state.checkout_lease.path),
        close=lambda: (_ for _ in ()).throw(OSError("cleanup path must not escape")),
    )
    with pytest.raises(ProjectTestRefusal) as refusal:
        await RepoProjectTests(
            state,
            runner=lambda *_args, **_kwargs: asyncio.sleep(
                0, result=CollectedExecutionResult(0, b"", b"", False, False, 0, 0)
            ),
            checkout_factory=lambda *_args, **_kwargs: checkout,
        ).execute(("tracked.txt",))

    assert refusal.value.code == "test_snapshot_cleanup_failed"
    assert events == [("repo_test_containment_refused", {
        "reason_code": "cleanup_failed",
        "repository": "owner/repo",
        "pull_request": 7,
    })]
    assert str(state.checkout_lease.path) not in str(refusal.value)


def test_project_test_real_executor_preserves_active_lease_for_later_commit(
    repo_tools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("requires Linux root to exercise the fixed worker identity")

    import mimir.contained_checkout as contained_checkout
    import mimir.contained_execution as contained_execution
    import mimir.worklink.checkout as worklink_checkout
    import mimir.worklink.worker_exec as worker_exec
    from mimir.worklink.worker_client import WorkerClient

    state = repo_tools[-2]
    lease = state.checkout_lease.path
    restored_modes: dict[Path, int] = {}
    for ancestor in (lease, *lease.parents):
        restored_modes[ancestor] = stat.S_IMODE(ancestor.stat().st_mode)
        ancestor.chmod(restored_modes[ancestor] | 0o001)
        if ancestor == Path("/tmp"):
            break
    boundary = Path("/tmp") / f"mimir-repo-test-{uuid.uuid4()}"
    checkout_root = boundary / "repo-test-checkouts"
    home_root = boundary / "homes"
    controller_home = boundary / "controller-home"
    socket_path = boundary / "executor.sock"
    boundary.mkdir()
    boundary.chmod(0o755)
    checkout_root.mkdir(mode=0o771)
    checkout_root.chmod(0o771)
    os.chown(checkout_root, 0, 1001)
    home_root.mkdir(mode=0o710)
    home_root.chmod(0o710)
    os.chown(home_root, 0, 1002)
    controller_home.mkdir(mode=0o700)
    os.chown(controller_home, 1001, 1001)
    canary = controller_home / "repo-test-canary"
    canary.write_text("protected", encoding="utf-8")
    os.chown(canary, 1001, 1001)
    canary.chmod(0o600)
    config_home = boundary / "config"
    python_executable = shutil.which("python")
    assert python_executable is not None
    _configure_worklink_test(config_home, "/bin/sh run-tests.sh")
    for path in (config_home, *config_home.rglob("*"), lease, *lease.rglob("*")):
        os.chown(path, 1001, 1001, follow_symlinks=False)
    monkeypatch.setenv("MIMIR_HOME", str(config_home))
    monkeypatch.setenv("HOME", str(controller_home))
    planted_conftest = _repo_test_conftest_payload(canary)
    _plant_repo_test_payload(lease, planted_conftest, python_executable)
    assert (lease / "conftest.py").read_bytes() == planted_conftest
    for path in (lease / "conftest.py", lease / "test_payload.py", lease / "run-tests.sh"):
        os.chown(path, 1001, 1001)
    before = _recursive_metadata(lease)

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    listener.bind(str(socket_path))
    socket_path.chmod(0o666)
    listener.listen(1)
    result_read, result_write = os.pipe()
    ready_read, ready_write = os.pipe()
    unsafe_root = boundary / "unsafe-snapshots"
    unsafe_root.mkdir()
    os.chown(unsafe_root, 1001, 1001)
    previous_roots = (
        contained_checkout.REPO_TEST_CHECKOUT_ROOT,
        worklink_checkout._REPO_TEST_CHECKOUT_ROOT,
        worker_exec.REPO_TEST_CHECKOUT_ROOT,
        worker_exec.HOME_ROOT,
        contained_execution.WorkerClient,
    )
    contained_checkout.REPO_TEST_CHECKOUT_ROOT = checkout_root
    worklink_checkout._REPO_TEST_CHECKOUT_ROOT = checkout_root
    worker_exec.REPO_TEST_CHECKOUT_ROOT = checkout_root
    worker_exec.HOME_ROOT = home_root
    controller_pid = os.fork()
    if controller_pid == 0:
        os.close(result_read)
        os.close(ready_read)
        try:
            os.setgroups([1002])
            os.setresgid(1001, 1001, 1001)
            os.setresuid(1001, 1001, 1001)
            assert os.access(lease, os.R_OK | os.X_OK)
            assert any(lease.iterdir())
            assert os.access(socket_path, os.W_OK)
            assert stat.S_ISSOCK(socket_path.stat().st_mode)
            assert os.access(config_home / "worklink.yaml", os.R_OK)
            assert (config_home / "worklink.yaml").read_text(encoding="utf-8")
            os.write(ready_write, b"controller-ready")
            os.close(ready_write)
            contained_execution.WorkerClient = lambda capability: WorkerClient(
                capability, socket_path=socket_path
            )
            result = asyncio.run(RepoProjectTests(state).execute(("test_payload.py",)))
            positive_canary = canary.read_text(encoding="utf-8")
            canary.write_text("protected", encoding="utf-8")
            unsafe_observation: dict[str, object] = {}
            unsafe_issued: list[Path] = []

            async def unsafe_runner(argv, directory, worker_env, projections, **_kwargs):
                execution_env = dict(worker_env)
                execution_env["HOME"] = str(controller_home)
                completed = await asyncio.to_thread(
                    subprocess.run,
                    argv,
                    cwd=directory.path,
                    env=execution_env,
                    capture_output=True,
                    check=False,
                )
                unsafe_observation.update(json.loads(
                    (directory.path / "snapshot-executed.json").read_text(encoding="utf-8")
                ))
                return CollectedExecutionResult(
                    completed.returncode,
                    completed.stdout,
                    completed.stderr,
                    False,
                    False,
                    0,
                    0,
                )

            unsafe_result = asyncio.run(RepoProjectTests(
                state,
                runner=unsafe_runner,
                checkout_factory=_snapshot_checkout_factory(unsafe_root, unsafe_issued),
            ).execute(("test_payload.py",)))
            lease_preserved = _recursive_metadata(lease) == before
            payload_preserved = (lease / "conftest.py").read_bytes() == planted_conftest
            for payload_path in ("conftest.py", "test_payload.py", "run-tests.sh"):
                (lease / payload_path).unlink()
            (lease / "tracked.txt").write_text("after test\n", encoding="utf-8")
            later_stage = RepoGitTools(state).execute(GitStage(("tracked.txt",))).ok
            later_commit = RepoGitTools(state).execute(
                GitCommit(("tracked.txt",), "after contained test")
            ).ok
            response = {
                "positive": asdict(result),
                "positive_canary": positive_canary,
                "unsafe": unsafe_observation,
                "unsafe_ok": unsafe_result.ok,
                "unsafe_snapshot_cleaned": (
                    len(unsafe_issued) == 1 and not unsafe_issued[0].exists()
                ),
                "final_canary": canary.read_text(encoding="utf-8"),
                "lease_preserved": lease_preserved,
                "payload_preserved": payload_preserved,
                "later_stage": later_stage,
                "later_commit": later_commit,
            }
            os.write(result_write, json.dumps(response).encode())
            os._exit(0)
        except BaseException as exc:
            os.write(result_write, json.dumps({
                "error": repr(exc),
                "traceback": traceback.format_exc(),
            }).encode())
            os._exit(1)

    os.close(result_write)
    os.close(ready_write)
    controller_reaped = False

    def controller_failure(stage: str) -> None:
        nonlocal controller_reaped
        readable, _, _ = select.select([result_read], [], [], 0)
        detail = os.read(result_read, 1_048_576).decode() if readable else ""
        reaped_pid, status = os.waitpid(controller_pid, os.WNOHANG)
        controller_reaped = reaped_pid == controller_pid
        pytest.fail(
            f"controller failed before {stage}: status={status}, result={detail or '<none>'}"
        )

    try:
        readable, _, _ = select.select([ready_read, result_read], [], [], 20)
        if result_read in readable:
            controller_failure("readiness")
        if ready_read not in readable:
            controller_failure("readiness deadline")
        assert os.read(ready_read, 64) == b"controller-ready"

        readable, _, _ = select.select([listener, result_read], [], [], 20)
        if result_read in readable:
            controller_failure("executor connection")
        if listener not in readable:
            controller_failure("executor connection deadline")
        connection, _ = listener.accept()
        _pid, uid, _gid = struct.unpack(
            "3i",
            connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")),
        )
        assert uid == 1001

        def execution_deadline(_signum, _frame):
            raise TimeoutError("contained repo-test proof exceeded 30 seconds")

        previous_alarm_handler = signal.signal(signal.SIGALRM, execution_deadline)
        signal.alarm(30)
        try:
            worker_exec.handle_connection(connection)
            _, status = os.waitpid(controller_pid, 0)
            controller_reaped = True
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous_alarm_handler)
        observed = json.loads(os.read(result_read, 1_048_576))
        assert os.waitstatus_to_exitcode(status) == 0, json.dumps(observed, indent=2)
        positive = observed["positive"]
        assert positive["ok"] is True, json.dumps(observed, indent=2)
        payload_line = next(
            line.removeprefix("MIMIR_PAYLOAD=")
            for line in positive["stdout"].splitlines()
            if line.startswith("MIMIR_PAYLOAD=")
        )
        payload = json.loads(payload_line)
        assert payload["uid"] == 1002
        assert Path(payload["home"]).parent == home_root
        assert payload["marker"] == "executed"
        assert payload["attacked"] is False
        assert observed["positive_canary"] == "protected"
        assert not Path(payload["home"]).exists()
        assert not any(checkout_root.rglob("checkout"))

        unsafe_observation = observed["unsafe"]
        assert observed["unsafe_ok"] is True
        assert unsafe_observation["marker"] == "executed"
        assert unsafe_observation["attacked"] is True
        assert unsafe_observation["home"] == str(controller_home)
        assert observed["final_canary"] == "altered"
        assert observed["unsafe_snapshot_cleaned"] is True
        assert canary.read_text(encoding="utf-8") == "altered"
        assert observed["lease_preserved"] is True
        assert observed["payload_preserved"] is True
        assert observed["later_stage"] is True
        assert observed["later_commit"] is True
    finally:
        if not controller_reaped:
            try:
                os.kill(controller_pid, 9)
            except ProcessLookupError:
                pass
            os.waitpid(controller_pid, 0)
        (
            contained_checkout.REPO_TEST_CHECKOUT_ROOT,
            worklink_checkout._REPO_TEST_CHECKOUT_ROOT,
            worker_exec.REPO_TEST_CHECKOUT_ROOT,
            worker_exec.HOME_ROOT,
            contained_execution.WorkerClient,
        ) = previous_roots
        os.close(result_read)
        os.close(ready_read)
        listener.close()
        shutil.rmtree(boundary, ignore_errors=True)
        for ancestor, mode in restored_modes.items():
            ancestor.chmod(mode)





@pytest.mark.parametrize("kind", ["argument_scope", "lease_scope", "inactive", "invalid_head"])
def test_recorded_mutation_head_cannot_widen_or_outlive_checkout_scope(repo_tools, kind: str) -> None:
    _origin, _source, scope, state, _tools = repo_tools
    lease = state.checkout_lease
    scope_id = scope.scope_id
    head = scope.observed_head_sha
    if kind == "argument_scope":
        scope_id = "other"
    elif kind == "lease_scope":
        object.__setattr__(state, "checkout_lease", replace(lease, scope_id="other"))
    elif kind == "inactive":
        object.__setattr__(state, "checkout_lease", replace(lease, revoked=True))
    else:
        head = "not-a-commit"
    with pytest.raises(ValueError, match="does not match"):
        state.record_git_head(scope_id, head)


@pytest.mark.parametrize(
    ("operation", "removed_action"),
    [
        (GitStatus(), RepoPRAction.INSPECT),
        (GitFetch(), RepoPRAction.CHECKOUT),
        (GitStage(("tracked.txt",)), RepoPRAction.WRITE),
        (GitCommit(("tracked.txt",), "message"), RepoPRAction.COMMIT),
        (GitMerge(), RepoPRAction.WRITE),
        (GitMerge(), RepoPRAction.COMMIT),
        (GitMergeAbort(), RepoPRAction.WRITE),
        (GitRebase(), RepoPRAction.WRITE),
        (GitRebase(), RepoPRAction.COMMIT),
        (GitRebaseAbort(), RepoPRAction.WRITE),
        (GitRevert("a" * 40), RepoPRAction.COMMIT),
        (GitRevertAbort(), RepoPRAction.WRITE),
        (GitPush(), RepoPRAction.PUSH),
    ],
)
def test_every_typed_operation_pins_its_scope_authority_check(
    repo_tools, operation, removed_action: RepoPRAction,
) -> None:
    _origin, _source, scope, old_state, _tools = repo_tools
    altered = replace(
        scope,
        allowed_operations=scope.allowed_operations - {removed_action.value},
    )
    lease = replace(old_state.checkout_lease, scope_id=altered.scope_id)
    state = RepoReviewState(altered)
    state.attach_checkout_lease(lease)
    tools = RepoGitTools(state)

    with pytest.raises(GitRefusal) as refusal:
        tools.execute(operation)

    assert refusal.value.code == "scope_action_denied"


def test_scope_action_check_is_shadowed_when_enforcement_is_disabled(repo_tools) -> None:
    _origin, _source, scope, old_state, _tools = repo_tools
    altered = replace(
        scope,
        allowed_operations=scope.allowed_operations - {RepoPRAction.WRITE.value},
    )
    lease = replace(old_state.checkout_lease, scope_id=altered.scope_id)
    state = RepoReviewState(altered)
    state.attach_checkout_lease(lease)

    result = RepoGitTools(state, enforce=False).execute(GitStage(("tracked.txt",)))

    assert result.ok is True


@pytest.mark.parametrize(
    "operation",
    [GitMerge(), GitMergeAbort(), GitRebase(), GitRebaseAbort(), GitRevertAbort()],
)
def test_merge_rebase_and_abort_are_separate_closed_operation_types(operation) -> None:
    assert not hasattr(operation, "argv")
    assert not hasattr(operation, "abort")


@pytest.mark.parametrize("failure,code", [("timeout", "timeout"), ("output", "output_limit")])
def test_git_execution_refuses_timeout_and_output_overflow(repo_tools, failure: str, code: str) -> None:
    _origin, _source, _scope, state, _tools = repo_tools

    def bounded_failure(argv, *, env, timeout, output_limit):
        if "status" in argv:
            return GitProcessResult(
                -9,
                stdout="x" * output_limit if failure == "output" else "",
                timed_out=failure == "timeout",
                output_limited=failure == "output",
            )
        return _bounded_subprocess_runner(
            argv, env=env, timeout=timeout, output_limit=output_limit,
        )

    tools = RepoGitTools(state, runner=bounded_failure)
    with pytest.raises(GitRefusal) as refusal:
        tools.execute(GitStatus())
    assert refusal.value.code == code


def test_git_execution_os_failure_is_a_named_refusal(repo_tools) -> None:
    state = repo_tools[-2]

    def failed_runner(argv, *, env, timeout, output_limit):
        raise OSError("exec failed")

    with pytest.raises(GitRefusal) as refusal:
        RepoGitTools(state, runner=failed_runner).execute(GitStatus())
    assert refusal.value.code == "git_failed"


@pytest.mark.parametrize(
    ("failure", "expected_code", "exception_type"),
    [
        (ToolPolicyRefusal("scope action denied"), "repository_authorization_refused", ToolPolicyRefusal),
        (GitRefusal("inactive_checkout", "lease binding missing"), "repository_binding_invalid", ToolPolicyRefusal),
        (RuntimeError("plain Git invocation failed"), "repository_git_failed", ToolException),
    ],
)
def test_repo_wrapper_failure_classes_have_distinct_stable_codes(
    repo_tools,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected_code: str,
    exception_type: type[Exception],
) -> None:
    from mimir.tools import repo as repo_module

    state = repo_tools[-2]
    if isinstance(failure, ToolPolicyRefusal):
        monkeypatch.setattr(repo_module, "_state", lambda *_args: (_ for _ in ()).throw(failure))
    else:
        monkeypatch.setattr(repo_module, "_state", lambda *_args: state)

        class FailingRepoGitTools:
            def __init__(self, review_state, *, enforce=True):
                self.review_state = review_state

            def execute(self, operation):
                raise failure

        monkeypatch.setattr(repo_module, "RepoGitTools", FailingRepoGitTools)

    with pytest.raises(exception_type) as refusal:
        repo_module._execute(None, "owner/repo", 7, GitStatus())

    assert f"repository operation rejected ({expected_code})" in str(refusal.value)
    assert "repository_operation_failed" not in str(refusal.value)


def test_repo_wrapper_git_stderr_redacts_embedded_remote_credential(
    repo_tools, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.tools import repo as repo_module

    secret_url = "https://agent:super-secret-password@example.invalid/owner/repo.git"

    class FailingRepoGitTools:
        def __init__(self, review_state, *, enforce=True):
            self.review_state = review_state

        def execute(self, operation):
            raise GitRefusal("git_failed", f"fatal: unable to access {secret_url}")

    monkeypatch.setattr(repo_module, "_state", lambda *_args: repo_tools[-2])
    monkeypatch.setattr(repo_module, "RepoGitTools", FailingRepoGitTools)

    with pytest.raises(ToolException) as refusal:
        repo_module._execute(None, "owner/repo", 7, GitStatus())

    # The wrapper is a second redaction boundary for injected/future runners.
    rendered = str(refusal.value)
    assert "repository operation rejected (repository_git_failed)" in rendered
    assert "super-secret-password" not in rendered
    assert "https://[REDACTED]@example.invalid/owner/repo.git" in rendered


def test_inspection_operation_types_cannot_express_fetch_or_mutation() -> None:
    for operation in (GitStatus(), GitDiff(), GitUnmerged()):
        assert not hasattr(operation, "argv")
        assert not hasattr(operation, "remote")
        assert not hasattr(operation, "ref")


@pytest.mark.parametrize("mode", ["timeout", "output"])
def test_real_subprocess_runner_enforces_wall_time_and_capture_cap(mode: str) -> None:
    command = (
        ("/bin/sh", "-c", "sleep 2")
        if mode == "timeout"
        else ("/bin/sh", "-c", "while :; do printf 1234567890; done")
    )
    result = _bounded_subprocess_runner(
        command,
        env={"PATH": "/usr/bin:/bin"},
        timeout=0.05 if mode == "timeout" else 2,
        output_limit=64,
    )
    if mode == "timeout":
        assert result.timed_out is True
    else:
        assert result.output_limited is True
        assert len(result.stdout.encode()) <= 64
