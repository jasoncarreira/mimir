"""Resend-nudge: forgot-to-send recovery (re-prompt once to call send_message).

Covers the pure helpers (channel gate, nudge text, 24h recidivism counter) and
the agent's ``_maybe_resend_nudge`` decision/recovery logic with a fake graph.
"""
from __future__ import annotations

from datetime import timezone
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from mimir.agent import Agent
from mimir.config import Config
from mimir.models import AgentEvent, AuthContext, InformationFlowLabels, SourceLabel, TurnInteractivity
import json

from mimir.resend_nudge import (
    build_nudge_text,
    channel_prefix_enabled,
    count_recent_no_sends,
    nudge_enabled,
)

UTC = timezone.utc


# ─── pure helpers ──────────────────────────────────────────────────────

def test_nudge_enabled_prefix_star_and_empty():
    assert nudge_enabled("discord-1", ("discord-",)) is True
    assert nudge_enabled("slack-1", ("discord-",)) is False
    assert nudge_enabled("anything", ("*",)) is True
    assert nudge_enabled("discord-1", ()) is False  # default off
    assert nudge_enabled(None, ("*",)) is False


def test_channel_prefix_enabled_prefix_star_and_empty():
    assert channel_prefix_enabled("discord-1", ("discord-",)) is True
    assert channel_prefix_enabled("slack-1", ("discord-",)) is False
    assert channel_prefix_enabled("anything", ("*",)) is True
    assert channel_prefix_enabled("discord-1", ()) is False
    assert channel_prefix_enabled(None, ("*",)) is False


def test_nudge_enabled_web_channels_always_on():
    # Web chat is single-user + interactive, so the nudge is on by DEFAULT for
    # any web-* channel regardless of MIMIR_RESEND_NUDGE_CHANNELS.
    assert nudge_enabled("web-jason", ()) is True
    assert nudge_enabled("web-default", ("discord-",)) is True
    assert nudge_enabled("web-", ()) is True
    # Non-web channels still require the allow-list opt-in.
    assert nudge_enabled("slack-1", ()) is False


def test_build_nudge_text_includes_tally_only_on_repeat():
    first = build_nudge_text("discord-1", 1)
    assert "send_message" in first and "discord-1" in first
    assert "times in the last" not in first  # no tally on the first occurrence
    third = build_nudge_text("discord-1", 3)
    assert "3 times in the last 24 hours" in third


def _write_events(path, rows):
    # events.jsonl is appended chronologically (oldest first); the windowed scan
    # tails it newest-first and breaks at the cutoff.
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_count_recent_no_sends_windowed_and_channel_scoped(tmp_path):
    events = tmp_path / "events.jsonl"
    _write_events(events, [
        # before the cutoff → excluded
        {"timestamp": "2026-06-16T20:00:00+00:00", "type": "interactive_turn_no_send_message", "channel_id": "discord-1"},
        # in window, discord-1 → counted
        {"timestamp": "2026-06-17T10:00:00+00:00", "type": "interactive_turn_no_send_message", "channel_id": "discord-1"},
        # other channel → excluded
        {"timestamp": "2026-06-17T11:00:00+00:00", "type": "interactive_turn_no_send_message", "channel_id": "discord-2"},
        # other type → excluded
        {"timestamp": "2026-06-17T12:00:00+00:00", "type": "send_message_sent", "channel_id": "discord-1"},
        # in window, discord-1 → counted
        {"timestamp": "2026-06-17T13:00:00+00:00", "type": "interactive_turn_no_send_message", "channel_id": "discord-1"},
    ])
    cutoff = "2026-06-17T00:00:00+00:00"
    assert count_recent_no_sends(events, "discord-1", cutoff) == 2
    assert count_recent_no_sends(events, "discord-2", cutoff) == 1
    assert count_recent_no_sends(events, "discord-3", cutoff) == 0


def test_count_recent_no_sends_missing_log_returns_zero(tmp_path):
    assert count_recent_no_sends(tmp_path / "nope.jsonl", "discord-1", "2026-06-17T00:00:00+00:00") == 0


def test_config_resend_nudge_channels_env(monkeypatch):
    monkeypatch.setenv("MIMIR_HOME", "/tmp/resend-nudge-test-home")
    monkeypatch.setenv("MIMIR_RESEND_NUDGE_CHANNELS", "discord-, slack-")
    monkeypatch.delenv("MIMIR_AUTO_DELIVER_FINAL_TEXT_CHANNELS", raising=False)
    cfg = Config.from_env()
    assert cfg.resend_nudge_channels == ("discord-", "slack-")
    monkeypatch.delenv("MIMIR_RESEND_NUDGE_CHANNELS")
    assert Config.from_env().resend_nudge_channels == ()  # default off


def test_config_auto_deliver_final_text_channels_env(monkeypatch):
    monkeypatch.setenv("MIMIR_HOME", "/tmp/auto-deliver-test-home")
    monkeypatch.setenv("MIMIR_AUTO_DELIVER_FINAL_TEXT_CHANNELS", "discord-, slack-")
    cfg = Config.from_env()
    assert cfg.auto_deliver_final_text_channels == ("discord-", "slack-")
    monkeypatch.delenv("MIMIR_AUTO_DELIVER_FINAL_TEXT_CHANNELS")
    assert Config.from_env().auto_deliver_final_text_channels == ()


# ─── _maybe_resend_nudge behavior (fake graph) ─────────────────────────

class _FakeGraph:
    """Stands in for the deepagents graph. Its astream optionally simulates a
    send_message by adding the channel to ctx.delivered_channel_ids."""

    def __init__(self, ctx, channel, *, deliver: bool):
        self._ctx = ctx
        self._channel = channel
        self._deliver = deliver
        self.astream_calls = 0

    async def astream(self, _inp, config=None, context=None, stream_mode=None):
        self.astream_calls += 1
        if self._deliver:
            self._ctx.delivered_channel_ids.add(self._channel)
        yield {"messages": [AIMessage(content="delivered")]}


def _fake_self(tmp_path, channels=("discord-",)):
    return SimpleNamespace(
        _harness_sink_allowed=Agent._harness_sink_allowed,
        _config=SimpleNamespace(
            resend_nudge_channels=channels,
            auto_deliver_final_text_channels=(),
            home=tmp_path,
        )
    )


def _ifc_context(channel="discord-1"):
    return {
        "ifc_labels": InformationFlowLabels(
            labels=frozenset({"private"}),
            source_channels=frozenset({channel}),
            sources=frozenset({SourceLabel(
                principal="user-1", domain="channel", resource_id=channel,
                bridge_instance="discord", sensitivity="private",
                authorized_principals=frozenset({"user-1"}),
            )}),
        ),
        "auth_context": AuthContext(
            principal="discord-U1",
            canonical_principal="user-1",
            roles=(),
            event_ingress=None,
            trigger="user_message",
            channel_id=channel,
            interactivity=TurnInteractivity.INTERACTIVE,
            enforcement_enabled=True,
            domain="channel",
            resource_id=channel,
            bridge_instance="discord",
        ),
    }


def _ctx(channel="discord-1", **kwargs):
    values = {"delivered_channel_ids": set(), **_ifc_context(channel), **kwargs}
    return SimpleNamespace(**values)


def _event(channel="discord-1", trigger="user_message"):
    return AgentEvent(trigger=trigger, channel_id=channel, content="hi", source="discord")


@pytest.fixture
def capture_events(monkeypatch):
    seen: list[str] = []

    async def _fake(name, **kw):
        seen.append(name)

    monkeypatch.setattr("mimir.agent.safe_log_event", _fake)
    return seen


@pytest.mark.asyncio
async def test_noop_when_channel_not_allowlisted(tmp_path):
    ctx = _ctx("slack-1")
    g = _FakeGraph(ctx, "slack-1", deliver=False)
    await Agent._maybe_resend_nudge(
        _fake_self(tmp_path, channels=("discord-",)), g, {}, ctx, _event("slack-1"),
        turn_id="t", turn_is_interactive=True, messages=[], events=[], output="reply",
    )
    assert g.astream_calls == 0  # slack-1 not in allowlist → no re-prompt


@pytest.mark.asyncio
async def test_noop_when_auto_deliver_enabled_for_channel(tmp_path, capture_events):
    ctx = _ctx()
    g = _FakeGraph(ctx, "discord-1", deliver=True)
    fake_self = _fake_self(tmp_path, channels=("discord-",))
    fake_self._config.auto_deliver_final_text_channels = ("discord-",)
    await Agent._maybe_resend_nudge(
        fake_self, g, {}, ctx, _event(),
        turn_id="t", turn_is_interactive=True, messages=[], events=[], output="reply",
    )
    assert g.astream_calls == 0
    assert "resend_nudge_issued" not in capture_events


@pytest.mark.asyncio
async def test_noop_when_already_delivered(tmp_path):
    ctx = _ctx(delivered_channel_ids={"discord-1"})
    g = _FakeGraph(ctx, "discord-1", deliver=False)
    await Agent._maybe_resend_nudge(
        _fake_self(tmp_path), g, {}, ctx, _event(),
        turn_id="t", turn_is_interactive=True, messages=[], events=[], output="reply",
    )
    assert g.astream_calls == 0


@pytest.mark.asyncio
async def test_noop_when_not_interactive_or_no_output(tmp_path):
    ctx = _ctx()
    g = _FakeGraph(ctx, "discord-1", deliver=False)
    # non-interactive
    await Agent._maybe_resend_nudge(
        _fake_self(tmp_path), g, {}, ctx, _event(trigger="scheduled_tick"),
        turn_id="t", turn_is_interactive=False, messages=[], events=[], output="reply",
    )
    # interactive but empty output
    await Agent._maybe_resend_nudge(
        _fake_self(tmp_path), g, {}, ctx, _event(),
        turn_id="t", turn_is_interactive=True, messages=[], events=[], output="   ",
    )
    assert g.astream_calls == 0


@pytest.mark.asyncio
async def test_reprompts_once_and_recovers(tmp_path, capture_events):
    ctx = _ctx()
    g = _FakeGraph(ctx, "discord-1", deliver=True)  # the re-prompt sends
    await Agent._maybe_resend_nudge(
        _fake_self(tmp_path), g, {}, ctx, _event(),
        turn_id="t", turn_is_interactive=True, messages=[], events=[], output="reply",
    )
    assert g.astream_calls == 1  # exactly one re-prompt
    assert "resend_nudge_issued" in capture_events
    assert "resend_nudge_failed" not in capture_events  # recovered
    assert "discord-1" in ctx.delivered_channel_ids


@pytest.mark.asyncio
async def test_reprompts_once_then_gives_up_and_logs_failed(tmp_path, capture_events):
    ctx = _ctx()
    g = _FakeGraph(ctx, "discord-1", deliver=False)  # still doesn't send
    await Agent._maybe_resend_nudge(
        _fake_self(tmp_path), g, {}, ctx, _event(),
        turn_id="t", turn_is_interactive=True, messages=[], events=[], output="reply",
    )
    assert g.astream_calls == 1  # one shot, no recursion
    assert "resend_nudge_issued" in capture_events
    assert "resend_nudge_failed" in capture_events
