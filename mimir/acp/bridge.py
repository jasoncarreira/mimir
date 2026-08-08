from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from mimir.acp.sdk import AgentMessageChunk, TextContentBlock
from mimir.bridges.base import Bridge, SendResult


class ACPBridge(Bridge):
    prefixes: ClassVar[tuple[str, ...]] = ("acp:",)
    name: ClassVar[str] = "acp"

    def __init__(self, publisher: Any | None = None) -> None:
        self._publisher = publisher
        self._publishers: dict[str, Any] = {}
        self._connected = False

    def bind(self, channel_id: str, publisher: Any) -> None:
        if not channel_id.startswith("acp:"):
            raise ValueError("invalid ACP channel")
        self._publishers[channel_id] = publisher

    def unbind(self, channel_id: str, publisher: Any) -> None:
        if self._publishers.get(channel_id) is publisher:
            self._publishers.pop(channel_id, None)

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

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
        del final, reply_to_message_id
        if not self._connected:
            return SendResult(sent=False, error="ACP bridge is disconnected")
        if not channel_id.startswith("acp:") or not isinstance(text, str):
            return SendResult(sent=False, error="invalid ACP channel")
        if attachment_paths or blocks or embed is not None:
            return SendResult(sent=False, error="rich ACP messages are unsupported")
        publisher = self._publishers.get(channel_id, self._publisher)
        if publisher is None:
            return SendResult(sent=False, error="unbound ACP channel")
        update = AgentMessageChunk(
            sessionUpdate="agent_message_chunk",
            content=TextContentBlock(type="text", text=text),
        )
        try:
            await publisher.publish_live(update)
        except Exception:
            return SendResult(sent=False, error="ACP delivery failed")
        return SendResult(sent=True, chunks=1)

    async def react(self, channel_id: str, message_id: str, emoji: str) -> bool:
        del channel_id, message_id, emoji
        return False
