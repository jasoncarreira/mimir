"""Real MCP SDK 2.x stdio server for the client contract tests."""

from __future__ import annotations

import asyncio
import os

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

server = MCPServer("mimir-contract")


@server.tool()
def greeting(name: str) -> str:
    """Greet a person."""
    return f"Hello, {name}!"


@server.tool()
def failure() -> str:
    """Report a deliberate tool failure."""
    raise ToolError("contract failure from server")


@server.tool(structured_output=False)
def structured_only() -> CallToolResult:
    """Return only structured content."""
    return CallToolResult(content=[], structured_content={"answer": 42})


@server.tool()
async def slow() -> str:
    """Wait longer than the client's call timeout."""
    await asyncio.sleep(5)
    return "late"


@server.tool()
async def die_mid_call() -> str:
    """Exit after receiving a request, before sending a result."""
    await asyncio.sleep(0.05)
    os._exit(17)


@server.tool()
def server_pid() -> str:
    """Return the subprocess ID for the shutdown assertion."""
    return str(os.getpid())


if __name__ == "__main__":
    server.run("stdio")
