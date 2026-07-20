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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from langchain_core.tools import StructuredTool, ToolException
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import Field, create_model

from ._atomic import atomic_write_json

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
_SERVER_CONFIG_NAMESPACE = uuid.UUID("4797522a-a5f1-47ef-9716-bfd7408b0b85")
_TOOL_ID_NAMESPACE = uuid.UUID("4d87c6fe-7512-43f7-97b7-e1376f0b092a")
MCP_POLICY_STORE_VERSION = 1

_SECRET_KEYWORDS = frozenset({
    "token", "secret", "password", "key", "auth", "credential",
    "api_key", "api-key", "access_token", "access-token",
})


def _is_secret_key(key: str) -> bool:
    """Check if a config key name suggests a secret value."""
    return any(kw in key.lower() for kw in _SECRET_KEYWORDS)


def _canonical_env_value(key: str, value: str) -> dict[str, Any]:
    """Return identity-bearing env metadata without resolved secret values."""
    references = _ENV_VAR_RE.findall(value)
    if _is_secret_key(key):
        return {"secret_references": references}
    parts: list[dict[str, str]] = []
    offset = 0
    for match in _ENV_VAR_RE.finditer(value):
        if match.start() > offset:
            parts.append({"literal": value[offset:match.start()]})
        parts.append({"secret_reference": match.group(1)})
        offset = match.end()
    if offset < len(value):
        parts.append({"literal": value[offset:]})
    return {"parts": parts or [{"literal": value}]}


def _canonical_config_digest(config: MCPServerConfig) -> str:
    """Generate a deterministic digest of non-secret config fields.

    Includes environment keys, non-secret literals, and secret-reference
    names. Resolved secret values are never part of the canonical payload.
    """
    env_identity = config.env_identity
    if env_identity is None:
        env_identity = {
            key: _canonical_env_value(key, value)
            for key, value in sorted((config.env or {}).items())
        }
    canonical = {
        "args": config.args,
        "command": config.command,
        "env": env_identity,
        "name": config.name,
        # Bind approvals to the immutable server identity, not only its
        # operator-facing name and otherwise-identical launch configuration.
        "server_config_id": config.server_config_id,
        "transport_type": "stdio",
    }
    content = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _derived_server_config_id(name: str) -> str:
    """Derive a restart-stable ID from the operator-owned logical name."""
    return str(uuid.uuid5(_SERVER_CONFIG_NAMESPACE, name))


def _tool_identity(server_config_id: str, tool_name: str) -> str:
    canonical = json.dumps([server_config_id, tool_name], separators=(",", ":"))
    return str(uuid.uuid5(_TOOL_ID_NAMESPACE, canonical))


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
    tool_id: str = ""
    transport_type: str = "stdio"
    endpoint_identity: str = ""
    config_digest: str = ""
    schema_digest: str = ""
    original_tool_name: str = ""
    classification: str = ""
    adapter_name: str = ""
    adapter_version: str = ""
    approval_version: str = ""
    policy_version: str = ""
    result_integrity: str = "untrusted"
    argument_egress: str = "taint_gated"
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
            server_config_id=server_config_id or config.server_config_id,
            tool_id=_tool_identity(server_config_id or config.server_config_id, tool_name),
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
            tool_id=self.tool_id,
            transport_type=self.transport_type,
            endpoint_identity=self.endpoint_identity,
            config_digest=new_config_digest,
            schema_digest=new_schema_digest,
            original_tool_name=tool_name,
            classification=self.classification,
            adapter_name=self.adapter_name,
            adapter_version=self.adapter_version,
            approval_version=self.approval_version,
            policy_version=self.policy_version,
            result_integrity="untrusted",
            argument_egress="taint_gated",
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


@dataclass(frozen=True)
class MCPToolPolicy:
    """Operator approval and IFC grants bound to discovery provenance."""

    tool_name: str
    classification: str
    adapter_name: str
    adapter_version: str
    approval_version: str
    policy_version: str
    config_digest: str
    schema_digest: str
    result_integrity: str = "untrusted"
    argument_egress: str = "taint_gated"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MCPToolPolicy":
        def value(*names: str) -> str:
            for name in names:
                if name in data:
                    return str(data[name]).strip()
            return ""

        policy = cls(
            tool_name=value("tool_name", "toolName", "original_tool_name"),
            classification=value("classification").lower(),
            adapter_name=value("adapter_name", "adapterName", "adapter_id", "adapterId"),
            adapter_version=value("adapter_version", "adapterVersion"),
            approval_version=value("approval_version", "approvalVersion"),
            policy_version=value("policy_version", "policyVersion"),
            config_digest=value("config_digest", "configDigest"),
            schema_digest=value("schema_digest", "schemaDigest"),
            result_integrity=(
                value("result_integrity", "resultIntegrity") or "untrusted"
            ).lower(),
            argument_egress=(
                value("argument_egress", "argumentEgress") or "taint_gated"
            ).lower(),
        )
        required = (
            policy.tool_name,
            policy.classification,
            policy.adapter_name,
            policy.adapter_version,
            policy.approval_version,
            policy.policy_version,
            policy.config_digest,
            policy.schema_digest,
        )
        if not all(required):
            raise ValueError("MCP tool policy requires all provenance and version fields")
        if policy.classification not in {"open", "resource_scoped", "admin_required"}:
            raise ValueError(f"invalid MCP classification: {policy.classification}")
        if policy.result_integrity not in {"trusted", "untrusted"}:
            raise ValueError(
                f"invalid MCP result_integrity: {policy.result_integrity}"
            )
        if policy.argument_egress not in {"allowed", "taint_gated"}:
            raise ValueError(
                f"invalid MCP argument_egress: {policy.argument_egress}"
            )
        return policy


@dataclass(frozen=True)
class MCPAdapterConfig:
    """Durable configuration for the built-in argument/resource adapter."""

    name: str
    version: str
    policy_version: str
    resource_argument: str = ""
    owner_argument: str = ""
    source: bool = False
    sink: bool = False
    direction: str = ""

    @property
    def flow_direction(self) -> str:
        if self.direction:
            return self.direction
        if self.source and self.sink:
            return "both"
        if self.source:
            return "source"
        if self.sink:
            return "sink"
        return "unknown"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MCPAdapterConfig":
        def value(*names: str) -> str:
            for name in names:
                if name in data:
                    return str(data[name]).strip()
            return ""

        direction = value("direction", "flow", "flow_direction", "flowDirection").lower()
        if not direction and ("source" in data or "sink" in data):
            source = bool(data.get("source", False))
            sink = bool(data.get("sink", False))
            direction = "both" if source and sink else "source" if source else "sink" if sink else "neither"
        if direction and direction not in {"source", "sink", "both", "neither"}:
            raise ValueError(f"invalid MCP adapter flow direction: {direction}")
        adapter = cls(
            name=value("name", "id", "adapter_name", "adapterName"),
            version=value("version", "adapter_version", "adapterVersion"),
            policy_version=value("policy_version", "policyVersion"),
            resource_argument=value("resource_argument", "resourceArgument"),
            owner_argument=value("owner_argument", "ownerArgument"),
            source=bool(data.get("source", False)),
            sink=bool(data.get("sink", False)),
            direction=direction,
        )
        if not adapter.name or not adapter.version or not adapter.policy_version:
            raise ValueError("MCP adapter requires name, version, and policy_version")
        return adapter


@dataclass
class MCPServerConfig:
    """Per-server config — command, args, and environment for one stdio MCP subprocess."""

    name: str
    command: str
    args: list[str]
    env: dict[str, str] | None = None
    server_config_id: str = ""
    policy_version: str = ""
    adapters: tuple[MCPAdapterConfig, ...] = ()
    tool_policies: tuple[MCPToolPolicy, ...] = ()
    env_identity: dict[str, dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if not self.server_config_id:
            self.server_config_id = _derived_server_config_id(self.name)

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
        env_identity: dict[str, dict[str, Any]] | None = None
        if isinstance(raw_env, dict):
            raw_values = {str(k): str(v) for k, v in raw_env.items()}
            env = {key: _expand_env_refs(value) for key, value in raw_values.items()}
            env_identity = {
                key: _canonical_env_value(key, value)
                for key, value in sorted(raw_values.items())
            }
        server_config_id = str(
            data.get("server_config_id", data.get("serverConfigId", ""))
        ).strip() or _derived_server_config_id(name)
        policy_version = str(
            data.get("policy_version", data.get("policyVersion", ""))
        ).strip()
        raw_adapters = data.get("adapters", [])
        raw_policies = data.get("tool_policies", data.get("toolPolicies", []))
        adapters = tuple(
            MCPAdapterConfig.from_dict(item)
            for item in raw_adapters
            if isinstance(item, dict)
        ) if isinstance(raw_adapters, list) else ()
        tool_policies: list[MCPToolPolicy] = []
        if isinstance(raw_policies, list):
            for item in raw_policies:
                if not isinstance(item, dict):
                    log.warning("Ignoring invalid non-object MCP tool policy for %s", name)
                    continue
                try:
                    tool_policies.append(MCPToolPolicy.from_dict(item))
                except (TypeError, ValueError) as exc:
                    log.warning("Ignoring invalid MCP tool policy for %s: %s", name, exc)
        return cls(
            name=name,
            command=command,
            args=args,
            env=env,
            server_config_id=server_config_id,
            policy_version=policy_version,
            adapters=adapters,
            tool_policies=tuple(tool_policies),
            env_identity=env_identity,
        )


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
        config_id = server_config_id or self.config.server_config_id
        for mcp_tool in result.tools:
            provenance = MCPProvenance.create(
                config=self.config,
                tool_name=mcp_tool.name,
                input_schema=mcp_tool.inputSchema or {},
                server_config_id=config_id,
            )
            policy = next(
                (
                    item for item in self.config.tool_policies
                    if item.tool_name == mcp_tool.name
                ),
                None,
            )
            if policy is not None:
                identity_matches = (
                    policy.config_digest == provenance.config_digest
                    and policy.schema_digest == provenance.schema_digest
                    and policy.policy_version == self.config.policy_version
                )
                provenance = replace(
                    provenance,
                    classification=policy.classification,
                    adapter_name=policy.adapter_name,
                    adapter_version=policy.adapter_version,
                    approval_version=policy.approval_version,
                    policy_version=policy.policy_version,
                    result_integrity=(
                        policy.result_integrity if identity_matches else "untrusted"
                    ),
                    argument_egress=(
                        policy.argument_egress if identity_matches else "taint_gated"
                    ),
                    is_tombstoned=not identity_matches,
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


class MCPPolicyStore:
    """Durable, non-secret inventory of discovered MCP tool identities."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid MCP policy store {self.path}: {exc}") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("version") != MCP_POLICY_STORE_VERSION
            or not isinstance(payload.get("tools"), dict)
        ):
            raise ValueError(f"invalid MCP policy store format: {self.path}")
        records = payload["tools"]
        if not all(isinstance(key, str) and isinstance(value, dict) for key, value in records.items()):
            raise ValueError(f"invalid MCP policy records: {self.path}")
        return records

    def save(self, records: dict[str, dict[str, Any]]) -> None:
        atomic_write_json(
            self.path,
            {"version": MCP_POLICY_STORE_VERSION, "tools": records},
        )


def _provenance_record(tool: StructuredTool, provenance: MCPProvenance) -> dict[str, Any]:
    return {
        "tool_id": provenance.tool_id,
        "server_config_id": provenance.server_config_id,
        "transport_type": provenance.transport_type,
        "endpoint_identity": provenance.endpoint_identity,
        "config_digest": provenance.config_digest,
        "schema_digest": provenance.schema_digest,
        "original_tool_name": provenance.original_tool_name,
        "display_name": tool.name,
        "classification": provenance.classification,
        "adapter_name": provenance.adapter_name,
        "adapter_version": provenance.adapter_version,
        "approval_version": provenance.approval_version,
        "policy_version": provenance.policy_version,
        "result_integrity": provenance.result_integrity,
        "argument_egress": provenance.argument_egress,
        "is_tombstoned": provenance.is_tombstoned,
    }


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
        policy_store_path: Path | None = None,
    ) -> None:
        self.connections: list[MCPConnection] = []
        self._init_timeout = initialize_timeout_s
        self._call_timeout = call_timeout_s
        self._shutdown_timeout = shutdown_timeout_s
        self._policy_store = MCPPolicyStore(policy_store_path) if policy_store_path else None
        self.policy_records: dict[str, dict[str, Any]] = {}

    def _validate_identities(
        self,
        configs: list[MCPServerConfig],
        records: dict[str, dict[str, Any]],
    ) -> None:
        seen: dict[str, MCPServerConfig] = {}
        for config in configs:
            prior = seen.get(config.server_config_id)
            if prior is not None:
                raise ValueError(
                    "duplicate MCP server_config_id "
                    f"{config.server_config_id!r} for {prior.name!r} and {config.name!r}"
                )
            seen[config.server_config_id] = config
            endpoints = {
                str(record.get("endpoint_identity", ""))
                for record in records.values()
                if record.get("server_config_id") == config.server_config_id
            }
            if endpoints and endpoints != {config.name}:
                raise ValueError(
                    f"MCP server_config_id {config.server_config_id!r} endpoint mismatch"
                )

    async def start_servers(self, configs: list[MCPServerConfig]) -> list[StructuredTool]:
        """Start every configured server, bridge their tools, return flat list.

        Detects ``mcp_{server}_{tool}`` namespaced-name collisions
        across servers and rejects the ambiguous tool set. Two
        servers can legitimately share a tool name as long as the
        server names differ; collision only happens when an operator
        configures the same server name twice or when underscore-laden
        tool names happen to produce the same namespaced string.
        """
        records = self._policy_store.load() if self._policy_store else {}
        self._validate_identities(configs, records)
        register_configured_mcp_adapters(configs)
        all_tools: list[StructuredTool] = []
        successful_config_ids: set[str] = set()
        for config in configs:
            try:
                conn = await self._connect(config)
                tools = await conn.discover_tools(
                    call_timeout_s=self._call_timeout,
                    server_config_id=config.server_config_id or None,
                )
                self.connections.append(conn)
                successful_config_ids.add(config.server_config_id)
                all_tools.extend(tools)
                log.info(
                    "MCP server connected: name=%s tools=%s",
                    config.name, [tool.name for tool in tools],
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
        display_identities: dict[str, str] = {}
        for tool in all_tools:
            provenance = get_tool_provenance(tool)
            if provenance is None:
                raise ValueError(f"MCP tool {tool.name!r} is missing provenance")
            if tool.name in display_identities:
                raise ValueError(
                    f"MCP display-name collision for {tool.name!r}; "
                    "display names are not authoritative identities"
                )
            display_identities[tool.name] = provenance.tool_id

        for tool in all_tools:
            provenance = get_tool_provenance(tool)
            assert provenance is not None
            _tool_provenance_registry.setdefault(provenance.tool_id, provenance)

        if self._policy_store:
            configured_ids = {config.server_config_id for config in configs}
            for record in records.values():
                server_id = str(record.get("server_config_id", ""))
                if server_id not in configured_ids or server_id in successful_config_ids:
                    record["is_tombstoned"] = True
            for tool in all_tools:
                provenance = get_tool_provenance(tool)
                assert provenance is not None
                records[provenance.tool_id] = _provenance_record(tool, provenance)
            self._policy_store.save(records)
        self.policy_records = records
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
        object.__setattr__(tool, "mcp_provenance", provenance)
        object.__setattr__(tool, "mcp_tool_id", provenance.tool_id)
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
    provenance = getattr(tool, "mcp_provenance", None)
    if provenance is not None:
        return provenance
    tool_id = getattr(tool, "mcp_tool_id", "")
    return _tool_provenance_registry.get(tool_id)


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


@dataclass(frozen=True)
class MCPAuthorizationRequest:
    """Exact validated MCP invocation presented to an adapter."""

    tool_name: str
    arguments: dict[str, Any]
    auth_context: Any
    provenance: MCPProvenance


@dataclass(frozen=True)
class MCPAuthorizationResult:
    """Structured adapter result; protected resources require ``allowed``."""

    decision: Any
    allowed: bool
    reason: str | None = None
    resources: tuple[str, ...] = ()
    source_resources: tuple[str, ...] = ()
    sink_resources: tuple[str, ...] = ()


MCPAdapterClassifier = Callable[[MCPAuthorizationRequest], MCPAuthorizationResult]


@dataclass(frozen=True)
class MCPAdapterRegistration:
    """Registered classifier for tools carrying matching provenance."""

    version: str
    policy_version: str
    classify: MCPAdapterClassifier
    flow_direction: str = "unknown"


def register_mcp_adapter(
    adapter_name: str,
    adapter_version: str,
    policy_version: str,
    classify: MCPAdapterClassifier,
    *,
    flow_direction: str = "unknown",
) -> None:
    """Register a classifier for MCP tools carrying ``adapter_name`` provenance."""
    if not callable(classify):
        raise TypeError("MCP adapter classifier must be callable")
    if flow_direction not in {"source", "sink", "both", "neither", "unknown"}:
        raise ValueError(f"invalid MCP adapter flow direction: {flow_direction}")
    _global_mcp_adapter_registry[adapter_name] = MCPAdapterRegistration(
        version=adapter_version,
        policy_version=policy_version,
        classify=classify,
        flow_direction=flow_direction,
    )


def get_mcp_adapter_info(adapter_name: str) -> MCPAdapterRegistration | None:
    """Get a registered MCP classifier by provenance adapter name."""
    return _global_mcp_adapter_registry.get(adapter_name)


def clear_mcp_adapter_registry() -> None:
    """Clear registered MCP classifiers. Used by tests."""
    _global_mcp_adapter_registry.clear()


_global_mcp_adapter_registry: dict[str, MCPAdapterRegistration] = {}


def _configured_resource_classifier(
    config: MCPAdapterConfig,
) -> MCPAdapterClassifier:
    """Build a strict same-owner scalar-resource classifier."""
    def classify(request: MCPAuthorizationRequest) -> MCPAuthorizationResult:
        from .access_control import OperationDecision

        decision = OperationDecision(request.provenance.classification)
        if decision is not OperationDecision.RESOURCE_SCOPED:
            return MCPAuthorizationResult(
                decision=decision,
                allowed=decision is OperationDecision.OPEN,
                reason=(
                    None
                    if decision is OperationDecision.OPEN
                    else "admin_required"
                ),
            )

        arguments = request.arguments
        resource = arguments.get(config.resource_argument) if config.resource_argument else None
        owner = arguments.get(config.owner_argument) if config.owner_argument else None
        principal = getattr(request.auth_context, "canonical_principal", None)
        if not isinstance(resource, str) or not resource.strip():
            return MCPAuthorizationResult(decision=decision, allowed=False, reason="mcp_resource_unknown")
        resource = resource.strip()
        if any(char in resource for char in ("*", "?", "[", "]")):
            return MCPAuthorizationResult(decision=decision, allowed=False, reason="mcp_resource_wildcard")
        if not isinstance(owner, str) or not owner.strip() or not isinstance(principal, str):
            return MCPAuthorizationResult(decision=decision, allowed=False, reason="mcp_owner_unknown")
        if owner.strip() != principal:
            return MCPAuthorizationResult(decision=decision, allowed=False, reason="mcp_wrong_owner")
        resources = (resource,)
        flow_direction = config.flow_direction
        return MCPAuthorizationResult(
            decision=decision,
            allowed=True,
            resources=resources,
            source_resources=resources if flow_direction in {"source", "both"} else (),
            sink_resources=resources if flow_direction in {"sink", "both"} else (),
        )
    return classify


def register_configured_mcp_adapters(configs: list[MCPServerConfig]) -> None:
    """Register only explicit operator-configured adapter definitions."""
    for server in configs:
        for adapter in server.adapters:
            existing = get_mcp_adapter_info(adapter.name)
            if existing is not None:
                if (
                    existing.version != adapter.version
                    or existing.policy_version != adapter.policy_version
                ):
                    log.warning("MCP adapter configuration conflict: name=%s", adapter.name)
                else:
                    continue
            register_mcp_adapter(
                adapter.name,
                adapter.version,
                adapter.policy_version,
                _configured_resource_classifier(adapter),
                flow_direction=adapter.flow_direction,
            )


def validate_mcp_policy(tools: list[Any]) -> list[dict[str, Any]]:
    """Return redacted actionable startup policy issues."""
    issues: list[dict[str, Any]] = []
    for tool in tools:
        provenance = get_tool_provenance(tool)
        if provenance is None:
            continue
        reason = None
        if provenance.is_tombstoned:
            reason = "drift"
        elif not provenance.classification:
            reason = "unclassified"
        elif not provenance.adapter_name:
            reason = "missing_adapter"
        elif get_mcp_adapter_info(provenance.adapter_name) is None:
            reason = "unregistered_adapter"
        if reason:
            issues.append({
                "reason": reason,
                "server_config_id": provenance.server_config_id,
                "original_tool_name": provenance.original_tool_name,
                "adapter_name": provenance.adapter_name or None,
                "policy_version": provenance.policy_version or None,
            })
    return issues


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
