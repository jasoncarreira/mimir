from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mimir.bridges._activity_panel import (
    ActivityPanel,
    ActivityPanelModel,
    ActivityStep,
    FoldedInput,
    render_panel_text,
)
from mimir.bridges.base import Bridge, MessageUpdate, SendResult
from mimir.channel_registry import ChannelRegistry
from mimir.turn_event_bus import TurnEventBus


class FakeSlackBridge(Bridge):
    prefixes = ("slack-",)
    name = "slack"

    def __init__(self) -> None:
        self.sends: list[dict[str, Any]] = []
        self.edits: list[MessageUpdate] = []

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def send(
        self,
        channel_id: str,
        text: str,
        attachment_paths: list[Path] | None = None,
        *,
        final: bool = True,
        reply_to_message_id: str | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> SendResult:
        self.sends.append(
            {
                "channel_id": channel_id,
                "text": text,
                "final": final,
                "reply_to_message_id": reply_to_message_id,
                "blocks": blocks,
            }
        )
        return SendResult(sent=True, message_id=f"panel-{len(self.sends)}", chunks=1)

    async def edit_message(
        self,
        channel_id: str,
        message_id: str,
        update: MessageUpdate,
    ) -> SendResult:
        self.edits.append(update)
        return SendResult(sent=True, message_id=message_id, chunks=1)

    async def react(self, channel_id: str, message_id: str, emoji: str) -> bool:
        return False


def _panel(allowlist: tuple[str, ...] = ("slack-",), debounce: float = 0.0):
    bus = TurnEventBus()
    channels = ChannelRegistry()
    bridge = FakeSlackBridge()
    channels.register(bridge)
    return ActivityPanel(bus, channels, allowlist, debounce_seconds=debounce), bridge


@pytest.mark.asyncio
async def test_panel_posts_in_slack_thread_and_renders_sanitized_blocks():
    panel, bridge = _panel()

    await panel.handle_event(
        {
            "type": "turn",
            "phase": "start",
            "turn_id": "t1",
            "channel_id": "slack-C01",
            "thread_ts": "111.222",
        }
    )
    await panel.handle_event(
        {
            "type": "tool_call",
            "phase": "end",
            "turn_id": "t1",
            "channel_id": "slack-C01",
            "id": "c1",
            "tool_name": "shell_exec",
            "args": {"cmd": "cat /secret/path TOKEN=abc"},
        }
    )
    await panel.handle_event(
        {
            "type": "injected_input",
            "phase": "end",
            "turn_id": "t1",
            "channel_id": "slack-C01",
            "count": 1,
            "inputs": [
                {
                    "source_id": "m2",
                    "author": "slack-U2",
                    "author_display": "Jason",
                    "text": "raw follow-up body",
                    "attachment_names": ["/secret/path.png"],
                }
            ],
        }
    )

    assert bridge.sends[0]["reply_to_message_id"] == "111.222"
    assert bridge.sends[0]["blocks"][0]["type"] == "section"
    text = "\n".join(update.text or "" for update in bridge.edits)
    assert "shell_exec" in text
    assert "Jason" in text
    assert "raw follow-up body" not in text
    assert "/secret/path" not in text
    assert "TOKEN=abc" not in text


@pytest.mark.asyncio
async def test_panel_debounces_span_updates_and_finalizes_from_outbound_signal():
    panel, bridge = _panel(debounce=60.0)

    await panel.handle_event(
        {"type": "turn", "phase": "start", "turn_id": "t1", "channel_id": "slack-C01"}
    )
    await panel.handle_event(
        {"type": "reasoning", "phase": "start", "turn_id": "t1", "channel_id": "slack-C01"}
    )
    await panel.handle_event(
        {"type": "tool_call", "phase": "start", "turn_id": "t1", "channel_id": "slack-C01", "tool_name": "send_message"}
    )
    await panel.handle_event(
        {"type": "outbound_message", "phase": "end", "turn_id": "t1", "channel_id": "slack-C01", "sent": True}
    )
    await panel.handle_event(
        {"type": "turn", "phase": "end", "turn_id": "t1", "channel_id": "slack-C01", "status": "ok"}
    )

    assert len(bridge.edits) == 2
    assert bridge.edits[-1].text == "✓ Reply posted"


@pytest.mark.asyncio
async def test_panel_does_not_infer_outbound_from_send_message_tool_args():
    panel, bridge = _panel()

    await panel.handle_event(
        {"type": "turn", "phase": "start", "turn_id": "t1", "channel_id": "slack-C01"}
    )
    await panel.handle_event(
        {
            "type": "tool_call",
            "phase": "end",
            "turn_id": "t1",
            "channel_id": "slack-C01",
            "tool_name": "send_message",
            "args": {"text": "a real reply maybe"},
        }
    )
    await panel.handle_event(
        {"type": "turn", "phase": "end", "turn_id": "t1", "channel_id": "slack-C01", "status": "ok"}
    )

    assert bridge.edits[-1].text != "✓ Reply posted"
    assert "steps" in (bridge.edits[-1].text or "")


@pytest.mark.asyncio
async def test_panel_reconciles_from_turn_end_summary_fields():
    panel, bridge = _panel()

    await panel.handle_event(
        {"type": "turn", "phase": "start", "turn_id": "t1", "channel_id": "slack-C01"}
    )
    await panel.handle_event(
        {
            "type": "turn",
            "phase": "end",
            "turn_id": "t1",
            "channel_id": "slack-C01",
            "status": "ok",
            "outbound_message_sent": False,
            "injected_input_count": 2,
        }
    )

    assert bridge.edits[-1].text == "✓ 1 steps · +2 follow-ups folded"


@pytest.mark.asyncio
async def test_panel_off_by_default_and_channel_allowlist():
    panel, bridge = _panel(allowlist=())
    await panel.handle_event(
        {"type": "turn", "phase": "start", "turn_id": "t1", "channel_id": "slack-C01"}
    )
    assert bridge.sends == []

    panel, bridge = _panel(allowlist=("discord-",))
    await panel.handle_event(
        {"type": "turn", "phase": "start", "turn_id": "t1", "channel_id": "slack-C01"}
    )
    assert bridge.sends == []


def test_render_final_summary_without_outbound_uses_folded_count():
    panel, _bridge = _panel()
    model = panel.models.setdefault(
        "t1",
        ActivityPanelModel(turn_id="t1", channel_id="slack-C01", finalized=True),
    )
    model.steps.append(ActivityStep("Thought", "done"))
    model.folded_inputs.append(FoldedInput(source_id="m1"))

    assert render_panel_text(model) == "✓ 1 steps · +1 follow-up folded"
