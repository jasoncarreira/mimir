from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mimir.bridges._activity_panel import (
    ActivityPanel,
    ActivityPanelModel,
    ActivityStep,
    FoldedInput,
    render_discord_panel,
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
        self.deletes: list[tuple[str, str]] = []
        self.delete_result = SendResult(sent=True, message_id="deleted", chunks=1)

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

    async def delete_message(self, channel_id: str, message_id: str) -> SendResult:
        self.deletes.append((channel_id, message_id))
        return self.delete_result

    async def react(self, channel_id: str, message_id: str, emoji: str) -> bool:
        return False


class FakeDiscordBridge(Bridge):
    prefixes = ("discord-",)
    name = "discord"

    def __init__(self) -> None:
        self.sends: list[dict[str, Any]] = []
        self.edits: list[MessageUpdate] = []
        self.deletes: list[tuple[str, str]] = []

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
        embed: Any | None = None,
    ) -> SendResult:
        self.sends.append(
            {
                "channel_id": channel_id,
                "text": text,
                "final": final,
                "reply_to_message_id": reply_to_message_id,
                "blocks": blocks,
                "embed": embed,
            }
        )
        return SendResult(sent=True, message_id=f"discord-panel-{len(self.sends)}", chunks=1)

    async def edit_message(
        self,
        channel_id: str,
        message_id: str,
        update: MessageUpdate,
    ) -> SendResult:
        self.edits.append(update)
        return SendResult(sent=True, message_id=message_id, chunks=1)

    async def delete_message(self, channel_id: str, message_id: str) -> SendResult:
        self.deletes.append((channel_id, message_id))
        return SendResult(sent=True, message_id=message_id, chunks=1)

    async def react(self, channel_id: str, message_id: str, emoji: str) -> bool:
        return False


def _panel(
    allowlist: tuple[str, ...] = ("slack-",),
    debounce: float = 0.0,
    detail_levels: tuple[str, ...] = (),
    delete_grace: float = 0.0,
):
    bus = TurnEventBus()
    channels = ChannelRegistry()
    bridge = FakeSlackBridge()
    channels.register(bridge)
    return ActivityPanel(
        bus,
        channels,
        allowlist,
        debounce_seconds=debounce,
        detail_levels=detail_levels,
        delete_grace_seconds=delete_grace,
    ), bridge


def _discord_panel(allowlist: tuple[str, ...] = ("discord-",), debounce: float = 0.0):
    bus = TurnEventBus()
    channels = ChannelRegistry()
    bridge = FakeDiscordBridge()
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
            "trigger": "user_message",
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
        {
            "type": "turn",
            "phase": "start",
            "turn_id": "t1",
            "channel_id": "slack-C01",
            "trigger": "user_message",
        }
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
async def test_panel_first_edit_does_not_depend_on_monotonic_clock_base(monkeypatch):
    panel, bridge = _panel(debounce=60.0)
    loop = __import__("asyncio").get_running_loop()
    monkeypatch.setattr(loop, "time", lambda: 0.1)

    await panel.handle_event(
        {
            "type": "turn",
            "phase": "start",
            "turn_id": "t1",
            "channel_id": "slack-C01",
            "trigger": "user_message",
        }
    )
    await panel.handle_event(
        {"type": "reasoning", "phase": "start", "turn_id": "t1", "channel_id": "slack-C01"}
    )
    await panel.handle_event(
        {"type": "turn", "phase": "end", "turn_id": "t1", "channel_id": "slack-C01", "status": "ok"}
    )

    assert len(bridge.edits) == 2
    assert bridge.edits[0].text == "*Working*\n◌ Thought"
    assert bridge.edits[-1].text == "✓ 1 steps"


@pytest.mark.asyncio
async def test_panel_does_not_infer_outbound_from_send_message_tool_args():
    panel, bridge = _panel()

    await panel.handle_event(
        {
            "type": "turn",
            "phase": "start",
            "turn_id": "t1",
            "channel_id": "slack-C01",
            "trigger": "user_message",
        }
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
        {
            "type": "turn",
            "phase": "start",
            "turn_id": "t1",
            "channel_id": "slack-C01",
            "trigger": "user_message",
        }
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
@pytest.mark.parametrize(
    "trigger",
    ["user_message", "poller", "scheduled_tick", "shell_job_complete"],
)
async def test_panel_posts_for_classified_work_triggers(trigger: str):
    panel, bridge = _panel()

    await panel.handle_event(
        {
            "type": "turn",
            "phase": "start",
            "turn_id": f"t-{trigger}",
            "channel_id": "slack-C01",
            "trigger": trigger,
        }
    )

    assert len(bridge.sends) == 1
    assert f"t-{trigger}" in panel.models


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "trigger",
    [
        "saga_session_end",
        "upgrade",
        "claude_code_spawn",
        "react_received",
        "reflect",
        "unknown",
        "new_framework_trigger",
    ],
)
async def test_panel_skips_internal_and_unclassified_triggers_without_model(trigger: str):
    panel, bridge = _panel()
    turn_id = f"t-{trigger}"

    await panel.handle_event(
        {
            "type": "turn",
            "phase": "start",
            "turn_id": turn_id,
            "channel_id": "slack-C01",
            "trigger": trigger,
        }
    )
    await panel.handle_event(
        {
            "type": "tool_call",
            "phase": "end",
            "turn_id": turn_id,
            "channel_id": "slack-C01",
            "tool_name": "shell_exec",
        }
    )
    await panel.handle_event(
        {
            "type": "turn",
            "phase": "end",
            "turn_id": turn_id,
            "channel_id": "slack-C01",
            "status": "ok",
        }
    )

    assert bridge.sends == []
    assert bridge.edits == []
    assert turn_id not in panel.models


@pytest.mark.asyncio
async def test_panel_skips_start_event_without_trigger_metadata():
    panel, bridge = _panel()

    await panel.handle_event(
        {"type": "turn", "phase": "start", "turn_id": "t-missing", "channel_id": "slack-C01"}
    )

    assert bridge.sends == []
    assert "t-missing" not in panel.models


@pytest.mark.asyncio
async def test_panel_off_by_default_and_channel_allowlist():
    panel, bridge = _panel(allowlist=())
    await panel.handle_event(
        {
            "type": "turn",
            "phase": "start",
            "turn_id": "t1",
            "channel_id": "slack-C01",
            "trigger": "user_message",
        }
    )
    assert bridge.sends == []

    panel, bridge = _panel(allowlist=("discord-",))
    await panel.handle_event(
        {
            "type": "turn",
            "phase": "start",
            "turn_id": "t1",
            "channel_id": "slack-C01",
            "trigger": "user_message",
        }
    )
    assert bridge.sends == []


@pytest.mark.asyncio
async def test_discord_panel_uses_embed_renderer_and_shared_lifecycle():
    panel, bridge = _discord_panel(debounce=60.0)

    await panel.handle_event(
        {
            "type": "turn",
            "phase": "start",
            "turn_id": "t1",
            "channel_id": "discord-101",
            "trigger": "user_message",
            "reply_to_message_id": "555",
        }
    )
    await panel.handle_event(
        {
            "type": "tool_call",
            "phase": "start",
            "turn_id": "t1",
            "channel_id": "discord-101",
            "tool_name": "shell_exec",
            "args": {"cmd": "cat /secret/path TOKEN=abc"},
        }
    )
    await panel.handle_event(
        {
            "type": "tool_result",
            "phase": "end",
            "turn_id": "t1",
            "channel_id": "discord-101",
            "tool_name": "shell_exec",
            "result": "TOKEN=abc /secret/path",
        }
    )
    await panel.handle_event(
        {"type": "turn", "phase": "end", "turn_id": "t1", "channel_id": "discord-101"}
    )

    assert bridge.sends[0]["text"] == ""
    assert bridge.sends[0]["reply_to_message_id"] == "555"
    assert bridge.sends[0]["embed"]["title"] == "Working"
    assert bridge.sends[0]["embed"]["description"] == "[ ] Working"
    assert bridge.sends[0]["blocks"] is None

    assert len(bridge.edits) == 2
    live_embed = bridge.edits[0].embed
    assert live_embed["title"] == "Working"
    assert "[ ] Calling shell_exec" in live_embed["description"]

    final_embed = bridge.edits[-1].embed
    assert final_embed["title"] == "Done"
    assert final_embed["description"] == "Done 1 steps"
    rendered = "\n".join(str(update.embed) for update in bridge.edits)
    assert "shell_exec" in rendered
    assert "TOKEN=abc" not in rendered
    assert "/secret/path" not in rendered


@pytest.mark.asyncio
async def test_discord_panel_inert_when_channel_not_allowlisted():
    panel, bridge = _discord_panel(allowlist=("slack-",))

    await panel.handle_event(
        {
            "type": "turn",
            "phase": "start",
            "turn_id": "t1",
            "channel_id": "discord-101",
            "trigger": "user_message",
        }
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


def test_render_discord_final_reply_posted_title_and_description():
    model = ActivityPanelModel(
        turn_id="t1",
        channel_id="discord-101",
        finalized=True,
        outbound_message_sent=True,
    )

    text, embed = render_discord_panel(model)

    assert text == ""
    assert embed["title"] == "Done"
    assert embed["description"] == "Done Reply posted"


@pytest.mark.asyncio
async def test_completed_tool_result_keeps_real_tool_name_without_redundant_call_row():
    panel, bridge = _panel()

    await panel.handle_event(
        {"type": "turn", "phase": "start", "turn_id": "t1", "channel_id": "slack-C01", "trigger": "user_message"}
    )
    await panel.handle_event(
        {
            "type": "tool_call",
            "phase": "start",
            "turn_id": "t1",
            "channel_id": "slack-C01",
            "id": "c1",
            "tool_name": "shell_exec",
        }
    )
    await panel.handle_event(
        {
            "type": "tool_result",
            "phase": "start",
            "turn_id": "t1",
            "channel_id": "slack-C01",
            "id": "c1",
            "tool_name": "shell_exec",
        }
    )
    await panel.handle_event(
        {
            "type": "tool_result",
            "phase": "end",
            "turn_id": "t1",
            "channel_id": "slack-C01",
            "id": "c1",
        }
    )
    await panel.handle_event(
        {"type": "turn", "phase": "end", "turn_id": "t1", "channel_id": "slack-C01", "status": "ok"}
    )

    final = bridge.edits[-1].text or ""
    model = panel.models["t1"]
    assert [step.label for step in model.steps] == ["Ran shell_exec"]
    assert "Ran skill" not in final
    assert "Skill shell_exec" not in "\n".join(update.text or "" for update in bridge.edits)


@pytest.mark.asyncio
async def test_tool_result_without_name_uses_ran_skill_fallback():
    panel, _bridge = _panel()

    await panel.handle_event(
        {"type": "turn", "phase": "start", "turn_id": "t1", "channel_id": "slack-C01", "trigger": "user_message"}
    )
    await panel.handle_event(
        {"type": "tool_result", "phase": "end", "turn_id": "t1", "channel_id": "slack-C01", "id": "c1"}
    )

    assert panel.models["t1"].steps[-1].label == "Ran skill"


@pytest.mark.asyncio
async def test_detailed_mode_shows_only_current_sanitized_detail_and_finalizes_compact():
    panel, bridge = _panel(detail_levels=("slack-C01:detailed",))

    await panel.handle_event(
        {"type": "turn", "phase": "start", "turn_id": "t1", "channel_id": "slack-C01", "trigger": "user_message"}
    )
    await panel.handle_event(
        {
            "type": "tool_call",
            "phase": "start",
            "turn_id": "t1",
            "channel_id": "slack-C01",
            "id": "c1",
            "tool_name": "shell_exec",
        }
    )
    await panel.handle_event(
        {
            "type": "tool_call",
            "phase": "chunk",
            "turn_id": "t1",
            "channel_id": "slack-C01",
            "id": "c1",
            "args_delta": {
                "cmd": "echo hi",
                "file_path": "/secret/path.txt",
                "token": "TOKEN=abc123",
                "text": "full inbound message body",
            },
        }
    )
    live = bridge.edits[-1].text or ""
    assert "args:" in live
    assert "echo hi" in live
    assert "/secret/path" not in live
    assert "TOKEN=abc123" not in live
    assert "full inbound message body" not in live

    await panel.handle_event(
        {
            "type": "tool_result",
            "phase": "start",
            "turn_id": "t1",
            "channel_id": "slack-C01",
            "id": "c1",
            "tool_name": "shell_exec",
        }
    )
    after_next = bridge.edits[-1].text or ""
    assert "echo hi" not in after_next
    assert "◌ Ran shell_exec" in after_next

    await panel.handle_event(
        {
            "type": "tool_result",
            "phase": "chunk",
            "turn_id": "t1",
            "channel_id": "slack-C01",
            "id": "c1",
            "content_delta": "ok SECRET=hidden " + ("x" * 500),
        }
    )
    result_live = bridge.edits[-1].text or ""
    assert "result:" in result_live
    assert "SECRET=hidden" not in result_live
    assert len(result_live) < 520

    await panel.handle_event(
        {
            "type": "tool_result",
            "phase": "end",
            "turn_id": "t1",
            "channel_id": "slack-C01",
            "id": "c1",
            "tool_name": "shell_exec",
            "status": "ok",
            "content": "ok",
        }
    )
    completed = bridge.edits[-1].text or ""
    assert "result:" not in completed

    await panel.handle_event(
        {"type": "turn", "phase": "end", "turn_id": "t1", "channel_id": "slack-C01", "status": "ok"}
    )
    assert bridge.edits[-1].text == "✓ 1 steps"


@pytest.mark.asyncio
async def test_coarse_mode_default_does_not_render_detail():
    panel, bridge = _panel()

    await panel.handle_event(
        {"type": "turn", "phase": "start", "turn_id": "t1", "channel_id": "slack-C01", "trigger": "user_message"}
    )
    await panel.handle_event(
        {
            "type": "tool_call",
            "phase": "end",
            "turn_id": "t1",
            "channel_id": "slack-C01",
            "id": "c1",
            "tool_name": "shell_exec",
            "args": {"cmd": "cat /secret/path", "token": "TOKEN=abc"},
        }
    )

    rendered = "\n".join(update.text or "" for update in bridge.edits)
    assert "shell_exec" in rendered
    assert "cat /secret/path" not in rendered
    assert "TOKEN=abc" not in rendered


@pytest.mark.asyncio
async def test_auto_delete_after_reply_success():
    panel, bridge = _panel(delete_grace=0.0)

    await panel.handle_event(
        {"type": "turn", "phase": "start", "turn_id": "t1", "channel_id": "slack-C01", "trigger": "user_message"}
    )
    await panel.handle_event(
        {"type": "outbound_message", "phase": "end", "turn_id": "t1", "channel_id": "slack-C01", "sent": True}
    )
    await panel.handle_event(
        {"type": "turn", "phase": "end", "turn_id": "t1", "channel_id": "slack-C01", "status": "ok"}
    )
    await __import__("asyncio").sleep(0)

    assert bridge.edits[-1].text == "✓ Reply posted"
    assert bridge.deletes == [("slack-C01", "panel-1")]


@pytest.mark.asyncio
async def test_auto_delete_failure_leaves_compact_done_state():
    panel, bridge = _panel(delete_grace=0.0)
    bridge.delete_result = SendResult(sent=False, message_id="panel-1", error="no scope")

    await panel.handle_event(
        {"type": "turn", "phase": "start", "turn_id": "t1", "channel_id": "slack-C01", "trigger": "user_message"}
    )
    await panel.handle_event(
        {"type": "outbound_message", "phase": "end", "turn_id": "t1", "channel_id": "slack-C01", "sent": True}
    )
    await panel.handle_event(
        {"type": "turn", "phase": "end", "turn_id": "t1", "channel_id": "slack-C01", "status": "ok"}
    )
    await __import__("asyncio").sleep(0)

    assert bridge.edits[-1].text == "✓ Reply posted"
    assert bridge.deletes == [("slack-C01", "panel-1")]


@pytest.mark.asyncio
async def test_auto_delete_skipped_for_failed_or_no_reply_turns():
    panel, bridge = _panel(delete_grace=0.0)

    await panel.handle_event(
        {"type": "turn", "phase": "start", "turn_id": "t1", "channel_id": "slack-C01", "trigger": "user_message"}
    )
    await panel.handle_event(
        {"type": "outbound_message", "phase": "end", "turn_id": "t1", "channel_id": "slack-C01", "sent": True}
    )
    await panel.handle_event(
        {"type": "turn", "phase": "end", "turn_id": "t1", "channel_id": "slack-C01", "status": "error"}
    )
    await __import__("asyncio").sleep(0)

    await panel.handle_event(
        {"type": "turn", "phase": "start", "turn_id": "t2", "channel_id": "slack-C01", "trigger": "user_message"}
    )
    await panel.handle_event(
        {"type": "turn", "phase": "end", "turn_id": "t2", "channel_id": "slack-C01", "status": "ok"}
    )
    await __import__("asyncio").sleep(0)

    assert bridge.deletes == []
