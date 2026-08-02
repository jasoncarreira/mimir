from __future__ import annotations

import base64
from dataclasses import replace
from pathlib import Path
import subprocess

import pytest
from langchain_core.tools import ToolException

from mimir.models import RepoPRAction, RepoPRActionScope, RepoReviewState
from mimir.pr_checkout_lease import (
    PRCheckoutLease,
    cleanup_pr_checkout_lease,
    create_pr_checkout_lease,
)
from mimir.project_tests import (
    ProjectTestProcessResult,
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


def test_checked_failure_stdout_is_redacted_but_commands_must_opt_in(repo_tools) -> None:
    _origin, _source, _scope, state, _tools = repo_tools
    secret = "stdout-secret"

    def failed_runner(argv, *, env, timeout, output_limit):
        if "status" in argv:
            return GitProcessResult(1, stdout=f"diagnostic {secret}")
        return _bounded_subprocess_runner(
            argv, env=env, timeout=timeout, output_limit=output_limit,
        )

    tools = RepoGitTools(state, runner=failed_runner)
    with pytest.raises(GitRefusal, match="Git operation failed"):
        tools._command(("status",), sensitive_values=(secret,))
    with pytest.raises(GitRefusal) as refusal:
        tools._checked(
            ("status",),
            sensitive_values=(secret,),
            report_stdout_on_failure=True,
        )

    assert str(refusal.value) == "diagnostic [REDACTED]"


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


def _clean_rebase_tools(tmp_path: Path) -> tuple[Path, Path, RepoPRActionScope, RepoReviewState]:
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
        event_type="pr_mergeability_rebase",
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


def test_force_push_guard_refuses_before_any_network_call(repo_tools, monkeypatch) -> None:
    _origin, _source, _scope, state, _tools = repo_tools
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


def test_project_tests_use_fixed_command_checkout_and_environment(
    repo_tools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _origin, _source, _scope, state, _tools = repo_tools
    home = tmp_path / "home"
    _configure_worklink_test(home)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_never_pass_to_tests")
    calls = []

    def runner(argv, *, cwd, env, timeout, output_limit):
        calls.append((argv, cwd, env, timeout, output_limit))
        return ProjectTestProcessResult(0, stdout=f"ok {cwd}")

    result = RepoProjectTests(state, runner=runner).execute(("tracked.txt::case",))

    assert result.ok is True
    assert result.code == "tests_passed"
    assert result.command == ("/usr/bin/true", "-q")
    assert result.command_source == "deployment"
    assert result.stdout == "ok <checkout>"
    argv, cwd, env, timeout, output_limit = calls[0]
    assert argv == ("/usr/bin/true", "-q", "tracked.txt::case")
    assert cwd == state.checkout_lease.path.resolve()
    assert "GITHUB_TOKEN" not in env
    assert "MIMIR_MODEL_SPEC" not in env
    assert timeout == 300.0
    assert output_limit == 64 * 1024


def test_project_tests_prefer_repository_command_and_report_source(
    repo_tools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _origin, _source, scope, state, _tools = repo_tools
    home = tmp_path / "home"
    home.mkdir()
    (home / "worklink.yaml").write_text(
        f"""
defaults:
  test_command: /usr/bin/false
repositories:
  - slug: owner/repo
    root: {scope.canonical_root}
    mode: rw
    origin: https://github.com/owner/repo.git
    base_branch: main
    test_command: /usr/bin/true --repository
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("MIMIR_HOME", str(home))
    calls = []

    def runner(argv, *, cwd, env, timeout, output_limit):
        calls.append(argv)
        return ProjectTestProcessResult(0)

    result = RepoProjectTests(state, runner=runner).execute(())

    assert calls == [("/usr/bin/true", "--repository")]
    assert result.command == ("/usr/bin/true", "--repository")
    assert result.command_source == "repository"


def test_project_tests_refuse_unresolvable_repository_command(
    repo_tools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _origin, _source, scope, state, _tools = repo_tools
    home = tmp_path / "home"
    home.mkdir()
    (home / "worklink.yaml").write_text(
        f"""
repositories:
  - slug: owner/repo
    root: {scope.canonical_root}
    mode: rw
    origin: https://github.com/owner/repo.git
    base_branch: main
    test_command: runner-that-does-not-exist --quiet
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("MIMIR_HOME", str(home))

    with pytest.raises(ProjectTestRefusal) as exc_info:
        RepoProjectTests(state).execute(())

    assert exc_info.value.code == "test_command_unresolvable"


def test_project_test_home_is_writable_and_fresh_per_execution(
    repo_tools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HOME must be writable, and a different directory each execution.

    Observed live on 2026-07-31: uv failed before reaching pytest because it
    could not create ``$HOME/.cache/uv`` under ``/nonexistent``.
    """
    _origin, _source, _scope, state, _tools = repo_tools
    home = tmp_path / "home"
    _configure_worklink_test(home)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    seen: list[Path] = []

    def runner(argv, *, cwd, env, timeout, output_limit):
        run_home = Path(env["HOME"])
        seen.append(run_home)
        assert run_home.is_dir(), "HOME must exist so a runner can create its cache"
        (run_home / ".cache").mkdir(parents=True, exist_ok=True)
        assert run_home != Path("/nonexistent")
        assert run_home != Path.home(), "the operator's real dotfiles stay unreachable"
        assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        return ProjectTestProcessResult(0, stdout="ok")

    tests = RepoProjectTests(state, runner=runner)
    tests.execute(())
    tests.execute(())

    assert len(seen) == 2
    assert seen[0] != seen[1], "each execution must get its own HOME"
    for run_home in seen:
        assert not run_home.exists(), "each HOME must be removed after the execution"


def test_project_test_home_does_not_carry_state_between_executions(
    repo_tools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """State planted by one execution must not be visible to the next.

    The tests that run in this HOME are PR-controlled, so a shared directory
    would let one PR plant ambient config -- a .npmrc, a pip.conf; only Git
    configuration is neutralised -- for a later PR to pick up.
    """
    _origin, _source, _scope, state, _tools = repo_tools
    home = tmp_path / "home"
    _configure_worklink_test(home)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    observed: list[bool] = []

    def runner(argv, *, cwd, env, timeout, output_limit):
        planted = Path(env["HOME"]) / ".npmrc"
        observed.append(planted.exists())
        planted.write_text("registry=http://attacker.invalid\n", encoding="utf-8")
        return ProjectTestProcessResult(0, stdout="ok")

    tests = RepoProjectTests(state, runner=runner)
    tests.execute(())
    tests.execute(())

    assert observed == [False, False], "planted config leaked into the next execution"


def test_project_test_parent_swap_during_run_cannot_delete_outside_tree(
    repo_tools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parent swapped mid-run must not redirect cleanup outside the home.

    The tests executing in the per-run HOME are PR-controlled. If cleanup resolved
    the home by pathname, replacing the parent with a symlink during the run would
    make rmtree traverse the swap and delete a matching tree elsewhere. Creation
    and deletion are both descriptor-relative, so the swap is inert.
    """
    _origin, _source, _scope, state, _tools = repo_tools
    home = tmp_path / "home"
    _configure_worklink_test(home)
    monkeypatch.setenv("MIMIR_HOME", str(home))

    # A tree outside the home that must survive, containing a same-named entry so
    # a pathname-based delete would actually hit something.
    outside = tmp_path / "outside"
    outside.mkdir()
    treasure = outside / "keep.txt"
    treasure.write_text("must survive\n", encoding="utf-8")

    parent = home.resolve() / "scratch" / "project-test-homes"

    captured: dict[str, str] = {}

    def runner(argv, *, cwd, env, timeout, output_limit):
        run_home = Path(env["HOME"])
        captured["name"] = run_home.name
        # Mirror the run directory's name inside the outside tree, then swap the
        # parent for a symlink to it -- exactly what PR-controlled test code could
        # do while executing.
        decoy = outside / run_home.name
        decoy.mkdir()
        (decoy / "keep.txt").write_text("decoy\n", encoding="utf-8")
        import shutil as _shutil
        _shutil.rmtree(parent)
        parent.symlink_to(outside, target_is_directory=True)
        return ProjectTestProcessResult(0, stdout="ok")

    RepoProjectTests(state, runner=runner).execute(())

    # The decoy is what a pathname-based cleanup destroys: after the swap,
    # parent/<run-name> resolves to outside/<run-name>. Descriptor-relative
    # deletion never resolves that pathname, so the decoy must be untouched.
    decoy = outside / captured["name"]
    assert decoy.is_dir(), "cleanup followed the swapped parent and deleted outside the home"
    assert (decoy / "keep.txt").read_text(encoding="utf-8") == "decoy\n"
    assert treasure.read_text(encoding="utf-8") == "must survive\n"


@pytest.mark.parametrize("depth", ["scratch", "project-test-homes"])
def test_project_test_refuses_symlinked_ancestor_at_any_depth(
    repo_tools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, depth: str,
) -> None:
    """A symlink at ANY component below MIMIR_HOME must refuse, not be followed.

    O_NOFOLLOW guards only the final component, so a symlink planted at the
    intermediate ``scratch`` level -- with a real ``project-test-homes`` inside the
    target -- would otherwise be followed by both mkdir(parents=True) and
    os.open(parent, O_NOFOLLOW), placing HOME outside the home tree and restoring
    the cross-run ambient-state channel.
    """
    _origin, _source, _scope, state, _tools = repo_tools
    home = tmp_path / "home"
    _configure_worklink_test(home)
    monkeypatch.setenv("MIMIR_HOME", str(home))

    outside = tmp_path / "outside"
    if depth == "scratch":
        # The target contains a REAL final component, so a final-component-only
        # check would succeed here.
        (outside / "project-test-homes").mkdir(parents=True)
        (home / "scratch").symlink_to(outside, target_is_directory=True)
    else:
        outside.mkdir()
        (home / "scratch").mkdir(parents=True, exist_ok=True)
        (home / "scratch" / "project-test-homes").symlink_to(
            outside, target_is_directory=True,
        )

    def runner(argv, *, cwd, env, timeout, output_limit):  # pragma: no cover
        raise AssertionError("runner must not be reached")

    with pytest.raises(ProjectTestRefusal) as refusal:
        RepoProjectTests(state, runner=runner).execute(())
    assert refusal.value.code == "test_cache_home_unavailable"
    # Nothing may have been created through the symlink.
    assert not any(outside.rglob("run-*")), "an execution home was created outside the home tree"


def test_project_test_failure_is_actionable_and_output_is_scrubbed(
    repo_tools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = repo_tools[-2]
    home = tmp_path / "home"
    _configure_worklink_test(home, "/usr/bin/false")
    monkeypatch.setenv("MIMIR_HOME", str(home))
    secret = "ghp_abcdefghijklmnopqrstuvwxyz"

    def runner(argv, *, cwd, env, timeout, output_limit):
        return ProjectTestProcessResult(
            3, stdout=f"failure in {cwd}\n{secret}", stderr=f"token={secret}",
        )

    result = RepoProjectTests(state, runner=runner).execute()

    assert result.ok is False
    assert result.code == "tests_failed"
    assert result.returncode == 3
    assert str(state.checkout_lease.path.resolve()) not in result.stdout
    assert secret not in result.stdout + result.stderr
    assert "[REDACTED]" in result.stdout + result.stderr


@pytest.mark.parametrize("selector", ["--flag", "../outside", "tracked.txt;id", "/tmp/test"])
def test_project_test_selector_injection_is_refused(
    repo_tools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selector: str,
) -> None:
    state = repo_tools[-2]
    home = tmp_path / "home"
    _configure_worklink_test(home)
    monkeypatch.setenv("MIMIR_HOME", str(home))

    with pytest.raises(ProjectTestRefusal) as refusal:
        RepoProjectTests(state).execute((selector,))
    assert refusal.value.code in {"test_selector_invalid", "test_selector_outside_checkout"}


def test_project_test_resolves_selector_before_checkout_containment(
    repo_tools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = repo_tools[-2]
    home = tmp_path / "home"
    _configure_worklink_test(home)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    outside = tmp_path / "outside-test"
    outside.write_text("outside\n", encoding="utf-8")
    (state.checkout_lease.path / "escaped-test").symlink_to(outside)

    with pytest.raises(ProjectTestRefusal) as refusal:
        RepoProjectTests(state).execute(("escaped-test",))
    assert refusal.value.code == "test_selector_outside_checkout"

    internal_target = state.checkout_lease.path / "tracked.txt"
    (state.checkout_lease.path / "internal-test-link").symlink_to(internal_target)
    with pytest.raises(ProjectTestRefusal) as internal:
        RepoProjectTests(state).execute(("internal-test-link",))
    assert internal.value.code == "test_selector_outside_checkout"


def test_project_test_scope_action_and_active_lease_guards_are_pinned(
    repo_tools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _origin, _source, scope, state, _tools = repo_tools
    home = tmp_path / "home"
    _configure_worklink_test(home)
    monkeypatch.setenv("MIMIR_HOME", str(home))

    denied_scope = replace(
        scope,
        allowed_operations=scope.allowed_operations - {RepoPRAction.TEST.value},
    )
    denied_state = RepoReviewState(denied_scope)
    with pytest.raises(ProjectTestRefusal) as denied:
        RepoProjectTests(denied_state).execute()
    assert denied.value.code == "scope_action_denied"

    object.__setattr__(state, "checkout_lease", replace(state.checkout_lease, revoked=True))
    with pytest.raises(ProjectTestRefusal) as inactive:
        RepoProjectTests(state).execute()
    assert inactive.value.code == "inactive_checkout"


def test_project_test_output_limit_returns_no_partial_unredactable_output(
    repo_tools, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = repo_tools[-2]
    home = tmp_path / "home"
    _configure_worklink_test(home)
    monkeypatch.setenv("MIMIR_HOME", str(home))

    def runner(argv, *, cwd, env, timeout, output_limit):
        return ProjectTestProcessResult(
            -9, stdout="possibly-partial-secret", output_limited=True,
        )

    result = RepoProjectTests(state, runner=runner).execute()
    assert result.code == "test_output_limit"
    assert result.stdout == result.stderr == ""


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
