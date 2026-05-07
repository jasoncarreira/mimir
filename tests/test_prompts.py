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


# ---- Inbound msg_id surfacing (so <react message="<id>"/> can target it) ----


def test_turn_prompt_includes_inbound_msg_id_in_header():
    """The Current-message header must surface ``msg_id: <id>`` when the
    inbound event carries a source_id, so the agent can target that
    message with ``<react message="<id>"/>`` instead of falling back to
    the just-sent assistant reply (memory/core/40-learned-behaviors.md).
    """
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    event = AgentEvent(
        trigger="user_message",
        channel_id="discord-1",
        content="hi",
        author="discord-99",
        author_display="alice",
        source_id="1234567890",
    )
    prompt = build_turn_prompt(event)
    assert "msg_id: 1234567890" in prompt
    # And it lives in the Current-message metadata bracket, not floating
    # somewhere else in the body.
    header_line = next(
        line for line in prompt.splitlines() if line.startswith("[event_kind:")
    )
    assert "msg_id: 1234567890" in header_line


def test_turn_prompt_omits_msg_id_when_source_id_missing():
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    event = AgentEvent(
        trigger="user_message",
        channel_id="discord-1",
        content="no id",
        author="discord-99",
    )
    prompt = build_turn_prompt(event)
    header_line = next(
        line for line in prompt.splitlines() if line.startswith("[event_kind:")
    )
    assert "msg_id" not in header_line


def test_turn_prompt_scheduled_tick_omits_msg_id():
    """Scheduled ticks have no inbound message — the synthetic header
    must not pretend otherwise."""
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    event = AgentEvent(
        trigger="scheduled_tick",
        channel_id="scheduler:heartbeat",
        content="",
        source_id="should-be-ignored",
    )
    prompt = build_turn_prompt(event)
    assert "msg_id" not in prompt


# ---- saga_session_id surfacing for chainlink #23 #26 (Option P) ----


def test_turn_prompt_includes_saga_session_id_in_user_message_header():
    """chainlink #23 #26 Option P: the Current-message header must
    surface ``saga_session_id: <id>`` so the model can pass it as the
    ``session_id`` arg on saga_query / saga_store / saga_feedback /
    saga_mark_contributions tool calls. Without it the MCP handler's
    ctx-resolution chain can only fall through to single_active or
    missing — fragile under multi-channel concurrency."""
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    event = AgentEvent(
        trigger="user_message",
        channel_id="discord-1",
        content="hi",
        author="discord-99",
        source_id="msg-1",
    )
    prompt = build_turn_prompt(event, saga_session_id="saga-discord-1-abc123")
    header_line = next(
        line for line in prompt.splitlines() if line.startswith("[event_kind:")
    )
    assert "saga_session_id: saga-discord-1-abc123" in header_line


def test_turn_prompt_includes_saga_session_id_in_scheduled_tick_header():
    """Heartbeats and crons fire saga_query / saga_store too — the
    saga_session_id needs to surface for ticks not just user messages
    so heartbeat-driven tool calls can scope correctly."""
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    event = AgentEvent(
        trigger="scheduled_tick",
        channel_id="scheduler:heartbeat",
        content="",
    )
    prompt = build_turn_prompt(event, saga_session_id="saga-scheduler-xyz")
    header_line = next(
        line for line in prompt.splitlines() if line.startswith("[scheduled_tick:")
    )
    assert "saga_session_id: saga-scheduler-xyz" in header_line


def test_turn_prompt_omits_saga_session_id_when_unset():
    """If no saga_session_id is passed (e.g. early bootstrap before
    SAGA registration), the header omits the field rather than
    rendering an empty/None value the model would echo verbatim."""
    from mimir.models import AgentEvent
    from mimir.prompts import build_turn_prompt

    event = AgentEvent(
        trigger="user_message",
        channel_id="discord-1",
        content="hi",
        author="discord-99",
    )
    prompt = build_turn_prompt(event)  # no saga_session_id kwarg
    assert "saga_session_id" not in prompt
