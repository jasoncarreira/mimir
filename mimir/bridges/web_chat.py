"""Local web-chat bridge (SPEC §7.2.1).

Registers two routes onto the shared aiohttp app:

  POST /chat              — inbound message (JSON body)
  GET  /chat/stream       — outbound SSE stream (one event per ``send`` call)

Inbound payload:
  {
    "content":    "the message text",
    "msg_id":     "client-side-id"    # optional
  }

The bridge calls ``dispatcher.enqueue(AgentEvent)`` directly — same path as
every other inbound. Outbound SSE clients receive a JSON line per send:

  data: {"channel_id": "web-x", "text": "...", "message_id": "..."}

Channel ids: anything starting with ``web-`` routes here. Chat endpoints
require a resolved per-user web identity and derive the channel from it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import web

from ..models import AgentEvent
from ..web_channels import DEFAULT_WEB_CHANNEL, web_channel_for_identity
from ..web_contracts import (
    json_error,
    json_success,
    make_chat_message_event,
    make_chat_reaction_event,
)
from .base import Bridge, SendResult

log = logging.getLogger(__name__)

EnqueueFn = Callable[[AgentEvent], Awaitable[bool]]

DEFAULT_CHANNEL = DEFAULT_WEB_CHANNEL
CHAT_STREAM_MAX_SUBSCRIBERS = int(os.environ.get("MIMIR_CHAT_STREAM_MAX_SUBSCRIBERS", "8"))


@dataclass
class _Subscriber:
    queue: asyncio.Queue
    allowed_channels: frozenset[str] | None = None


def _web_channel_for(canonical: str) -> str:
    """Per-user web channel id derived from an identity canonical.

    Authenticated users' default-channel traffic routes to ``web-<canonical>``
    so a user's conversation — their messages plus the agent's replies (which go
    to the turn's channel) — segregates per user and history is scoped
    naturally. The canonical is the unique per-user matching key, so normal
    canonicals are used VERBATIM: slugifying them (lower-casing / collapsing
    punctuation) could map two distinct canonicals onto one channel and leak
    history across users. Reserved channel collisions are escaped by the shared
    web-channel helper. Channel ids are arbitrary strings here (cf. ``slack-U…``
    / ``discord-…``); downstream path derivation sanitizes them where needed
    (e.g. saga session ids).
    """
    return web_channel_for_identity(canonical)


def _chat_identity(request: web.Request):
    """Return the resolved per-user chat identity or a route-level auth error.

    The global web auth gate intentionally stays open for non-chat routes in
    dev/open mode, but chat is per-user state and must never fall back to an
    anonymous shared channel.
    """
    if request.get("auth_is_master"):
        return None, web.json_response(
            {
                "error": "master_key_not_chat_identity",
                "detail": "the admin master key cannot use chat; use a per-user key",
            },
            status=403,
        )
    identity = request.get("auth_identity")
    if identity is None:
        return None, web.json_response({"error": "chat_login_required"}, status=401)
    return identity, None


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
    _subscribers: list[_Subscriber] = field(default_factory=list, init=False)
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
        for subscriber in subscribers:
            try:
                subscriber.queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

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
        # chainlink #5: ``final`` is informational. The web stub fans
        # each send out as its own SSE payload, so a plan-then-result
        # turn naturally arrives as two events without further
        # gating. No typing-indicator affordance to hold.
        del final, reply_to_message_id, blocks
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
            for subscriber in list(self._subscribers):
                if (
                    subscriber.allowed_channels is not None
                    and channel_id not in subscriber.allowed_channels
                ):
                    continue
                try:
                    subscriber.queue.put_nowait(payload)
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
            for subscriber in list(self._subscribers):
                if (
                    subscriber.allowed_channels is not None
                    and channel_id not in subscriber.allowed_channels
                ):
                    continue
                try:
                    subscriber.queue.put_nowait(payload)
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
        identity, auth_error = _chat_identity(request)
        if auth_error is not None:
            return None, None, auth_error
        assert identity is not None
        channel_id = _web_channel_for(identity.canonical)
        # #487: type-check, don't coerce — a truthy non-dict ``extra`` survives
        # ``or {}`` and later ``event.extra.get(...)`` raises.
        extra = body.get("extra")
        if extra is not None and not isinstance(extra, dict):
            return None, None, web.json_response(
                {"error": "extra must be an object"}, status=400
            )

        # Trusted attribution (github #726): the author comes from the
        # AUTHENTICATED per-user key, not the client body (which is spoofable).
        # The admin master key is not a chat identity, and dev/open anonymous
        # chat is rejected above, so no shared web-default channel is reachable.
        author = identity.display_name or identity.canonical
        author_id = identity.canonical

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
            code = message if error.status in (401, 403) else "bad_request"
            return json_error(code, message, status=error.status)
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

    def _allowed_stream_channels(self, request: web.Request) -> frozenset[str]:
        identity = request.get("auth_identity")
        assert identity is not None
        return frozenset({_web_channel_for(identity.canonical)})

    async def _handle_stream(self, request: web.Request) -> web.StreamResponse:
        _, auth_error = _chat_identity(request)
        if auth_error is not None:
            return auth_error

        if self._lock is None:
            self._lock = asyncio.Lock()

        # Subscribe before prepare to reserve capacity under the lock. Otherwise
        # concurrent connection attempts could all pass a separate len() check
        # before any of them appended, overshooting the cap.
        q: asyncio.Queue = asyncio.Queue(maxsize=128)
        subscriber = _Subscriber(q, self._allowed_stream_channels(request))
        async with self._lock:
            if len(self._subscribers) >= CHAT_STREAM_MAX_SUBSCRIBERS:
                return web.Response(text="too many chat streams", status=429)
            self._subscribers.append(subscriber)

        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                # Ask nginx-style reverse proxies not to buffer the SSE stream.
                "X-Accel-Buffering": "no",
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
        try:
            await resp.prepare(request)
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
                if subscriber in self._subscribers:
                    self._subscribers.remove(subscriber)
        return resp

    async def _handle_history(self, request: web.Request) -> web.Response:
        """GET /api/v1/chat/history?channel_id=web-default&limit=50.

        Restore the authenticated user's prior conversation (user + assistant
        messages, oldest→newest) so re-opening the chat reloads it instead of
        starting empty. The channel is derived from auth identity; client
        channel_id query parameters are ignored.
        """
        from ..history import get_global_buffer

        identity, auth_error = _chat_identity(request)
        if auth_error is not None:
            if auth_error.status == 401:
                return json_error("chat_login_required", "chat login required", status=401)
            return json_error(
                "master_key_not_chat_identity",
                "the admin master key cannot use chat; use a per-user key",
                status=403,
            )
        assert identity is not None
        channel_id = _web_channel_for(identity.canonical)
        try:
            limit = int(request.query.get("limit", "50"))
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 200))

        buffer = get_global_buffer()
        records = buffer.recent_in_channel(channel_id, limit) if buffer is not None else []
        messages = [
            {
                "message_id": m.msg_id or "",
                "role": "assistant" if m.kind == "assistant_message" else "user",
                "channel_id": m.channel_id,
                "author": m.author_display or m.author,
                "text": m.content,
                "ts": m.ts,
            }
            for m in records
            if m.kind in ("user_message", "assistant_message")
        ]
        return json_success({"channel_id": channel_id, "messages": messages})

    def register_routes(self, app: web.Application) -> None:
        """Mount /chat (POST) + /chat/stream (GET) + history on the shared app."""
        existing = {(r.method, r.resource.canonical) for r in app.router.routes()}
        if ("POST", "/chat") not in existing:
            app.router.add_post("/chat", self._handle_post)
        if ("POST", "/api/v1/chat") not in existing:
            app.router.add_post("/api/v1/chat", self._handle_post_v1)
        if ("GET", "/api/v1/chat/history") not in existing:
            app.router.add_get("/api/v1/chat/history", self._handle_history)
        if ("GET", "/chat/stream") not in existing:
            app.router.add_get("/chat/stream", self._handle_stream)
