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


# ---- Inbound attachments rendering ---------------------------------------


def test_turn_prompt_renders_inbound_attachments():
    """When the event carries attachment_names (set by bridges that
    download inbound files), the turn prompt body grows an
    ``Attachments:`` block listing each path so the agent can
    ``Read`` them."""
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    event = AgentEvent(
        trigger="user_message",
        channel_id="discord-1",
        content="see attached",
        author="discord-99",
        author_display="alice",
        attachment_names=[
            "/home/mimir/attachments/inbound/discord/1/2-x-report.pdf",
            "/home/mimir/attachments/inbound/discord/1/2-y-chart.png",
        ],
    )
    prompt = build_turn_prompt(event)
    assert "see attached" in prompt
    assert "Attachments:" in prompt
    assert "report.pdf" in prompt
    assert "chart.png" in prompt


def test_turn_prompt_omits_attachments_section_when_empty():
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    event = AgentEvent(
        trigger="user_message",
        channel_id="discord-1",
        content="no files this time",
        author="discord-99",
    )
    prompt = build_turn_prompt(event)
    assert "Attachments:" not in prompt
