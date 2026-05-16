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
from pathlib import Path

import pytest

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
async def test_manager_skips_collisions(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same server name registered twice (operator misconfig) → second
    # set's tools all collide and get dropped with a warning.
    from mimir.mcp_client import MCPManager, MCPServerConfig

    mgr = MCPManager()
    cfg_a = MCPServerConfig(name="dup", command="x", args=[])
    cfg_b = MCPServerConfig(name="dup", command="y", args=[])
    sessions = {"dup": _FakeSession([_FakeMCPTool("ping")])}
    # _connect is called twice but both go through the same fake session
    _patch_connect(monkeypatch, sessions)
    tools = await mgr.start_servers([cfg_a, cfg_b])
    assert [t.name for t in tools] == ["mcp_dup_ping"]


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
