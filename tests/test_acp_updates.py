from __future__ import annotations

import pytest

from mimir.acp.updates import UpdateDispatcher


class Publisher:
    def __init__(self) -> None:
        self.updates = []

    async def publish_live(self, update):
        self.updates.append(update)


@pytest.mark.asyncio
async def test_tool_mapping_redaction_and_todos() -> None:
    publisher = Publisher()
    dispatcher = UpdateDispatcher(publisher)
    dispatcher.enqueue({"type": "reasoning", "phase": "chunk", "text": "private"})
    dispatcher.enqueue({"type": "tool_call", "phase": "start", "id": "1", "tool_name": "write_todos"})
    dispatcher.enqueue({"type": "tool_call", "phase": "end", "id": "1", "tool_name": "write_todos", "args": {"todos": [{"content": "Do it", "status": "pending"}]}, "password": "secret"})
    dispatcher.enqueue({"type": "tool_result", "phase": "end", "id": "1", "tool_name": "write_todos", "status": "ok", "content": {"token": "secret", "ok": True}})
    await dispatcher.drain()
    assert [item.session_update for item in publisher.updates] == ["tool_call", "tool_call_update", "tool_call_update", "plan"]
    assert publisher.updates[2].raw_output == {"token": "[redacted]", "ok": True}
    assert publisher.updates[3].entries[0].priority == "medium"
    await dispatcher.close()


@pytest.mark.asyncio
async def test_send_message_and_partial_todos_are_suppressed() -> None:
    publisher = Publisher()
    dispatcher = UpdateDispatcher(publisher)
    dispatcher.enqueue({"type": "tool_call", "phase": "start", "id": "s", "tool_name": "send_message"})
    dispatcher.enqueue({"type": "tool_call", "phase": "end", "id": "t", "tool_name": "write_todos", "args": {"todos": [{"content": "x"}]}})
    dispatcher.enqueue({"type": "tool_result", "phase": "end", "id": "t", "tool_name": "write_todos", "status": "ok"})
    await dispatcher.drain()
    assert all(getattr(item, "session_update", None) != "plan" for item in publisher.updates)
    await dispatcher.close()


@pytest.mark.asyncio
async def test_orphan_result_synthesizes_pending() -> None:
    publisher = Publisher()
    dispatcher = UpdateDispatcher(publisher)
    dispatcher.enqueue({"type": "tool_result", "phase": "end", "id": "x", "tool_name": "search", "status": "error", "content": "details"})
    await dispatcher.drain()
    assert [item.status for item in publisher.updates] == ["pending", "failed"]
    await dispatcher.close()
