from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest
from langchain_core.tools import ToolException

from mimir.access_control import (
    CLIENT_FILE_RESOURCE_POLICY,
    OperationDecision,
    ToolFlowDirection,
    canonical_client_file_resource,
    get_operation_catalog,
    get_sink_category,
    get_tool_flow_direction,
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


def _context(provider: FakeProvider) -> TurnCapabilityContext:
    return TurnCapabilityContext(
        permission_broker=None,
        provider=provider,
        profile_policy=MIMIR_HANDS_V1,
        connection_generation=1,
        prompt_epoch=1,
        acp_delivery=True,
        lease=SimpleNamespace(closed=False),
        cwd="/untrusted/audit-only",
    )


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
    assert tuple(tools) == ("read", "edit", "shell")
    assert _thaw(tools["read"].input_schema) == {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    }
    assert _thaw(tools["read"].result_schema) == {
        "type": "object",
        "properties": {"content": {"type": "string"}},
        "required": ["content"],
        "additionalProperties": False,
    }
    assert set(_thaw(tools["edit"].input_schema)["properties"]) == {
        "path", "oldText", "newText"
    }
    assert set(_thaw(tools["shell"].result_schema)["properties"]) == {
        "stdout", "stderr", "exitCode"
    }
    assert {policy.classification for policy in MIMIR_HANDS_V1.tools} == {
        "resource_scoped", "admin_required"
    }
    assert all(len(policy.input_schema_digest) == 64 for policy in tools.values())


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
async def test_wrappers_fail_closed_without_exact_profile_or_valid_result() -> None:
    with pytest.raises(ToolException):
        await hands_read.ainvoke({"path": "a"})
    provider = FakeProvider({"read": {"content": 1}})
    token = set_turn_capability_context(_context(provider))
    try:
        with pytest.raises(ToolException):
            await hands_read.ainvoke({"path": "a"})
    finally:
        reset_turn_capability_context(token)


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
