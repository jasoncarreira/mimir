"""Unit tests for ``mimir.config`` — env parsing, caps, Config.from_env defaults,
and Config properties (chainlink #247, slice 2/5).

Coverage map:
- ``_parse_sources``     — all three output shapes (frozenset, frozenset(), None)
- ``_env_float``         — unset/empty/valid
- ``_env_int``           — unset/valid/negative
- ``_turns_cap``         — default, normal, ceiling clamp
- ``_events_cap``        — default, normal, ceiling clamp
- ``Config.from_env``    — defaults, MIMIR_HOME, MIMIR_AGENT_ID, MIMIR_WEB_PORT,
                           MIMIR_MODEL, MIMIR_FILE_OP_ROOTS, MIMIR_PROMPTS_DIR,
                           MIMIR_TURNS_ARCHIVE_DIR, MIMIR_RECENT_SOURCES,
                           MIMIR_TOOL_CALL_BUDGET
- ``Config`` properties  — logs_dir, turns_log, events_log, commitments_log,
                           sdk_env_overrides (omits empty values)

NB: ``_env_bool`` and ``_parse_folders`` have dedicated files in the test
suite (test_config_env_bool.py, test_config_folders.py); this file adds the
remaining surface.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from mimir.config import (
    _EVENTS_CAP_DEFAULT,
    _EVENTS_CAP_MAX,
    _TURNS_CAP_DEFAULT,
    _TURNS_CAP_MAX,
    _env_float,
    _env_int,
    _events_cap,
    _parse_sources,
    _turns_cap,
)


# ---------------------------------------------------------------------------
# _parse_sources
# ---------------------------------------------------------------------------

class TestParseSources:
    """``_parse_sources`` maps MIMIR_RECENT_SOURCES env text to a frozenset or None."""

    def test_empty_string_returns_empty_frozenset(self) -> None:
        """Explicitly empty value → allow nothing (bench-friendly default)."""
        assert _parse_sources("") == frozenset()

    def test_none_equiv_empty_returns_empty_frozenset(self) -> None:
        """None-like (empty after strip) → same as empty."""
        assert _parse_sources("   ") == frozenset()

    def test_star_returns_none(self) -> None:
        """``*`` → None means allow every source including legacy source=None."""
        assert _parse_sources("*") is None

    def test_all_returns_none(self) -> None:
        """``all`` is an alias for ``*``."""
        assert _parse_sources("all") is None

    def test_all_is_case_sensitive(self) -> None:
        """``all`` check is a literal string match — ``ALL`` is NOT the sentinel,
        it falls through to tokenisation and produces a frozenset.
        This pins the current behaviour so a future refactor doesn't
        silently change the semantics.
        """
        result = _parse_sources("ALL")
        # "ALL" is not in {"*", "all"} → parsed as a source name, lowercased
        assert result == frozenset({"all"})

    def test_star_with_whitespace(self) -> None:
        """Whitespace around ``*`` is stripped before comparison."""
        assert _parse_sources("  *  ") is None

    def test_normal_comma_list(self) -> None:
        result = _parse_sources("slack,discord,web")
        assert result == frozenset({"slack", "discord", "web"})

    def test_normalises_to_lowercase(self) -> None:
        """Tokens are lowercased for normalisation."""
        result = _parse_sources("Slack,DISCORD")
        assert result == frozenset({"slack", "discord"})

    def test_strips_whitespace_around_tokens(self) -> None:
        result = _parse_sources(" slack , discord , web ")
        assert result == frozenset({"slack", "discord", "web"})

    def test_empty_tokens_skipped(self) -> None:
        """Consecutive commas produce empty tokens — those are discarded."""
        result = _parse_sources(",slack,,discord,")
        assert result == frozenset({"slack", "discord"})

    def test_single_token(self) -> None:
        assert _parse_sources("stdin") == frozenset({"stdin"})

    def test_default_sources_string(self) -> None:
        """The default string hard-coded in from_env produces the expected set."""
        raw = "slack,discord,bluesky,web,stdin"
        result = _parse_sources(raw)
        assert result == frozenset({"slack", "discord", "bluesky", "web", "stdin"})


# ---------------------------------------------------------------------------
# _env_float
# ---------------------------------------------------------------------------

class TestEnvFloat:
    def test_unset_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MIMIR_TEST_FLOAT_X", raising=False)
        assert _env_float("MIMIR_TEST_FLOAT_X", 0.9) == pytest.approx(0.9)

    def test_empty_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MIMIR_TEST_FLOAT_X", "")
        assert _env_float("MIMIR_TEST_FLOAT_X", 1.5) == pytest.approx(1.5)

    def test_valid_float(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MIMIR_TEST_FLOAT_X", "3.14")
        assert _env_float("MIMIR_TEST_FLOAT_X", 0.0) == pytest.approx(3.14)

    def test_integer_string_parses_as_float(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MIMIR_TEST_FLOAT_X", "5")
        assert _env_float("MIMIR_TEST_FLOAT_X", 0.0) == pytest.approx(5.0)

    def test_zero_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MIMIR_TEST_FLOAT_X", "0")
        assert _env_float("MIMIR_TEST_FLOAT_X", 99.9) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# _env_int
# ---------------------------------------------------------------------------

class TestEnvInt:
    def test_unset_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MIMIR_TEST_INT_X", raising=False)
        assert _env_int("MIMIR_TEST_INT_X", 42) == 42

    def test_empty_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MIMIR_TEST_INT_X", "")
        assert _env_int("MIMIR_TEST_INT_X", 7) == 7

    def test_valid_integer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MIMIR_TEST_INT_X", "100")
        assert _env_int("MIMIR_TEST_INT_X", 0) == 100

    def test_negative_integer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MIMIR_TEST_INT_X", "-5")
        assert _env_int("MIMIR_TEST_INT_X", 0) == -5


# ---------------------------------------------------------------------------
# _turns_cap / _events_cap
# ---------------------------------------------------------------------------

class TestLogCaps:
    """Caps clamp at hard ceiling values to prevent runaway disk usage."""

    def test_turns_cap_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MIMIR_MAX_TURNS", raising=False)
        assert _turns_cap() == _TURNS_CAP_DEFAULT

    def test_turns_cap_normal_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MIMIR_MAX_TURNS", "1000")
        assert _turns_cap() == 1000

    def test_turns_cap_clamps_at_max(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Values above _TURNS_CAP_MAX are silently clamped down."""
        monkeypatch.setenv("MIMIR_MAX_TURNS", str(_TURNS_CAP_MAX + 99999))
        assert _turns_cap() == _TURNS_CAP_MAX

    def test_events_cap_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MIMIR_MAX_EVENTS", raising=False)
        assert _events_cap() == _EVENTS_CAP_DEFAULT

    def test_events_cap_normal_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MIMIR_MAX_EVENTS", "10000")
        assert _events_cap() == 10000

    def test_events_cap_clamps_at_max(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MIMIR_MAX_EVENTS", str(_EVENTS_CAP_MAX + 99999))
        assert _events_cap() == _EVENTS_CAP_MAX

    def test_events_default_is_15x_turns_default(self) -> None:
        """Events cap default is 15× turns cap default (matches measured ~14 events/turn)."""
        assert _EVENTS_CAP_DEFAULT == _TURNS_CAP_DEFAULT * 15

    def test_events_max_is_15x_turns_max(self) -> None:
        assert _EVENTS_CAP_MAX == _TURNS_CAP_MAX * 15


# ---------------------------------------------------------------------------
# Config.from_env — field defaults + overrides
# ---------------------------------------------------------------------------

class TestConfigFromEnv:
    """Key from_env fields get sensible defaults and honour env overrides.

    Each test calls ``Config.from_env()`` with MIMIR_HOME=/tmp so it
    doesn't depend on the live container's home directory.
    """

    def _base(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Set the bare minimum to make from_env() deterministic in tests."""
        monkeypatch.setenv("MIMIR_HOME", "/tmp")
        # Avoid picking up the live oauth credentials path from the container.
        monkeypatch.setenv("MIMIR_CLAUDE_OAUTH_CREDENTIALS", "")

    def test_home_from_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
        monkeypatch.setenv("MIMIR_CLAUDE_OAUTH_CREDENTIALS", "")
        from mimir.config import Config
        cfg = Config.from_env()
        assert cfg.home == tmp_path.resolve()

    def test_agent_id_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._base(monkeypatch)
        monkeypatch.delenv("MIMIR_AGENT_ID", raising=False)
        from mimir.config import Config
        cfg = Config.from_env()
        assert cfg.agent_id == "mimir"

    def test_agent_id_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._base(monkeypatch)
        monkeypatch.setenv("MIMIR_AGENT_ID", "sentinel")
        from mimir.config import Config
        cfg = Config.from_env()
        assert cfg.agent_id == "sentinel"

    def test_web_port_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._base(monkeypatch)
        monkeypatch.delenv("MIMIR_WEB_PORT", raising=False)
        from mimir.config import Config
        cfg = Config.from_env()
        assert cfg.web_port == 8080

    def test_web_port_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._base(monkeypatch)
        monkeypatch.setenv("MIMIR_WEB_PORT", "9090")
        from mimir.config import Config
        cfg = Config.from_env()
        assert cfg.web_port == 9090

    def test_attachments_max_bytes_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """#495: a sane 25MiB inbound-attachment cap by default, so the bridge
        size gate is armed (it's a no-op when None)."""
        self._base(monkeypatch)
        monkeypatch.delenv("MIMIR_ATTACHMENTS_MAX_BYTES", raising=False)
        from mimir.config import Config
        cfg = Config.from_env()
        assert cfg.attachments_max_bytes == 25 * 1024 * 1024

    def test_attachments_max_bytes_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._base(monkeypatch)
        monkeypatch.setenv("MIMIR_ATTACHMENTS_MAX_BYTES", str(500 * 1024 * 1024))
        from mimir.config import Config
        cfg = Config.from_env()
        assert cfg.attachments_max_bytes == 500 * 1024 * 1024

    def test_model_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._base(monkeypatch)
        monkeypatch.delenv("MIMIR_MODEL", raising=False)
        from mimir.config import Config
        cfg = Config.from_env()
        assert cfg.model == "claude-opus-4-7"

    def test_model_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._base(monkeypatch)
        monkeypatch.setenv("MIMIR_MODEL", "claude-haiku-4-5")
        from mimir.config import Config
        cfg = Config.from_env()
        assert cfg.model == "claude-haiku-4-5"

    def test_tool_call_budget_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._base(monkeypatch)
        monkeypatch.delenv("MIMIR_TOOL_CALL_BUDGET", raising=False)
        from mimir.config import Config
        cfg = Config.from_env()
        assert cfg.tool_call_budget == 120

    def test_tool_call_budget_zero_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Setting MIMIR_TOOL_CALL_BUDGET=0 disables the cap entirely."""
        self._base(monkeypatch)
        monkeypatch.setenv("MIMIR_TOOL_CALL_BUDGET", "0")
        from mimir.config import Config
        cfg = Config.from_env()
        assert cfg.tool_call_budget == 0

    def test_file_op_extra_roots_empty_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._base(monkeypatch)
        monkeypatch.delenv("MIMIR_FILE_OP_ROOTS", raising=False)
        from mimir.config import Config
        cfg = Config.from_env()
        assert cfg.file_op_extra_roots == []

    def test_file_op_extra_roots_colon_separated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._base(monkeypatch)
        monkeypatch.setenv("MIMIR_FILE_OP_ROOTS", "/workspace/mimir:/benchmark")
        from mimir.config import Config
        cfg = Config.from_env()
        assert cfg.file_op_extra_roots == [Path("/workspace/mimir"), Path("/benchmark")]

    def test_prompts_dir_none_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._base(monkeypatch)
        monkeypatch.delenv("MIMIR_PROMPTS_DIR", raising=False)
        from mimir.config import Config
        cfg = Config.from_env()
        assert cfg.prompts_dir is None

    def test_prompts_dir_resolves_path(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._base(monkeypatch)
        monkeypatch.setenv("MIMIR_PROMPTS_DIR", str(tmp_path))
        from mimir.config import Config
        cfg = Config.from_env()
        assert cfg.prompts_dir == tmp_path.resolve()

    def test_turns_archive_dir_none_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._base(monkeypatch)
        monkeypatch.delenv("MIMIR_TURNS_ARCHIVE_DIR", raising=False)
        from mimir.config import Config
        cfg = Config.from_env()
        assert cfg.turns_archive_dir is None

    def test_turns_archive_dir_resolves_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._base(monkeypatch)
        monkeypatch.setenv("MIMIR_TURNS_ARCHIVE_DIR", str(tmp_path))
        from mimir.config import Config
        cfg = Config.from_env()
        assert cfg.turns_archive_dir == tmp_path.resolve()

    def test_recent_sources_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default MIMIR_RECENT_SOURCES includes the five standard channels."""
        self._base(monkeypatch)
        monkeypatch.delenv("MIMIR_RECENT_SOURCES", raising=False)
        from mimir.config import Config
        cfg = Config.from_env()
        # Default string is "slack,discord,bluesky,web,stdin"
        assert isinstance(cfg.recent_sources, frozenset)
        assert "discord" in cfg.recent_sources
        assert "slack" in cfg.recent_sources

    def test_recent_sources_star_allows_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._base(monkeypatch)
        monkeypatch.setenv("MIMIR_RECENT_SOURCES", "*")
        from mimir.config import Config
        cfg = Config.from_env()
        assert cfg.recent_sources is None

    def test_home_dotenv_loads_defaults(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
        monkeypatch.setenv("MIMIR_CLAUDE_OAUTH_CREDENTIALS", "")
        monkeypatch.delenv("MIMIR_MODEL_SPEC", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        (tmp_path / ".env").write_text(
            "MIMIR_MODEL_SPEC=anthropic:claude-haiku-4-5\n"
            "ANTHROPIC_API_KEY=from-dotenv\n",
            encoding="utf-8",
        )

        from mimir.config import Config
        cfg = Config.from_env()

        assert cfg.model_spec == "anthropic:claude-haiku-4-5"
        assert cfg.anthropic_api_key == "from-dotenv"
        assert os.environ["ANTHROPIC_API_KEY"] == "from-dotenv"

    def test_home_dotenv_does_not_override_exported_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
        monkeypatch.setenv("MIMIR_CLAUDE_OAUTH_CREDENTIALS", "")
        monkeypatch.setenv("MIMIR_MODEL_SPEC", "openai:gpt-4.1-mini")
        (tmp_path / ".env").write_text(
            "MIMIR_MODEL_SPEC=anthropic:claude-haiku-4-5\n",
            encoding="utf-8",
        )

        from mimir.config import Config
        cfg = Config.from_env()

        assert cfg.model_spec == "openai:gpt-4.1-mini"

    def test_home_dotenv_absent_is_noop(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
        monkeypatch.setenv("MIMIR_CLAUDE_OAUTH_CREDENTIALS", "")
        monkeypatch.delenv("MIMIR_MODEL_SPEC", raising=False)
        assert not (tmp_path / ".env").exists()

        from mimir.config import Config
        cfg = Config.from_env()

        assert cfg.model_spec == "claude-code:claude-sonnet-4-6"
        assert not (tmp_path / ".env").exists()


# ---------------------------------------------------------------------------
# Config properties
# ---------------------------------------------------------------------------

class TestConfigProperties:
    def _cfg(self, monkeypatch: pytest.MonkeyPatch, home: str = "/tmp") -> object:
        monkeypatch.setenv("MIMIR_HOME", home)
        monkeypatch.setenv("MIMIR_CLAUDE_OAUTH_CREDENTIALS", "")
        from mimir.config import Config
        return Config.from_env()

    def test_logs_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        cfg = self._cfg(monkeypatch, home=str(tmp_path))
        assert cfg.logs_dir == tmp_path.resolve() / "logs"

    def test_turns_log(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        cfg = self._cfg(monkeypatch, home=str(tmp_path))
        assert cfg.turns_log == tmp_path.resolve() / "logs" / "turns.jsonl"

    def test_events_log(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        cfg = self._cfg(monkeypatch, home=str(tmp_path))
        assert cfg.events_log == tmp_path.resolve() / "logs" / "events.jsonl"

    def test_commitments_log(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """commitments_log lives under ``.mimir/`` (not ``logs/``) so the
        indexer doesn't treat it as searchable content.
        """
        cfg = self._cfg(monkeypatch, home=str(tmp_path))
        assert cfg.commitments_log == tmp_path.resolve() / ".mimir" / "commitments.jsonl"

    def test_sdk_env_overrides_omits_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """sdk_env_overrides only includes non-empty override values."""
        monkeypatch.setenv("MIMIR_HOME", "/tmp")
        monkeypatch.setenv("MIMIR_CLAUDE_OAUTH_CREDENTIALS", "")
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
        monkeypatch.delenv("ANTHROPIC_CUSTOM_MODEL_OPTION", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS", raising=False)
        from mimir.config import Config
        cfg = Config.from_env()
        assert cfg.sdk_env_overrides() == {}

    def test_sdk_env_overrides_includes_set_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MIMIR_HOME", "/tmp")
        monkeypatch.setenv("MIMIR_CLAUDE_OAUTH_CREDENTIALS", "")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example.com")
        monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-4-7")
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_CUSTOM_MODEL_OPTION", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS", raising=False)
        from mimir.config import Config
        cfg = Config.from_env()
        overrides = cfg.sdk_env_overrides()
        assert overrides["ANTHROPIC_BASE_URL"] == "https://proxy.example.com"
        assert overrides["ANTHROPIC_MODEL"] == "claude-opus-4-7"
        assert "ANTHROPIC_AUTH_TOKEN" not in overrides


# ---------------------------------------------------------------------------
# _env_int / _env_float garbage handling (chainlink #259)
# ---------------------------------------------------------------------------

class TestEnvNumericGarbage:
    """Garbage numeric env vars warn + fall back to default rather than
    crashing boot with an opaque ValueError traceback (matches _env_bool)."""

    def test_env_int_garbage_returns_default_and_warns(self, monkeypatch, caplog):
        import logging
        monkeypatch.setenv("MIMIR_TEST_INT_X", "808O")  # typo: letter O
        with caplog.at_level(logging.WARNING):
            assert _env_int("MIMIR_TEST_INT_X", 8080) == 8080
        assert any("not a valid integer" in r.getMessage() for r in caplog.records)

    def test_env_float_garbage_returns_default_and_warns(self, monkeypatch, caplog):
        import logging
        monkeypatch.setenv("MIMIR_TEST_FLOAT_X", "3.1foo")
        with caplog.at_level(logging.WARNING):
            assert _env_float("MIMIR_TEST_FLOAT_X", 2.5) == pytest.approx(2.5)
        assert any("not a valid float" in r.getMessage() for r in caplog.records)
