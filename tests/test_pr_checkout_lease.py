from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
import subprocess

import pytest

from mimir.access_control import CapabilityTier, ToolRegistry, build_trigger_service_principal
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
    cleanup_pr_checkout_lease,
    create_pr_checkout_lease,
    recover_pr_checkout_lease,
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True,
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


def test_pr_checkout_lease_fails_closed_on_moved_head(tmp_path: Path) -> None:
    _repo, scope = _repo_and_scope(tmp_path)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()

    with pytest.raises(RuntimeError, match="head does not match immutable scope"):
        create_pr_checkout_lease(
            replace(scope, observed_head_sha="f" * 40),
            owner="mimir-bot",
            lease_root=lease_root,
        )

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
        source_root=source.resolve(), base_sha="b" * 40, head_sha="a" * 40,
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
