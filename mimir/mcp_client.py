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
Ported from open-strix's ``mcp_client.py`` with PR #181 review
hardening: per-server exit stacks (one server's hang doesn't block
others), timeouts on ``initialize`` + ``call_tool``, regex-based
``${VAR}`` expansion (handles ``"Bearer ${TOKEN}"`` patterns), and
warning-on collision for ``mcp_{server}_{tool}`` namespaced names.

Chainlink #870 adds provenance tracking:
- Versioned provenance record on each wrapper with immutable server_config_id,
  transport/endpoint identity, canonical non-secret config and schema digests,
  original tool name, adapter/version, approval and policy version
- Classification via registered adapters for resource-scoped authorization
- Drift detection that invalidates approval without logging secrets
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from langchain_core.tools import StructuredTool, ToolException
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import Field, create_model

log = logging.getLogger(__name__)

# Timeouts. Servers and tools that hang past these silently shouldn't
# block the agent turn or the startup pipeline forever.
DEFAULT_MCP_INITIALIZE_TIMEOUT_S = 30.0
DEFAULT_MCP_CALL_TIMEOUT_S = 60.0
DEFAULT_MCP_SHUTDOWN_TIMEOUT_S = 10.0

# Matches ``${VAR_NAME}`` anywhere in a string. Pre-fix the parser only
# accepted the exact form ``^${VAR}$`` — common operator patterns like
# ``"Bearer ${TOKEN}"`` or ``"${A}${B}"`` were passed through literally,
# shipping the placeholder to the subprocess instead of the secret.
_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

_SECRET_KEYWORDS = frozenset({
    "token", "secret", "password", "key", "auth", "credential",
    "api_key", "api-key", "access_token", "access-token",
})


def _is_secret_key(key: str) -> bool:
    """Check if a config key name suggests a secret value."""
    return any(kw in key.lower() for kw in _SECRET_KEYWORDS)


def _canonical_config_digest(config: MCPServerConfig) -> str:
    """Generate a deterministic digest of non-secret config fields.

    Excludes env vars that contain secrets - only uses name, command,
    and args to create a stable identity for the server configuration.
    """
    canonical_parts = [config.name, config.command, json.dumps(config.args, sort_keys=True)]
    content = "|".join(canonical_parts)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _schema_digest(input_schema: dict[str, Any]) -> str:
    """Generate a deterministic digest of the tool's input schema.

    Used for drift detection - any change to the schema will produce
    a different digest, indicating the tool's contract has changed.
    """
    normalized = json.dumps(input_schema, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class MCPProvenance:
    """Versioned provenance record for an MCP tool wrapper (chainlink #870).

    Immutable metadata that identifies the tool's origin, configuration,
    and approval state. Used for drift detection and authorization.
    """
    server_config_id: str
    transport_type: str = "stdio"
    endpoint_identity: str = ""
    config_digest: str = ""
    schema_digest: str = ""
    original_tool_name: str = ""
    adapter_name: str = ""
    adapter_version: str = ""
    approval_version: str = ""
    policy_version: str = ""
    is_tombstoned: bool = False

    @classmethod
    def create(
        cls,
        config: MCPServerConfig,
        tool_name: str,
        input_schema: dict[str, Any],
        *,
        server_config_id: str | None = None,
    ) -> "MCPProvenance":
        """Create a new provenance record for an MCP tool."""
        return cls(
            server_config_id=server_config_id or str(uuid.uuid4()),
            endpoint_identity=config.name,
            config_digest=_canonical_config_digest(config),
            schema_digest=_schema_digest(input_schema),
            original_tool_name=tool_name,
        )

    def with_drift_detection(
        self,
        config: MCPServerConfig,
        tool_name: str,
        input_schema: dict[str, Any],
    ) -> "MCPProvenance":
        """Create a new provenance record with drift detection checks.

        Returns a new provenance with is_tombstoned=True if:
        - The tool name has changed
        - The config digest has changed
        - The schema digest has changed
        """
        new_config_digest = _canonical_config_digest(config)
        new_schema_digest = _schema_digest(input_schema)

        name_changed = tool_name != self.original_tool_name
        config_changed = new_config_digest != self.config_digest
        schema_changed = new_schema_digest != self.schema_digest

        if name_changed or config_changed or schema_changed:
            log.warning(
                "MCP tool drift detected for %s: name_changed=%s, config_changed=%s, schema_changed=%s",
                self.original_tool_name, name_changed, config_changed, schema_changed,
            )

        return MCPProvenance(
            server_config_id=self.server_config_id,
            transport_type=self.transport_type,
            endpoint_identity=self.endpoint_identity,
            config_digest=new_config_digest,
            schema_digest=new_schema_digest,
            original_tool_name=tool_name,
            adapter_name=self.adapter_name,
            adapter_version=self.adapter_version,
            approval_version=self.approval_version,
            policy_version=self.policy_version,
            is_tombstoned=name_changed or config_changed or schema_changed,
        )


def _expand_env_refs(value: str) -> str:
    """Replace every ``${VAR}`` occurrence with ``os.environ[VAR]``.

    Missing variables become an empty string AND log a warning so
    operators see the misconfiguration. Pre-fix the missing-var case
    was silent — a server would start with an empty secret.
    """
    def _sub(match: re.Match[str]) -> str:
        var_name = match.group(1)
        if var_name not in os.environ:
            log.warning(
                "MCP env expansion: %r referenced in config but not set "
                "in environment — substituting empty string", var_name,
            )
            return ""
        return os.environ[var_name]
    return _ENV_VAR_RE.sub(_sub, value)


@dataclass
class MCPServerConfig:
    """Per-server config — command, args, and environment for one stdio MCP subprocess."""

    name: str
    command: str
    args: list[str]
    env: dict[str, str] | None = None

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        name_override: str | None = None,
    ) -> MCPServerConfig:
        """Build from a dict. Expands ``${VAR}`` placeholders in env values.

        When ``name_override`` is supplied (the Claude Desktop dict-shape
        path supplies the name as a key, not a value), it's used instead
        of ``data["name"]``.
        """
        if name_override is not None:
            name = name_override.strip()
        else:
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
            env = {str(k): _expand_env_refs(str(v)) for k, v in raw_env.items()}
        return cls(name=name, command=command, args=args, env=env)


class MCPConnection:
    """A live MCP server connection plus the tools it exposes."""

    def __init__(
        self,
        config: MCPServerConfig,
        session: ClientSession,
        exit_stack: AsyncExitStack,
    ) -> None:
        self.config = config
        self.session = session
        self.exit_stack = exit_stack
        self.tool_names: list[str] = []

    async def discover_tools(
        self,
        call_timeout_s: float = DEFAULT_MCP_CALL_TIMEOUT_S,
        server_config_id: str | None = None,
    ) -> list[StructuredTool]:
        """Call ``list_tools`` and bridge each into a LangChain StructuredTool.

        Args:
            call_timeout_s: Timeout for each tool call.
            server_config_id: Optional immutable ID for this server config.
                             If not provided, one will be generated.
        """
        result = await self.session.list_tools()
        tools: list[StructuredTool] = []
        config_id = server_config_id or str(uuid.uuid4())
        for mcp_tool in result.tools:
            provenance = MCPProvenance.create(
                config=self.config,
                tool_name=mcp_tool.name,
                input_schema=mcp_tool.inputSchema or {},
                server_config_id=config_id,
            )
            lc_tool = _bridge_mcp_tool(
                server_name=self.config.name,
                tool_name=mcp_tool.name,
                description=mcp_tool.description or "",
                input_schema=mcp_tool.inputSchema or {},
                session=self.session,
                call_timeout_s=call_timeout_s,
                provenance=provenance,
            )
            tools.append(lc_tool)
            self.tool_names.append(mcp_tool.name)
        return tools


class MCPManager:
    """Lifecycle owner for all MCP server connections.

    Pre-fix every server shared a single ``AsyncExitStack`` — a hang in
    one session's ``__aexit__`` blocked the teardown of every server
    after it in the stack. Now each connection owns its own
    ``AsyncExitStack``; shutdown gathers them concurrently with
    ``return_exceptions=True`` under an overall timeout so one stuck
    process can't keep the others alive.
    """

    def __init__(
        self,
        *,
        initialize_timeout_s: float = DEFAULT_MCP_INITIALIZE_TIMEOUT_S,
        call_timeout_s: float = DEFAULT_MCP_CALL_TIMEOUT_S,
        shutdown_timeout_s: float = DEFAULT_MCP_SHUTDOWN_TIMEOUT_S,
    ) -> None:
        self.connections: list[MCPConnection] = []
        self._init_timeout = initialize_timeout_s
        self._call_timeout = call_timeout_s
        self._shutdown_timeout = shutdown_timeout_s

    async def start_servers(self, configs: list[MCPServerConfig]) -> list[StructuredTool]:
        """Start every configured server, bridge their tools, return flat list.

        Detects ``mcp_{server}_{tool}`` namespaced-name collisions
        across servers and skips the duplicate (warning logged). Two
        servers can legitimately share a tool name as long as the
        server names differ; collision only happens when an operator
        configures the same server name twice or when underscore-laden
        tool names happen to produce the same namespaced string.
        """
        all_tools: list[StructuredTool] = []
        seen_names: set[str] = set()
        for config in configs:
            try:
                conn = await self._connect(config)
                tools = await conn.discover_tools(call_timeout_s=self._call_timeout)
                self.connections.append(conn)
                accepted_tool_names: list[str] = []
                for t in tools:
                    if t.name in seen_names:
                        log.warning(
                            "MCP tool name collision: %r already registered — "
                            "dropping duplicate from server %r", t.name, config.name,
                        )
                        continue
                    seen_names.add(t.name)
                    accepted_tool_names.append(t.name)
                    all_tools.append(t)
                log.info(
                    "MCP server connected: name=%s tools=%s",
                    config.name, accepted_tool_names,
                )
            except asyncio.TimeoutError:
                log.warning(
                    "MCP server '%s' initialize timed out after %.1fs",
                    config.name, self._init_timeout,
                )
            except Exception as exc:
                # Don't block startup — log + skip so the agent still
                # boots with native tools + the MCP servers that worked.
                log.warning(
                    "MCP server '%s' failed to start: %s", config.name, exc,
                )
        return all_tools

    async def _connect(self, config: MCPServerConfig) -> MCPConnection:
        """Start one MCP server subprocess and complete the handshake.

        Each connection gets its own ``AsyncExitStack`` so a hang on
        teardown of one server doesn't block any other server's
        teardown. ``session.initialize`` is bounded by
        ``initialize_timeout_s`` to fail loud on a misbehaving server
        instead of stalling _on_startup forever.
        """
        server_params = StdioServerParameters(
            command=config.command, args=config.args, env=config.env,
        )
        stack = AsyncExitStack()
        try:
            transport = await stack.enter_async_context(stdio_client(server_params))
            read_stream, write_stream = transport
            session = await stack.enter_async_context(
                ClientSession(read_stream, write_stream),
            )
            await asyncio.wait_for(
                session.initialize(), timeout=self._init_timeout,
            )
            return MCPConnection(config=config, session=session, exit_stack=stack)
        except BaseException:
            # If anything in the connect path fails, tear THIS server's
            # stack down before re-raising so we don't leak the
            # subprocess.
            await stack.aclose()
            raise

    async def shutdown(self) -> None:
        """Tear down every subprocess + session.

        ``asyncio.gather`` with ``return_exceptions=True`` ensures one
        stuck server can't keep the others alive. Each gather is bounded
        by ``shutdown_timeout_s`` so process exit isn't held up.
        """
        if not self.connections:
            return
        tasks = [
            asyncio.create_task(conn.exit_stack.aclose())
            for conn in self.connections
        ]
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=self._shutdown_timeout,
            )
        except asyncio.TimeoutError:
            log.warning(
                "MCP shutdown exceeded %.1fs — some servers may not have "
                "torn down cleanly", self._shutdown_timeout,
            )
            for t in tasks:
                if not t.done():
                    t.cancel()
        self.connections.clear()


_JSON_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
    "null": type(None),
}


def _python_type_for_schema(prop: dict[str, Any]) -> type:
    """Best-effort JSON Schema → Python type for pydantic.create_model.

    Handles primitives + the common ``["string","null"]`` union pattern.
    Anything more exotic (anyOf/oneOf with multiple non-null branches,
    enums, nested object schemas) falls back to ``Any`` so the tool
    stays callable; the JSON Schema can still drive prose validation
    via the description block.
    """
    schema_type = prop.get("type")
    if isinstance(schema_type, list):
        non_null = [t for t in schema_type if t != "null"]
        if len(non_null) == 1:
            return _JSON_TYPE_MAP.get(non_null[0], Any)  # type: ignore[arg-type]
        return Any  # type: ignore[return-value]
    if isinstance(schema_type, str):
        return _JSON_TYPE_MAP.get(schema_type, Any)  # type: ignore[return-value]
    return Any  # type: ignore[return-value]


def _build_args_schema(
    namespaced_name: str, input_schema: dict[str, Any]
) -> Any:
    """Build a pydantic BaseModel class from the remote JSON schema.

    Pre-fix the schema was dumped into the description string and the
    model got no typed validation. With ``args_schema=`` on the
    StructuredTool, LangChain/LangGraph enforces required fields +
    primitive coercion at call time, and surfaces the exact required
    field list to the model in the tool-spec.

    Returns ``None`` when the schema has no usable ``properties`` (the
    StructuredTool then accepts arbitrary kwargs like before).
    """
    properties = input_schema.get("properties") or {}
    if not isinstance(properties, dict) or not properties:
        return None
    required_fields = set(input_schema.get("required") or [])
    fields: dict[str, Any] = {}
    for prop_name, prop_info in properties.items():
        if not isinstance(prop_info, dict):
            continue
        py_type = _python_type_for_schema(prop_info)
        prop_desc = prop_info.get("description", "")
        if prop_name in required_fields:
            fields[prop_name] = (py_type, Field(..., description=prop_desc))
        else:
            default = prop_info.get("default", None)
            fields[prop_name] = (
                py_type | None if py_type is not Any else Any,  # type: ignore[operator]
                Field(default=default, description=prop_desc),
            )
    try:
        # The model name affects pydantic's repr + validation error
        # paths; namespacing it on the tool name keeps multiple bridged
        # MCP tools from sharing a class.
        return create_model(f"{namespaced_name}_args", **fields)
    except Exception:  # noqa: BLE001 — pydantic may reject exotic schemas
        log.debug(
            "args_schema build failed for %s; falling back to schema-in-description",
            namespaced_name,
        )
        return None


def _bridge_mcp_tool(
    *,
    server_name: str,
    tool_name: str,
    description: str,
    input_schema: dict[str, Any],
    session: ClientSession,
    call_timeout_s: float = DEFAULT_MCP_CALL_TIMEOUT_S,
    provenance: MCPProvenance | None = None,
) -> StructuredTool:
    """Wrap one MCP tool as a LangChain StructuredTool.

    Tool name is namespaced ``mcp_{server}_{tool}`` so collisions with
    mimir built-ins or other MCP servers are impossible. The remote
    JSON Schema is converted to a pydantic ``args_schema`` where
    possible — LangChain enforces required-field presence + primitive
    coercion at call time. Falls back to schema-in-description when
    the JSON schema uses constructs pydantic can't accept (nested
    object schemas, complex anyOf unions, $ref).

    Provenance is attached as a custom attribute for drift detection
    and authorization (chainlink #870).
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
    args_schema = _build_args_schema(namespaced_name, input_schema)

    async def _call_mcp_tool(**kwargs: Any) -> str:
        try:
            result = await asyncio.wait_for(
                session.call_tool(tool_name, kwargs if kwargs else None),
                timeout=call_timeout_s,
            )
        except asyncio.TimeoutError as exc:
            raise ToolException(
                f"MCP tool '{tool_name}' timed out after {call_timeout_s}s"
            ) from exc
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

    kwargs: dict[str, Any] = {
        "coroutine": _call_mcp_tool,
        "name": namespaced_name,
        "description": full_description,
        "handle_tool_error": True,
    }
    if args_schema is not None:
        kwargs["args_schema"] = args_schema
    tool = StructuredTool.from_function(**kwargs)
    if provenance is not None:
        _tool_provenance_registry[namespaced_name] = provenance
    return tool


def parse_mcp_server_configs(raw: Any) -> list[MCPServerConfig]:
    """Parse a list (or Claude Desktop dict) of server configs.

    Three accepted shapes:

    * Bare list: ``[{"name": "a", "command": "x"}, ...]``
    * Wrapped list: ``{"mcpServers": [...]}``  /  ``{"mcp_servers": [...]}``
    * **Claude Desktop dict**: ``{"mcpServers": {"a": {"command": "x"}}}``

    Pre-fix only the first two were accepted; Claude's actual format
    (third shape) silently produced zero servers and operators copying
    a working ``claude_desktop_config.json`` got no MCP wiring with no
    warning. Now both list and dict shapes work and the dict's keys
    are injected as the ``name`` field. Bad entries are skipped with
    a warning rather than raised.
    """
    # Unwrap the {"mcpServers": ...} envelope before deciding shape.
    if isinstance(raw, dict):
        raw = raw.get("mcpServers") or raw.get("mcp_servers") or raw

    configs: list[MCPServerConfig] = []

    if isinstance(raw, dict):
        # Claude Desktop dict shape: name is the key, body is the value.
        for name, body in raw.items():
            if not isinstance(body, dict):
                log.warning(
                    "Skipping MCP server %r: body is not a dict (got %s)",
                    name, type(body).__name__,
                )
                continue
            try:
                configs.append(MCPServerConfig.from_dict(body, name_override=name))
            except ValueError as exc:
                log.warning("Skipping invalid MCP server %r: %s", name, exc)
        return configs

    if not isinstance(raw, list):
        return []

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


_tool_provenance_registry: dict[str, MCPProvenance] = {}


def clear_provenance_registry() -> None:
    """Clear the provenance registry. Used by tests."""
    global _tool_provenance_registry
    _tool_provenance_registry.clear()


def get_tool_provenance(tool: Any) -> MCPProvenance | None:
    """Extract provenance from an MCP tool wrapper, if present."""
    tool_name = getattr(tool, "name", None)
    if tool_name is not None and tool_name in _tool_provenance_registry:
        return _tool_provenance_registry[tool_name]
    return getattr(tool, "mcp_provenance", None)


def check_tool_drift(
    tool: Any,
    config: MCPServerConfig,
    tool_name: str,
    input_schema: dict[str, Any],
) -> MCPProvenance | None:
    """Check for drift between current tool state and original provenance.

    Returns a new provenance record with is_tombstoned=True if drift is detected.
    Returns None if the tool has no provenance (not an MCP tool).
    """
    provenance = get_tool_provenance(tool)
    if provenance is None:
        return None

    return provenance.with_drift_detection(
        config=config,
        tool_name=tool_name,
        input_schema=input_schema,
    )


MCPAdapterClassifier = Callable[[str, Any | None], Any]


@dataclass(frozen=True)
class MCPAdapterRegistration:
    """Registered classifier for tools carrying matching provenance."""

    version: str
    policy_version: str
    classify: MCPAdapterClassifier


def register_mcp_adapter(
    adapter_name: str,
    adapter_version: str,
    policy_version: str,
    classify: MCPAdapterClassifier,
) -> None:
    """Register a classifier for MCP tools carrying ``adapter_name`` provenance."""
    if not callable(classify):
        raise TypeError("MCP adapter classifier must be callable")
    _global_mcp_adapter_registry[adapter_name] = MCPAdapterRegistration(
        version=adapter_version,
        policy_version=policy_version,
        classify=classify,
    )


def get_mcp_adapter_info(adapter_name: str) -> MCPAdapterRegistration | None:
    """Get a registered MCP classifier by provenance adapter name."""
    return _global_mcp_adapter_registry.get(adapter_name)


def clear_mcp_adapter_registry() -> None:
    """Clear registered MCP classifiers. Used by tests."""
    _global_mcp_adapter_registry.clear()


_global_mcp_adapter_registry: dict[str, MCPAdapterRegistration] = {}


def check_stale_policy_on_startup(
    tools: list[Any],
    expected_policy_version: str,
) -> list[dict[str, Any]]:
    """Check for tools with stale policy versions on startup.

    Returns a list of tools that have a different policy version than expected.
    This allows administrators to identify tools that need re-approval.
    """
    stale_tools: list[dict[str, Any]] = []
    for tool in tools:
        provenance = get_tool_provenance(tool)
        if provenance is None:
            continue
        if provenance.policy_version and provenance.policy_version != expected_policy_version:
            stale_tools.append({
                "tool_name": tool.name,
                "expected_policy": expected_policy_version,
                "actual_policy": provenance.policy_version,
                "original_tool_name": provenance.original_tool_name,
                "server_config_id": provenance.server_config_id,
            })
    return stale_tools
