"""System + turn prompt assembly (mimir/prompts.py).

Phase coverage focuses on v0.4 additions; legacy assembly is exercised
indirectly by agent / dispatcher tests."""

from __future__ import annotations

import os

import pytest

from mimir.config import Config
from mimir.prompts import build_system_prompt


# ---- v0.4 §6: operator alert channel surfacing ---------------------------


def test_system_prompt_includes_operator_alert_channel():
    sp = build_system_prompt(operator_alert_channel="dm-slack-U05ABC")
    assert "## Operator config" in sp
    assert "Operator alert channel: dm-slack-U05ABC" in sp


def test_system_prompt_omits_operator_alert_channel_when_unset():
    sp = build_system_prompt()
    assert "Operator alert channel" not in sp
    assert "## Operator config" not in sp


def test_system_prompt_omits_operator_alert_channel_when_empty():
    sp = build_system_prompt(operator_alert_channel="")
    assert "Operator alert channel" not in sp


# ---- Config env wiring ---------------------------------------------------


def test_config_reads_operator_alert_channel_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MIMIR_OPERATOR_ALERT_CHANNEL", "dm-discord-99")
    cfg = Config.from_env()
    assert cfg.operator_alert_channel == "dm-discord-99"


def test_config_operator_alert_channel_default_empty(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MIMIR_OPERATOR_ALERT_CHANNEL", raising=False)
    cfg = Config.from_env()
    assert cfg.operator_alert_channel == ""
