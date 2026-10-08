"""Stdio contracts against an actual MCP SDK server and mimir's real client."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Awaitable, Callable

import pytest
from langchain_core.tools import StructuredTool, ToolException

from mimir.mcp_client import MCPConnection, MCPManager, MCPServerConfig

# Bound the entire lifecycle, including finally-block transport teardown.
pytestmark = pytest.mark.timeout(60)

SERVER = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"


async def _contract(
    check: Callable[[MCPConnection, dict[str, StructuredTool]], Awaitable[None]],
    *,
    call_timeout_s: float = 2.0,
    cross_task_shutdown: bool = False,
) -> None:
    manager = MCPManager(call_timeout_s=call_timeout_s)
    pid: int | None = None
    try:
        tools = await manager.start_servers([
            MCPServerConfig(name="contract", command=sys.executable, args=[str(SERVER)]),
        ], fail_fast=True)
        assert not manager.startup_failures
        assert len(manager.connections) == 1
        connection = manager.connections[0]
        assert isinstance(connection, MCPConnection)
        by_name = {tool.name.removeprefix("mcp_contract_"): tool for tool in tools}
        pid = int(await by_name["server_pid"].coroutine())
        await check(connection, by_name)
    finally:
        if cross_task_shutdown:
            await asyncio.create_task(manager.shutdown())
        else:
            await manager.shutdown()
        if pid is not None:
            # Only observe the process owned by this connection, not ambient children.
            for _ in range(50):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.02)
            else:
                pytest.fail(f"MCP child {pid} survived shutdown")


@pytest.mark.asyncio
async def test_stdio_handshake_initialize_and_discovery() -> None:
    async def check(conn: MCPConnection, tools: dict[str, StructuredTool]) -> None:
        initialized = await conn.session.initialize()
        assert initialized.server_info.name == "mimir-contract"
        assert {"greeting", "failure", "structured_only", "slow", "die_mid_call"} <= tools.keys()
        greeting = tools["greeting"]
        assert isinstance(greeting, StructuredTool)
        assert greeting.name == "mcp_contract_greeting"
        assert greeting.args_schema is not None
        assert greeting.args_schema.model_fields["name"].is_required()
        listed = await conn.session.list_tools()
        descriptor = next(tool for tool in listed.tools if tool.name == "greeting")
        assert descriptor.input_schema["properties"]["name"]["type"] == "string"
        assert greeting.args_schema.model_json_schema()["properties"]["name"]["type"] == "string"

    await asyncio.wait_for(_contract(check), timeout=30)


@pytest.mark.asyncio
async def test_stdio_shutdown_from_different_task(caplog: pytest.LogCaptureFixture) -> None:
    async def check(_conn: MCPConnection, tools: dict[str, StructuredTool]) -> None:
        assert await tools["greeting"].coroutine(name="Ada") == "Hello, Ada!"

    await asyncio.wait_for(_contract(check, cross_task_shutdown=True), timeout=30)
    assert not [
        record for record in caplog.records
        if record.name == "mimir.mcp_client" and "shutdown failed" in record.getMessage()
    ]


@pytest.mark.asyncio
async def test_stdio_multiple_servers_shutdown_from_different_task(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = MCPManager()
    pids: list[int] = []
    try:
        tools = await manager.start_servers([
            MCPServerConfig(name=name, command=sys.executable, args=[str(SERVER)])
            for name in ("first", "second")
        ], fail_fast=True)
        assert len(manager.connections) == 2
        for tool in tools:
            if tool.name.endswith("_server_pid"):
                pids.append(int(await tool.coroutine()))
        assert len(set(pids)) == 2
    finally:
        await asyncio.create_task(manager.shutdown())
    await manager.shutdown()  # Repeated shutdown must be harmless.
    assert not manager.connections
    assert manager._owner_task is not None and manager._owner_task.done()
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert not [
        record for record in caplog.records
        if record.name == "mimir.mcp_client" and "shutdown failed" in record.getMessage()
    ]


@pytest.mark.asyncio
async def test_stdio_fail_fast_cleans_up_started_servers() -> None:
    manager = MCPManager()
    with pytest.raises(FileNotFoundError):
        await manager.start_servers([
            MCPServerConfig(name="first", command=sys.executable, args=[str(SERVER)]),
            MCPServerConfig(name="missing", command="/nonexistent/mcp-contract-server", args=[]),
        ], fail_fast=True)
    assert not manager.connections
    assert manager._owner_task is not None and manager._owner_task.done()
    assert len(manager.startup_failures) == 1
    assert manager.startup_failures[0]["server_name"] == "missing"
    await asyncio.create_task(manager.shutdown())


@pytest.mark.asyncio
async def test_stdio_successful_text_call() -> None:
    async def check(_conn: MCPConnection, tools: dict[str, StructuredTool]) -> None:
        assert await tools["greeting"].coroutine(name="Ada") == "Hello, Ada!"

    await asyncio.wait_for(_contract(check), timeout=30)


@pytest.mark.asyncio
async def test_stdio_tool_error_surfaces_server_text() -> None:
    async def check(_conn: MCPConnection, tools: dict[str, StructuredTool]) -> None:
        with pytest.raises(ToolException, match="contract failure from server"):
            await tools["failure"].coroutine()

    await asyncio.wait_for(_contract(check), timeout=30)


@pytest.mark.asyncio
async def test_stdio_structured_content_without_text() -> None:
    async def check(_conn: MCPConnection, tools: dict[str, StructuredTool]) -> None:
        assert json.loads(await tools["structured_only"].coroutine()) == {"answer": 42}

    await asyncio.wait_for(_contract(check), timeout=30)


@pytest.mark.asyncio
async def test_stdio_call_timeout() -> None:
    async def check(_conn: MCPConnection, tools: dict[str, StructuredTool]) -> None:
        with pytest.raises(ToolException, match="timed out after 0.1s"):
            await tools["slow"].coroutine()

    await asyncio.wait_for(_contract(check, call_timeout_s=0.1), timeout=30)


@pytest.mark.asyncio
async def test_stdio_server_dies_mid_call_without_hanging() -> None:
    async def check(_conn: MCPConnection, tools: dict[str, StructuredTool]) -> None:
        with pytest.raises(ToolException, match="MCP tool 'die_mid_call' failed"):
            await tools["die_mid_call"].coroutine()

    await asyncio.wait_for(_contract(check), timeout=30)
