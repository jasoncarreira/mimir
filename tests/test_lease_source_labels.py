from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from mimir import access_control as access_control_module
from mimir.access_control import (
    OperationDecision,
    SinkGate,
    ToolAuthorization,
    begin_protected_result_capture,
    classify_protected_result,
    end_protected_result_capture,
    protected_result_source,
)
from mimir.models import (
    AuthContext,
    InformationFlowLabels,
    InformationFlowState,
    RepoPRActionScope,
    RepoReviewState,
    SourceLabel,
    TurnInteractivity,
)
from mimir.pr_checkout_lease import active_pr_checkout_lease_for_path


@pytest.fixture(autouse=True)
def _self_login(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "mimir-bot")
    real_head_check = access_control_module._lease_head_is_author_attested
    # Most tests in this module exercise scope/lease binding with synthetic Git
    # metadata. Dedicated tests below exercise the real checkout-head predicate.
    monkeypatch.setattr(
        access_control_module, "_lease_head_is_author_attested",
        lambda path, branch, head: True,
    )
    yield real_head_check


def _recorded_lease(
    root: Path,
    *,
    repository: str = "owner/repo",
    expires_at: datetime | None = None,
    scope: RepoPRActionScope | None = None,
) -> tuple[Path, Path, dict[str, object]]:
    checkout = root / "scope-lease"
    git_directory = checkout / ".git"
    git_directory.mkdir(parents=True)
    target = checkout / "src" / "work.py"
    target.parent.mkdir()
    target.write_text("work product\n", encoding="utf-8")
    now = datetime.now(UTC)
    record: dict[str, object] = {
        "version": 3,
        "canonical_repo": repository,
        "canonical_origin": f"https://github.com/{repository}.git",
        "source_root": str(root.parent / "source"),
        "scope_base_sha": "b" * 40,
        "base_sha": "b" * 40,
        "head_sha": "a" * 40,
        "destination_ref": "refs/heads/worklink/7",
        "owner": "mimir-bot",
        "scope_id": (scope or _scope(repository)).scope_id,
        "path": str(checkout),
        "lease_root": str(root),
        "created_at": now.isoformat(),
        "expires_at": (expires_at or now + timedelta(hours=1)).isoformat(),
        "recovery_id": "recovery-id",
        "pr_number": 7,
    }
    (git_directory / "mimir-pr-checkout-lease.json").write_text(
        json.dumps(record), encoding="utf-8",
    )
    return checkout, target, record


def _scope(
    repository: str = "owner/repo",
    *,
    pr_number: int = 7,
    observed_head_sha: str = "a" * 40,
    author: str = "mimir-bot",
) -> RepoPRActionScope:
    return RepoPRActionScope(
        provenance="server_discovered",
        canonical_repo=repository,
        canonical_root="/srv/source",
        canonical_origin=f"https://github.com/{repository}.git",
        principal="mimir-bot",
        event_type="pr_review",
        allowed_operations=frozenset({"repo.push"}),
        pr_number=pr_number,
        head_repo=repository,
        head_remote="origin",
        destination_ref="refs/heads/worklink/7",
        observed_head_sha=observed_head_sha,
        base_ref="main",
        observed_base_sha="b" * 40,
        pull_request_author=author,
    )


def _auth(
    labels: InformationFlowLabels | None = None,
    *,
    repository: str = "owner/repo",
    pr_number: int = 7,
    observed_head_sha: str = "a" * 40,
    scope: RepoPRActionScope | None = None,
    recorded_verdict: bool = True,
) -> AuthContext:
    current = labels or InformationFlowLabels()
    scope = scope or _scope(
        repository, pr_number=pr_number, observed_head_sha=observed_head_sha,
    )
    state = InformationFlowState(labels=current)
    if recorded_verdict:
        state.pr_checkout_author_trust[scope.scope_id] = True
    return AuthContext(
        principal="operator",
        canonical_principal="operator",
        roles=("user",),
        event_ingress=None,
        trigger="user_message",
        channel_id="channel-1",
        interactivity=TurnInteractivity.INTERACTIVE,
        enforcement_enabled=True,
        ifc_labels=current,
        ifc_state=state,
        repo_pr_action_scope=scope,
    )


def _real_attested_lease(tmp_path: Path):
    from mimir.git_bootstrap import DEFAULT_USER_EMAIL, DEFAULT_USER_NAME

    checkout = tmp_path / "checkout"
    subprocess.run(
        ["git", "init", "-q", "-b", "worklink/7", str(checkout)], check=True,
    )
    target = checkout / "work.py"
    target.write_text("attested\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(checkout), "add", "work.py"], check=True)
    subprocess.run([
        "git", "-C", str(checkout),
        "-c", "user.name=collaborator", "-c", "user.email=collaborator@example.test",
        "commit", "-qm", "attested head",
    ], check=True)
    head = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    scope = _scope(observed_head_sha=head, author="collaborator")
    lease = SimpleNamespace(
        path=checkout, scope_id=scope.scope_id, canonical_repo=scope.canonical_repo,
        pr_number=scope.pr_number, head_sha=head, owner=scope.principal,
        is_active=True,
    )
    auth = _auth(scope=scope)
    return auth, scope, lease, target, (DEFAULT_USER_NAME, DEFAULT_USER_EMAIL)


def test_attested_lease_head_accepts_only_server_identity_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _self_login,
) -> None:
    real_head_check = _self_login
    monkeypatch.setattr(
        access_control_module, "_lease_head_is_author_attested", real_head_check,
    )
    auth, scope, lease, target, server_identity = _real_attested_lease(tmp_path)

    assert access_control_module._attested_pr_checkout_lease(auth, scope, lease)

    target.write_text("server remediation\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(lease.path), "add", "work.py"], check=True)
    subprocess.run([
        "git", "-C", str(lease.path),
        "-c", f"user.name={server_identity[0]}",
        "-c", f"user.email={server_identity[1]}",
        "commit", "-qm", "server remediation",
    ], check=True)
    assert access_control_module._attested_pr_checkout_lease(auth, scope, lease)

    target.write_text("foreign commit\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(lease.path), "add", "work.py"], check=True)
    subprocess.run([
        "git", "-C", str(lease.path),
        "-c", "user.name=foreign", "-c", "user.email=foreign@example.test",
        "commit", "-qm", "foreign commit",
    ], check=True)
    assert not access_control_module._attested_pr_checkout_lease(auth, scope, lease)


def test_attested_lease_head_rejects_foreign_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _self_login,
) -> None:
    monkeypatch.setattr(
        access_control_module, "_lease_head_is_author_attested", _self_login,
    )
    auth, scope, lease, _target, _identity = _real_attested_lease(tmp_path)
    subprocess.run(
        ["git", "-C", str(lease.path), "checkout", "-q", "-b", "foreign"], check=True,
    )

    assert not access_control_module._attested_pr_checkout_lease(auth, scope, lease)


@pytest.mark.parametrize("tool_name", ["repo_status", "repo_diff", "repo_test"])
def test_repo_result_inherits_matching_attested_lease(
    tool_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _self_login,
) -> None:
    from mimir.tools.repo import _publish_attested_lease_result

    monkeypatch.setattr(
        access_control_module, "_lease_head_is_author_attested", _self_login,
    )
    auth, scope, lease, _target, _identity = _real_attested_lease(tmp_path)
    state = RepoReviewState(scope)
    state.attach_checkout_lease(lease)
    token = begin_protected_result_capture()
    try:
        _publish_attested_lease_result(SimpleNamespace(context=auth), state)
    finally:
        provenance = end_protected_result_capture(token)

    labels = classify_protected_result(
        tool_name, {"repository": "owner/repo", "pull_request": 7}, auth,
        ToolAuthorization(
            tool_name=tool_name, decision=OperationDecision.RESOURCE_SCOPED,
            allowed=True, repo_pr_action_scope=scope,
        ),
        result="repository output", provenance=provenance,
    )

    assert labels is not None
    source, = labels.sources
    assert (source.integrity, source.integrity_effect) == ("trusted", "active_ingest")
    auth.ifc_state.merge(labels)
    assert not auth.ifc_state.has_untrusted_active_ingest()


def test_repo_result_without_author_verdict_stays_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _self_login,
) -> None:
    from mimir.tools.repo import _publish_attested_lease_result

    monkeypatch.setattr(
        access_control_module, "_lease_head_is_author_attested", _self_login,
    )
    auth, scope, lease, _target, _identity = _real_attested_lease(tmp_path)
    auth.ifc_state.pr_checkout_author_trust[scope.scope_id] = False
    state = RepoReviewState(scope)
    state.attach_checkout_lease(lease)
    token = begin_protected_result_capture()
    try:
        _publish_attested_lease_result(SimpleNamespace(context=auth), state)
    finally:
        provenance = end_protected_result_capture(token)

    assert provenance is None
    labels = classify_protected_result(
        "repo_status", {"repository": "owner/repo", "pull_request": 7}, auth,
        ToolAuthorization(
            tool_name="repo_status", decision=OperationDecision.RESOURCE_SCOPED,
            allowed=True, repo_pr_action_scope=scope,
        ),
        result="repository output", provenance=provenance,
    )
    assert labels is not None
    source, = labels.sources
    assert source.integrity == "untrusted"
    auth.ifc_state.merge(labels)
    assert auth.ifc_state.has_untrusted_active_ingest()


@pytest.mark.parametrize("tool_name", ["pr_job_log", "pr_comment"])
def test_non_author_content_repository_results_remain_untrusted(
    tool_name: str,
) -> None:
    auth = _auth()
    scope = auth.repo_pr_action_scope
    assert scope is not None
    labels = classify_protected_result(
        tool_name, {"repository": "owner/repo", "pull_request": 7}, auth,
        ToolAuthorization(
            tool_name=tool_name, decision=OperationDecision.RESOURCE_SCOPED,
            allowed=True, repo_pr_action_scope=scope,
        ),
        result="external output",
    )

    assert labels is not None
    source, = labels.sources
    assert (source.integrity, source.integrity_effect) == (
        "untrusted", "active_ingest",
    )


@pytest.mark.parametrize(
    ("verdict", "mismatch"),
    [(True, None), (False, None), (None, None),
     (True, "number"), (True, "head_sha"), (True, "author"),
     (True, "missing_author"), (True, "no_attestation")],
)
def test_checkout_records_native_author_trust_for_file_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    verdict: bool | None, mismatch: str | None,
) -> None:
    from dataclasses import replace

    from mimir.forge import PullRequestProjection
    from mimir.tools import forge, repo

    author = "" if mismatch == "missing_author" else "collaborator"
    scope = _scope(author=author)
    auth = _auth(scope=scope, recorded_verdict=mismatch is not None)
    runtime = SimpleNamespace(context=auth)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    checkout, target, _ = _recorded_lease(lease_root, scope=scope)
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))
    lease = active_pr_checkout_lease_for_path(target)
    assert lease is not None
    state = RepoReviewState(action_scope=scope)
    auth.server_discovered_pr_states.remember(state)
    monkeypatch.setattr(forge, "remediation_checkout_preflight", lambda *args: (state, None))
    monkeypatch.setattr(repo, "acquire_pr_checkout_lease", lambda *args, **kwargs: (lease, ()))
    metadata = PullRequestProjection(
        7, "Title", "open", author, False, "main", "change",
        "a" * 40, True, "created", "updated",
    )
    if mismatch in {"number", "head_sha", "author"}:
        metadata = replace(metadata, **{
            mismatch: {"number": 8, "head_sha": "c" * 40, "author": "other"}[mismatch],
        })
        auth.ifc_state.repository_author_trust.resolve(
            "owner/repo", "collaborator", lambda: True,
        )
    calls = []

    def attest(repository, author):
        calls.append((repository, author))
        return verdict

    client = SimpleNamespace(
        get_pull_request=lambda scope: metadata,
        get_diff=lambda scope: "diff --git a/src/work.py b/src/work.py",
        author_is_trusted=attest,
    )
    if mismatch == "no_attestation":
        client.author_is_trusted = None
    monkeypatch.setattr(forge, "_client", lambda scope: client)
    result = repo.repo_checkout.func("owner/repo", 7, runtime=runtime)
    assert result["status"] == "checked_out"
    assert result["path"] == str(checkout)
    assert auth.ifc_state.pr_checkout_author_trust[scope.scope_id] is (
        verdict if mismatch is None else None
    )
    assert calls == ([] if mismatch else [("owner/repo", "collaborator")])

    def no_attestation(*args):
        pytest.fail("filesystem read attempted author attestation")

    monkeypatch.setattr(client, "author_is_trusted", no_attestation)
    labels = classify_protected_result(
        "read_file", {"file_path": str(target)}, auth,
        ToolAuthorization(tool_name="read_file", decision="resource_scoped", allowed=True),
        result=target.read_text(),
    )
    trusted = verdict is True and mismatch is None
    assert labels is not None
    assert len(labels.sources) == 1
    source = labels.sources[0]
    assert (source.domain, source.integrity, source.integrity_effect) == (
        ("repository", "trusted", "informational") if trusted
        else ("filesystem", "untrusted", "active_ingest")
    )
    assert source.resource_id == (
        f"owner/repo#pull/7@{'a' * 40}" if trusted else str(target)
    )
    auth.ifc_state.merge(labels)
    assert auth.ifc_state.has_untrusted_active_ingest() is (not trusted)
    if mismatch is not None:
        return

    # Definitive verdicts are shared; unavailable attestations must be retried.
    if verdict is None:
        monkeypatch.setattr(client, "author_is_trusted", attest)
    token = begin_protected_result_capture()
    try:
        result = forge.pr_diff.func("owner/repo", 7, runtime=runtime)
    finally:
        provenance = end_protected_result_capture(token)
    assert provenance is not None
    labels = classify_protected_result(
        "pr_diff", {"repository": "owner/repo", "pull_request": 7}, auth,
        ToolAuthorization(
            tool_name="pr_diff", decision="resource_scoped", allowed=True,
            repo_pr_action_scope=scope,
        ),
        result=result, provenance=provenance,
    )
    assert labels is not None
    assert len(labels.sources) == 1
    source = labels.sources[0]
    assert (source.domain, source.integrity, source.integrity_effect) == (
        "repository", "trusted" if trusted else "untrusted", "active_ingest",
    )
    assert source.resource_id == f"owner/repo#pull/7@{'a' * 40}"
    auth.ifc_state.merge(labels)
    assert auth.ifc_state.has_untrusted_active_ingest() is (not trusted)
    assert len(calls) == (2 if verdict is None else 1)


@pytest.mark.parametrize("author", ["collaborator", "mimir-bot"])
def test_lease_without_recorded_verdict_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, author: str,
) -> None:
    scope = _scope(author=author)
    auth = _auth(scope=scope, recorded_verdict=False)
    auth.ifc_state.repository_author_trust.resolve("owner/repo", author, lambda: True)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    _, target, _ = _recorded_lease(lease_root, scope=scope)
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))

    source = protected_result_source(
        auth, principal="filesystem", domain="filesystem",
        resource_id=str(target), bridge_instance="filesystem",
    )

    assert (source.domain, source.integrity, source.integrity_effect) == (
        "filesystem", "untrusted", "active_ingest",
    )
    auth.ifc_state.merge(InformationFlowLabels().with_source(source))
    assert auth.ifc_state.has_untrusted_active_ingest()



def test_metadata_failure_after_acquisition_clears_recorded_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport failure on re-acquisition must not leave a stale trusted verdict.

    The verdict is cleared before metadata is fetched precisely so a raising
    fetch fails closed. Forge ConnectionErrors are observed in production, so
    this is a reachable path, not a theoretical one.
    """
    from mimir.forge import PullRequestProjection
    from mimir.tools import forge, repo

    scope = _scope(author="collaborator")
    auth = _auth(scope=scope)
    runtime = SimpleNamespace(context=auth)
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    checkout, target, _ = _recorded_lease(lease_root, scope=scope)
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))
    lease = active_pr_checkout_lease_for_path(target)
    assert lease is not None
    state = RepoReviewState(action_scope=scope)
    auth.server_discovered_pr_states.remember(state)
    monkeypatch.setattr(forge, "remediation_checkout_preflight", lambda *args: (state, None))
    monkeypatch.setattr(repo, "acquire_pr_checkout_lease", lambda *args, **kwargs: (lease, ()))
    metadata = PullRequestProjection(
        7, "Title", "open", "collaborator", False, "main", "change",
        "a" * 40, True, "created", "updated",
    )
    client = SimpleNamespace(
        get_pull_request=lambda scope: metadata,
        get_diff=lambda scope: "diff --git a/src/work.py b/src/work.py",
        author_is_trusted=lambda repository, author: True,
    )
    monkeypatch.setattr(forge, "_client", lambda scope: client)

    # First acquisition records an affirmative verdict.
    assert repo.repo_checkout.func("owner/repo", 7, runtime=runtime)["path"] == str(checkout)
    assert auth.ifc_state.pr_checkout_author_trust[scope.scope_id] is True

    # Re-acquisition where the metadata fetch raises, as a forge transport
    # failure does. The recorded verdict must not survive it.
    def failing_metadata(scope):
        raise ConnectionError("forge transport failed")

    monkeypatch.setattr(client, "get_pull_request", failing_metadata)
    with pytest.raises(Exception):
        repo.repo_checkout.func("owner/repo", 7, runtime=runtime)
    assert auth.ifc_state.pr_checkout_author_trust[scope.scope_id] is None

    # And the lease read that the stale verdict would have trusted is untrusted.
    labels = classify_protected_result(
        "read_file", {"file_path": str(target)}, auth,
        ToolAuthorization(tool_name="read_file", decision="resource_scoped", allowed=True),
        result=target.read_text(),
    )
    assert labels is not None
    source = labels.sources[0]
    assert (source.domain, source.integrity, source.integrity_effect) == (
        "filesystem", "untrusted", "active_ingest",
    )

@pytest.mark.parametrize("tool_name", ["read_file", "grep"])
def test_active_lease_file_results_use_repository_source_labels(
    tool_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    _checkout, target, _record = _recorded_lease(lease_root)
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))
    auth = _auth()

    labels = classify_protected_result(
        tool_name,
        {"path": str(target), "file_path": str(target)},
        auth,
        ToolAuthorization(
            tool_name=tool_name,
            decision=OperationDecision.RESOURCE_SCOPED,
            allowed=True,
        ),
        result="ok",
    )

    assert labels is not None
    assert len(labels.sources) == 1
    source = labels.sources[0]
    assert source.principal == "operator"
    assert source.domain == "repository"
    assert source.resource_id == f"owner/repo#pull/7@{'a' * 40}"
    assert source.bridge_instance == "forge"
    assert source.integrity == "trusted"
    assert source.integrity_effect == "informational"


def test_cross_pr_scope_keeps_lease_read_untrusted_filesystem_ingest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    _checkout, target, _record = _recorded_lease(lease_root)
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "mimir-bot")

    source = protected_result_source(
        _auth(pr_number=8), principal="filesystem", domain="filesystem",
        resource_id=str(target), bridge_instance="filesystem",
    )

    assert source.domain == "filesystem"
    assert (source.integrity, source.integrity_effect) == (
        "untrusted", "active_ingest",
    )


def _assert_scope_mismatch_is_untrusted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    field: str,
    value: object,
) -> None:
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    _checkout, target, _record = _recorded_lease(lease_root)
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))
    scope = _scope()
    object.__setattr__(scope, field, value)

    source = protected_result_source(
        _auth(scope=scope), principal="filesystem", domain="filesystem",
        resource_id=str(target), bridge_instance="filesystem",
    )

    assert source.domain == "filesystem"
    assert (source.integrity, source.integrity_effect) == (
        "untrusted", "active_ingest",
    )


def test_different_scope_id_keeps_lease_read_untrusted_filesystem_ingest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_scope_mismatch_is_untrusted(
        tmp_path, monkeypatch, field="scope_id", value="c" * 64,
    )


def test_different_repository_keeps_lease_read_untrusted_filesystem_ingest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_scope_mismatch_is_untrusted(
        tmp_path, monkeypatch, field="canonical_repo", value="other/repo",
    )


def test_different_pr_number_keeps_lease_read_untrusted_filesystem_ingest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_scope_mismatch_is_untrusted(
        tmp_path, monkeypatch, field="pr_number", value=8,
    )


def test_different_head_keeps_lease_read_untrusted_filesystem_ingest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_scope_mismatch_is_untrusted(
        tmp_path, monkeypatch, field="observed_head_sha", value="c" * 40,
    )


def test_edit_file_acknowledgement_does_not_ingest_a_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    _checkout, target, _record = _recorded_lease(lease_root)
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))

    labels = classify_protected_result(
        "edit_file",
        {"file_path": str(target)},
        _auth(),
        ToolAuthorization(
            tool_name="edit_file",
            decision=OperationDecision.RESOURCE_SCOPED,
            allowed=True,
        ),
        result="Successfully replaced 1 instance(s)",
    )

    assert labels is None


@pytest.mark.parametrize("record_state", ["missing", "expired", "malformed"])
def test_non_active_lease_record_keeps_filesystem_active_ingest(
    record_state: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    _checkout, target, _record = _recorded_lease(
        lease_root,
        expires_at=(
            datetime.now(UTC) - timedelta(seconds=1)
            if record_state == "expired"
            else None
        ),
    )
    metadata = target.parents[1] / ".git" / "mimir-pr-checkout-lease.json"
    if record_state == "missing":
        metadata.unlink()
    elif record_state == "malformed":
        metadata.write_text("{not-json", encoding="utf-8")
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))

    source = protected_result_source(
        _auth(), principal="filesystem", domain="filesystem",
        resource_id=str(target), bridge_instance="filesystem",
    )

    assert source.principal == "filesystem"
    assert source.domain == "filesystem"
    assert source.integrity == "untrusted"
    assert source.integrity_effect == "active_ingest"


def test_lease_path_symlink_escape_is_untrusted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    checkout, _target, _record = _recorded_lease(lease_root)
    outside = tmp_path / "outside.py"
    outside.write_text("outside\n", encoding="utf-8")
    escaped = checkout / "escaped.py"
    escaped.symlink_to(outside)
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))

    assert active_pr_checkout_lease_for_path(escaped) is None
    source = protected_result_source(
        _auth(), principal="filesystem", domain="filesystem",
        resource_id=str(escaped), bridge_instance="filesystem",
    )

    assert source.domain == "filesystem"
    assert (source.integrity, source.integrity_effect) == (
        "untrusted", "active_ingest",
    )


def test_lease_repository_sources_only_flow_to_their_own_forge_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    _checkout, target, _record = _recorded_lease(lease_root)
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))
    source = protected_result_source(
        _auth(), principal="filesystem", domain="filesystem",
        resource_id=str(target), bridge_instance="filesystem",
    )
    labels = InformationFlowLabels().with_source(source)

    own = _auth(labels)
    own_decision = SinkGate.check_sink_flow(
        "repo_push", "owner/repo", labels, own, enforce=True,
        repo_pr_action_scope=own.repo_pr_action_scope,
    )
    assert own_decision.allowed is True, own_decision.reason

    untrusted = SourceLabel(
        principal="filesystem", domain="filesystem",
        resource_id="/outside/input.txt", bridge_instance="filesystem",
        sensitivity="internal", authorized_principals=frozenset({"operator"}),
        source_kind="protected_tool", integrity="untrusted",
        integrity_effect="active_ingest",
    )
    mixed = labels.with_source(untrusted)
    mixed_auth = _auth(mixed)
    mixed_decision = SinkGate.check_sink_flow(
        "repo_push", "owner/repo", mixed, mixed_auth, enforce=True,
        repo_pr_action_scope=mixed_auth.repo_pr_action_scope,
    )
    assert mixed_decision.allowed is False
    assert mixed_decision.reason == "ifc_label_blocked:forge"

    other = _auth(labels, repository="other/repo")
    other_decision = SinkGate.check_sink_flow(
        "repo_push", "other/repo", labels, other, enforce=True,
        repo_pr_action_scope=other.repo_pr_action_scope,
    )
    assert other_decision.allowed is False
    assert other_decision.reason == "ifc_label_blocked:forge"

    for tool_name, target in (
        ("post_message", "channel-2"),
        ("memory_store", "semantic"),
    ):
        decision = SinkGate.check_sink_flow(
            tool_name, target, labels, own, enforce=True,
        )
        assert decision.allowed is False
