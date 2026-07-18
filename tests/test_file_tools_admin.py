"""Admin authorization for file read tools (chainlink #868).

Tests that file, directory, glob, grep, attachment, index, wiki/state,
and file_search reads require admin identity until a principal-owned
virtual-root design exists. Verifies same-admin success, regular-user
denial, and missing-context denial without leaking resolved paths.
"""

from __future__ import annotations

import time
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest
from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.runtime import Runtime

from mimir._context import get_current_turn, reset_current_turn, set_current_turn
from mimir.models import AuthContext, InformationFlowLabels, TurnContext
from mimir.identities import IdentityResolver
from mimir.tools.budget_gate import BudgetGateMiddleware


def _make_ctx(budget: int = 5) -> TurnContext:
    return TurnContext(
        turn_id="t-file-admin",
        session_id="ch-1",
        trigger="user_message",
        channel_id="ch-1",
        started_at=time.monotonic(),
        tool_call_budget=budget,
    )


def _make_request(
    tool_name: str = "fake_tool",
    tool_call_id: str = "tc-1",
    auth_context: AuthContext | None = None,
) -> ToolCallRequest:
    if auth_context is None:
        turn = get_current_turn()
        auth_context = getattr(turn, "auth_context", None) if turn is not None else None
    return ToolCallRequest(
        tool_call={"name": tool_name, "args": {}, "id": tool_call_id, "type": "tool_call"},
        tool=None,
        state=None,
        runtime=Runtime(context=auth_context),
    )


def _attach_auth(ctx: TurnContext, resolver: IdentityResolver | None = None) -> None:
    roles = ()
    canonical = ctx.author
    if resolver is not None and ctx.author is not None:
        roles = resolver.access_metadata(ctx.author).roles
        canonical = resolver.resolve(ctx.author)
    ctx.auth_context = AuthContext(
        principal=ctx.author,
        canonical_principal=canonical,
        roles=roles,
        event_ingress=ctx.event_ingress,
        trigger=ctx.trigger,
        channel_id=ctx.channel_id,
        interactivity=None,
        enforcement_enabled=ctx.access_control_enforced,
        ifc_labels=InformationFlowLabels(),
    )


def _resolver(tmp_path: Path, body: str) -> IdentityResolver:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "identities.yaml").write_text(dedent(body), encoding="utf-8")
    resolver = IdentityResolver(home=tmp_path)
    resolver.reload()
    return resolver


FILE_READ_TOOLS = [
    "read_file",
    "ls",
    "glob",
    "grep",
    "download_files",
    "file_search",
    "rebuild_index",
    "get_turn",
    "mimir_get_turn",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", FILE_READ_TOOLS)
async def test_file_read_admin_denies_non_admin(tmp_path: Path, tool_name: str) -> None:
    """Non-admin user is denied when enforcement is on."""
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
        """,
    )
    ctx = _make_ctx(budget=0)
    ctx.author = "slack-U1"
    ctx.identity_resolver = resolver
    ctx.access_control_enforced = True
    _attach_auth(ctx, resolver)
    mw = BudgetGateMiddleware()
    handler_calls = 0

    async def handler(req: ToolCallRequest) -> ToolMessage:
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(content="read content", tool_call_id=req.tool_call["id"])

    token = set_current_turn(ctx)
    try:
        out = await mw.awrap_tool_call(_make_request(tool_name, "id-read"), handler)
    finally:
        reset_current_turn(token)

    assert isinstance(out, ToolMessage)
    assert out.status == "error"
    assert "requires an admin identity" in str(out.content)
    assert handler_calls == 0
    error_content = str(out.content)
    assert "/mimir-home" not in error_content
    assert str(tmp_path) not in error_content


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", FILE_READ_TOOLS)
async def test_file_read_admin_allows_admin_user(tmp_path: Path, tool_name: str) -> None:
    """Admin user can use file read tools even with enforcement on."""
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: bob
            aliases: [slack-U2]
            access: {roles: [user, admin]}
        """,
    )
    ctx = _make_ctx(budget=0)
    ctx.author = "slack-U2"
    ctx.identity_resolver = resolver
    ctx.access_control_enforced = True
    _attach_auth(ctx, resolver)
    mw = BudgetGateMiddleware()
    handler_calls = 0

    async def handler(req: ToolCallRequest) -> ToolMessage:
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(content="read content", tool_call_id=req.tool_call["id"])

    token = set_current_turn(ctx)
    try:
        out = await mw.awrap_tool_call(_make_request(tool_name, "id-read"), handler)
    finally:
        reset_current_turn(token)

    assert isinstance(out, ToolMessage)
    assert out.status != "error" or "admin" not in str(out.content).lower()
    assert handler_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", FILE_READ_TOOLS)
async def test_file_read_admin_denies_missing_context(monkeypatch: pytest.MonkeyPatch, tool_name: str) -> None:
    """Missing auth context is denied when enforcement is on."""
    monkeypatch.setenv("MIMIR_ACCESS_CONTROL_ENFORCED", "true")
    mw = BudgetGateMiddleware()
    handler_calls = 0

    async def handler(req: ToolCallRequest) -> ToolMessage:
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(content="read content", tool_call_id=req.tool_call["id"])

    out = await mw.awrap_tool_call(_make_request(tool_name, "id-read"), handler)

    assert isinstance(out, ToolMessage)
    assert out.status == "error"
    assert "requires an admin identity" in str(out.content)
    assert handler_calls == 0
    error_content = str(out.content)
    assert "/mimir-home" not in error_content
    assert "state/" not in error_content
    assert "memory/" not in error_content


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", FILE_READ_TOOLS)
async def test_file_read_shadow_mode_allows_when_not_enforced(monkeypatch: pytest.MonkeyPatch, tool_name: str) -> None:
    """File reads still work in shadow mode when enforcement is off."""
    monkeypatch.setenv("MIMIR_ACCESS_CONTROL_ENFORCED", "false")
    mw = BudgetGateMiddleware()
    handler_calls = 0

    async def handler(req: ToolCallRequest) -> ToolMessage:
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(content="read content", tool_call_id=req.tool_call["id"])

    out = await mw.awrap_tool_call(_make_request(tool_name, "id-read"), handler)

    assert isinstance(out, ToolMessage)
    assert out.content == "read content"
    assert handler_calls == 1


@pytest.mark.asyncio
async def test_builtin_read_tool_denies_non_admin(tmp_path: Path) -> None:
    """Deepagents built-in 'Read' tool is denied for non-admin."""
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
        """,
    )
    ctx = _make_ctx(budget=0)
    ctx.author = "slack-U1"
    ctx.identity_resolver = resolver
    ctx.access_control_enforced = True
    _attach_auth(ctx, resolver)
    mw = BudgetGateMiddleware()
    handler_calls = 0

    async def handler(req: ToolCallRequest) -> ToolMessage:
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(content="read content", tool_call_id=req.tool_call["id"])

    token = set_current_turn(ctx)
    try:
        out = await mw.awrap_tool_call(_make_request("Read", "id-read"), handler)
    finally:
        reset_current_turn(token)

    assert isinstance(out, ToolMessage)
    assert out.status == "error"
    assert "requires an admin identity" in str(out.content)
    assert handler_calls == 0


@pytest.mark.asyncio
async def test_builtin_glob_tool_denies_non_admin(tmp_path: Path) -> None:
    """Deepagents built-in 'Glob' tool is denied for non-admin."""
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
        """,
    )
    ctx = _make_ctx(budget=0)
    ctx.author = "slack-U1"
    ctx.identity_resolver = resolver
    ctx.access_control_enforced = True
    _attach_auth(ctx, resolver)
    mw = BudgetGateMiddleware()
    handler_calls = 0

    async def handler(req: ToolCallRequest) -> ToolMessage:
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(content="glob result", tool_call_id=req.tool_call["id"])

    token = set_current_turn(ctx)
    try:
        out = await mw.awrap_tool_call(_make_request("Glob", "id-glob"), handler)
    finally:
        reset_current_turn(token)

    assert isinstance(out, ToolMessage)
    assert out.status == "error"
    assert "requires an admin identity" in str(out.content)
    assert handler_calls == 0


@pytest.mark.asyncio
async def test_builtin_grep_tool_denies_non_admin(tmp_path: Path) -> None:
    """Deepagents built-in 'Grep' tool is denied for non-admin."""
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
        """,
    )
    ctx = _make_ctx(budget=0)
    ctx.author = "slack-U1"
    ctx.identity_resolver = resolver
    ctx.access_control_enforced = True
    _attach_auth(ctx, resolver)
    mw = BudgetGateMiddleware()
    handler_calls = 0

    async def handler(req: ToolCallRequest) -> ToolMessage:
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(content="grep result", tool_call_id=req.tool_call["id"])

    token = set_current_turn(ctx)
    try:
        out = await mw.awrap_tool_call(_make_request("Grep", "id-grep"), handler)
    finally:
        reset_current_turn(token)

    assert isinstance(out, ToolMessage)
    assert out.status == "error"
    assert "requires an admin identity" in str(out.content)
    assert handler_calls == 0


@pytest.mark.asyncio
async def test_mcp_file_tool_variant_denies_non_admin(tmp_path: Path) -> None:
    """MCP name variants of admin tools are denied for non-admin."""
    resolver = _resolver(
        tmp_path,
        """
        people:
          - canonical: alice
            aliases: [slack-U1]
            access: {roles: [user]}
        """,
    )
    ctx = _make_ctx(budget=0)
    ctx.author = "slack-U1"
    ctx.identity_resolver = resolver
    ctx.access_control_enforced = True
    _attach_auth(ctx, resolver)
    mw = BudgetGateMiddleware()
    handler_calls = 0

    async def handler(req: ToolCallRequest) -> ToolMessage:
        nonlocal handler_calls
        handler_calls += 1
        return ToolMessage(content="result", tool_call_id=req.tool_call["id"])

    token = set_current_turn(ctx)
    try:
        out = await mw.awrap_tool_call(_make_request("mcp__mimir__read_file", "id-mcp"), handler)
    finally:
        reset_current_turn(token)

    assert isinstance(out, ToolMessage)
    assert out.status == "error"
    assert "requires an admin identity" in str(out.content)
    assert handler_calls == 0
