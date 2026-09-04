from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mimir.access_control import (
    OperationDecision,
    SinkGate,
    ToolAuthorization,
    classify_protected_result,
    protected_result_source,
)
from mimir.models import (
    AuthContext,
    InformationFlowLabels,
    InformationFlowState,
    RepoPRActionScope,
    SourceLabel,
    TurnInteractivity,
)
from mimir.pr_checkout_lease import active_pr_checkout_lease_for_path


def _recorded_lease(
    root: Path,
    *,
    repository: str = "owner/repo",
    expires_at: datetime | None = None,
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
        "scope_id": "scope-id",
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


def _scope(repository: str = "owner/repo") -> RepoPRActionScope:
    return RepoPRActionScope(
        provenance="server_discovered",
        canonical_repo=repository,
        canonical_root="/srv/source",
        canonical_origin=f"https://github.com/{repository}.git",
        principal="mimir-bot",
        event_type="pr_review",
        allowed_operations=frozenset({"repo.push"}),
        pr_number=7,
        head_repo=repository,
        head_remote="origin",
        destination_ref="refs/heads/worklink/7",
        observed_head_sha="a" * 40,
        base_ref="main",
        observed_base_sha="b" * 40,
    )


def _auth(
    labels: InformationFlowLabels | None = None,
    *,
    repository: str = "owner/repo",
) -> AuthContext:
    current = labels or InformationFlowLabels()
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
        ifc_state=InformationFlowState(labels=current),
        repo_pr_action_scope=_scope(repository),
    )


@pytest.mark.parametrize("tool_name", ["read_file", "grep", "edit_file"])
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
