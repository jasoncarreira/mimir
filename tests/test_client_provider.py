from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest
from langchain_core.tools import ToolException

from mimir.access_control import (
    CLIENT_FILE_RESOURCE_POLICY,
    ClientFileResourcePolicy,
    OperationDecision,
    ToolFlowDirection,
    canonical_client_file_resource,
    get_operation_catalog,
    get_sink_category,
    get_tool_flow_direction,
    get_tool_registry,
    SinkCategory,
)
from mimir.tools.client_provider import (
    HANDS_TOOLS,
    MIMIR_HANDS_V1,
    PROVIDER_PROFILES,
    TurnCapabilityContext,
    get_provider_profile,
    hands_edit,
    hands_read,
    hands_shell,
    reset_turn_capability_context,
    set_turn_capability_context,
)
from mimir.models import AuthContext
from mimir.tools.registry import all_mimir_tools


class FakeProvider:
    def __init__(self, results: Mapping[str, Mapping[str, Any]]) -> None:
        self.results = results
        self.calls: list[tuple[str, Mapping[str, Any]]] = []

    async def call_tool(
        self, name: str, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        self.calls.append((name, arguments))
        return self.results[name]


def _context(
    provider: FakeProvider,
    *,
    profile_policy: Any = MIMIR_HANDS_V1,
) -> TurnCapabilityContext:
    return TurnCapabilityContext(
        permission_broker=None,
        provider=provider,
        profile_policy=profile_policy,
        connection_generation=1,
        prompt_epoch=1,
        acp_delivery=True,
        lease=SimpleNamespace(closed=False),
        cwd="/untrusted/audit-only",
    )




def _admin_auth() -> AuthContext:
    return AuthContext(
        principal="acp:admin",
        canonical_principal="admin",
        roles=("admin",),
        event_ingress=None,
        trigger="user_message",
        channel_id=None,
        interactivity=None,
        enforcement_enabled=True,
    )


async def _invoke_authorized_read(
    provider: FakeProvider,
    path: object,
    *,
    auth_context: AuthContext | None = None,
    profile_policy: Any = MIMIR_HANDS_V1,
) -> dict[str, str]:
    token = set_turn_capability_context(
        _context(provider, profile_policy=profile_policy)
    )
    try:
        authorization = get_tool_registry().authorize_tool(
            "hands_read",
            auth_context,
            enforce=True,
            arguments={"path": path},
        )
        if not authorization.allowed:
            raise ToolException(authorization.reason or "hands_read denied")
        return await hands_read.ainvoke({"path": path})
    finally:
        reset_turn_capability_context(token)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw(child) for child in value]
    return value


def test_registry_contains_only_immutable_mimir_hands_profile() -> None:
    assert tuple(PROVIDER_PROFILES) == ("mimir-hands",)
    assert get_provider_profile("mimir-hands") is MIMIR_HANDS_V1
    assert get_provider_profile("unknown") is None
    assert MIMIR_HANDS_V1.profile_id == "mimir.hands.v1"
    assert MIMIR_HANDS_V1.resource_policy is CLIENT_FILE_RESOURCE_POLICY
    with pytest.raises(TypeError):
        PROVIDER_PROFILES["other"] = MIMIR_HANDS_V1
    with pytest.raises(FrozenInstanceError):
        MIMIR_HANDS_V1.adapter = "client-selected"


def test_profile_has_exact_provider_schemas_and_server_metadata() -> None:
    tools = {policy.provider_name: policy for policy in MIMIR_HANDS_V1.tools}
    expected = {
        "read": (
            {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {"content": {"type": "string"}},
                "required": ["content"],
                "additionalProperties": False,
            },
            "39b714704935190561ed407980480b9a4a0b346b97346e0bff71fb9ace820194",
            "c9fd9a503f520ca7c958067d2db0bc2b1eef6fe5bbae6c7b9356cb921593c49a",
        ),
        "edit": (
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "oldText": {"type": "string"},
                    "newText": {"type": "string"},
                },
                "required": ["path", "oldText", "newText"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {"changed": {"type": "boolean"}},
                "required": ["changed"],
                "additionalProperties": False,
            },
            "44f2c54b1a8fc6eaebbff775cba08b64d9d6bb81f752e76e2c3181fe941773bb",
            "edf6d1c05ea9a0eb0767bed2015520eb6f3acc9fd0c7615d0ccfc332001820ce",
        ),
        "shell": (
            {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                    "exitCode": {"type": "integer"},
                },
                "required": ["stdout", "stderr", "exitCode"],
                "additionalProperties": False,
            },
            "efcd767e81a864d776ee5d1d5757f469a0a9719f26d1cc0b3bae611597585186",
            "0cc998ce157fd5a7389d5ea6a2e6a86d20e70d0e1b4db06d8b91e8f17ec51907",
        ),
    }
    assert tuple(tools) == tuple(expected)
    for name, (input_schema, result_schema, input_digest, result_digest) in expected.items():
        assert _thaw(tools[name].input_schema) == input_schema
        assert _thaw(tools[name].result_schema) == result_schema
        assert tools[name].input_schema_digest == input_digest
        assert tools[name].result_schema_digest == result_digest
    assert {policy.classification for policy in MIMIR_HANDS_V1.tools} == {
        "resource_scoped", "admin_required"
    }


def test_client_file_identity_is_opaque_canonical_utf8() -> None:
    assert canonical_client_file_resource("relative/../x") == "client-file:relative%2F..%2Fx"
    assert canonical_client_file_resource("/tmp/a b") == "client-file:%2Ftmp%2Fa%20b"
    assert canonical_client_file_resource("é\\file") == "client-file:%C3%A9%5Cfile"
    assert canonical_client_file_resource("") is None
    assert canonical_client_file_resource("bad\x00path") is None
    assert canonical_client_file_resource("\ud800") is None
    assert CLIENT_FILE_RESOURCE_POLICY.allows("client-file:any%2Fpath")
    assert not CLIENT_FILE_RESOURCE_POLICY.allows("client-file:any/path")
    assert not CLIENT_FILE_RESOURCE_POLICY.allows("client-file:%gg")
    assert not CLIENT_FILE_RESOURCE_POLICY.allows("file:any")


@pytest.mark.asyncio
async def test_wrappers_route_through_current_turn_provider() -> None:
    first = FakeProvider({
        "read": {"content": "one"},
        "edit": {"changed": True},
        "shell": {"stdout": "ok", "stderr": "", "exitCode": 0},
    })
    second = FakeProvider({"read": {"content": "two"}})
    token = set_turn_capability_context(_context(first))
    try:
        assert await hands_read.ainvoke({"path": "a"}) == {"content": "one"}
        assert await hands_edit.ainvoke(
            {"path": "a", "old_text": "x", "new_text": "y"}
        ) == {"changed": True}
        assert await hands_shell.ainvoke({"command": "pwd"}) == {
            "stdout": "ok", "stderr": "", "exitCode": 0
        }
        nested = set_turn_capability_context(_context(second))
        try:
            assert await hands_read.ainvoke({"path": "a"}) == {"content": "two"}
        finally:
            reset_turn_capability_context(nested)
        assert await hands_read.ainvoke({"path": "a"}) == {"content": "one"}
    finally:
        reset_turn_capability_context(token)
    assert first.calls[1] == (
        "edit", {"path": "a", "oldText": "x", "newText": "y"}
    )


@pytest.mark.asyncio
async def test_hands_read_central_authorization_allows_admitted_admin() -> None:
    provider = FakeProvider({"read": {"content": "trusted route"}})

    assert await _invoke_authorized_read(
        provider, "client/../notes.txt", auth_context=_admin_auth()
    ) == {"content": "trusted route"}
    assert provider.calls == [("read", {"path": "client/../notes.txt"})]


@pytest.mark.asyncio
async def test_hands_read_central_authorization_denies_before_provider_dispatch() -> None:
    cases = [
        (None, MIMIR_HANDS_V1, "a"),
        (replace(_admin_auth(), roles=("user",)), MIMIR_HANDS_V1, "a"),
        (_admin_auth(), None, "a"),
        (_admin_auth(), replace(MIMIR_HANDS_V1, profile_id="other"), "a"),
        (_admin_auth(), replace(MIMIR_HANDS_V1, resource_policy=None), "a"),
        (
            _admin_auth(),
            replace(
                MIMIR_HANDS_V1,
                resource_policy=ClientFileResourcePolicy(
                    namespace="client-file", grant="client-file:*"
                ),
            ),
            "a",
        ),
        (
            _admin_auth(),
            replace(
                MIMIR_HANDS_V1,
                resource_policy=ClientFileResourcePolicy(
                    namespace="other", grant="other:*"
                ),
            ),
            "a",
        ),
        (
            _admin_auth(),
            replace(
                MIMIR_HANDS_V1,
                resource_policy=ClientFileResourcePolicy(
                    namespace="client-file", grant="client-file:specific"
                ),
            ),
            "a",
        ),
        (_admin_auth(), MIMIR_HANDS_V1, ""),
        (_admin_auth(), MIMIR_HANDS_V1, "bad\x00path"),
        (_admin_auth(), MIMIR_HANDS_V1, 1),
    ]
    for auth_context, profile_policy, path in cases:
        provider = FakeProvider({"read": {"content": "must not run"}})
        with pytest.raises(ToolException):
            await _invoke_authorized_read(
                provider,
                path,
                auth_context=auth_context,
                profile_policy=profile_policy,
            )
        assert provider.calls == []


@pytest.mark.asyncio
async def test_hands_read_rejects_malformed_provider_result() -> None:
    provider = FakeProvider({"read": {"content": 1}})

    with pytest.raises(ToolException, match="malformed result"):
        await _invoke_authorized_read(provider, "a", auth_context=_admin_auth())
    assert provider.calls == [("read", {"path": "a"})]


def test_hands_surface_is_static_without_client_mcp_registration() -> None:
    names = [tool.name for tool in all_mimir_tools(require_coding_available=False)]
    assert names.count("hands_read") == 1
    assert names.count("hands_edit") == 1
    assert names.count("hands_shell") == 1
    assert tuple(tool.name for tool in HANDS_TOOLS) == (
        "hands_read", "hands_edit", "hands_shell"
    )
    source = Path(__file__).parents[1] / "mimir" / "tools" / "client_provider.py"
    tree = ast.parse(source.read_text())
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert not any(module.endswith("tools.mcp") or module == ".mcp" for module in imported)


def test_access_control_catalogs_hands_profile_policy() -> None:
    catalog = get_operation_catalog()
    assert catalog.get_decision("hands_read") is OperationDecision.RESOURCE_SCOPED
    assert catalog.get_decision("hands_edit") is OperationDecision.ADMIN_REQUIRED
    assert catalog.get_decision("hands_shell") is OperationDecision.ADMIN_REQUIRED
    assert get_tool_flow_direction("hands_read") is ToolFlowDirection.SOURCE
    assert get_tool_flow_direction("hands_edit") is ToolFlowDirection.BOTH
    assert get_tool_flow_direction("hands_shell") is ToolFlowDirection.BOTH
    assert get_sink_category("hands_edit") is SinkCategory.EXTERNAL_MCP
    assert get_sink_category("hands_shell") is SinkCategory.SHELL_PROCESS
