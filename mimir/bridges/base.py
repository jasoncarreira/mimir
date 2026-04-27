"""Bridge ABC (SPEC §7.2.1).

Each bridge owns a long-lived connection to one channel source (Slack,
Discord, Bluesky DM, Web UI, benchmark stdout) and exposes a uniform
``send`` / ``react`` surface. Inbound is bridge-driven: when a real message
arrives the bridge constructs an ``AgentEvent`` and calls
``dispatcher.enqueue(event)`` directly — no return-from-callback contract.

Bridges run as asyncio coroutines inside the mimir process. Each bridge is
opt-in via env config (DISCORD_TOKEN, SLACK_BOT_TOKEN, BSKY_HANDLE...) so a
deployment that only needs the benchmark bridge doesn't pay any startup
cost for absent libraries.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar


@dataclass
class SendResult:
    sent: bool
    message_id: str | None = None
    chunks: int = 0
    error: str | None = None


class Bridge(ABC):
    """Single-source bridge between mimir and one channel backend."""

    # Channel-id prefix(es) this bridge claims (e.g. "slack-", "dm-slack-").
    # The ChannelRegistry routes outbound calls based on these prefixes.
    prefixes: ClassVar[tuple[str, ...]] = ()

    # Short identifier for logs ("slack", "discord", "bsky", "web", "bench").
    name: ClassVar[str] = "bridge"

    @abstractmethod
    async def connect(self) -> None:
        """Open the bridge's long-lived connection. Idempotent."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Close cleanly. Called from server shutdown."""

    @abstractmethod
    async def send(
        self,
        channel_id: str,
        text: str,
        attachment_paths: list[Path] | None = None,
    ) -> SendResult:
        """Emit ``text`` to ``channel_id``. Returns a ``SendResult`` —
        ``sent=False`` plus an ``error`` string for soft failures the model
        can react to. Hard failures raise."""

    @abstractmethod
    async def react(self, channel_id: str, message_id: str, emoji: str) -> bool:
        """Add a reaction to a prior message. Returns False when the bridge
        has no native reaction support (e.g. Bluesky) — caller logs and
        moves on."""
