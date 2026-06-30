from __future__ import annotations

import asyncio
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
    detail_levels: tuple[tuple[str, str], ...] = (),
    delete_grace: float = 2.0,
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
            "type": "tool_result",
            "phase": "end",
            "turn_id": "t1",
            "channel_id": "slack-C01",
            "id": "c1",
            "tool_name": "shell_exec",
            "content": "TOKEN=abc /secret/path",
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
    panel, bridge = _panel(debounce=60.0, delete_grace=60.0)

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
    assert bridge.deletes == []


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
async def test_tool_result_end_uses_real_tool_name_and_call_row_is_not_completed():
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
            "phase": "end",
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
            "tool_name": "shell_exec",
            "status": "ok",
        }
    )
    await panel.handle_event(
        {"type": "turn", "phase": "end", "turn_id": "t1", "channel_id": "slack-C01"}
    )

    assert bridge.edits[-1].text == "✓ 1 steps"
    assert panel.models["t1"].steps[0].label == "Ran shell_exec"
    rendered = "\n".join(update.text or "" for update in bridge.edits)
    assert "Skill shell_exec" not in rendered
    assert "Ran skill" not in rendered


@pytest.mark.asyncio
async def test_tool_result_name_falls_back_to_shared_span_id():
    panel, _bridge = _panel()

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
            "type": "tool_result",
            "phase": "start",
            "turn_id": "t1",
            "channel_id": "slack-C01",
            "id": "c1",
            "tool_name": "memory_query",
        }
    )
    await panel.handle_event(
        {
            "type": "tool_result",
            "phase": "end",
            "turn_id": "t1",
            "channel_id": "slack-C01",
            "id": "c1",
            "status": "ok",
        }
    )

    assert panel.models["t1"].steps[-1].label == "Ran memory_query"


@pytest.mark.asyncio
async def test_tool_result_without_name_keeps_ran_skill_fallback():
    panel, _bridge = _panel()

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
            "type": "tool_result",
            "phase": "end",
            "turn_id": "t1",
            "channel_id": "slack-C01",
            "id": "c1",
            "status": "ok",
        }
    )

    assert panel.models["t1"].steps[-1].label == "Ran skill"


@pytest.mark.asyncio
async def test_coarse_default_redacts_detail_from_unopted_channel():
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
            "phase": "start",
            "turn_id": "t1",
            "channel_id": "slack-C01",
            "id": "c1",
            "tool_name": "shell_exec",
            "args": {"cmd": "cat /secret/path TOKEN=abc"},
        }
    )

    text = "\n".join(update.text or "" for update in bridge.edits)
    assert "Calling shell_exec" in text
    assert "cat" not in text
    assert "/secret/path" not in text
    assert "TOKEN=abc" not in text


@pytest.mark.asyncio
async def test_detailed_mode_shows_only_inflight_scrubbed_detail_then_drops_it():
    panel, bridge = _panel(detail_levels=(("slack-C01", "detailed"),))

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
            "phase": "start",
            "turn_id": "t1",
            "channel_id": "slack-C01",
            "id": "c1",
            "tool_name": "shell_exec",
            "args": {"cmd": "cat /secret/path TOKEN=abc"},
        }
    )
    live_text = bridge.edits[-1].text or ""
    assert "Calling shell_exec" in live_text
    assert "args: cmd" in live_text
    assert "cat" not in live_text
    assert "/secret/path" not in live_text
    assert "TOKEN=abc" not in live_text

    await panel.handle_event(
        {
            "type": "tool_call",
            "phase": "end",
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
            "phase": "chunk",
            "turn_id": "t1",
            "channel_id": "slack-C01",
            "id": "c1",
            "content_delta": "ok SECRET=value /tmp/result.txt",
        }
    )
    result_text = bridge.edits[-1].text or ""
    assert "Running shell_exec" in result_text
    assert "ok [redacted] [path]" in result_text
    assert "Calling shell_exec" not in result_text
    assert "/tmp/result.txt" not in result_text
    assert "SECRET=value" not in result_text

    await panel.handle_event(
        {
            "type": "tool_result",
            "phase": "end",
            "turn_id": "t1",
            "channel_id": "slack-C01",
            "id": "c1",
            "tool_name": "shell_exec",
            "content": "ok SECRET=value /tmp/result.txt",
        }
    )
    await panel.handle_event(
        {"type": "turn", "phase": "end", "turn_id": "t1", "channel_id": "slack-C01"}
    )
    final_text = bridge.edits[-1].text or ""
    assert final_text == "✓ 1 steps"
    assert "ok [redacted]" not in final_text
    assert "/tmp/result.txt" not in final_text


@pytest.mark.asyncio
async def test_detailed_mode_renders_arg_keys_not_raw_values():
    panel, bridge = _panel(detail_levels=(("slack-C01", "detailed"),))

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
            "phase": "start",
            "turn_id": "t1",
            "channel_id": "slack-C01",
            "id": "c1",
            "tool_name": "shell_exec",
            "args": {
                "cmd": "cat attachments/secret.png",
                "password": "hunter2",
                "Authorization": "Bearer sk-live-secret-value",
                "file_path": "C:\\Users\\Jason\\secret.txt",
            },
        }
    )

    live_text = bridge.edits[-1].text or ""
    assert "args: cmd, password, Authorization, file_path" in live_text
    assert "hunter2" not in live_text
    assert "sk-live-secret-value" not in live_text
    assert "attachments/secret.png" not in live_text
    assert "C:\\Users\\Jason\\secret.txt" not in live_text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content, leaked",
    [
        ('{"password": "hunter2", "path": "attachments/secret.png"}', ["hunter2", "attachments/secret.png"]),
        ("{'API_KEY': 'sk-1234567890', 'file': 'C:\\\\Users\\\\Jason\\\\secret.txt'}", ["sk-1234567890", "C:\\\\Users\\\\Jason\\\\secret.txt"]),
        ("Authorization: Bearer ghp_abcdefghijklmnopqrstuvwxyz123456", ["ghp_abcdefghijklmnopqrstuvwxyz123456"]),
        ("aws AKIA1234567890ABCDEF and relative memory/core/00-identity.md", ["AKIA1234567890ABCDEF", "memory/core/00-identity.md"]),
        ("opaque Aa1234567890Bb1234567890Cc1234567890Dd1234567890", ["Aa1234567890Bb1234567890Cc1234567890Dd1234567890"]),
    ],
)
async def test_detailed_mode_scrubs_realistic_result_secret_and_path_shapes(content: str, leaked: list[str]):
    panel, bridge = _panel(detail_levels=(("slack-C01", "detailed"),))

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
            "phase": "chunk",
            "turn_id": "t1",
            "channel_id": "slack-C01",
            "id": "c1",
            "content_delta": content,
        }
    )

    rendered = bridge.edits[-1].text or ""
    assert "result:" in rendered
    for value in leaked:
        assert value not in rendered


@pytest.mark.asyncio
async def test_panel_auto_deletes_after_real_reply():
    panel, bridge = _panel(delete_grace=0.0)

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
            "type": "outbound_message",
            "phase": "end",
            "turn_id": "t1",
            "channel_id": "slack-C01",
            "sent": True,
        }
    )
    await panel.handle_event(
        {"type": "turn", "phase": "end", "turn_id": "t1", "channel_id": "slack-C01"}
    )
    await asyncio.sleep(0.01)

    assert bridge.edits[-1].text == "✓ Reply posted"
    assert bridge.deletes == [("slack-C01", "panel-1")]


@pytest.mark.asyncio
async def test_panel_delete_failure_leaves_compact_done_state(caplog):
    panel, bridge = _panel(delete_grace=0.0)
    bridge.delete_result = SendResult(sent=False, message_id="panel-1", error="delete unsupported")

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
            "type": "outbound_message",
            "phase": "end",
            "turn_id": "t1",
            "channel_id": "slack-C01",
            "sent": True,
        }
    )
    with caplog.at_level("DEBUG", logger="mimir.bridges._activity_panel"):
        await panel.handle_event(
            {"type": "turn", "phase": "end", "turn_id": "t1", "channel_id": "slack-C01"}
        )
        await asyncio.sleep(0.01)

    assert bridge.edits[-1].text == "✓ Reply posted"
    assert bridge.deletes == [("slack-C01", "panel-1")]
    assert "activity panel delete failed: delete unsupported" in caplog.text


@pytest.mark.asyncio
async def test_panel_does_not_auto_delete_failed_or_no_reply_turns():
    panel, bridge = _panel(delete_grace=0.0)

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
        {"type": "turn", "phase": "end", "turn_id": "t1", "channel_id": "slack-C01"}
    )
    await asyncio.sleep(0)
    assert bridge.deletes == []

    await panel.handle_event(
        {
            "type": "turn",
            "phase": "start",
            "turn_id": "t2",
            "channel_id": "slack-C01",
            "trigger": "user_message",
        }
    )
    await panel.handle_event(
        {
            "type": "outbound_message",
            "phase": "end",
            "turn_id": "t2",
            "channel_id": "slack-C01",
            "sent": True,
        }
    )
    await panel.handle_event(
        {
            "type": "turn",
            "phase": "end",
            "turn_id": "t2",
            "channel_id": "slack-C01",
            "status": "error",
        }
    )
    await asyncio.sleep(0)
    assert bridge.deletes == []


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
