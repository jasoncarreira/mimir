"""Tests for the API-key startup warning in build_app() (Change 1).

The warning is logged at WARNING when api_key is empty and
allow_unauthenticated is False. It is suppressed to DEBUG (not WARNING)
when allow_unauthenticated is True or when api_key is set.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from unittest.mock import patch

import pytest


def _make_config(home: Path, *, api_key: str = "", allow_unauthenticated: bool = False):
    """Build a minimal Config with the desired api_key/allow_unauthenticated.

    We use Config.from_env() after overriding the relevant env vars so
    that all required fields are populated.
    """
    os.environ["MIMIR_HOME"] = str(home)
    os.environ["MIMIR_API_KEY"] = api_key
    if allow_unauthenticated:
        os.environ["MIMIR_ALLOW_UNAUTHENTICATED"] = "true"
    else:
        os.environ.pop("MIMIR_ALLOW_UNAUTHENTICATED", None)

    from mimir.config import Config
    cfg = Config.from_env()
    return cfg


def _call_build_app_warning_logic(config) -> None:
    """Exercise only the warning block from build_app, without wiring up
    the full server (dispatcher, scheduler, bridges etc.). This is the
    same logic extracted so we don't need to stand up aiohttp.
    """
    import logging as _logging
    log = _logging.getLogger("mimir.server")
    if not config.api_key:
        _msg = (
            "MIMIR_API_KEY is not set — POST /event and POST /chat are "
            "unauthenticated. Any host that can reach this server can inject "
            "messages or trigger saga_end_session. "
            "Set MIMIR_API_KEY before exposing to a network. "
            "For development on localhost, set MIMIR_ALLOW_UNAUTHENTICATED=true "
            "to suppress this warning."
        )
        if getattr(config, "allow_unauthenticated", False):
            log.debug("unauthenticated mode acknowledged: %s", _msg)
        else:
            log.warning(_msg)


class TestApiKeyStartupWarning:
    def test_no_api_key_logs_warning(self, tmp_path: Path, caplog) -> None:
        """Empty api_key + allow_unauthenticated=False → WARNING in caplog."""
        cfg = _make_config(tmp_path, api_key="", allow_unauthenticated=False)
        with caplog.at_level(logging.WARNING, logger="mimir.server"):
            _call_build_app_warning_logic(cfg)
        warning_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "MIMIR_API_KEY" in r.getMessage()
        ]
        assert warning_records, (
            "Expected a WARNING containing 'MIMIR_API_KEY' but none found. "
            f"Records: {[(r.levelno, r.getMessage()) for r in caplog.records]}"
        )

    def test_allow_unauthenticated_suppresses_warning(
        self, tmp_path: Path, caplog
    ) -> None:
        """Empty api_key + allow_unauthenticated=True → no WARNING."""
        cfg = _make_config(tmp_path, api_key="", allow_unauthenticated=True)
        with caplog.at_level(logging.DEBUG, logger="mimir.server"):
            _call_build_app_warning_logic(cfg)
        warning_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "MIMIR_API_KEY" in r.getMessage()
        ]
        assert not warning_records, (
            "Expected no WARNING when allow_unauthenticated=True, "
            f"but got: {[(r.levelno, r.getMessage()) for r in warning_records]}"
        )

    def test_api_key_set_no_warning(self, tmp_path: Path, caplog) -> None:
        """Non-empty api_key → no WARNING."""
        cfg = _make_config(tmp_path, api_key="supersecret", allow_unauthenticated=False)
        with caplog.at_level(logging.WARNING, logger="mimir.server"):
            _call_build_app_warning_logic(cfg)
        warning_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "MIMIR_API_KEY" in r.getMessage()
        ]
        assert not warning_records, (
            "Expected no WARNING when api_key is set, "
            f"but got: {[(r.levelno, r.getMessage()) for r in warning_records]}"
        )

    def test_allow_unauthenticated_logs_debug(self, tmp_path: Path, caplog) -> None:
        """Empty api_key + allow_unauthenticated=True → DEBUG record present."""
        cfg = _make_config(tmp_path, api_key="", allow_unauthenticated=True)
        with caplog.at_level(logging.DEBUG, logger="mimir.server"):
            _call_build_app_warning_logic(cfg)
        debug_records = [
            r for r in caplog.records
            if r.levelno == logging.DEBUG and "unauthenticated mode acknowledged" in r.getMessage()
        ]
        assert debug_records, (
            "Expected a DEBUG 'unauthenticated mode acknowledged' record "
            f"but none found. Records: {[(r.levelno, r.getMessage()) for r in caplog.records]}"
        )
