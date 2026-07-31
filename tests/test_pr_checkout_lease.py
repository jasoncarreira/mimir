from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from mimir.access_control import (
    CapabilityTier,
    ToolRegistry,
    build_trigger_service_principal,
)
from mimir.models import (
    AuthContext,
    InformationFlowLabels,
    RepoPRAction,
    RepoPRActionScope,
    RepoReviewState,
    TurnInteractivity,
)
from mimir.pr_checkout_lease import (
    PRCheckoutLease,
    acquire_pr_checkout_lease,
    cleanup_pr_checkout_lease,
    create_pr_checkout_lease,
    recover_pr_checkout_lease,
)
from mimir._context import reset_current_turn, set_current_turn
from mimir.readonly_backend import WriteGuardBackend


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git", "-C", str(cwd),
            "-c", "user.name=test", "-c", "user.email=test@example.com",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _repo_and_scope(tmp_path: Path) -> tuple[Path, RepoPRActionScope]:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    repo = tmp_path / "source"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    _git(repo, "checkout", "-q", "-b", "main")
    (repo / "file.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "-q", "origin", "HEAD:main")
    _git(repo, "checkout", "-q", "-b", "worklink/7")
    (repo / "file.txt").write_text("pull request\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "pr")
    head_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "-q", "origin", "HEAD:worklink/7")
    return repo, RepoPRActionScope(
        provenance="poller_payload",
        canonical_repo="owner/repo",
        canonical_root=str(repo.resolve()),
        canonical_origin=str(origin),
        principal="mimir-bot",
        event_type="pr_changes_requested_stale",
        allowed_operations=frozenset(action.value for action in RepoPRAction),
        pr_number=7,
        head_repo="owner/repo",
        head_remote="origin",
        destination_ref="refs/heads/worklink/7",
        observed_head_sha=head_sha,
        base_ref="main",
        observed_base_sha=base_sha,
    )


def test_pr_checkout_lease_checks_out_exact_authorized_head_and_recovers(tmp_path: Path) -> None:
    repo, scope = _repo_and_scope(tmp_path)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    state = RepoReviewState(scope)

    lease = create_pr_checkout_lease(
        scope, owner="mimir-bot", lease_root=lease_root, review_state=state,
    )

    assert lease.path.parent == lease_root.resolve()
    assert lease.path != repo
    assert lease.destination_ref == scope.destination_ref
    assert lease.scope_base_sha == scope.observed_base_sha
    assert lease.base_sha == scope.observed_base_sha
    assert _git(lease.path, "rev-parse", "HEAD") == scope.observed_head_sha
    assert state.root == str(lease.path)
    assert state.checkout_lease is lease
    recovered = recover_pr_checkout_lease(
        lease.path, scope, owner="mimir-bot", lease_root=lease_root,
    )
    assert recovered.recovered is True
    assert recovered.scope_id == scope.scope_id

    assert cleanup_pr_checkout_lease(lease, review_state=state) is True
    assert cleanup_pr_checkout_lease(lease, review_state=state) is False
    assert state.checkout_lease is None


def test_second_attempt_resumes_unpushed_commit(tmp_path: Path) -> None:
    _repo, scope = _repo_and_scope(tmp_path)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    first = create_pr_checkout_lease(scope, owner=scope.principal, lease_root=lease_root)
    (first.path / "fix.txt").write_text("retained fix\n", encoding="utf-8")
    _git(first.path, "add", "fix.txt")
    _git(first.path, "commit", "-q", "-m", "retained fix")
    fix_head = _git(first.path, "rev-parse", "HEAD")
    state = RepoReviewState(scope)

    resumed, candidates = acquire_pr_checkout_lease(
        scope, owner=scope.principal, lease_root=lease_root, review_state=state,
    )

    assert resumed.path == first.path
    assert resumed.recovered is True
    assert candidates == (fix_head,)
    assert state.checkout_lease is resumed
    assert state.git_expected_head == fix_head
    assert len([path for path in lease_root.iterdir() if path.is_dir()]) == 1


def test_expired_lease_with_unpushed_work_is_named_and_renewed(tmp_path: Path) -> None:
    _repo, scope = _repo_and_scope(tmp_path)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    first = create_pr_checkout_lease(scope, owner=scope.principal, lease_root=lease_root)
    (first.path / "fix.txt").write_text("expired retained fix\n", encoding="utf-8")
    _git(first.path, "add", "fix.txt")
    _git(first.path, "commit", "-q", "-m", "expired retained fix")
    fix_head = _git(first.path, "rev-parse", "HEAD")
    metadata_path = first.path / ".git" / "mimir-pr-checkout-lease.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["expires_at"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    metadata_path.write_text(json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8")

    resumed, candidates = acquire_pr_checkout_lease(
        scope, owner=scope.principal, lease_root=lease_root,
    )

    assert resumed.path == first.path
    assert resumed.is_active
    assert candidates == (fix_head,)
    renewed = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert datetime.fromisoformat(renewed["expires_at"]) > datetime.now(UTC)


def test_expired_lease_refuses_resume_after_pr_head_moves(tmp_path: Path) -> None:
    repo, scope = _repo_and_scope(tmp_path)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    lease = create_pr_checkout_lease(scope, owner=scope.principal, lease_root=lease_root)
    (lease.path / "fix.txt").write_text("stale fix\n", encoding="utf-8")
    _git(lease.path, "add", "fix.txt")
    _git(lease.path, "commit", "-q", "-m", "stale fix")
    metadata_path = lease.path / ".git" / "mimir-pr-checkout-lease.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    expired_at = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    metadata["expires_at"] = expired_at
    metadata_path.write_text(json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8")
    (repo / "new-head.txt").write_text("new head\n", encoding="utf-8")
    _git(repo, "add", "new-head.txt")
    _git(repo, "commit", "-q", "-m", "move PR head")
    _git(repo, "push", "-q", "origin", "HEAD:worklink/7")

    with pytest.raises(RuntimeError, match="PR head advanced"):
        acquire_pr_checkout_lease(scope, owner=scope.principal, lease_root=lease_root)

    retained = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert retained["expires_at"] == expired_at
    assert lease.path.is_dir()


def test_acquire_refuses_scope_without_current_checkout_authority(tmp_path: Path) -> None:
    _repo, scope = _repo_and_scope(tmp_path)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    create_pr_checkout_lease(scope, owner=scope.principal, lease_root=lease_root)
    unauthorized = replace(
        scope,
        allowed_operations=scope.allowed_operations - {RepoPRAction.CHECKOUT.value},
    )

    with pytest.raises(RuntimeError, match="does not grant PR checkout"):
        acquire_pr_checkout_lease(
            unauthorized, owner=unauthorized.principal, lease_root=lease_root,
        )


def test_acquire_refuses_scope_mismatch_in_matching_lease_slot(tmp_path: Path) -> None:
    _repo, scope = _repo_and_scope(tmp_path)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    lease = create_pr_checkout_lease(scope, owner=scope.principal, lease_root=lease_root)
    metadata_path = lease.path / ".git" / "mimir-pr-checkout-lease.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["scope_id"] = "f" * 64
    metadata_path.write_text(json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="scope mismatch"):
        acquire_pr_checkout_lease(scope, owner=scope.principal, lease_root=lease_root)

    assert len([path for path in lease_root.iterdir() if path.is_dir()]) == 1


def test_acquire_reports_all_divergent_unpublished_candidates(tmp_path: Path) -> None:
    _repo, scope = _repo_and_scope(tmp_path)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    candidates = []
    for name in ("first", "second"):
        lease = create_pr_checkout_lease(scope, owner=scope.principal, lease_root=lease_root)
        (lease.path / f"{name}.txt").write_text(f"{name} fix\n", encoding="utf-8")
        _git(lease.path, "add", f"{name}.txt")
        _git(lease.path, "commit", "-q", "-m", f"{name} fix")
        candidates.append(_git(lease.path, "rev-parse", "HEAD"))

    with pytest.raises(RuntimeError, match="refusing implicit selection") as refusal:
        acquire_pr_checkout_lease(scope, owner=scope.principal, lease_root=lease_root)

    assert all(candidate in str(refusal.value) for candidate in candidates)


def test_pr_checkout_lease_cleanup_accepts_commit_on_lease_branch(tmp_path: Path) -> None:
    _repo, scope = _repo_and_scope(tmp_path)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    lease = create_pr_checkout_lease(scope, owner="mimir-bot", lease_root=lease_root)
    (lease.path / "fix.txt").write_text("authorized fix\n", encoding="utf-8")
    _git(lease.path, "add", "fix.txt")
    _git(lease.path, "commit", "-q", "-m", "fix")

    assert _git(lease.path, "rev-parse", "HEAD") != lease.head_sha
    assert cleanup_pr_checkout_lease(lease) is True


def test_pr_checkout_lease_cleanup_refuses_mismatched_origin(tmp_path: Path) -> None:
    _repo, scope = _repo_and_scope(tmp_path)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    lease = create_pr_checkout_lease(scope, owner="mimir-bot", lease_root=lease_root)
    _git(lease.path, "remote", "set-url", "origin", "https://example.com/other.git")

    with pytest.raises(RuntimeError) as refusal:
        cleanup_pr_checkout_lease(lease)

    assert str(refusal.value) == (
        "PR checkout lease cleanup origin mismatch: "
        f"expected {scope.canonical_origin!r}; actual 'https://example.com/other.git'"
    )
    assert lease.path.is_dir()


def test_pr_checkout_lease_cleanup_accepts_metadata_before_additive_field(
    tmp_path: Path,
) -> None:
    _repo, scope = _repo_and_scope(tmp_path)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    lease = create_pr_checkout_lease(scope, owner="mimir-bot", lease_root=lease_root)
    metadata_path = lease.path / ".git" / "mimir-pr-checkout-lease.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["version"] = 1
    del metadata["scope_base_sha"]
    metadata_path.write_text(json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8")

    assert cleanup_pr_checkout_lease(lease) is True


def test_pr_checkout_lease_cleanup_refuses_directory_from_another_lease(
    tmp_path: Path,
) -> None:
    _repo, scope = _repo_and_scope(tmp_path)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    lease = create_pr_checkout_lease(scope, owner="mimir-bot", lease_root=lease_root)
    other = create_pr_checkout_lease(scope, owner="mimir-bot", lease_root=lease_root)

    with pytest.raises(RuntimeError) as refusal:
        cleanup_pr_checkout_lease(replace(lease, path=other.path))

    message = str(refusal.value)
    assert "PR checkout lease cleanup metadata" in message
    assert "mismatch: expected" in message
    assert "; actual " in message
    assert lease.path.is_dir()
    assert other.path.is_dir()


def test_pr_checkout_lease_cleanup_refuses_wrong_branch(tmp_path: Path) -> None:
    _repo, scope = _repo_and_scope(tmp_path)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    lease = create_pr_checkout_lease(scope, owner="mimir-bot", lease_root=lease_root)
    _git(lease.path, "branch", "other", lease.head_sha)
    _git(lease.path, "checkout", "-q", "other")

    with pytest.raises(RuntimeError) as refusal:
        cleanup_pr_checkout_lease(lease)

    assert str(refusal.value) == (
        "PR checkout lease cleanup branch mismatch: "
        f"expected {scope.head_ref!r}; actual 'other'"
    )
    assert lease.path.is_dir()


def test_pr_checkout_lease_cleanup_refuses_unrelated_head_on_expected_branch(
    tmp_path: Path,
) -> None:
    _repo, scope = _repo_and_scope(tmp_path)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    lease = create_pr_checkout_lease(scope, owner="mimir-bot", lease_root=lease_root)
    _git(lease.path, "checkout", "-q", "--orphan", "unrelated")
    _git(lease.path, "rm", "-q", "-rf", ".")
    (lease.path / "replacement.txt").write_text("unrelated\n", encoding="utf-8")
    _git(lease.path, "add", "replacement.txt")
    _git(lease.path, "commit", "-q", "-m", "unrelated history")
    unrelated_head = _git(lease.path, "rev-parse", "HEAD")
    _git(lease.path, "branch", "-M", scope.head_ref)

    with pytest.raises(RuntimeError) as refusal:
        cleanup_pr_checkout_lease(lease)

    assert str(refusal.value) == (
        "PR checkout lease cleanup ancestry mismatch: "
        f"expected HEAD descendant of {lease.head_sha!r}; actual {unrelated_head!r}"
    )
    assert lease.path.is_dir()


def test_pr_checkout_lease_cleanup_refuses_missing_required_identity_field(
    tmp_path: Path,
) -> None:
    _repo, scope = _repo_and_scope(tmp_path)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    lease = create_pr_checkout_lease(scope, owner="mimir-bot", lease_root=lease_root)
    metadata_path = lease.path / ".git" / "mimir-pr-checkout-lease.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    del metadata["recovery_id"]
    metadata_path.write_text(json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(RuntimeError) as refusal:
        cleanup_pr_checkout_lease(lease)

    assert str(refusal.value) == (
        "PR checkout lease cleanup metadata recovery_id mismatch: "
        f"expected {lease.recovery_id!r}; actual <missing>"
    )
    assert lease.path.is_dir()


def test_pr_checkout_lease_fails_closed_on_moved_head(tmp_path: Path) -> None:
    repo, scope = _repo_and_scope(tmp_path)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    (repo / "head-moved.txt").write_text("new head\n", encoding="utf-8")
    _git(repo, "add", "head-moved.txt")
    _git(repo, "commit", "-q", "-m", "move head")
    moved_head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "-q", "origin", "HEAD:worklink/7")

    with pytest.raises(RuntimeError) as refusal:
        create_pr_checkout_lease(
            scope,
            owner="mimir-bot",
            lease_root=lease_root,
        )

    assert "PR head advanced" in str(refusal.value)
    assert scope.observed_head_sha in str(refusal.value)
    assert moved_head in str(refusal.value)
    assert list(lease_root.iterdir()) == []


def test_pr_checkout_lease_accepts_base_advanced_by_unrelated_merge(tmp_path: Path) -> None:
    repo, scope = _repo_and_scope(tmp_path)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    _git(repo, "checkout", "-q", "main")
    (repo / "unrelated.txt").write_text("merged work\n", encoding="utf-8")
    _git(repo, "add", "unrelated.txt")
    _git(repo, "commit", "-q", "-m", "unrelated merge")
    advanced_base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "-q", "origin", "HEAD:main")

    lease = create_pr_checkout_lease(scope, owner="mimir-bot", lease_root=lease_root)

    assert lease.scope_base_sha == scope.observed_base_sha
    assert lease.base_sha == advanced_base
    assert _git(lease.path, "merge-base", lease.head_sha, lease.base_sha) == scope.observed_base_sha


def test_pr_checkout_lease_refuses_rewritten_base_with_both_identities(tmp_path: Path) -> None:
    repo, scope = _repo_and_scope(tmp_path)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    _git(repo, "checkout", "-q", "--orphan", "rewritten-main")
    _git(repo, "rm", "-q", "-rf", ".")
    (repo / "replacement.txt").write_text("replacement history\n", encoding="utf-8")
    _git(repo, "add", "replacement.txt")
    _git(repo, "commit", "-q", "-m", "replace main history")
    rewritten_base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "-q", "--force", "origin", "HEAD:main")

    with pytest.raises(RuntimeError) as refusal:
        create_pr_checkout_lease(scope, owner="mimir-bot", lease_root=lease_root)

    message = str(refusal.value)
    assert "PR base history rewritten" in message
    assert f"scoped base {scope.observed_base_sha} is stale" in message
    assert f"fetched base {rewritten_base}" in message
    assert "merge-base none" in message
    assert list(lease_root.iterdir()) == []


def test_pr_checkout_lease_refuses_base_that_already_contains_head(tmp_path: Path) -> None:
    repo, scope = _repo_and_scope(tmp_path)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    _git(repo, "push", "-q", "origin", f"{scope.observed_head_sha}:main")

    with pytest.raises(RuntimeError) as refusal:
        create_pr_checkout_lease(scope, owner="mimir-bot", lease_root=lease_root)

    message = str(refusal.value)
    assert "PR base already contains the authorized head" in message
    assert f"fetched base {scope.observed_head_sha}" in message
    assert f"authorized head {scope.observed_head_sha} is stale as an open PR" in message
    assert list(lease_root.iterdir()) == []


def test_pr_checkout_recovery_refuses_scope_mismatch_and_symlink(tmp_path: Path) -> None:
    _repo, scope = _repo_and_scope(tmp_path)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    lease = create_pr_checkout_lease(scope, owner="mimir-bot", lease_root=lease_root)

    with pytest.raises(RuntimeError, match="scope mismatch"):
        recover_pr_checkout_lease(
            lease.path,
            replace(scope, canonical_repo="other/repo"),
            owner="mimir-bot",
            lease_root=lease_root,
        )
    alias = lease_root / "alias"
    alias.symlink_to(lease.path, target_is_directory=True)
    with pytest.raises(RuntimeError, match="escapes"):
        recover_pr_checkout_lease(alias, scope, owner="mimir-bot", lease_root=lease_root)


def test_review_state_refuses_mismatched_or_inactive_checkout_lease(
    tmp_path: Path,
) -> None:
    _repo, scope = _repo_and_scope(tmp_path)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    lease = create_pr_checkout_lease(scope, owner="mimir-bot", lease_root=lease_root)

    refused = (
        replace(lease, scope_id="other-scope"),
        replace(lease, owner="other-owner"),
        replace(lease, revoked=True),
    )
    for candidate in refused:
        state = RepoReviewState(scope)
        with pytest.raises(ValueError, match="does not match review scope"):
            state.attach_checkout_lease(candidate)
        assert state.checkout_lease is None
        assert state.checked_out is False


@pytest.mark.asyncio
async def test_protected_reads_allow_only_tracked_files_in_exact_authorized_pr_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, initial_scope = _repo_and_scope(tmp_path)
    fixture_source = Path(__file__).with_name("test_access_control.py")
    published = repo / "tests" / "test_access_control.py"
    published.parent.mkdir()
    published.write_bytes(fixture_source.read_bytes())
    tracked_protected_source = repo / ".env"
    tracked_protected_source.write_text("TOKEN=placeholder\n", encoding="utf-8")
    _git(repo, "add", "tests/test_access_control.py", ".env")
    _git(repo, "commit", "-q", "-m", "add authorization tests")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "-q", "origin", "HEAD:worklink/7")
    scope = replace(initial_scope, observed_head_sha=head)

    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    state = RepoReviewState(scope)
    lease = create_pr_checkout_lease(
        scope, owner=scope.principal, lease_root=lease_root, review_state=state,
    )
    other_lease = create_pr_checkout_lease(
        scope, owner=scope.principal, lease_root=lease_root,
    )
    tracked = lease.path / "tests" / "test_access_control.py"
    tracked_protected = lease.path / ".env"
    other_tracked = other_lease.path / "tests" / "test_access_control.py"
    assert "-----BEGIN OPENSSH PRIVATE KEY-----" in tracked.read_text(encoding="utf-8")

    untracked = lease.path / "notes.txt"
    untracked.write_text(
        "-----BEGIN OPENSSH PRIVATE KEY-----\nnot published\n", encoding="utf-8",
    )
    untracked_env = lease.path / "scratch" / ".env"
    untracked_env.parent.mkdir()
    untracked_env.write_text("TOKEN=ghp_" + "a" * 30 + "\n", encoding="utf-8")
    escape = lease.path / "escaped-tests.py"
    escape.symlink_to(repo / "tests" / "test_access_control.py")

    read_roots = (str(lease.path), str(other_lease.path), str(repo))
    auth = AuthContext(
        principal="service:poller:github-activity",
        canonical_principal="poller:github-activity",
        roles=("service",),
        event_ingress=None,
        trigger="poller",
        channel_id="poller:github-activity",
        interactivity=TurnInteractivity.NON_INTERACTIVE,
        is_service=True,
        service_authority=SimpleNamespace(filesystem_read_roots=read_roots),
        enforcement_enabled=True,
        repo_review_state=state,
        repo_pr_action_scope=scope,
    )
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path / "home"))
    events = []
    monkeypatch.setattr(
        "mimir.tools.budget_gate._emit_event_sync",
        lambda kind, **fields: events.append((kind, fields)),
    )

    token = set_current_turn(SimpleNamespace(turn_id="tracked-pr-read", auth_context=auth))
    try:
        lease_backend = WriteGuardBackend(lease.path, [])
        sync_result = lease_backend.read(str(tracked))
        async_result = await lease_backend.aread(str(tracked), offset=5000, limit=20)
        grep_result = lease_backend.grep("BEGIN OPENSSH", str(tracked))
        glob_result = lease_backend.glob("test_access_control.py", str(tracked.parent))
        untracked_result = lease_backend.read(str(untracked))
        untracked_grep = lease_backend.grep("BEGIN OPENSSH", str(untracked))
        tracked_protected_result = lease_backend.read(str(tracked_protected))
        env_result = lease_backend.read(str(untracked_env))
        escape_result = lease_backend.read(str(escape))
        other_result = WriteGuardBackend(other_lease.path, []).read(str(other_tracked))
        live_result = WriteGuardBackend(repo, []).read(str(published))
    finally:
        reset_current_turn(token)

    assert sync_result.error is None
    assert "-----BEGIN OPENSSH PRIVATE KEY-----" in sync_result.file_data["content"]
    assert async_result.error is None
    assert any(match["path"].endswith("tests/test_access_control.py") for match in grep_result.matches)
    assert any(match["path"].endswith("tests/test_access_control.py") for match in glob_result.matches)
    assert untracked_grep.matches == []
    refusal = (
        "Read denied: protected_read_target. For published PR content, "
        "use pr_files or pr_diff."
    )
    assert untracked_result.error == refusal
    assert tracked_protected_result.error == refusal
    assert env_result.error == refusal
    assert escape_result.error is not None
    assert other_result.error == refusal
    assert live_result.error == refusal
    denied_targets = {
        fields["target"]
        for kind, fields in events
        if kind == "hard_boundary_denied" and fields["reason"] == "protected_read_target"
    }
    assert str(untracked) in denied_targets
    assert str(tracked_protected) in denied_targets
    assert str(untracked_env) in denied_targets
    assert str(other_tracked) in denied_targets
    assert str(published) in denied_targets

    for denied_auth in (
        replace(auth, is_service=False),
        replace(auth, repo_review_state=None, repo_pr_action_scope=None),
        replace(auth, repo_pr_action_scope=initial_scope),
    ):
        token = set_current_turn(SimpleNamespace(turn_id="unauthorized-pr-read", auth_context=denied_auth))
        try:
            denied = WriteGuardBackend(lease.path, []).read(str(tracked))
        finally:
            reset_current_turn(token)
        assert denied.error == refusal

    lease.revoke()
    token = set_current_turn(SimpleNamespace(turn_id="revoked-pr-read", auth_context=auth))
    try:
        revoked = WriteGuardBackend(lease.path, []).read(str(tracked))
    finally:
        reset_current_turn(token)
    assert revoked.error == refusal


def _service_auth(service) -> AuthContext:
    labels = InformationFlowLabels()
    return AuthContext(
        principal="service:poller:github-activity",
        canonical_principal="poller:github-activity",
        roles=(),
        event_ingress=None,
        trigger="poller",
        channel_id="poller:github-activity",
        interactivity=TurnInteractivity.NON_INTERACTIVE,
        is_service=True,
        service_authority=service,
        enforcement_enabled=True,
        ifc_labels=labels,
    )


def test_github_activity_write_grant_is_exactly_active_lease_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    source = tmp_path / "source"
    lease_root = tmp_path / "leases"
    lease_path = lease_root / "scope-lease"
    sibling = lease_root / "other-lease"
    for path in (home, source, lease_path, sibling):
        path.mkdir(parents=True)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", f"{source}:rw")
    scope = RepoPRActionScope(
        provenance="poller_payload", canonical_repo="o/r",
        canonical_root=str(source.resolve()), canonical_origin="https://github.com/o/r.git",
        principal="mimir-bot", event_type="pr_changes_requested_stale",
        allowed_operations=frozenset(action.value for action in RepoPRAction),
        pr_number=7, head_repo="o/r", head_remote="origin",
        destination_ref="refs/heads/worklink/7", observed_head_sha="a" * 40,
        base_ref="main", observed_base_sha="b" * 40,
    )
    now = datetime.now(UTC)
    lease = PRCheckoutLease(
        canonical_repo="o/r", canonical_origin=scope.canonical_origin,
        source_root=source.resolve(), scope_base_sha="b" * 40,
        base_sha="b" * 40, head_sha="a" * 40,
        destination_ref=scope.destination_ref, owner="mimir-bot", scope_id=scope.scope_id,
        path=lease_path, lease_root=lease_root, created_at=now,
        expires_at=now + timedelta(hours=1), recovery_id="recovery",
    )
    state = RepoReviewState(scope)
    state.attach_checkout_lease(lease)
    service = build_trigger_service_principal(
        canonical="poller:github-activity", trigger="poller", profile="github",
        tier=CapabilityTier.CODE_EXECUTION, capabilities=("write_file", "edit_file"),
        creation_path="test",
    )
    auth = replace(_service_auth(service), repo_review_state=state)
    registry = ToolRegistry()

    def allowed(path: Path) -> bool:
        return registry.authorize_tool(
            "write_file", auth, enforce=True, target_channel=str(path),
        ).allowed

    assert allowed(lease_path / "change.py") is True
    assert allowed(source / "change.py") is False
    assert allowed(sibling / "change.py") is False
    lease.revoke()
    assert allowed(lease_path / "change.py") is False
