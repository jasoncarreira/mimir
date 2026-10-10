"""Shared loose bounds for successful asynchronous test operations."""

from __future__ import annotations

import asyncio
from collections.abc import Callable


HANG_GUARD_SECONDS = 30


async def wait_until(predicate: Callable[[], bool]) -> None:
    """Wait for component-owned state without imposing a short polling budget."""
    async def observe() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(observe(), HANG_GUARD_SECONDS)
