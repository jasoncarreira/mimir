from __future__ import annotations

from dataclasses import replace
from contextlib import nullcontext
from pathlib import Path
import subprocess

import pytest

from mimir.models import RepoPRAction, RepoPRActionScope, RepoReviewState
from mimir.pr_checkout_lease import PRCheckoutLease, create_pr_checkout_lease
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
)


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
    push = next(argv for argv in calls if "push" in argv)
    assert push[-4:] == ("push", "--porcelain", scope.canonical_origin, f"HEAD:{scope.destination_ref}")
    assert not any(arg in push for arg in ("--force", "--force-with-lease", "--delete", "--tags", "--mirror", "--all"))
    ls_remote_index = next(index for index, argv in enumerate(calls) if "ls-remote" in argv)
    push_index = calls.index(push)
    assert push_index == ls_remote_index + 1
    assert _git(Path(scope.canonical_origin), "rev-parse", scope.destination_ref) == _git(
        state.checkout_lease.path, "rev-parse", "HEAD",
    )


def test_https_push_uses_scope_bound_proxy_without_credential_leak(repo_tools) -> None:
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
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def runner(argv, *, env, timeout, output_limit):
        calls.append((argv, env))
        return _bounded_subprocess_runner(
            argv, env=env, timeout=timeout, output_limit=output_limit,
        )

    tools = RepoGitTools(
        state,
        runner=runner,
        push_proxy_factory=lambda bound_scope, head: nullcontext(str(origin)),
    )
    tools.execute(GitCommit(("push.txt",), "push mutation"))
    assert tools.execute(GitPush()).ok

    push = next(argv for argv, _env in calls if "push" in argv)
    assert push[-2:] == (str(origin), f"HEAD:{https_scope.destination_ref}")
    assert token not in "\0".join(push)
    assert all(token not in "\0".join(env.values()) for _argv, env in calls)
    assert "credential" not in _git(lease.path, "config", "--local", "--list")
    assert _git(origin, "rev-parse", https_scope.destination_ref) == _git(lease.path, "rev-parse", "HEAD")


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
        f"push failed; local commit {stranded_head} remains unpushed in preserved "
        f"checkout lease {lease.path.resolve()}"
    )
    assert "upstream diagnostic" not in str(refusal.value)
    assert lease.is_active
    assert _git(lease.path, "rev-parse", "HEAD") == stranded_head


def test_force_push_guard_refuses_before_any_network_call(repo_tools, monkeypatch) -> None:
    _origin, _source, _scope, _state, tools = repo_tools
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
    _origin, _source, _scope, state, tools = repo_tools
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
        RepoGitTools(state)
    assert symlink_refusal.value.code == "invalid_checkout"

    outside = tmp_path / "outside-checkout"
    outside.mkdir()
    object.__setattr__(state, "checkout_lease", replace(lease, path=outside))
    with pytest.raises(GitRefusal) as escape_refusal:
        RepoGitTools(state)
    assert escape_refusal.value.code == "invalid_checkout"


def test_repo_url_rewrite_config_is_refused_before_network(repo_tools) -> None:
    _origin, _source, _scope, state, tools = repo_tools
    _git(state.checkout_lease.path, "config", "url.file:///tmp/attacker.insteadOf", "unused:")
    with pytest.raises(GitRefusal) as refusal:
        tools.execute(GitFetch())
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
    tools = repo_tools[-1]
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


def _configure_worklink_test(home: Path, command: str = "env -u MIMIR_MODEL_SPEC /usr/bin/true -q") -> None:
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
    assert result.stdout == "ok <checkout>"
    argv, cwd, env, timeout, output_limit = calls[0]
    assert argv == ("/usr/bin/true", "-q", "tracked.txt::case")
    assert cwd == state.checkout_lease.path.resolve()
    assert "GITHUB_TOKEN" not in env
    assert "MIMIR_MODEL_SPEC" not in env
    assert timeout == 300.0
    assert output_limit == 64 * 1024


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
