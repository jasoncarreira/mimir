"""Tests for ``saga.async_pool.BoundedAsyncPool`` — the bookkeeping
primitive shared between saga's ``_AsyncClaudePool`` and mimir's
``ClientPool`` (chainlink #46 / Phase 2 of #20).

The pool *policy* (when to grow, when to drain, how to recycle) lives
in each consumer; this primitive is just the bookkeeping skeleton
(max-size validation, lazy condition binding, idle stack). Tests
exercise just that surface so consumer-specific tests stay in their
own files."""

from __future__ import annotations

import asyncio

import pytest

from saga.async_pool import BoundedAsyncPool


def test_validates_max_size_on_construction():
    BoundedAsyncPool(max_size=1)  # OK, the smallest valid cap.
    BoundedAsyncPool(max_size=42)  # Arbitrary larger cap also fine.

    with pytest.raises(ValueError, match=r"max_size must be >= 1"):
        BoundedAsyncPool(max_size=0)
    with pytest.raises(ValueError, match=r"max_size must be >= 1"):
        BoundedAsyncPool(max_size=-3)


def test_max_size_is_read_only_property():
    pool = BoundedAsyncPool[int](max_size=4)
    assert pool.max_size == 4
    # The base class deliberately doesn't expose a setter; the cap is
    # an invariant set at construction. Consumers that need to override
    # in tests reach into ``_max_size`` directly.
    with pytest.raises(AttributeError):
        pool.max_size = 8  # type: ignore[misc]


def test_idle_stack_starts_empty():
    pool = BoundedAsyncPool[int](max_size=4)
    assert pool._idle == []


def test_condition_lazy_bound_no_running_loop_required_at_construction():
    # No event loop is running here (synchronous test). Construction
    # must succeed; the condition only binds on first ``_condition()``
    # call, which is what consumers do under their own ``async with``.
    pool = BoundedAsyncPool[int](max_size=4)
    assert pool._cond is None


@pytest.mark.asyncio
async def test_condition_binds_on_first_use():
    pool = BoundedAsyncPool[int](max_size=4)
    cond = pool._condition()
    assert isinstance(cond, asyncio.Condition)
    # Same instance returned on subsequent calls.
    assert pool._condition() is cond


@pytest.mark.asyncio
async def test_condition_per_instance_not_shared():
    """Two pools must not share a condition — each binds its own to
    the running loop."""
    pool_a = BoundedAsyncPool[int](max_size=4)
    pool_b = BoundedAsyncPool[int](max_size=4)
    assert pool_a._condition() is not pool_b._condition()


@pytest.mark.asyncio
async def test_condition_works_as_async_context_manager():
    """The lazy-bound condition must be usable in ``async with cond:``
    / ``cond.wait()`` / ``cond.notify()`` flows — that's what the
    consumer pools rely on."""
    pool = BoundedAsyncPool[int](max_size=4)
    cond = pool._condition()

    notified = asyncio.Event()

    async def waiter():
        async with cond:
            await cond.wait()
            notified.set()

    task = asyncio.create_task(waiter())
    # Yield once so the waiter reaches cond.wait().
    await asyncio.sleep(0.01)

    async with cond:
        cond.notify()

    await asyncio.wait_for(notified.wait(), timeout=1)
    await task
