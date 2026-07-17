"""Tests for ``mimir.mcp_client`` config parsing + tool registry plumbing.

Subprocess + session behavior is delegated to ``mcp.client.stdio``;
those paths need a real MCP server to exercise end-to-end, so we
don't attempt them in unit tests. We do cover:

* ``MCPServerConfig.from_dict`` shape validation + ${ENV} expansion.
* ``parse_mcp_server_configs`` — list form, ``mcpServers`` wrapper, bad entries.
* ``load_mcp_server_configs`` — inline JSON, file path, malformed JSON.
* Registry: ``get_mcp_tools`` + ``all_mimir_tools`` integration.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from mimir.access_control import OperationDecision
from mimir.mcp_client import (
    MCPServerConfig,
    load_mcp_server_configs,
    parse_mcp_server_configs,
)
from mimir.tools.mcp import clear_mcp_tools, get_mcp_tools, set_mcp_tools


# ─── MCPServerConfig.from_dict ──────────────────────────────────────


class TestMCPServerConfigFromDict:
    def test_minimal_valid(self) -> None:
        cfg = MCPServerConfig.from_dict(
            {"name": "github", "command": "uvx", "args": ["mcp-server-github"]}
        )
        assert cfg.name == "github"
        assert cfg.command == "uvx"
        assert cfg.args == ["mcp-server-github"]
        assert cfg.env is None

    def test_missing_name_raises(self) -> None:
        with pytest.raises(ValueError, match="name"):
            MCPServerConfig.from_dict({"command": "uvx", "args": []})

    def test_missing_command_raises(self) -> None:
        with pytest.raises(ValueError, match="command"):
            MCPServerConfig.from_dict({"name": "github"})

    def test_blank_name_raises(self) -> None:
        with pytest.raises(ValueError, match="name"):
            MCPServerConfig.from_dict({"name": "   ", "command": "x"})

    def test_args_coerced_to_strings(self) -> None:
        cfg = MCPServerConfig.from_dict(
            {"name": "x", "command": "y", "args": [1, 2.5, "three"]}
        )
        assert cfg.args == ["1", "2.5", "three"]

    def test_non_list_args_drop_to_empty(self) -> None:
        cfg = MCPServerConfig.from_dict({"name": "x", "command": "y", "args": "not a list"})
        assert cfg.args == []

    def test_env_var_expansion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
        cfg = MCPServerConfig.from_dict(
            {
                "name": "github",
                "command": "uvx",
                "args": [],
                "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}", "LITERAL": "static-value"},
            }
        )
        assert cfg.env is not None
        assert cfg.env["GITHUB_TOKEN"] == "ghp_secret"
        assert cfg.env["LITERAL"] == "static-value"

    def test_env_var_expansion_missing_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MISSING_VAR", raising=False)
        cfg = MCPServerConfig.from_dict(
            {"name": "x", "command": "y", "args": [], "env": {"X": "${MISSING_VAR}"}}
        )
        # Missing → empty string (don't leak the ${VAR} placeholder).
        assert cfg.env == {"X": ""}

    def test_env_var_expansion_in_compound_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pre-fix the parser only matched ``^${VAR}$`` exactly —
        # ``"Bearer ${TOKEN}"`` and ``"${A}${B}"`` were shipped to the
        # subprocess literally. The regex-based expansion handles both.
        monkeypatch.setenv("TOKEN", "ghp_secret")
        monkeypatch.setenv("A", "alpha")
        monkeypatch.setenv("B", "beta")
        cfg = MCPServerConfig.from_dict(
            {
                "name": "x", "command": "y", "args": [],
                "env": {
                    "AUTH": "Bearer ${TOKEN}",
                    "JOINED": "${A}${B}",
                    "INFIX": "prefix-${A}-suffix",
                },
            }
        )
        assert cfg.env == {
            "AUTH": "Bearer ghp_secret",
            "JOINED": "alphabeta",
            "INFIX": "prefix-alpha-suffix",
        }

    def test_env_var_compound_missing_var(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.delenv("MISSING", raising=False)
        with caplog.at_level("WARNING"):
            cfg = MCPServerConfig.from_dict(
                {"name": "x", "command": "y", "args": [],
                 "env": {"AUTH": "Bearer ${MISSING}"}}
            )
        # Missing var collapses to empty in compound contexts.
        assert cfg.env == {"AUTH": "Bearer "}
        # And we log so the operator sees it instead of failing silently.
        assert any("MISSING" in r.message for r in caplog.records)


# ─── parse_mcp_server_configs ──────────────────────────────────────


class TestParseMCPServerConfigs:
    def test_bare_list(self) -> None:
        out = parse_mcp_server_configs(
            [{"name": "a", "command": "cmd", "args": []}]
        )
        assert len(out) == 1
        assert out[0].name == "a"

    def test_mcpservers_wrapper(self) -> None:
        # Claude Code's config format uses this wrapper.
        out = parse_mcp_server_configs(
            {"mcpServers": [{"name": "a", "command": "cmd"}]}
        )
        assert len(out) == 1
        assert out[0].name == "a"

    def test_snake_case_wrapper(self) -> None:
        out = parse_mcp_server_configs({"mcp_servers": [{"name": "a", "command": "cmd"}]})
        assert len(out) == 1

    def test_non_list_returns_empty(self) -> None:
        assert parse_mcp_server_configs("not a list") == []
        assert parse_mcp_server_configs(42) == []

    def test_claude_desktop_dict_shape(self) -> None:
        # The actual format Claude Desktop / Claude Code use: name is
        # the dict key, body is the value. Pre-fix this silently
        # produced zero servers; operators copying a working
        # claude_desktop_config.json got no MCP wiring.
        out = parse_mcp_server_configs(
            {
                "mcpServers": {
                    "github": {"command": "uvx", "args": ["mcp-server-github"]},
                    "filesystem": {"command": "npx", "args": ["@mcp/filesystem"]},
                }
            }
        )
        names = sorted(c.name for c in out)
        assert names == ["filesystem", "github"]
        # Verify the dict key was injected as the name field.
        github = next(c for c in out if c.name == "github")
        assert github.command == "uvx"

    def test_dict_shape_without_wrapper(self) -> None:
        # Just a top-level dict without ``mcpServers`` envelope.
        out = parse_mcp_server_configs(
            {"a": {"command": "x"}, "b": {"command": "y"}}
        )
        assert sorted(c.name for c in out) == ["a", "b"]

    def test_dict_shape_skips_non_dict_bodies(self) -> None:
        # If an entry's body is malformed, drop just that one, keep others.
        out = parse_mcp_server_configs(
            {
                "good": {"command": "x"},
                "broken": "not-a-dict",
                "also_broken": {},  # no command → ValueError → skipped
            }
        )
        assert [c.name for c in out] == ["good"]

    def test_skips_bad_entries(self) -> None:
        out = parse_mcp_server_configs(
            [
                {"name": "ok", "command": "cmd"},
                {"name": ""},  # invalid — skipped
                "not a dict",  # skipped
                {"name": "ok2", "command": "cmd2"},
            ]
        )
        # Bad entries silently skipped; good ones kept.
        assert [c.name for c in out] == ["ok", "ok2"]


# ─── load_mcp_server_configs ───────────────────────────────────────


class TestLoadMCPServerConfigs:
    def test_both_unset_returns_empty(self) -> None:
        assert load_mcp_server_configs(json_inline=None, json_path=None) == []
        assert load_mcp_server_configs(json_inline="", json_path="") == []

    def test_inline_json_list(self) -> None:
        configs = load_mcp_server_configs(
            json_inline='[{"name": "a", "command": "cmd"}]', json_path=None
        )
        assert len(configs) == 1
        assert configs[0].name == "a"

    def test_inline_json_with_wrapper(self) -> None:
        configs = load_mcp_server_configs(
            json_inline='{"mcpServers": [{"name": "a", "command": "c"}]}',
            json_path=None,
        )
        assert len(configs) == 1

    def test_inline_wins_over_path(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "mcp.json"
        cfg_file.write_text(json.dumps([{"name": "from-file", "command": "x"}]))
        configs = load_mcp_server_configs(
            json_inline='[{"name": "from-inline", "command": "x"}]',
            json_path=str(cfg_file),
        )
        assert configs[0].name == "from-inline"

    def test_inline_malformed_json_returns_empty(self) -> None:
        # Don't crash startup on a bad env var; log + skip.
        assert load_mcp_server_configs(json_inline="not json {{{", json_path=None) == []

    def test_path_file_missing_returns_empty(self) -> None:
        assert load_mcp_server_configs(
            json_inline=None, json_path="/nonexistent/path.json"
        ) == []

    def test_path_load_valid(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "mcp.json"
        cfg_file.write_text(json.dumps({"mcpServers": [{"name": "x", "command": "c"}]}))
        configs = load_mcp_server_configs(json_inline=None, json_path=str(cfg_file))
        assert len(configs) == 1
        assert configs[0].name == "x"

    def test_path_malformed_json_returns_empty(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "mcp.json"
        cfg_file.write_text("not json")
        assert load_mcp_server_configs(json_inline=None, json_path=str(cfg_file)) == []


# ─── tools.mcp registry ────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_mcp_tools() -> None:
    clear_mcp_tools()
    yield
    clear_mcp_tools()


class TestMCPToolRegistry:
    def test_empty_by_default(self) -> None:
        assert get_mcp_tools() == []

    def test_set_then_get_returns_copy(self) -> None:
        fake = ["tool_a", "tool_b"]
        set_mcp_tools(fake)
        out = get_mcp_tools()
        assert out == fake
        # Mutating the returned list mustn't affect cached state.
        out.append("tool_c")
        assert get_mcp_tools() == fake

    def test_all_mimir_tools_includes_mcp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from mimir.tools import all_mimir_tools

        monkeypatch.setenv("MIMIR_MODEL_SPEC", "claude-code:foo")  # disable web tools
        # Use a langchain Tool so the registry doesn't choke on the type.
        from langchain_core.tools import StructuredTool

        async def _fake_coro(**_: object) -> str:
            return "ok"

        fake = StructuredTool.from_function(
            coroutine=_fake_coro,
            name="mcp_demo_ping",
            description="fake",
            handle_tool_error=True,
        )
        set_mcp_tools([fake])
        names = {t.name for t in all_mimir_tools()}
        assert "mcp_demo_ping" in names


# ─── Config integration ────────────────────────────────────────────


def test_config_mcp_servers_inline_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from mimir.config import Config

    monkeypatch.setenv("MIMIR_HOME", "/tmp")
    monkeypatch.setenv(
        "MIMIR_MCP_SERVERS_JSON",
        '[{"name": "demo", "command": "uvx", "args": ["mcp-server-demo"]}]',
    )
    monkeypatch.delenv("MIMIR_MCP_SERVERS_PATH", raising=False)
    cfg = Config.from_env()
    assert len(cfg.mcp_servers) == 1
    assert cfg.mcp_servers[0].name == "demo"


def test_config_mcp_servers_default_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    from mimir.config import Config

    monkeypatch.setenv("MIMIR_HOME", "/tmp")
    monkeypatch.delenv("MIMIR_MCP_SERVERS_JSON", raising=False)
    monkeypatch.delenv("MIMIR_MCP_SERVERS_PATH", raising=False)
    cfg = Config.from_env()
    assert cfg.mcp_servers == []


# ─── MCPManager — mocked ClientSession paths ───────────────────────


class _FakeMCPTool:
    """Stand-in for an MCP tool descriptor returned by list_tools()."""

    def __init__(self, name: str, description: str = "", schema: dict | None = None) -> None:
        self.name = name
        self.description = description
        self.inputSchema = schema or {}


class _FakeListResult:
    def __init__(self, tools: list[_FakeMCPTool]) -> None:
        self.tools = tools


class _FakeSession:
    """Just enough of mcp.ClientSession to drive MCPManager + discover_tools."""

    def __init__(
        self,
        tools: list[_FakeMCPTool],
        *,
        initialize_delay: float = 0.0,
        initialize_fails: bool = False,
    ) -> None:
        self._tools = tools
        self._initialize_delay = initialize_delay
        self._initialize_fails = initialize_fails

    async def initialize(self) -> None:
        if self._initialize_delay:
            import asyncio as _asyncio
            await _asyncio.sleep(self._initialize_delay)
        if self._initialize_fails:
            raise RuntimeError("initialize boom")

    async def list_tools(self) -> _FakeListResult:
        return _FakeListResult(self._tools)


def _patch_connect(monkeypatch: pytest.MonkeyPatch, sessions: dict[str, _FakeSession]) -> None:
    """Replace ``MCPManager._connect`` with a fake that uses provided sessions.

    Wrapped in a closure rather than a callable class so Python's
    method-binding doesn't get in the way — assigning a plain instance
    to ``MCPManager._connect`` skips ``self`` injection.
    """
    from contextlib import AsyncExitStack
    from mimir.mcp_client import MCPConnection, MCPManager

    async def _fake_connect(self, config):  # type: ignore[no-untyped-def]
        session = sessions[config.name]
        import asyncio as _asyncio
        await _asyncio.wait_for(session.initialize(), timeout=self._init_timeout)
        return MCPConnection(config=config, session=session, exit_stack=AsyncExitStack())

    monkeypatch.setattr(MCPManager, "_connect", _fake_connect)


@pytest.mark.asyncio
async def test_manager_starts_servers_and_bridges_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.mcp_client import MCPManager, MCPServerConfig

    mgr = MCPManager()
    cfg_a = MCPServerConfig(name="alpha", command="x", args=[])
    cfg_b = MCPServerConfig(name="beta", command="y", args=[])
    sessions = {
        "alpha": _FakeSession([_FakeMCPTool("ping"), _FakeMCPTool("pong")]),
        "beta": _FakeSession([_FakeMCPTool("status")]),
    }
    _patch_connect(monkeypatch, sessions)
    tools = await mgr.start_servers([cfg_a, cfg_b])
    names = sorted(t.name for t in tools)
    assert names == ["mcp_alpha_ping", "mcp_alpha_pong", "mcp_beta_status"]


@pytest.mark.asyncio
async def test_manager_rejects_duplicate_derived_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    from mimir.mcp_client import MCPManager, MCPServerConfig

    mgr = MCPManager()
    cfg_a = MCPServerConfig(name="dup", command="x", args=[])
    cfg_b = MCPServerConfig(name="dup", command="y", args=[])
    sessions = {"dup": _FakeSession([_FakeMCPTool("ping")])}
    # _connect is called twice but both go through the same fake session
    _patch_connect(monkeypatch, sessions)
    with pytest.raises(ValueError, match="duplicate MCP server_config_id"):
        await mgr.start_servers([cfg_a, cfg_b])


@pytest.mark.asyncio
async def test_manager_skips_failed_server(monkeypatch: pytest.MonkeyPatch) -> None:
    # One server fails to initialize; the other still loads.
    from mimir.mcp_client import MCPManager, MCPServerConfig

    mgr = MCPManager()
    cfg_bad = MCPServerConfig(name="bad", command="x", args=[])
    cfg_ok = MCPServerConfig(name="ok", command="y", args=[])
    sessions = {
        "bad": _FakeSession([], initialize_fails=True),
        "ok": _FakeSession([_FakeMCPTool("ping")]),
    }
    _patch_connect(monkeypatch, sessions)
    tools = await mgr.start_servers([cfg_bad, cfg_ok])
    assert [t.name for t in tools] == ["mcp_ok_ping"]


@pytest.mark.asyncio
async def test_manager_initialize_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    # A server whose initialize() hangs past the timeout is skipped.
    from mimir.mcp_client import MCPManager, MCPServerConfig

    mgr = MCPManager(initialize_timeout_s=0.05)
    cfg = MCPServerConfig(name="slow", command="x", args=[])
    sessions = {"slow": _FakeSession([], initialize_delay=1.0)}
    _patch_connect(monkeypatch, sessions)
    tools = await mgr.start_servers([cfg])
    assert tools == []


@pytest.mark.asyncio
async def test_bridge_tool_surfaces_call_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An MCP tool that hangs past call_timeout_s raises ToolException
    # (LangGraph surfaces that as a recoverable tool-error to the model).
    import asyncio as _asyncio
    from mimir.mcp_client import _bridge_mcp_tool
    from langchain_core.tools import ToolException

    class _SlowSession:
        async def call_tool(self, name, kwargs):  # type: ignore[no-untyped-def]
            await _asyncio.sleep(1.0)

    tool = _bridge_mcp_tool(
        server_name="s", tool_name="t", description="",
        input_schema={}, session=_SlowSession(), call_timeout_s=0.05,
    )
    with pytest.raises(ToolException, match="timed out"):
        await tool.coroutine()


@pytest.mark.asyncio
async def test_bridge_tool_surfaces_is_error_result() -> None:
    from mimir.mcp_client import _bridge_mcp_tool
    from langchain_core.tools import ToolException

    class _ErrContent:
        text = "remote validation failed"

    class _ErrResult:
        isError = True
        content = [_ErrContent()]

    class _ErrSession:
        async def call_tool(self, name, kwargs):  # type: ignore[no-untyped-def]
            return _ErrResult()

    tool = _bridge_mcp_tool(
        server_name="s", tool_name="t", description="",
        input_schema={}, session=_ErrSession(),
    )
    with pytest.raises(ToolException, match="remote validation failed"):
        await tool.coroutine()


def test_bridge_tool_builds_args_schema() -> None:
    # Pre-fix the JSON schema was dumped into the description string
    # only; args_schema was unset and LangChain skipped type validation.
    # Now build a pydantic model so required fields are enforced at
    # call time and the tool-spec shown to the LLM is properly typed.
    from mimir.mcp_client import _bridge_mcp_tool

    class _Dummy:
        pass

    tool = _bridge_mcp_tool(
        server_name="s", tool_name="add", description="adds two numbers",
        input_schema={
            "type": "object",
            "properties": {
                "a": {"type": "integer", "description": "first addend"},
                "b": {"type": "integer", "description": "second addend"},
                "label": {"type": "string", "description": "optional label"},
            },
            "required": ["a", "b"],
        },
        session=_Dummy(),  # type: ignore[arg-type]
    )
    assert tool.args_schema is not None
    fields = tool.args_schema.model_fields
    assert set(fields.keys()) == {"a", "b", "label"}
    assert fields["a"].is_required()
    assert fields["b"].is_required()
    assert not fields["label"].is_required()


def test_bridge_tool_args_schema_fallback_on_no_properties() -> None:
    from mimir.mcp_client import _bridge_mcp_tool

    class _Dummy:
        pass

    # Empty/missing properties → args_schema is None (StructuredTool
    # accepts arbitrary kwargs in that case).
    _bridge_mcp_tool(
        server_name="s", tool_name="t", description="",
        input_schema={"type": "object"},
        session=_Dummy(),  # type: ignore[arg-type]
    )
    # langchain may auto-generate a schema from the function signature;
    # what we're asserting is that ``_build_args_schema`` itself returned
    # None for an empty properties block.
    from mimir.mcp_client import _build_args_schema
    assert _build_args_schema("x", {"type": "object"}) is None
    assert _build_args_schema("x", {}) is None


@pytest.mark.asyncio
async def test_bridge_tool_renders_text_content() -> None:
    from mimir.mcp_client import _bridge_mcp_tool

    class _TextContent:
        text = "hello"

    class _OKResult:
        isError = False
        content = [_TextContent()]

    class _OKSession:
        async def call_tool(self, name, kwargs):  # type: ignore[no-untyped-def]
            return _OKResult()

    tool = _bridge_mcp_tool(
        server_name="s", tool_name="t", description="",
        input_schema={}, session=_OKSession(),
    )
    out = await tool.coroutine()
    assert out == "hello"


class TestMCPProvenance:
    """Tests for MCP provenance tracking (chainlink #870)."""

    def test_provenance_creation(self) -> None:
        from mimir.mcp_client import MCPServerConfig, MCPProvenance

        config = MCPServerConfig(
            name="github", command="uvx", args=["mcp-server-github"]
        )
        provenance = MCPProvenance.create(
            config=config,
            tool_name="search_repos",
            input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        )
        assert provenance.server_config_id is not None
        assert provenance.original_tool_name == "search_repos"
        assert provenance.config_digest != ""
        assert provenance.schema_digest != ""
        assert provenance.is_tombstoned is False

    def test_provenance_config_digest_includes_secret_key_not_value(self) -> None:
        from mimir.mcp_client import MCPServerConfig, _canonical_config_digest

        config_with_secret = MCPServerConfig(
            name="github",
            command="uvx",
            args=["mcp-server-github"],
            env={"GITHUB_TOKEN": "secret123"},
        )
        config_without_secret = MCPServerConfig(
            name="github",
            command="uvx",
            args=["mcp-server-github"],
            env={},
        )
        digest_with = _canonical_config_digest(config_with_secret)
        digest_without = _canonical_config_digest(config_without_secret)
        assert digest_with != digest_without

        rotated = MCPServerConfig(
            name="github",
            command="uvx",
            args=["mcp-server-github"],
            env={"GITHUB_TOKEN": "rotated-secret"},
        )
        assert _canonical_config_digest(rotated) == digest_with

    def test_provenance_schema_digest(self) -> None:
        from mimir.mcp_client import _schema_digest

        schema1 = {"type": "object", "properties": {"a": {"type": "string"}}}
        schema2 = {"type": "object", "properties": {"a": {"type": "string"}}}
        schema3 = {"type": "object", "properties": {"b": {"type": "integer"}}}

        digest1 = _schema_digest(schema1)
        digest2 = _schema_digest(schema2)
        digest3 = _schema_digest(schema3)

        assert digest1 == digest2
        assert digest1 != digest3

    def test_provenance_attached_to_tool(self) -> None:
        from mimir.mcp_client import _bridge_mcp_tool, get_tool_provenance, MCPServerConfig, MCPProvenance
        from mimir.tools.mcp import clear_mcp_tools

        clear_mcp_tools()

        class _Dummy:
            pass

        config = MCPServerConfig(name="test", command="x", args=[])
        provenance = MCPProvenance.create(
            config=config,
            tool_name="ping",
            input_schema={},
        )
        tool = _bridge_mcp_tool(
            server_name="test",
            tool_name="ping",
            description="",
            input_schema={},
            session=_Dummy(),
            provenance=provenance,
        )
        retrieved = get_tool_provenance(tool)
        assert retrieved is not None
        assert retrieved.original_tool_name == "ping"

    def test_provenance_with_drift_detection(self) -> None:
        from mimir.mcp_client import MCPServerConfig, MCPProvenance

        config = MCPServerConfig(
            name="github", command="uvx", args=["mcp-server-github"]
        )
        original = MCPProvenance.create(
            config=config,
            tool_name="search",
            input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        )
        assert original.is_tombstoned is False

        drifted = original.with_drift_detection(
            config=config,
            tool_name="search",
            input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        )
        assert drifted.is_tombstoned is True

    def test_provenance_name_change_triggers_tombstone(self) -> None:
        from mimir.mcp_client import MCPServerConfig, MCPProvenance

        config = MCPServerConfig(name="test", command="x", args=[])
        original = MCPProvenance.create(
            config=config,
            tool_name="old_name",
            input_schema={},
        )
        drifted = original.with_drift_detection(
            config=config,
            tool_name="new_name",
            input_schema={},
        )
        assert drifted.is_tombstoned is True

    def test_secret_rotation_is_stable_but_reference_and_key_changes_drift(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from mimir.mcp_client import MCPServerConfig, _canonical_config_digest

        monkeypatch.setenv("GMAIL_TOKEN", "first-secret")
        original = MCPServerConfig.from_dict({
            "name": "mail", "command": "mail-mcp",
            "env": {"AUTH_TOKEN": "Bearer ${GMAIL_TOKEN}"},
        })
        monkeypatch.setenv("GMAIL_TOKEN", "rotated-secret")
        rotated = MCPServerConfig.from_dict({
            "name": "mail", "command": "mail-mcp",
            "env": {"AUTH_TOKEN": "Bearer ${GMAIL_TOKEN}"},
        })
        changed_ref = MCPServerConfig.from_dict({
            "name": "mail", "command": "mail-mcp",
            "env": {"AUTH_TOKEN": "Bearer ${OTHER_TOKEN}"},
        })
        changed_key = MCPServerConfig.from_dict({
            "name": "mail", "command": "mail-mcp",
            "env": {"CREDENTIAL": "Bearer ${GMAIL_TOKEN}"},
        })

        digest = _canonical_config_digest(original)
        assert _canonical_config_digest(rotated) == digest
        assert _canonical_config_digest(changed_ref) != digest
        assert _canonical_config_digest(changed_key) != digest
        assert "first-secret" not in json.dumps(original.env_identity)

    def test_derived_server_and_tool_ids_are_stable(self) -> None:
        from mimir.mcp_client import MCPProvenance, MCPServerConfig

        first_config = MCPServerConfig(name="mail", command="mail-mcp", args=[])
        second_config = MCPServerConfig(name="mail", command="mail-mcp", args=[])
        first = MCPProvenance.create(first_config, "send", {})
        second = MCPProvenance.create(second_config, "send", {})

        assert first_config.server_config_id == second_config.server_config_id
        assert first.server_config_id == second.server_config_id
        assert first.tool_id == second.tool_id


class TestMCPDurableIdentity:
    @pytest.mark.asyncio
    async def test_ambiguous_display_name_collision_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from mimir.mcp_client import MCPManager, MCPServerConfig

        configs = [
            MCPServerConfig(name="a_b", command="x", args=[], server_config_id="server-ab"),
            MCPServerConfig(name="a", command="y", args=[], server_config_id="server-a"),
        ]
        _patch_connect(monkeypatch, {
            "a_b": _FakeSession([_FakeMCPTool("c")]),
            "a": _FakeSession([_FakeMCPTool("b_c")]),
        })

        with pytest.raises(ValueError, match="display-name collision"):
            await MCPManager().start_servers(configs)

    @pytest.mark.asyncio
    async def test_duplicate_explicit_id_fails_before_connect(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from mimir.mcp_client import MCPManager, MCPServerConfig

        called = False

        async def connect(*_args):  # type: ignore[no-untyped-def]
            nonlocal called
            called = True

        monkeypatch.setattr(MCPManager, "_connect", connect)
        configs = [
            MCPServerConfig(name="one", command="x", args=[], server_config_id="shared"),
            MCPServerConfig(name="two", command="y", args=[], server_config_id="shared"),
        ]
        with pytest.raises(ValueError, match="duplicate MCP server_config_id"):
            await MCPManager().start_servers(configs)
        assert called is False

    @pytest.mark.asyncio
    async def test_restart_persists_identity_and_tombstones_removed_tool(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        from mimir.mcp_client import MCPManager, MCPServerConfig, get_tool_provenance

        path = tmp_path / "mcp-policy.json"
        config = MCPServerConfig(
            name="mail", command="mail-mcp", args=[], server_config_id="mail-production",
        )
        sessions = {"mail": _FakeSession([_FakeMCPTool("send"), _FakeMCPTool("draft")])}
        _patch_connect(monkeypatch, sessions)
        first_tools = await MCPManager(policy_store_path=path).start_servers([config])
        first_ids = {
            get_tool_provenance(tool).original_tool_name: get_tool_provenance(tool).tool_id
            for tool in first_tools
        }

        sessions["mail"] = _FakeSession([_FakeMCPTool("send")])
        second_manager = MCPManager(policy_store_path=path)
        second_tools = await second_manager.start_servers([config])
        second = get_tool_provenance(second_tools[0])
        assert second is not None
        assert second.tool_id == first_ids["send"]
        assert second_manager.policy_records[first_ids["draft"]]["is_tombstoned"] is True
        assert second_manager.policy_records[first_ids["draft"]]["original_tool_name"] == "draft"

    @pytest.mark.asyncio
    async def test_policy_store_never_contains_expanded_secret(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        from mimir.mcp_client import MCPManager, MCPServerConfig

        monkeypatch.setenv("GMAIL_TOKEN", "expanded-super-secret")
        config = MCPServerConfig.from_dict({
            "name": "mail", "command": "mail-mcp",
            "env": {"AUTH_TOKEN": "${GMAIL_TOKEN}"},
        })
        path = tmp_path / "mcp-policy.json"
        _patch_connect(monkeypatch, {"mail": _FakeSession([_FakeMCPTool("send")])})

        await MCPManager(policy_store_path=path).start_servers([config])

        persisted = path.read_text(encoding="utf-8")
        assert "expanded-super-secret" not in persisted

    @pytest.mark.asyncio
    async def test_persisted_endpoint_mismatch_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        from mimir.mcp_client import MCPManager, MCPServerConfig

        path = tmp_path / "mcp-policy.json"
        original = MCPServerConfig(
            name="mail", command="mail-mcp", args=[], server_config_id="shared-id",
        )
        _patch_connect(monkeypatch, {"mail": _FakeSession([_FakeMCPTool("send")])})
        await MCPManager(policy_store_path=path).start_servers([original])

        changed = MCPServerConfig(
            name="calendar", command="calendar-mcp", args=[], server_config_id="shared-id",
        )
        with pytest.raises(ValueError, match="endpoint mismatch"):
            await MCPManager(policy_store_path=path).start_servers([changed])


class TestMCPAdapterRegistry:
    """Tests for MCP adapter registry."""

    def setup_method(self) -> None:
        from mimir.mcp_client import clear_mcp_adapter_registry

        clear_mcp_adapter_registry()

    def teardown_method(self) -> None:
        from mimir.mcp_client import clear_mcp_adapter_registry

        clear_mcp_adapter_registry()

    def test_register_and_get_adapter(self) -> None:
        from mimir.access_control import OperationDecision
        from mimir.mcp_client import register_mcp_adapter, get_mcp_adapter_info

        def classifier(_tool: str, _context: object) -> OperationDecision:
            return OperationDecision.OPEN
        register_mcp_adapter("my-adapter", "v1.0", "policy-v1", classifier)
        info = get_mcp_adapter_info("my-adapter")
        assert info is not None
        assert info.version == "v1.0"
        assert info.policy_version == "policy-v1"
        assert info.classify is classifier

    def test_get_nonexistent_adapter(self) -> None:
        from mimir.mcp_client import get_mcp_adapter_info

        assert get_mcp_adapter_info("nonexistent") is None


class TestMCPResourceAdapter:
    """Tests for MCP authorization (chainlink #870)."""

    def setup_method(self) -> None:
        from mimir.mcp_client import clear_mcp_adapter_registry
        from mimir.tools.mcp import clear_mcp_tools

        clear_mcp_adapter_registry()
        clear_mcp_tools()

    def teardown_method(self) -> None:
        from mimir.mcp_client import clear_mcp_adapter_registry
        from mimir.tools.mcp import clear_mcp_tools

        clear_mcp_adapter_registry()
        clear_mcp_tools()

    def test_mcp_tool_requires_admin_when_no_tools_registered(self) -> None:
        from mimir.access_control import (
            MCPResourceAdapter,
            OperationDecision,
        )
        from mimir.tools.mcp import clear_mcp_tools

        clear_mcp_tools()

        decision = MCPResourceAdapter.get_decision("mcp_github_ping", None)
        assert decision == OperationDecision.ADMIN_REQUIRED

    def test_non_mcp_tool_passes_through(self) -> None:
        from mimir.access_control import MCPResourceAdapter

        decision = MCPResourceAdapter.get_decision("bash", None)
        assert decision is None

    def test_provenanced_but_unclassified_tool_requires_admin(self) -> None:
        from mimir.access_control import MCPResourceAdapter, OperationDecision
        from mimir.mcp_client import MCPServerConfig, MCPProvenance

        provenance = MCPProvenance.create(
            config=MCPServerConfig(name="github", command="x", args=[]),
            tool_name="write_issue",
            input_schema={},
        )

        class _Context:
            mcp_provenance = provenance

        decision = MCPResourceAdapter.get_decision(
            "mcp_github_write_issue", _Context()
        )
        assert decision == OperationDecision.ADMIN_REQUIRED

    @pytest.mark.parametrize(
        "classified_decision",
        [
            OperationDecision.OPEN,
            OperationDecision.RESOURCE_SCOPED,
            OperationDecision.ADMIN_REQUIRED,
        ],
    )
    def test_registered_adapter_supplies_explicit_decision(
        self, classified_decision: OperationDecision
    ) -> None:
        from mimir.access_control import MCPResourceAdapter
        from mimir.mcp_client import (
            MCPServerConfig,
            MCPProvenance,
            register_mcp_adapter,
        )

        register_mcp_adapter(
            "github-policy",
            "adapter-v1",
            "policy-v1",
            lambda _tool, _context: classified_decision,
        )
        provenance = replace(
            MCPProvenance.create(
                config=MCPServerConfig(name="github", command="x", args=[]),
                tool_name="write_issue",
                input_schema={},
            ),
            adapter_name="github-policy",
            adapter_version="adapter-v1",
            policy_version="policy-v1",
        )

        class _Context:
            mcp_provenance = provenance

        assert (
            MCPResourceAdapter.get_decision(
                "mcp_github_write_issue", _Context()
            )
            == classified_decision
        )

    def test_adapter_version_mismatch_requires_admin(self) -> None:
        from mimir.access_control import MCPResourceAdapter, OperationDecision
        from mimir.mcp_client import (
            MCPServerConfig,
            MCPProvenance,
            register_mcp_adapter,
        )

        register_mcp_adapter(
            "github-policy",
            "adapter-v2",
            "policy-v1",
            lambda _tool, _context: OperationDecision.OPEN,
        )
        provenance = replace(
            MCPProvenance.create(
                config=MCPServerConfig(name="github", command="x", args=[]),
                tool_name="write_issue",
                input_schema={},
            ),
            adapter_name="github-policy",
            adapter_version="adapter-v1",
            policy_version="policy-v1",
        )

        class _Context:
            mcp_provenance = provenance

        assert (
            MCPResourceAdapter.get_decision(
                "mcp_github_write_issue", _Context()
            )
            == OperationDecision.ADMIN_REQUIRED
        )

    def test_regular_principal_cannot_call_arbitrary_provenanced_write_tool(self) -> None:
        from mimir.access_control import OperationDecision, ToolRegistry
        from mimir.mcp_client import MCPServerConfig, MCPProvenance
        from mimir.models import AuthContext

        provenance = MCPProvenance.create(
            config=MCPServerConfig(name="github", command="x", args=[]),
            tool_name="write_issue",
            input_schema={},
        )
        context = AuthContext(
            principal="alice",
            canonical_principal="alice",
            roles=("user",),
            event_ingress="bridge",
            trigger="user_message",
            channel_id="discord-1",
            interactivity=None,
            enforcement_enabled=True,
        )
        object.__setattr__(context, "mcp_provenance", provenance)

        authorization = ToolRegistry().authorize_tool(
            "mcp_github_write_issue", context, enforce=True
        )

        assert authorization.decision == OperationDecision.ADMIN_REQUIRED
        assert authorization.allowed is False
        assert authorization.reason == "admin_required"

    def test_mcp_tool_with_tombstoned_provenance_requires_admin(self) -> None:
        from mimir.access_control import (
            MCPResourceAdapter,
            OperationDecision,
        )
        from mimir.mcp_client import MCPServerConfig, MCPProvenance
        from mimir.tools.mcp import clear_mcp_tools, set_mcp_tools
        from mimir.mcp_client import _bridge_mcp_tool

        clear_mcp_tools()

        class _Dummy:
            pass

        config = MCPServerConfig(name="test", command="x", args=[])
        provenance = MCPProvenance.create(
            config=config,
            tool_name="ping",
            input_schema={},
        )
        tombstoned = provenance.with_drift_detection(
            config=config,
            tool_name="ping",
            input_schema={"type": "object"},
        )

        tool = _bridge_mcp_tool(
            server_name="test",
            tool_name="ping",
            description="",
            input_schema={"type": "object"},
            session=_Dummy(),
            provenance=tombstoned,
        )

        set_mcp_tools([tool])

        decision = MCPResourceAdapter.get_decision("mcp_test_ping", None)
        assert decision == OperationDecision.ADMIN_REQUIRED


class TestStalePolicyReporting:
    """Tests for stale policy detection on startup."""

    def test_check_stale_policy_on_startup(self) -> None:
        from dataclasses import replace
        from mimir.mcp_client import (
            MCPServerConfig,
            MCPProvenance,
            check_stale_policy_on_startup,
        )

        config = MCPServerConfig(name="test", command="x", args=[])
        provenance_v1 = replace(
            MCPProvenance.create(
                config=config,
                tool_name="ping",
                input_schema={},
            ),
            policy_version="policy-v1",
        )

        provenance_v2 = replace(
            MCPProvenance.create(
                config=config,
                tool_name="pong",
                input_schema={},
            ),
            policy_version="policy-v2",
        )

        class FakeTool:
            def __init__(self, name: str, prov: MCPProvenance):
                self.name = name
                self.mcp_provenance = prov

        tools = [
            FakeTool("mcp_test_ping", provenance_v1),
            FakeTool("mcp_test_pong", provenance_v2),
        ]

        stale = check_stale_policy_on_startup(tools, "policy-v1")
        assert len(stale) == 1
        assert stale[0]["tool_name"] == "mcp_test_pong"

    def test_no_stale_tools_when_all_match(self) -> None:
        from dataclasses import replace
        from mimir.mcp_client import (
            MCPServerConfig,
            MCPProvenance,
            check_stale_policy_on_startup,
        )

        config = MCPServerConfig(name="test", command="x", args=[])
        provenance = replace(
            MCPProvenance.create(
                config=config,
                tool_name="ping",
                input_schema={},
            ),
            policy_version="policy-v1",
        )

        class FakeTool:
            def __init__(self, name: str, prov: MCPProvenance):
                self.name = name
                self.mcp_provenance = prov

        tools = [FakeTool("mcp_test_ping", provenance)]

        stale = check_stale_policy_on_startup(tools, "policy-v1")
        assert stale == []

    def test_non_mcp_tools_ignored(self) -> None:
        from mimir.mcp_client import check_stale_policy_on_startup

        class FakeTool:
            name = "bash"

        stale = check_stale_policy_on_startup([FakeTool()], "policy-v1")
        assert stale == []


class TestUnderscoreDisplayNameCollisions:
    """Tests for underscore/display-name collision handling."""

    def test_underscore_in_tool_name(self) -> None:
        from mimir.mcp_client import _bridge_mcp_tool

        class _Dummy:
            pass

        tool = _bridge_mcp_tool(
            server_name="my_server",
            tool_name="get_user_profile",
            description="Get user profile",
            input_schema={},
            session=_Dummy(),
        )
        assert tool.name == "mcp_my_server_get_user_profile"

    def test_collision_detection_in_manager(self) -> None:
        from mimir.mcp_client import MCPManager, MCPServerConfig
        from mimir.tools.mcp import clear_mcp_tools

        clear_mcp_tools()

        class _FakeMCPTool:
            def __init__(self, name: str):
                self.name = name
                self.description = ""
                self.inputSchema = {}

        class _FakeListResult:
            def __init__(self, tools: list):
                self.tools = tools

        class _FakeSession:
            async def initialize(self) -> None:
                pass

            async def list_tools(self) -> _FakeListResult:
                return _FakeListResult([_FakeMCPTool("get_user")])

        from contextlib import AsyncExitStack

        async def fake_connect(self, config):  # type: ignore[no-untyped-def]
            return MCPConnection(
                config=config,
                session=_FakeSession(),
                exit_stack=AsyncExitStack(),
            )

        import asyncio
        from mimir.mcp_client import MCPConnection

        mgr = MCPManager()
        cfg = MCPServerConfig(name="test", command="x", args=[])

        orig_connect = MCPManager._connect
        MCPManager._connect = fake_connect

        async def run_test():
            tools = await mgr.start_servers([cfg])
            return tools

        try:
            tools = asyncio.run(run_test())
            assert len(tools) == 1
            assert tools[0].name == "mcp_test_get_user"
        finally:
            MCPManager._connect = orig_connect
            clear_mcp_tools()


class TestDuplicateProvenance:
    """Tests for duplicate provenance detection."""

    def test_different_servers_same_tool_name(self) -> None:
        from mimir.mcp_client import MCPServerConfig, MCPProvenance

        config_a = MCPServerConfig(name="server_a", command="x", args=[])
        config_b = MCPServerConfig(name="server_b", command="y", args=[])

        prov_a = MCPProvenance.create(
            config=config_a,
            tool_name="ping",
            input_schema={},
        )
        prov_b = MCPProvenance.create(
            config=config_b,
            tool_name="ping",
            input_schema={},
        )

        assert prov_a.server_config_id != prov_b.server_config_id
        assert prov_a.config_digest != prov_b.config_digest

    def test_immutable_server_config_id(self) -> None:
        from mimir.mcp_client import MCPServerConfig, MCPProvenance

        config = MCPServerConfig(name="test", command="x", args=[])
        prov = MCPProvenance.create(
            config=config,
            tool_name="ping",
            input_schema={},
            server_config_id="fixed-id-123",
        )
        assert prov.server_config_id == "fixed-id-123"


class TestWrapperMetadataLoss:
    """Tests for wrapper metadata preservation."""

    def test_tool_without_provenance_returns_none(self) -> None:
        from mimir.mcp_client import get_tool_provenance

        class FakeTool:
            name = "regular_tool"

        provenance = get_tool_provenance(FakeTool())
        assert provenance is None

    def test_drift_check_on_none_provenance(self) -> None:
        from mimir.mcp_client import check_tool_drift, MCPServerConfig

        class FakeTool:
            name = "mcp_test_ping"

        config = MCPServerConfig(name="test", command="x", args=[])
        result = check_tool_drift(FakeTool(), config, "ping", {})
        assert result is None


class TestProductionMCPPolicyWiring:
    """Durable policy is applied by the same manager path production uses."""

    @staticmethod
    def _config(*, schema: dict | None = None) -> MCPServerConfig:
        from mimir.mcp_client import _canonical_config_digest, _schema_digest

        schema = schema or {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repository": {"type": "string"},
            },
            "required": ["owner", "repository"],
        }
        base = MCPServerConfig(name="github", command="x", args=[])
        return MCPServerConfig.from_dict({
            "name": "github",
            "command": "x",
            "server_config_id": "github-production",
            "policy_version": "policy-v1",
            "adapters": [{
                "name": "github-owner",
                "version": "adapter-v1",
                "policy_version": "policy-v1",
                "owner_argument": "owner",
                "resource_argument": "repository",
                "source": True,
            }],
            "tool_policies": [{
                "tool_name": "get_repository",
                "classification": "resource_scoped",
                "adapter_name": "github-owner",
                "adapter_version": "adapter-v1",
                "approval_version": "approval-v1",
                "policy_version": "policy-v1",
                "config_digest": _canonical_config_digest(base),
                "schema_digest": _schema_digest(schema),
            }],
        })

    @pytest.mark.asyncio
    async def test_manager_applies_policy_and_preserves_identity_on_restart(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from contextlib import AsyncExitStack
        from mimir.mcp_client import (
            MCPConnection,
            MCPManager,
            clear_mcp_adapter_registry,
            get_tool_provenance,
        )

        schema = {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repository": {"type": "string"},
            },
            "required": ["owner", "repository"],
        }

        class Tool:
            name = "get_repository"
            description = ""
            inputSchema = schema

        class Session:
            async def list_tools(self):  # type: ignore[no-untyped-def]
                return type("Result", (), {"tools": [Tool()]})()

        async def connect(_manager, config):  # type: ignore[no-untyped-def]
            return MCPConnection(config, Session(), AsyncExitStack())

        clear_mcp_adapter_registry()
        monkeypatch.setattr(MCPManager, "_connect", connect)
        config = self._config(schema=schema)
        first = await MCPManager().start_servers([config])
        second = await MCPManager().start_servers([config])

        first_provenance = get_tool_provenance(first[0])
        second_provenance = get_tool_provenance(second[0])
        assert first_provenance is not None
        assert second_provenance is not None
        assert first_provenance.server_config_id == "github-production"
        assert second_provenance.server_config_id == first_provenance.server_config_id
        assert first_provenance.classification == "resource_scoped"
        assert first_provenance.is_tombstoned is False
        assert getattr(first[0], "mcp_provenance") is first_provenance

        from mimir.mcp_client import clear_provenance_registry

        clear_provenance_registry()
        assert get_tool_provenance(first[0]) is first_provenance

    @pytest.mark.asyncio
    async def test_discovery_marks_schema_drift_stale(self) -> None:
        from contextlib import AsyncExitStack
        from mimir.mcp_client import MCPConnection, get_tool_provenance, validate_mcp_policy

        approved_schema = {
            "type": "object",
            "properties": {
                "owner": {"type": "string"},
                "repository": {"type": "string"},
            },
            "required": ["owner", "repository"],
        }
        changed_schema = {**approved_schema, "required": ["repository"]}

        class Tool:
            name = "get_repository"
            description = ""
            inputSchema = changed_schema

        class Session:
            async def list_tools(self):  # type: ignore[no-untyped-def]
                return type("Result", (), {"tools": [Tool()]})()

        tools = await MCPConnection(
            self._config(schema=approved_schema), Session(), AsyncExitStack()
        ).discover_tools()
        provenance = get_tool_provenance(tools[0])
        assert provenance is not None and provenance.is_tombstoned
        assert validate_mcp_policy(tools)[0]["reason"] == "drift"

    @pytest.mark.parametrize(
        ("arguments", "allowed", "reason"),
        [
            ({"owner": "alice", "repository": "repo-1"}, True, None),
            ({"owner": "bob", "repository": "repo-1"}, False, "mcp_wrong_owner"),
            ({"owner": "alice", "repository": "*"}, False, "mcp_resource_wildcard"),
            ({"owner": "alice", "repository": "secrets/*"}, False, "mcp_resource_wildcard"),
            ({"owner": "alice", "repository": ["a", "b"]}, False, "mcp_resource_unknown"),
            ({"owner": "alice"}, False, "mcp_resource_unknown"),
        ],
    )
    def test_argument_resource_authorization(
        self, arguments: dict, allowed: bool, reason: str | None,
    ) -> None:
        from mimir.access_control import ToolRegistry
        from mimir.mcp_client import (
            MCPProvenance,
            _bridge_mcp_tool,
            clear_mcp_adapter_registry,
            register_configured_mcp_adapters,
        )
        from mimir.models import AuthContext, InformationFlowLabels

        config = self._config()
        register_configured_mcp_adapters([config])
        policy = config.tool_policies[0]
        provenance = replace(
            MCPProvenance.create(
                config, "get_repository", {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repository": {"type": "string"},
                    },
                    "required": ["owner", "repository"],
                },
                server_config_id=config.server_config_id,
            ),
            classification=policy.classification,
            adapter_name=policy.adapter_name,
            adapter_version=policy.adapter_version,
            approval_version=policy.approval_version,
            policy_version=policy.policy_version,
        )
        tool = _bridge_mcp_tool(
            server_name="github",
            tool_name="get_repository",
            description="",
            input_schema={},
            session=object(),
            provenance=provenance,
        )
        context = AuthContext(
            principal="alice",
            canonical_principal="alice",
            roles=("user",),
            event_ingress="bridge",
            trigger="user_message",
            channel_id="ch-1",
            interactivity=None,
            enforcement_enabled=True,
        )
        authorization = ToolRegistry().authorize_tool(
            tool.name,
            context,
            enforce=True,
            mcp_tool=tool,
            arguments=arguments,
            ifc_labels=InformationFlowLabels(),
        )
        assert authorization.allowed is allowed
        assert authorization.reason == reason
        clear_mcp_adapter_registry()

    def test_missing_adapter_and_exception_fail_closed(self) -> None:
        from mimir.access_control import ToolRegistry
        from mimir.mcp_client import (
            MCPProvenance,
            _bridge_mcp_tool,
            clear_mcp_adapter_registry,
            register_mcp_adapter,
        )
        from mimir.models import AuthContext

        config = self._config()
        policy = config.tool_policies[0]
        provenance = replace(
            MCPProvenance.create(config, "get_repository", {}, server_config_id=config.server_config_id),
            classification=policy.classification,
            adapter_name=policy.adapter_name,
            adapter_version=policy.adapter_version,
            approval_version=policy.approval_version,
            policy_version=policy.policy_version,
        )
        tool = _bridge_mcp_tool(
            server_name="github", tool_name="get_repository", description="",
            input_schema={}, session=object(), provenance=provenance,
        )
        context = AuthContext(
            principal="alice", canonical_principal="alice", roles=("user",),
            event_ingress="bridge", trigger="user_message", channel_id="ch-1",
            interactivity=None, enforcement_enabled=True,
        )
        clear_mcp_adapter_registry()
        missing = ToolRegistry().authorize_tool(
            tool.name, context, enforce=True, mcp_tool=tool,
            arguments={"owner": "alice", "repository": "repo-1"},
        )
        assert missing.reason == "mcp_missing_adapter"
        assert missing.allowed is False

        shadow_missing = ToolRegistry().authorize_tool(
            tool.name, context, enforce=False, mcp_tool=tool,
            arguments={"owner": "alice", "repository": "repo-1"},
        )
        assert shadow_missing.allowed is True
        assert shadow_missing.is_shadow_decision is True
        assert shadow_missing.reason == "mcp_missing_adapter"

        def explode(_request):  # type: ignore[no-untyped-def]
            raise RuntimeError("secret must not authorize")

        register_mcp_adapter("github-owner", "adapter-v1", "policy-v1", explode)
        failed = ToolRegistry().authorize_tool(
            tool.name, context, enforce=True, mcp_tool=tool,
            arguments={"owner": "alice", "repository": "repo-1"},
        )
        assert failed.reason == "mcp_adapter_exception"
        assert failed.allowed is False

        shadow_failed = ToolRegistry().authorize_tool(
            tool.name, context, enforce=False, mcp_tool=tool,
            arguments={"owner": "alice", "repository": "repo-1"},
        )
        assert shadow_failed.allowed is True
        assert shadow_failed.is_shadow_decision is True
        assert shadow_failed.reason == "mcp_adapter_exception"
        clear_mcp_adapter_registry()
