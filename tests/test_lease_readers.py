"""Typed readers execute the current lease grant without global file roots."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from mimir._context import reset_current_turn, set_current_turn
from mimir.access_control import (
    CapabilityTier,
    ToolRegistry,
    build_trigger_service_principal,
    service_filesystem_read_roots,
)
from mimir.models import AuthContext, RepoPRActionScope, RepoPRScopeRegistry, RepoReviewState
from mimir.pr_checkout_lease import PRCheckoutLease
from mimir.read_policy import configured_non_admin_read_roots, resolve_non_admin_read_target
from mimir.readonly_backend import FileToolRouter, WriteGuardBackend, build_file_tool_routes


@pytest.fixture(params=["external", "home"])
def leased_reader(tmp_path, monkeypatch, request):
    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    lease_root = (home if request.param == "home" else tmp_path) / "pr-leases"
    checkout = lease_root / "active"
    checkout.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "mimir-bot")
    monkeypatch.delenv("MIMIR_FILE_TOOL_ROOTS", raising=False)
    scope = RepoPRActionScope(
        provenance="poller_payload", canonical_repo="owner/repo",
        canonical_root=str(tmp_path / "source"),
        canonical_origin="https://github.com/owner/repo.git",
        principal="mimir-bot", event_type="pr_changes_requested_stale",
        allowed_operations=frozenset({"repo.push"}), pr_number=42,
        head_repo="owner/repo", head_remote="origin",
        destination_ref="refs/heads/worklink/42", observed_head_sha="a" * 40,
        base_ref="main", observed_base_sha="b" * 40, pull_request_author="mimir-bot",
    )
    state = RepoReviewState(scope)
    now = datetime.now(UTC)
    state.attach_checkout_lease(PRCheckoutLease(
        canonical_repo=scope.canonical_repo, canonical_origin=scope.canonical_origin,
        source_root=Path(scope.canonical_root), scope_base_sha=scope.observed_base_sha,
        base_sha=scope.observed_base_sha, head_sha=scope.observed_head_sha,
        destination_ref=scope.destination_ref, owner=scope.principal,
        scope_id=scope.scope_id, path=checkout, lease_root=lease_root,
        created_at=now, expires_at=now + timedelta(hours=1), recovery_id="reader-test",
        pr_number=42,
    ))
    service = build_trigger_service_principal(
        canonical="poller:github-activity", trigger="poller", profile="github",
        tier=CapabilityTier.CODE_EXECUTION,
        capabilities=json.loads((Path(__file__).resolve().parents[1] /
            "mimir/optional-skills/github-poller/pollers.json").read_text())["pollers"][0]["authority"]["capabilities"],
        creation_path="test",
    )
    auth = AuthContext(
        principal=f"service:{service.canonical}", canonical_principal=service.canonical,
        roles=("service",), event_ingress=None, trigger=service.trigger,
        channel_id="poller:test", interactivity=None, is_service=True,
        service_authority=service, enforcement_enabled=True,
        repo_review_state=state, repo_pr_action_scope=scope,
    )
    # Match the agent's lease routing, not an operator file-root grant.
    backend = FileToolRouter(
        default=WriteGuardBackend(home, ["state"], guard_outside_root=True),
        routes=build_file_tool_routes([(str(lease_root), "rw")]),
    )
    return home, checkout, state, auth, backend


@pytest.mark.parametrize("batched", [False, True])
@pytest.mark.asyncio
async def test_typed_readers_execute_active_lease(leased_reader, batched):
    home, checkout, state, auth, backend = leased_reader
    if batched:
        auth = replace(auth, repo_review_state=None, repo_pr_action_scope=None,
                       repo_pr_scope_registry=RepoPRScopeRegistry((state,)))
    target = checkout / "review.txt"
    target.write_text("lease needle\n", encoding="utf-8")
    token = set_current_turn(SimpleNamespace(turn_id="lease-reader", auth_context=auth))
    try:
        assert checkout not in configured_non_admin_read_roots()
        assert resolve_non_admin_read_target(str(target), scan_file=True) == target
        registry = ToolRegistry()
        for tool, args in (
            ("read_file", {"file_path": str(target)}),
            ("ls", {"path": str(checkout)}),
            ("glob", {"path": str(checkout), "pattern": "*.txt"}),
            ("grep", {"path": str(checkout), "pattern": "needle"}),
        ):
            assert tool in auth.service_authority.capabilities
            decision = registry.authorize_tool(tool, auth, enforce=True, arguments=args)
            assert decision.allowed, decision
        for result in (backend.read(str(target)), await backend.aread(str(target))):
            assert result.error is None
            assert result.file_data["content"] == "lease needle\n"
        for result in (backend.ls(str(checkout)), await backend.als(str(checkout))):
            assert result.error is None
            assert str(target) in {entry["path"] for entry in result.entries}
        for result in (
            backend.glob("*.txt", str(checkout)), await backend.aglob("*.txt", str(checkout)),
            backend.grep("needle", str(checkout)), await backend.agrep("needle", str(checkout)),
        ):
            assert result.error is None
            assert {match["path"] for match in result.matches} == {str(target)}
        assert not (checkout / "new.txt").exists()
        assert target.read_text() == "lease needle\n"
    finally:
        reset_current_turn(token)


@pytest.mark.parametrize("location", ["sibling", "outside"])
def test_lease_readers_refuse_out_of_lease(leased_reader, location):
    home, checkout, _, auth, _ = leased_reader
    target = (checkout.parent if location == "sibling" else home.parent) / "outside.txt"
    target.write_text("ordinary content\n")
    token = set_current_turn(SimpleNamespace(turn_id="outside-reader", auth_context=auth))
    try:
        assert not ToolRegistry().authorize_tool(
            "read_file", auth, enforce=True, arguments={"file_path": str(target)},
        ).allowed
        assert resolve_non_admin_read_target(str(target), scan_file=True) is None
    finally:
        reset_current_turn(token)


@pytest.mark.parametrize("invalid", ["sibling", "revoked"])
def test_lease_backend_rechecks_live_scope(leased_reader, invalid):
    _, checkout, state, auth, backend = leased_reader
    target = checkout / "ordinary.txt"
    target.write_text("ordinary content\n")
    token = set_current_turn(SimpleNamespace(turn_id="live-reader", auth_context=auth))
    try:
        assert backend.read(str(target)).error is None
        if invalid == "revoked":
            state.checkout_lease.revoke()
        else:
            target = checkout.parent / "sibling" / "ordinary.txt"
            target.parent.mkdir()
            target.write_text("outside content\n")
        assert backend.read(str(target)).error
    finally:
        reset_current_turn(token)


def test_lease_roots_require_matching_service_identity(leased_reader):
    _, checkout, _, auth, _ = leased_reader
    service = auth.service_authority
    assert checkout in service_filesystem_read_roots(service, auth_context=auth)
    other = replace(service, canonical="poller:other-github")
    assert checkout not in service_filesystem_read_roots(other, auth_context=auth)


@pytest.mark.parametrize("invalid", ["scope_id", "owner", "expired", "revoked"])
def test_lease_roots_reject_invalid_grants(leased_reader, invalid):
    _, checkout, state, auth, backend = leased_reader
    target = checkout / "ordinary.txt"
    target.write_text("ordinary content\n")
    assert checkout in service_filesystem_read_roots(auth.service_authority, auth_context=auth)
    field, value = {
        "scope_id": ("scope_id", "other-scope"),
        "owner": ("owner", "other-owner"),
        "expired": ("expires_at", datetime.now(UTC) - timedelta(seconds=1)),
        "revoked": ("revoked", True),
    }[invalid]
    # Model stale/corrupt server state without the attachment-time validation.
    object.__setattr__(state, "checkout_lease", replace(state.checkout_lease, **{field: value}))
    token = set_current_turn(SimpleNamespace(turn_id="invalid-reader", auth_context=auth))
    try:
        assert checkout not in service_filesystem_read_roots(auth.service_authority, auth_context=auth)
        assert resolve_non_admin_read_target(str(target), scan_file=True) is None
        assert backend.read(str(target)).error
    finally:
        reset_current_turn(token)


@pytest.mark.parametrize("destination", ["lease", "state", "same_lease"])
@pytest.mark.parametrize("reader", ["authorize", "read", "aread", "ls", "als", "glob", "aglob", "grep", "agrep"])
@pytest.mark.asyncio
async def test_lease_resolver_confines_symlink_to_selected_root(leased_reader, destination, reader):
    home, checkout, state, auth, backend = leased_reader
    sibling = checkout.parent / "second"
    sibling.mkdir()
    second = RepoReviewState(replace(state.action_scope, pr_number=43))
    second.attach_checkout_lease(replace(
        state.checkout_lease, path=sibling, scope_id=second.action_scope.scope_id, pr_number=43,
    ))
    auth = replace(auth, repo_review_state=None, repo_pr_action_scope=None,
                   repo_pr_scope_registry=RepoPRScopeRegistry((state, second)))
    directory = {"lease": sibling, "state": home / "state", "same_lease": checkout / "local"}[destination]
    directory.mkdir(exist_ok=True)
    target = directory / "ordinary.txt"
    target.write_text("ordinary content\n")
    link = checkout / "escape"
    link.symlink_to(directory, target_is_directory=True)
    allowed = destination == "same_lease"
    token = set_current_turn(SimpleNamespace(turn_id="symlink-reader", auth_context=auth))
    try:
        assert resolve_non_admin_read_target(str(target), scan_file=True) == target
        assert resolve_non_admin_read_target(str(link / target.name), scan_file=True) == (target if allowed else None)
        if reader == "authorize":
            for tool in ("read_file", "ls", "glob", "grep"):
                for path, expected in ((directory, True), (link, allowed)):
                    args = ({"file_path": str(path / target.name)} if tool == "read_file"
                            else {"path": str(path), "pattern": "ordinary"})
                    decision = ToolRegistry().authorize_tool(tool, auth, enforce=True, arguments=args)
                    assert decision.allowed == expected, decision
        else:
            for path, expected in ((directory, True), (link, allowed)):
                method = getattr(backend, reader)
                if reader in {"read", "aread"}:
                    result = method(str(path / target.name))
                elif reader in {"ls", "als"}:
                    result = method(str(path))
                else:
                    result = method("*.txt" if "glob" in reader else "ordinary", str(path))
                if reader.startswith("a"):
                    result = await result
                if reader in {"read", "aread"}:
                    assert bool(result.file_data) == expected
                    if expected:
                        assert result.file_data["content"] == "ordinary content\n"
                    else:
                        assert result.error
                else:
                    entries = result.entries if reader in {"ls", "als"} else result.matches
                    assert bool(entries) == expected
                if expected:
                    assert result.error is None
    finally:
        reset_current_turn(token)


def test_lease_readers_keep_path_and_content_guards(leased_reader):
    home, checkout, state, auth, backend = leased_reader
    sibling = checkout.parent / "sibling"
    sibling.mkdir()
    (sibling / "other.txt").write_text("outside needle\n")
    (checkout / "escape").symlink_to(sibling, target_is_directory=True)
    (checkout / ".env").write_text("needle\n")
    (checkout / "untracked.txt").write_text("ghp_" + "a" * 30 + "\n")
    token = set_current_turn(SimpleNamespace(turn_id="lease-guards", auth_context=auth))
    try:
        for path in (checkout / "escape" / "other.txt", checkout / ".env",
                     checkout / "untracked.txt"):
            assert resolve_non_admin_read_target(str(path), scan_file=True) is None
        for path in (checkout / ".env", checkout / "untracked.txt",
                     checkout / "escape" / "other.txt", sibling / "other.txt"):
            assert backend.read(str(path)).error
        for tool, args in (
            ("read_file", {"file_path": str(checkout / "escape" / "other.txt")}),
            ("ls", {"path": str(checkout / "escape")}),
            ("glob", {"path": str(checkout / "escape"), "pattern": "*"}),
            ("grep", {"path": str(checkout / "escape"), "pattern": "needle"}),
        ):
            assert not ToolRegistry().authorize_tool(tool, auth, enforce=True,
                                                     arguments=args).allowed
        assert not ToolRegistry().authorize_tool(
            "read_file", auth, enforce=True,
            arguments={"file_path": str(sibling / "other.txt")},
        ).allowed
        for result in (backend.glob("**/*", str(checkout)), backend.grep("needle", str(checkout))):
            assert not result.matches
        state.checkout_lease.revoke()
        assert not ToolRegistry().authorize_tool(
            "ls", auth, enforce=True, arguments={"path": str(checkout)},
        ).allowed
        assert backend.read(str(checkout / ".env")).error
        (checkout / "ordinary.txt").write_text("revoked needle\n")
        assert backend.read(str(checkout / "ordinary.txt")).error
        for result in (backend.glob("*.txt", str(checkout)), backend.grep("needle", str(checkout))):
            assert not result.matches
    finally:
        reset_current_turn(token)


@pytest.mark.parametrize("command", [
    "sed -n '1,20p' review.txt", "git blame review.txt",
    "gh issue comment 42 --body hello", "npm test", "python -c 'print(1)'",
])
@pytest.mark.parametrize("restricted", [False, True])
def test_shell_refusal_names_only_held_tools(leased_reader, command, restricted):
    import json
    import re

    from mimir.access_control import (
        ServiceShellBindingRule,
        parse_service_shell_argv_with_diagnostics,
    )

    manifest = Path(__file__).resolve().parents[1] / "mimir/optional-skills/github-poller/pollers.json"
    poller = next(
        item for item in json.loads(manifest.read_text())["pollers"]
        if item["name"] == "github-activity"
    )
    capabilities = frozenset(poller["authority"]["capabilities"])
    assert {"read_file", "grep", "glob", "ls", "unsupported_operation"} <= capabilities
    assert "issue_comment" not in capabilities
    _, _, state, auth, _ = leased_reader
    if restricted:
        capabilities = frozenset({"pr_metadata", "unsupported_operation"})
    service = replace(auth.service_authority, capabilities=capabilities)
    argv, reason, rule = parse_service_shell_argv_with_diagnostics(
        command, "repo_review", service=service, review_state=state,
    )
    assert argv is None
    assert rule == ServiceShellBindingRule.PROFILE_ALLOWLIST
    named = set(re.findall(
        r"\b(?:pr_\w+|repo_\w+|issue_comment|read_file|glob|grep|ls|unsupported_operation)\b",
        reason,
    )) - {"repo_review"}
    assert named <= capabilities
    assert "pr_metadata" in named
    assert "unsupported_operation" in named
    assert "pr_*" not in reason and "repo_*" not in reason
    assert "do not retry through shell or HTTP commands" in reason
    if not restricted:
        assert {"read_file", "grep", "glob", "ls"} <= named


def test_shell_refusal_omits_unheld_unsupported_operation(leased_reader):
    from mimir.access_control import parse_service_shell_argv_with_diagnostics

    _, _, state, auth, _ = leased_reader
    service = replace(auth.service_authority, capabilities=frozenset())
    argv, reason, _ = parse_service_shell_argv_with_diagnostics(
        "git blame review.txt", "repo_review", service=service, review_state=state,
    )
    assert argv is None
    assert "unsupported_operation" not in reason
    assert "report the limitation; do not retry through shell or HTTP commands" in reason
