from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

from mimir.saga import _llm


@pytest.fixture
def claude(monkeypatch):
    state = SimpleNamespace(stage=None, entered=asyncio.Event(), clients=[])

    class AssistantMessage:
        content = [SimpleNamespace(text=" reply ")]

    class Client:
        def __init__(self, **kwargs):
            self.closed = False
            state.clients.append(self)

        async def step(self, stage):
            if state.stage == stage:
                state.entered.set()
                await asyncio.Future()
            if state.stage == stage + "_error":
                raise RuntimeError("broken transport")

        async def connect(self):
            await self.step("connect")

        async def query(self, prompt):
            await self.step("query")

        async def receive_response(self):
            yield AssistantMessage()
            await self.step("response")

        async def disconnect(self):
            self.closed = True
            await self.step("disconnect")

    monkeypatch.setitem(sys.modules, "claude_agent_sdk", SimpleNamespace(
        ClaudeAgentOptions=lambda **kwargs: kwargs,
        ClaudeSDKClient=Client,
        AssistantMessage=AssistantMessage,
    ))
    pool = _llm._AsyncClaudePool(max_size=1, recycle_after=10)
    monkeypatch.setattr(_llm, "_get_async_claude_pool", lambda: pool)
    state.pool = pool
    return state


async def call(timeout=0.02):
    return await _llm.call_llm(
        {"provider": "claude_code", "timeout": timeout}, prompt="hello",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["connect", "query", "response"])
async def test_hung_sdk_times_out_and_restores_capacity(claude, stage):
    claude.stage = stage
    assert await asyncio.wait_for(call(), 1) == ""
    assert claude.entered.is_set()
    assert claude.clients[0].closed
    assert claude.pool.size == 0
    assert claude.pool._idle == []

    claude.stage = None
    assert await asyncio.wait_for(call(), 1) == "reply"
    assert len(claude.clients) == 2
    assert claude.pool.size == 1
    await claude.pool.aclose()


@pytest.mark.asyncio
async def test_acquire_deadline_does_not_discard_borrowed_runner(claude):
    borrowed = await claude.pool.acquire()
    assert await asyncio.wait_for(call(), 1) == ""
    assert claude.pool.size == 1
    assert claude.clients == []
    await claude.pool.release(borrowed)
    assert await asyncio.wait_for(call(), 1) == "reply"
    assert claude.pool._idle == [borrowed]
    await claude.pool.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["connect", "query", "response"])
async def test_cancellation_discards_runner_and_wakes_waiter(claude, stage):
    claude.stage = stage
    task = asyncio.create_task(call(timeout=10))
    await asyncio.wait_for(claude.entered.wait(), 1)
    waiter = asyncio.create_task(claude.pool.acquire())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    replacement = await asyncio.wait_for(waiter, 1)
    assert claude.clients[0].closed
    assert replacement._client is None
    assert claude.pool.size == 1
    await claude.pool.release(replacement)
    claude.stage = None
    assert await call() == "reply"
    await claude.pool.aclose()


@pytest.mark.asyncio
async def test_cancelled_acquire_preserves_capacity(claude):
    borrowed = await claude.pool.acquire()
    task = asyncio.create_task(call(timeout=10))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert claude.pool.size == 1
    await claude.pool.release(borrowed)
    assert await call() == "reply"
    await claude.pool.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["connect_error", "query_error", "response_error"])
async def test_broken_transport_is_not_reused(claude, stage):
    claude.stage = stage
    assert await call() == ""
    assert claude.clients[0].closed
    assert claude.pool.size == 0
    claude.stage = None
    assert await call() == "reply"
    await claude.pool.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_cleanup", [False, True])
async def test_discard_restores_capacity_even_when_close_hangs(claude, cancel_cleanup):
    assert await call() == "reply"
    runner = await claude.pool.acquire()
    claude.stage = "disconnect"
    task = asyncio.create_task(claude.pool.discard(runner, timeout=0.02))
    await asyncio.wait_for(claude.entered.wait(), 1)
    if cancel_cleanup:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await asyncio.wait_for(task, 1)
    assert claude.pool.size == 0
    assert claude.pool._idle == []
    claude.stage = None
    assert await call() == "reply"
    await claude.pool.aclose()
