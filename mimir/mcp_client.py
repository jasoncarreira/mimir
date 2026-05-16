"""MCP client — run MCP servers as subprocesses and bridge their tools into LangChain.

Each ``MCPServerConfig`` describes one stdio subprocess (command + args +
env). ``MCPManager.start_servers`` spawns them concurrently, calls
``ClientSession.list_tools``, and wraps each remote tool as a
``langchain_core.tools.StructuredTool``. The resulting flat list is
appended to the agent's tool surface alongside mimir's native tools.

Tool names are namespaced as ``mcp_{server}_{tool}`` so collisions
with mimir built-ins are impossible. Errors thrown by an MCP tool
propagate as ``ToolException`` — LangGraph surfaces those to the
model as tool error messages, so the agent can recover rather than
crash the turn.

Lifecycle is managed by ``server.py``: ``_on_startup`` calls
``start_servers``, ``_on_cleanup`` calls ``shutdown``. Failure to
connect to any single server is non-fatal — the server is logged
and skipped, other servers and mimir's native tools keep working.
Ported from open-strix's ``mcp_client.py``.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool, ToolException
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

log = logging.getLogger(__name__)


@dataclass
class MCPServerConfig:
    """Per-server config — command, args, and environment for one stdio MCP subprocess."""

    name: str
    command: str
    args: list[str]
    env: dict[str, str] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MCPServerConfig:
        """Build from a dict. Expands ``${VAR}`` placeholders in env values."""
        name = str(data.get("name", "")).strip()
        if not name:
            raise ValueError("MCP server config requires a 'name' field")
        command = str(data.get("command", "")).strip()
        if not command:
            raise ValueError(f"MCP server '{name}' requires a 'command' field")
        raw_args = data.get("args", [])
        args = [str(a) for a in raw_args] if isinstance(raw_args, list) else []
        raw_env = data.get("env")
        env: dict[str, str] | None = None
        if isinstance(raw_env, dict):
            env = {}
            for k, v in raw_env.items():
                val = str(v)
                # Expand ${VAR} references from the process environment.
                # Keeps secrets out of MCP config files: operators can write
                # env: { GITHUB_TOKEN: "${GITHUB_TOKEN}" } and load from .env.
                if val.startswith("${") and val.endswith("}"):
                    var_name = val[2:-1]
                    val = os.environ.get(var_name, "")
                env[str(k)] = val
        return cls(name=name, command=command, args=args, env=env)


class MCPConnection:
    """A live MCP server connection plus the tools it exposes."""

    def __init__(self, config: MCPServerConfig, session: ClientSession) -> None:
        self.config = config
        self.session = session
        self.tool_names: list[str] = []

    async def discover_tools(self) -> list[StructuredTool]:
        """Call ``list_tools`` and bridge each into a LangChain StructuredTool."""
        result = await self.session.list_tools()
        tools: list[StructuredTool] = []
        for mcp_tool in result.tools:
            lc_tool = _bridge_mcp_tool(
                server_name=self.config.name,
                tool_name=mcp_tool.name,
                description=mcp_tool.description or "",
                input_schema=mcp_tool.inputSchema,
                session=self.session,
            )
            tools.append(lc_tool)
            self.tool_names.append(mcp_tool.name)
        return tools


class MCPManager:
    """Lifecycle owner for all MCP server connections.

    Holds an ``AsyncExitStack`` so ``shutdown()`` cleanly terminates
    every subprocess + closes every session regardless of which one
    raised — same pattern open-strix uses. Survives a single-server
    failure by logging + continuing; only a manager-wide ``shutdown``
    tears everything down.
    """

    def __init__(self) -> None:
        self._exit_stack = AsyncExitStack()
        self.connections: list[MCPConnection] = []

    async def start_servers(self, configs: list[MCPServerConfig]) -> list[StructuredTool]:
        """Start every configured server, bridge their tools, return flat list."""
        all_tools: list[StructuredTool] = []
        for config in configs:
            try:
                conn = await self._connect(config)
                tools = await conn.discover_tools()
                self.connections.append(conn)
                all_tools.extend(tools)
                log.info(
                    "MCP server connected: name=%s tools=%s",
                    config.name,
                    [t.name for t in tools],
                )
            except Exception as exc:
                # Don't block startup — log + skip so the agent still
                # boots with native tools + the MCP servers that worked.
                log.warning(
                    "MCP server '%s' failed to start: %s", config.name, exc,
                )
        return all_tools

    async def _connect(self, config: MCPServerConfig) -> MCPConnection:
        server_params = StdioServerParameters(
            command=config.command,
            args=config.args,
            env=config.env,
        )
        transport = await self._exit_stack.enter_async_context(stdio_client(server_params))
        read_stream, write_stream = transport
        session = await self._exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream),
        )
        await session.initialize()
        return MCPConnection(config=config, session=session)

    async def shutdown(self) -> None:
        """Tear down every subprocess + session via the AsyncExitStack."""
        await self._exit_stack.aclose()
        self.connections.clear()


def _bridge_mcp_tool(
    *,
    server_name: str,
    tool_name: str,
    description: str,
    input_schema: dict[str, Any],
    session: ClientSession,
) -> StructuredTool:
    """Wrap one MCP tool as a LangChain StructuredTool.

    Tool name is namespaced ``mcp_{server}_{tool}`` so collisions with
    mimir built-ins or other MCP servers are impossible. The remote
    JSON Schema is summarized into the tool description; full schema
    enforcement lives on the MCP server side.
    """
    properties = input_schema.get("properties", {})
    required_fields = set(input_schema.get("required", []))
    schema_desc_parts: list[str] = []
    for prop_name, prop_info in properties.items():
        prop_type = prop_info.get("type", "string")
        prop_desc = prop_info.get("description", "")
        req = " (required)" if prop_name in required_fields else ""
        schema_desc_parts.append(f"  {prop_name} ({prop_type}{req}): {prop_desc}")
    full_description = (
        f"{description}\n\nParameters:\n" + "\n".join(schema_desc_parts)
        if schema_desc_parts
        else description
    )
    namespaced_name = f"mcp_{server_name}_{tool_name}"

    async def _call_mcp_tool(**kwargs: Any) -> str:
        try:
            result = await session.call_tool(tool_name, kwargs if kwargs else None)
        except Exception as exc:
            raise ToolException(f"MCP tool '{tool_name}' failed: {exc}") from exc
        if result.isError:
            text_parts = [c.text for c in result.content if hasattr(c, "text")]
            error_text = "\n".join(text_parts) if text_parts else "Unknown error"
            raise ToolException(f"MCP tool '{tool_name}' returned error: {error_text}")
        parts: list[str] = []
        for content in result.content:
            if hasattr(content, "text"):
                parts.append(content.text)
            elif hasattr(content, "data"):
                parts.append(f"[{getattr(content, 'mimeType', 'binary')} data]")
            else:
                parts.append(json.dumps(content.model_dump(), default=str))
        return "\n".join(parts) if parts else "(empty result)"

    return StructuredTool.from_function(
        coroutine=_call_mcp_tool,
        name=namespaced_name,
        description=full_description,
        handle_tool_error=True,
    )


def parse_mcp_server_configs(raw: Any) -> list[MCPServerConfig]:
    """Parse a list of server configs from a JSON-decoded payload.

    Accepts either a top-level list ``[{name, command, args, env}, ...]``
    or a dict wrapper ``{"mcpServers": [...]}`` (matches Claude Code's
    convention). Bad entries are skipped with a warning rather than
    raised — one malformed server shouldn't block the others.
    """
    if isinstance(raw, dict):
        raw = raw.get("mcpServers") or raw.get("mcp_servers") or []
    if not isinstance(raw, list):
        return []
    configs: list[MCPServerConfig] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            configs.append(MCPServerConfig.from_dict(item))
        except ValueError as exc:
            log.warning("Skipping invalid MCP server config: %s", exc)
    return configs


def load_mcp_server_configs(*, json_inline: str | None, json_path: str | None) -> list[MCPServerConfig]:
    """Build the configured server list from the two env-driven sources.

    Inline ``MIMIR_MCP_SERVERS_JSON`` wins if both are set. If neither
    is set or the JSON is malformed, returns an empty list — MCP is
    opt-in and a missing config means "no MCP servers."
    """
    raw: Any = None
    if json_inline:
        try:
            raw = json.loads(json_inline)
        except json.JSONDecodeError as exc:
            log.warning("MIMIR_MCP_SERVERS_JSON failed to parse: %s", exc)
            return []
    elif json_path:
        path = Path(json_path).expanduser()
        if not path.is_file():
            log.warning("MIMIR_MCP_SERVERS_PATH does not exist: %s", path)
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            log.warning("MCP config %s failed to parse: %s", path, exc)
            return []
    if raw is None:
        return []
    return parse_mcp_server_configs(raw)
