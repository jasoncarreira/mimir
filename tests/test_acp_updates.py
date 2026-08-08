from __future__ import annotations

import asyncio

import pytest

from mimir.acp.sdk import RequestError
from mimir.acp.updates import UpdateDispatcher


class Publisher:
    def __init__(self) -> None:
        self.updates = []
        self.calls = 0
        self.fail_at: int | None = None
        self.block = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def publish_live(self, update):
        self.calls += 1
        self.entered.set()
        if self.block:
            await self.release.wait()
        if self.fail_at == self.calls:
            raise RuntimeError("delivery")
        self.updates.append(update)


async def finish(dispatcher: UpdateDispatcher) -> None:
    await dispatcher.close()


@pytest.mark.asyncio
async def test_complete_suppression_tool_and_redaction_mapping() -> None:
    publisher = Publisher()
    dispatcher = UpdateDispatcher(publisher)
    for kind in ["turn", "reasoning", "model", "outbound_message", "injected_input"]:
        dispatcher.enqueue({"type": kind, "phase": "end", "content": "private"})
    dispatcher.enqueue({"type": "tool_call", "phase": "start", "id": "s", "tool_name": "send_message"})
    dispatcher.enqueue({"type": "tool_call", "phase": "chunk", "id": "1", "tool_name": "search", "args": "delta"})
    dispatcher.enqueue({"type": "tool_result", "phase": "start", "id": "1", "tool_name": "search"})
    dispatcher.enqueue({"type": "tool_result", "phase": "chunk", "id": "1", "tool_name": "search", "content": "delta"})
    dispatcher.enqueue({"type": "tool_call", "phase": "start", "id": "1", "tool_name": "search"})
    dispatcher.enqueue({"type": "tool_call", "phase": "end", "id": "1", "tool_name": "search", "args": {"apiKey": "x", "nested": [{"password": "y", "ok": 1}], "_auth": "private", "nan": float("nan")}})
    dispatcher.enqueue({"type": "tool_result", "phase": "end", "id": "1", "tool_name": "search", "status": "ok", "content": {"accessToken": "z", "result": True}})
    await dispatcher.drain()
    assert [item.session_update for item in publisher.updates] == ["tool_call", "tool_call_update", "tool_call_update"]
    assert [item.status for item in publisher.updates] == ["pending", "in_progress", "completed"]
    assert publisher.updates[1].raw_input == {"apiKey": "[redacted]", "nested": [{"password": "[redacted]", "ok": 1}], "nan": "[redacted]"}
    assert publisher.updates[2].raw_output == {"accessToken": "[redacted]", "result": True}
    await finish(dispatcher)


@pytest.mark.asyncio
async def test_orphan_ends_synthesize_pending_in_order() -> None:
    publisher = Publisher()
    dispatcher = UpdateDispatcher(publisher)
    dispatcher.enqueue({"type": "tool_call", "phase": "end", "id": "a", "tool_name": "edit", "args": {"path": "x"}})
    dispatcher.enqueue({"type": "tool_result", "phase": "end", "id": "b", "tool_name": "search", "status": "error", "content": "details"})
    await dispatcher.drain()
    assert [(item.tool_call_id, item.status) for item in publisher.updates] == [("a", "pending"), ("a", "in_progress"), ("b", "pending"), ("b", "failed")]
    await finish(dispatcher)


@pytest.mark.asyncio
async def test_write_todos_is_one_ordered_full_replacement() -> None:
    publisher = Publisher()
    dispatcher = UpdateDispatcher(publisher)
    todos = [{"content": "First", "status": "in_progress"}, {"content": "Second", "status": "pending"}, {"content": "Third", "status": "completed"}]
    dispatcher.enqueue({"type": "tool_call", "phase": "end", "id": "t", "tool_name": "write_todos", "args": {"todos": todos}})
    dispatcher.enqueue({"type": "tool_result", "phase": "end", "id": "t", "tool_name": "write_todos", "status": "ok", "content": "done"})
    await dispatcher.drain()
    plans = [item for item in publisher.updates if item.session_update == "plan"]
    assert len(plans) == 1
    assert [(entry.content, entry.status, entry.priority) for entry in plans[0].entries] == [("First", "in_progress", "medium"), ("Second", "pending", "medium"), ("Third", "completed", "medium")]
    await finish(dispatcher)


@pytest.mark.parametrize("args,status", [({}, "ok"), ({"todos": []}, "ok"), ({"todos": [{"content": "", "status": "pending"}]}, "ok"), ({"todos": [{"content": "x", "status": "bad"}]}, "ok"), ({"todos": [{"content": "x", "status": "pending", "priority": "high"}]}, "ok"), ({"todos": [{"content": "x", "status": "pending"}], "delta": True}, "ok"), ({"todos": [{"content": "x", "status": "pending"}]}, "error")])
@pytest.mark.asyncio
async def test_invalid_partial_delta_or_failed_todos_emit_no_plan(args, status) -> None:
    publisher = Publisher()
    dispatcher = UpdateDispatcher(publisher)
    dispatcher.enqueue({"type": "tool_call", "phase": "end", "id": "t", "tool_name": "write_todos", "args": args})
    dispatcher.enqueue({"type": "tool_result", "phase": "end", "id": "t", "tool_name": "write_todos", "status": status})
    await dispatcher.drain()
    assert all(item.session_update != "plan" for item in publisher.updates)
    await finish(dispatcher)


@pytest.mark.asyncio
async def test_fifo_pressure_and_drain_wait_for_every_update() -> None:
    publisher = Publisher()
    publisher.block = True
    dispatcher = UpdateDispatcher(publisher)
    for index in range(300):
        dispatcher.enqueue({"type": "tool_call", "phase": "end", "id": str(index), "tool_name": "search", "args": {"index": index}})
    await publisher.entered.wait()
    drain = asyncio.create_task(dispatcher.drain())
    await asyncio.sleep(0)
    assert not drain.done()
    publisher.release.set()
    await drain
    progress = [item.raw_input["index"] for item in publisher.updates if item.status == "in_progress"]
    assert progress == list(range(300))
    await finish(dispatcher)


@pytest.mark.asyncio
async def test_first_delivery_failure_is_retained_and_remaining_queue_is_acked() -> None:
    publisher = Publisher()
    publisher.fail_at = 2
    dispatcher = UpdateDispatcher(publisher)
    for index in range(5):
        dispatcher.enqueue({"type": "tool_call", "phase": "start", "id": str(index), "tool_name": "search"})
    with pytest.raises(RequestError, match="Internal error") as caught:
        await dispatcher.drain()
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert publisher.calls == 2
    assert publisher.updates[0].tool_call_id == "0"
    await finish(dispatcher)


@pytest.mark.asyncio
async def test_terminal_failure_is_recorded_before_nominal_success_queue_drains() -> None:
    publisher = Publisher()
    dispatcher = UpdateDispatcher(publisher)
    dispatcher.enqueue({"type": "tool_call", "phase": "start", "id": "a", "tool_name": "search"})
    dispatcher.enqueue({"type": "tool_call", "phase": "start", "id": "b", "tool_name": "edit"})
    await dispatcher.queue.join()
    dispatcher.enqueue({"type": "tool_result", "phase": "end", "id": "a", "tool_name": "search", "status": "ok", "content": "nominal"})
    dispatcher.enqueue({"type": "tool_result", "phase": "end", "id": "b", "tool_name": "edit", "status": "ok", "content": "nominal"})
    core_error = RuntimeError("core private detail")
    with pytest.raises(RequestError, match="Internal error") as caught:
        await dispatcher.terminalize_failure(core_error)
    assert caught.value.__cause__ is core_error
    assert [(item.tool_call_id, item.status) for item in publisher.updates] == [("a", "pending"), ("b", "pending"), ("a", "failed"), ("b", "failed")]
    assert dispatcher._open_tools == {}
    await finish(dispatcher)


@pytest.mark.asyncio
async def test_terminal_publish_failure_stops_publication_but_closes_all_tools() -> None:
    publisher = Publisher()
    dispatcher = UpdateDispatcher(publisher)
    for tool_id in ["a", "b", "c"]:
        dispatcher.enqueue({"type": "tool_call", "phase": "start", "id": tool_id, "tool_name": "search"})
    await dispatcher.queue.join()
    publisher.fail_at = 4
    first = asyncio.CancelledError()
    with pytest.raises(RequestError) as caught:
        await dispatcher.terminalize_failure(first)
    assert caught.value.__cause__ is first
    assert publisher.calls == 4
    assert dispatcher._open_tools == {}
    assert [(item.tool_call_id, item.status) for item in publisher.updates[-1:]] == [("c", "pending")]
    await finish(dispatcher)
