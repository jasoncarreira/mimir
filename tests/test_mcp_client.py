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
