"""Shared shutdown signal for long-lived HTTP streams."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from aiohttp import web


@dataclass
class HTTPShutdown:
    event: asyncio.Event = field(default_factory=asyncio.Event)
    active_streams: int = 0
    closing_streams: int = 0
    signalled_at: float | None = None


HTTP_SHUTDOWN: web.AppKey[HTTPShutdown] = web.AppKey("http_shutdown", HTTPShutdown)


async def queue_or_shutdown(queue: asyncio.Queue, shutdown: HTTPShutdown, heartbeat: float):
    """Wait for an item, heartbeat, or shutdown; prefer shutdown over a queued item."""
    if shutdown.event.is_set():
        return None
    get = asyncio.create_task(queue.get())
    closing = asyncio.create_task(shutdown.event.wait())
    try:
        done, _ = await asyncio.wait(
            (get, closing), timeout=heartbeat, return_when=asyncio.FIRST_COMPLETED,
        )
        if closing in done or shutdown.event.is_set():
            return None
        if get in done:
            return get.result()
        raise asyncio.TimeoutError
    finally:
        for task in (get, closing):
            if not task.done():
                task.cancel()
        await asyncio.gather(get, closing, return_exceptions=True)
