"""Context-preserving submission to explicitly owned worker pools."""

from __future__ import annotations

import asyncio
import contextvars
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Callable, TypeVar

T = TypeVar("T")


async def run_in_pool(
    pool: ThreadPoolExecutor, fn: Callable[..., T], /, *args, **kwargs,
) -> T:
    """Like to_thread, without sharing asyncio's latency-sensitive default pool."""
    call = partial(contextvars.copy_context().run, fn, *args, **kwargs)
    return await asyncio.get_running_loop().run_in_executor(pool, call)
