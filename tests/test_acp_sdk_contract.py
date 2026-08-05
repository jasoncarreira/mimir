from __future__ import annotations

import ast
import asyncio
import inspect
import io
import json
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest

from mimir.acp import sdk
from mimir.acp.stdio import _DrainProtocol, _ReservedFrameTransport


EXPECTED_SCHEMA_IMPORTS = {
    "AgentCapabilities",
    "AudioContentBlock",
    "AuthenticateRequest",
    "AuthenticateResponse",
    "AuthMethodAgent",
    "CancelNotification",
    "ClientCapabilities",
    "EmbeddedResourceContentBlock",
    "ImageContentBlock",
    "Implementation",
    "InitializeRequest",
    "InitializeResponse",
    "LoadSessionRequest",
    "LoadSessionResponse",
    "McpCapabilities",
    "NewSessionRequest",
    "NewSessionResponse",
    "PromptCapabilities",
    "PromptRequest",
    "PromptResponse",
    "ResourceContentBlock",
    "TextContentBlock",
}

FORBIDDEN_SCHEMA_IMPORTS = {
    "AcpMcpServer",
    "McpServer",
    "McpServerStdio",
    "HttpMcpServer",
    "SseMcpServer",
}


def test_pinned_distribution_and_protocol_version() -> None:
    assert version("agent-client-protocol") == "0.12.0"
    assert sdk.PROTOCOL_VERSION == 1


def test_sdk_schema_import_boundary_is_exact() -> None:
    source = Path(sdk.__file__).read_text()
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "acp.schema"
        for alias in node.names
    }
    assert imports == EXPECTED_SCHEMA_IMPORTS
    assert FORBIDDEN_SCHEMA_IMPORTS.isdisjoint(vars(sdk))
    assert sdk.McpCapabilities.__name__ == "McpCapabilities"


def test_pinned_schema_aliases_and_handler_models() -> None:
    assert sdk.InitializeRequest.model_fields["protocol_version"].alias == "protocolVersion"
    assert sdk.InitializeRequest.model_fields["field_meta"].alias == "_meta"
    assert sdk.AuthenticateRequest.model_fields["method_id"].alias == "methodId"
    assert sdk.AuthenticateRequest.model_fields["field_meta"].alias == "_meta"
    assert sdk.InitializeResponse.model_fields["agent_capabilities"].alias == "agentCapabilities"
    assert sdk.AgentCapabilities.model_fields["load_session"].alias == "loadSession"
    assert sdk.PromptCapabilities.model_fields["embedded_context"].alias == "embeddedContext"
    assert getattr(sdk.Agent.initialize, "__param_model__") is sdk.InitializeRequest
    assert getattr(sdk.Agent.authenticate, "__param_model__") is sdk.AuthenticateRequest
    assert getattr(sdk.Agent.new_session, "__param_model__") is sdk.NewSessionRequest
    assert getattr(sdk.Agent.load_session, "__param_model__") is sdk.LoadSessionRequest
    assert getattr(sdk.Agent.prompt, "__param_model__") is sdk.PromptRequest
    assert getattr(sdk.Agent.cancel, "__param_model__") is sdk.CancelNotification


def test_pinned_agent_signatures() -> None:
    assert list(inspect.signature(sdk.Agent.initialize).parameters) == [
        "self",
        "protocol_version",
        "client_capabilities",
        "client_info",
        "kwargs",
    ]
    assert list(inspect.signature(sdk.Agent.authenticate).parameters) == [
        "self",
        "method_id",
        "kwargs",
    ]
    assert list(inspect.signature(sdk.Agent.new_session).parameters) == [
        "self",
        "cwd",
        "additional_directories",
        "mcp_servers",
        "kwargs",
    ]
    assert list(inspect.signature(sdk.Agent.load_session).parameters) == [
        "self",
        "cwd",
        "session_id",
        "mcp_servers",
        "additional_directories",
        "kwargs",
    ]
    assert list(inspect.signature(sdk.Agent.prompt).parameters) == [
        "self",
        "session_id",
        "prompt",
        "kwargs",
    ]
    runner_parameters = inspect.signature(sdk.run_agent).parameters
    assert list(runner_parameters) == [
        "agent",
        "input_stream",
        "output_stream",
        "use_unstable_protocol",
        "stdio_buffer_limit_bytes",
        "connection_kwargs",
    ]
    assert runner_parameters["input_stream"].default is None
    assert runner_parameters["output_stream"].default is None
    assert runner_parameters["use_unstable_protocol"].kind is inspect.Parameter.KEYWORD_ONLY
    assert runner_parameters["use_unstable_protocol"].default is False
    assert runner_parameters["stdio_buffer_limit_bytes"].default == 52_428_800
    assert runner_parameters["connection_kwargs"].kind is inspect.Parameter.VAR_KEYWORD


def test_error_objects_are_exact() -> None:
    assert sdk.AUTH_METHOD_ID == "mimir-web-key"
    assert sdk.auth_required_error().to_error_obj() == {
        "code": -32000,
        "message": "Authentication required",
        "data": {"methodId": "mimir-web-key"},
    }
    assert sdk.method_not_found_error("_example").to_error_obj() == {
        "code": -32601,
        "message": "Method not found",
        "data": {"method": "_example"},
    }


@pytest.mark.asyncio
async def test_runner_uses_concrete_streams_in_pinned_direction_and_stable_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = asyncio.StreamReader()
    protocol = _DrainProtocol()
    transport = _ReservedFrameTransport(io.BytesIO(), protocol)
    writer = asyncio.StreamWriter(transport, protocol, None, asyncio.get_running_loop())
    agent = object()
    captured: dict[str, Any] = {}

    async def fake_run_agent(passed_agent: object, **kwargs: Any) -> None:
        captured["agent"] = passed_agent
        captured.update(kwargs)

    monkeypatch.setattr(sdk, "run_agent", fake_run_agent)
    await sdk.run_stdio_agent(agent, request_reader=reader, response_writer=writer)
    assert captured == {
        "agent": agent,
        "input_stream": writer,
        "output_stream": reader,
        "use_unstable_protocol": False,
    }


class _ContractAgent:
    def __init__(self) -> None:
        self.authenticate_call: tuple[str, dict[str, Any]] | None = None
        self.extension_calls: list[tuple[str, dict[str, Any]]] = []
        self.notification_calls: list[tuple[str, dict[str, Any]]] = []
        self.fork_called = False

    async def authenticate(self, method_id: str, **kwargs: Any) -> sdk.AuthenticateResponse:
        self.authenticate_call = (method_id, kwargs)
        return sdk.AuthenticateResponse()

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.extension_calls.append((method, params))
        return {"extension": method}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        self.notification_calls.append((method, params))

    async def fork_session(self, **kwargs: Any) -> dict[str, Any]:
        self.fork_called = True
        return {"sessionId": "forbidden"}


@pytest.mark.asyncio
async def test_actual_router_meta_extensions_normalization_and_stable_mode() -> None:
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "authenticate",
            "params": {
                "methodId": "mimir-web-key",
                "_meta": {"mimir.webKey": "secret", "principal": "asserted"},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "_example",
            "params": {"value": 7},
        },
        {
            "jsonrpc": "2.0",
            "method": "_notice",
            "params": {"value": 8},
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "session/fork",
            "params": {"sessionId": "s", "cwd": "/tmp"},
        },
    ]
    reader = asyncio.StreamReader()
    for request in requests:
        reader.feed_data((json.dumps(request) + "\n").encode())
    reader.feed_eof()
    output = io.BytesIO()
    protocol = _DrainProtocol()
    transport = _ReservedFrameTransport(output, protocol)
    writer = asyncio.StreamWriter(transport, protocol, None, asyncio.get_running_loop())
    agent = _ContractAgent()

    with pytest.warns(UserWarning, match="unstable protocol"):
        await sdk.run_stdio_agent(agent, request_reader=reader, response_writer=writer)

    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert responses == [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 2, "result": {"extension": "example"}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "error": {
                "code": -32601,
                "message": "Method not found",
                "data": {"method": "session/fork"},
            },
        },
    ]
    assert agent.authenticate_call == (
        "mimir-web-key",
        {"mimir.webKey": "secret", "principal": "asserted"},
    )
    assert agent.extension_calls == [("example", {"value": 7})]
    assert agent.notification_calls == [("notice", {"value": 8})]
    assert agent.fork_called is False
