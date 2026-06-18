"""Local web-chat bridge (SPEC §7.2.1).

Registers two routes onto the shared aiohttp app:

  POST /chat              — inbound message (JSON body)
  GET  /chat/stream       — outbound SSE stream (one event per ``send`` call)

Inbound payload:
  {
    "channel_id": "web-<id>",       # optional; defaults to "web-default"
    "content":    "the message text",
    "author":     "alice",            # optional
    "msg_id":     "client-side-id"    # optional
  }

The bridge calls ``dispatcher.enqueue(AgentEvent)`` directly — same path as
every other inbound. Outbound SSE clients receive a JSON line per send:

  data: {"channel_id": "web-x", "text": "...", "message_id": "..."}

Channel ids: anything starting with ``web-`` routes here. Default if the
client doesn't specify one is ``web-default`` so a curl-style poke works.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import web

from ..models import AgentEvent
from ..web_contracts import (
    json_error,
    json_success,
    make_chat_message_event,
    make_chat_reaction_event,
)
from .base import Bridge, SendResult

log = logging.getLogger(__name__)

EnqueueFn = Callable[[AgentEvent], Awaitable[bool]]

DEFAULT_CHANNEL = "web-default"
SSE_HEARTBEAT_S = 15.0


@dataclass
class WebChatBridge(Bridge):
    """In-process web bridge that fan-outs sends to all SSE subscribers.

    Args:
        enqueue: dispatcher's enqueue coroutine (set at construction; the
            bridge has no other inbound machinery).
    """

    enqueue: EnqueueFn
    home: Path
    _subscribers: list[asyncio.Queue] = field(default_factory=list, init=False)
    _lock: asyncio.Lock | None = field(default=None, init=False)

    prefixes = ("web-",)
    name = "web"

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        # Drain all subscribers so any open SSE connection sees EOF.
        # CR2 (external I/O) fix: previously this iterated
        # ``list(self._subscribers)`` without acquiring ``self._lock``,
        # while subscribe/unsubscribe DO. A shutdown that races a new
        # SSE client mid-subscribe could ``put_nowait(None)`` on a
        # queue that was just removed, or miss a queue just appended.
        # Acquiring the lock makes the snapshot consistent.
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass

    async def send(
        self,
        channel_id: str,
        text: str,
        attachment_paths: list[Path] | None = None,
        *,
        final: bool = True,
    ) -> SendResult:
        # chainlink #5: ``final`` is informational. The web stub fans
        # each send out as its own SSE payload, so a plan-then-result
        # turn naturally arrives as two events without further
        # gating. No typing-indicator affordance to hold.
        del final
        message_id = uuid.uuid4().hex[:12]
        legacy_payload = {
            "channel_id": channel_id,
            "text": text,
            "message_id": message_id,
            "attachments": [str(p) for p in attachment_paths or []],
        }
        payload = make_chat_message_event(
            channel_id=channel_id,
            text=text,
            message_id=message_id,
            attachments=[str(p) for p in attachment_paths or []],
        )
        payload.update(legacy_payload)
        # Fan out to every connected SSE subscriber. Lazy lock — set once we
        # have an event loop.
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            for q in list(self._subscribers):
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    # Slow consumer; drop silently rather than block sends.
                    pass
        return SendResult(sent=True, message_id=message_id, chunks=1)

    async def react(self, channel_id: str, message_id: str, emoji: str) -> bool:
        # Reactions broadcast as a "react" event over the same SSE stream.
        if self._lock is None:
            self._lock = asyncio.Lock()
        payload = make_chat_reaction_event(
            channel_id=channel_id,
            message_id=message_id,
            emoji=emoji,
        )
        payload["_event"] = "react"
        async with self._lock:
            for q in list(self._subscribers):
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    pass
        return True

    # ---- HTTP route handlers ------------------------------------------

    async def _build_inbound_event(
        self,
        request: web.Request,
    ) -> tuple[AgentEvent | None, str | None, web.Response | None]:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return None, None, web.json_response({"error": "invalid json"}, status=400)
        content = (body.get("content") or "").strip()
        if not content:
            return None, None, web.json_response({"error": "content required"}, status=400)
        channel_id = (body.get("channel_id") or DEFAULT_CHANNEL).strip()
        if not channel_id.startswith("web-"):
            channel_id = "web-" + channel_id
        # #487: type-check, don't coerce — a truthy non-dict ``extra`` survives
        # ``or {}`` and later ``event.extra.get(...)`` raises.
        extra = body.get("extra")
        if extra is not None and not isinstance(extra, dict):
            return None, None, web.json_response(
                {"error": "extra must be an object"}, status=400
            )

        # Trusted attribution (github #726): when the auth middleware resolved a
        # per-user identity, the author comes from the AUTHENTICATED key, not the
        # client body (which is spoofable). The admin master key is NOT a chat
        # identity — reject it so every chat message is attributable to a real
        # person. Dev/open mode (no key configured) keeps the legacy
        # client-asserted author.
        identity = request.get("auth_identity")
        if request.get("auth_is_master"):
            return None, None, web.json_response(
                {
                    "error": "master_key_not_chat_identity",
                    "detail": "the admin master key cannot post chat; use a per-user key",
                },
                status=403,
            )
        if identity is not None:
            author = identity.display_name or identity.canonical
            author_id = identity.canonical
        else:
            author = body.get("author")
            author_id = body.get("author_id")

        event = AgentEvent(
            trigger="user_message",
            channel_id=channel_id,
            content=content,
            author=author,
            author_id=author_id,
            source_id=body.get("msg_id") or uuid.uuid4().hex[:12],
            source="web",
            extra=extra or {},
        )
        return event, channel_id, None

    async def _handle_post(self, request: web.Request) -> web.Response:
        event, channel_id, error = await self._build_inbound_event(request)
        if error is not None:
            return error
        assert event is not None and channel_id is not None
        accepted = await self.enqueue(event)
        if not accepted:
            return web.json_response(
                {"error": "queue_full_or_closed", "channel_id": channel_id},
                status=503,
            )
        return web.json_response({"ok": True, "channel_id": channel_id})

    async def _handle_post_v1(self, request: web.Request) -> web.Response:
        event, channel_id, error = await self._build_inbound_event(request)
        if error is not None:
            try:
                legacy = json.loads(error.text or "{}")
            except json.JSONDecodeError:
                legacy = {"error": error.text or "invalid request"}
            message = str(legacy.get("error") or "invalid request")
            return json_error("bad_request", message, status=error.status)
        assert event is not None and channel_id is not None
        accepted = await self.enqueue(event)
        if not accepted:
            return json_error(
                "queue_full_or_closed",
                "queue full or closed",
                status=503,
                details={"channel_id": channel_id},
            )
        return json_success({"channel_id": channel_id, "source_id": event.source_id})

    async def _handle_stream(self, request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                # No ``Access-Control-Allow-Origin: *`` (was set pre-PR
                # #104). With the auth middleware now gating /chat/stream,
                # a malicious cross-origin page can't open the stream
                # without the key — but a wildcard ACAO would still
                # allow a page that already has the key (e.g. via a
                # phishing prompt that mimics the API-key dialog) to
                # exfiltrate the live agent feed cross-origin. Same-
                # origin only. Operators who want curl access from a
                # different host can still hit the endpoint directly;
                # CORS only restricts browsers.
            },
        )
        await resp.prepare(request)

        # Subscribe — bounded queue so a stuck client doesn't grow unboundedly.
        q: asyncio.Queue = asyncio.Queue(maxsize=128)
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            self._subscribers.append(q)
        try:
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=SSE_HEARTBEAT_S)
                except asyncio.TimeoutError:
                    # Heartbeat keeps proxies from closing the connection.
                    await resp.write(b": heartbeat\n\n")
                    continue
                if item is None:
                    break
                line = "data: " + json.dumps(item, ensure_ascii=False) + "\n\n"
                await resp.write(line.encode("utf-8"))
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            async with self._lock:
                if q in self._subscribers:
                    self._subscribers.remove(q)
        return resp

    def register_routes(self, app: web.Application) -> None:
        """Mount /chat (POST) + /chat/stream (GET) on the shared app."""
        existing = {(r.method, r.resource.canonical) for r in app.router.routes()}
        if ("POST", "/chat") not in existing:
            app.router.add_post("/chat", self._handle_post)
        if ("POST", "/api/v1/chat") not in existing:
            app.router.add_post("/api/v1/chat", self._handle_post_v1)
        if ("GET", "/chat/stream") not in existing:
            app.router.add_get("/chat/stream", self._handle_stream)
