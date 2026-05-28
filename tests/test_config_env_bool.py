"""Tests for the canonical ``_env_bool`` parser (chainlink #238).

Before this consolidation, ``mimir/config.py`` had three different
boolean parsers in the same file with subtly-different truthy sets —
e.g. ``allow_unauthenticated`` rejected ``on`` while ``_env_bool``
accepted it. These tests pin the uniform behavior so future operator-
facing flags can't drift.
"""
from __future__ import annotations

import logging

import pytest

from mimir.config import Config, _env_bool


# Fields where the production default is True.
_DEFAULT_TRUE_FIELDS = [
    ("MIMIR_CROSS_PLATFORM_PULL", "cross_platform_pull"),
    ("MIMIR_USAGE_BLOCK", "usage_block_enabled"),
    ("MIMIR_CAPTURE_RATE_LIMITS", "capture_rate_limits"),
    ("MIMIR_CONTEXT_1M", "context_1m"),
]

# Fields where the production default is False.
_DEFAULT_FALSE_FIELDS = [
    ("MIMIR_ALLOW_UNAUTHENTICATED", "allow_unauthenticated"),
    ("MIMIR_ONBOARDING_MODE", "onboarding_mode"),
]

_ALL_BOOL_FIELDS = _DEFAULT_TRUE_FIELDS + _DEFAULT_FALSE_FIELDS

# The canonical truthy/falsy alphabet, mixed-case to defend the
# case-insensitivity contract.
_TRUTHY_VALUES = ["1", "true", "TRUE", "True", "yes", "YES", "on", "ON", "y", "Y"]
_FALSY_VALUES = ["0", "false", "FALSE", "no", "NO", "off", "OFF", "n", "N"]


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wipe every MIMIR_* env var the config reads so the test starts
    from a clean defaults state."""
    import os

    for key in list(os.environ.keys()):
        if key.startswith("MIMIR_") or key in {
            "SLACK_BOT_TOKEN",
            "SLACK_APP_TOKEN",
            "BSKY_HANDLE",
            "BSKY_APP_PASSWORD",
        }:
            monkeypatch.delenv(key, raising=False)


class TestEnvBoolDirect:
    """Direct ``_env_bool`` calls — the helper's contract."""

    @pytest.mark.parametrize("value", _TRUTHY_VALUES)
    def test_truthy_values_return_true(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MIMIR_TEST_BOOL", value)
        assert _env_bool("MIMIR_TEST_BOOL", False) is True

    @pytest.mark.parametrize("value", _FALSY_VALUES)
    def test_falsy_values_return_false(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MIMIR_TEST_BOOL", value)
        assert _env_bool("MIMIR_TEST_BOOL", True) is False

    def test_unset_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MIMIR_TEST_BOOL", raising=False)
        assert _env_bool("MIMIR_TEST_BOOL", True) is True
        assert _env_bool("MIMIR_TEST_BOOL", False) is False

    def test_empty_string_returns_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MIMIR_TEST_BOOL", "")
        assert _env_bool("MIMIR_TEST_BOOL", True) is True
        assert _env_bool("MIMIR_TEST_BOOL", False) is False

    def test_whitespace_is_trimmed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MIMIR_TEST_BOOL", "  true  ")
        assert _env_bool("MIMIR_TEST_BOOL", False) is True

    def test_garbage_returns_default_and_warns(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("MIMIR_TEST_BOOL", "maybe")
        with caplog.at_level(logging.WARNING, logger="mimir.config"):
            assert _env_bool("MIMIR_TEST_BOOL", True) is True
            assert _env_bool("MIMIR_TEST_BOOL", False) is False
        # One warning per call.
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 2
        for rec in warnings:
            assert "MIMIR_TEST_BOOL" in rec.getMessage()
            assert "maybe" in rec.getMessage()


class TestConfigBoolFieldsUniform:
    """Every Config bool field reads through ``_env_bool`` — so the
    truthy/falsy alphabet behaves identically regardless of which field
    the operator is configuring. Pre-#238 this was *not* true.
    """

    @pytest.mark.parametrize("env_var, attr", _ALL_BOOL_FIELDS)
    @pytest.mark.parametrize("value", _TRUTHY_VALUES)
    def test_every_field_accepts_every_truthy_value(
        self,
        env_var: str,
        attr: str,
        value: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv(env_var, value)
        cfg = Config.from_env()
        assert getattr(cfg, attr) is True, (
            f"{env_var}={value!r} should set {attr}=True after chainlink #238 "
            f"(pre-fix, {env_var}=on / =y was rejected for fields using inline "
            f"`in {{true,1,yes}}` parsers)."
        )

    @pytest.mark.parametrize("env_var, attr", _ALL_BOOL_FIELDS)
    @pytest.mark.parametrize("value", _FALSY_VALUES)
    def test_every_field_accepts_every_falsy_value(
        self,
        env_var: str,
        attr: str,
        value: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _clear_env(monkeypatch)
        monkeypatch.setenv(env_var, value)
        cfg = Config.from_env()
        assert getattr(cfg, attr) is False

    @pytest.mark.parametrize("env_var, attr", _DEFAULT_TRUE_FIELDS)
    def test_default_true_fields_default_true_when_unset(
        self,
        env_var: str,
        attr: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _clear_env(monkeypatch)
        cfg = Config.from_env()
        assert getattr(cfg, attr) is True

    @pytest.mark.parametrize("env_var, attr", _DEFAULT_FALSE_FIELDS)
    def test_default_false_fields_default_false_when_unset(
        self,
        env_var: str,
        attr: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _clear_env(monkeypatch)
        cfg = Config.from_env()
        assert getattr(cfg, attr) is False

    @pytest.mark.parametrize("env_var, attr", _ALL_BOOL_FIELDS)
    def test_garbage_falls_back_to_default_for_every_field(
        self,
        env_var: str,
        attr: str,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Pre-#238 the falsy-rule fields treated garbage as True (truthy=
        anything-not-falsy). After #238 garbage logs a warning and falls
        back to the documented default."""
        _clear_env(monkeypatch)
        monkeypatch.setenv(env_var, "garbage-not-a-bool")
        expected_default = env_var in {ev for ev, _ in _DEFAULT_TRUE_FIELDS}
        with caplog.at_level(logging.WARNING, logger="mimir.config"):
            cfg = Config.from_env()
        assert getattr(cfg, attr) is expected_default
        assert any(
            env_var in r.getMessage() and "garbage-not-a-bool" in r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING
        ), f"expected a warning citing {env_var} and its bad value"
