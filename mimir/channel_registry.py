"""Channel ID prefix → Bridge dispatch (SPEC §7.2.3).

Each bridge declares its prefixes (e.g. ``"slack-"``, ``"dm-slack-"``) and
the registry routes outbound ``send`` / ``react`` calls accordingly. Looking
up by longest-prefix-first means a bridge can claim both a public-channel
namespace and a DM namespace (Slack does this).

Inbound flows through ``dispatcher.enqueue`` directly from each bridge's
own callback — the registry isn't involved on the inbound path.
"""

from __future__ import annotations

import logging
from typing import Iterable

from .bridges.base import Bridge, SendResult

log = logging.getLogger(__name__)


# Triggers that represent a live, user-facing exchange the agent can reply
# into by default. Allowlist (not denylist) so any NEW automated/synthetic
# trigger defaults to NON-interactive — a fail-safe: a future cron/poller-
# style trigger can't accidentally auto-default a user-facing send.
# ``shell_job_complete`` is interactive only when it lands on a bridge
# channel (it reports back to wherever the spawn was launched from); the
# bridge-presence check in ``is_interactive_turn`` enforces that.
INTERACTIVE_TRIGGERS: frozenset[str] = frozenset(
    {"user_message", "shell_job_complete"}
)


def is_interactive_turn(
    channel_id: str | None,
    trigger: str | None,
    registry: "ChannelRegistry | None",
) -> bool:
    """True when this turn is a live user-facing exchange the agent can
    reply into by default — the channel is served by a registered bridge
    AND the trigger is one of :data:`INTERACTIVE_TRIGGERS`.

    Non-interactive turns (``scheduled_tick`` heartbeats, ``poller`` batches,
    ``saga_session_end`` synthesis, ``upgrade`` maintenance, and anything on
    a non-bridge ``scheduler:`` / ``poller:`` channel) return ``False``:
    there is no user to default a reply to, so ``send_message`` must be given
    an explicit ``channel_id`` (e.g. the operator alert channel).
    """
    if not channel_id or not trigger:
        return False
    if trigger not in INTERACTIVE_TRIGGERS:
        return False
    if registry is None:
        return False
    return registry.find(channel_id) is not None


# chainlink #508: optional ``deliver:`` channel for pollers + scheduled ticks.
# The literal ``OPERATOR_CHANNEL`` resolves to the configured operator alert
# channel (``MIMIR_OPERATOR_ALERT_CHANNEL``) so a job can target "wherever the
# operator listens" without hard-coding the id.
OPERATOR_CHANNEL_SENTINEL = "OPERATOR_CHANNEL"


def resolve_deliver_channel(
    raw: str | None, operator_alert_channel: str | None,
) -> str | None:
    """Resolve a poller/tick ``deliver:`` value to a concrete channel id.

    ``OPERATOR_CHANNEL`` → the configured operator alert channel; any other
    value is returned verbatim. Returns ``None`` when unset, or when the
    sentinel is used but no operator alert channel is configured (graceful: the
    delivery instruction / failure notice is simply suppressed)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw == OPERATOR_CHANNEL_SENTINEL:
        return (operator_alert_channel or "").strip() or None
    return raw


async def post_job_failure_notice(
    channels: "ChannelRegistry | None",
    deliver_channel: str | None,
    *,
    label: str,
    error: str,
) -> None:
    """Post the mechanical ``⚠️ <job> failed`` notice to a job's ``deliver:``
    channel (chainlink #508). This is the ONLY auto-send in the deliver flow —
    a turn that failed can't surface its own result, so the framework reports
    the failure on the operator's behalf. Best-effort; never raises."""
    if channels is None or not deliver_channel:
        return
    try:
        await channels.send(
            deliver_channel, f"⚠️ {label} failed: {error}", final=True,
        )
    except Exception as exc:  # noqa: BLE001 — a failure notice must not cascade
        log.debug("post_job_failure_notice send to %s failed: %s", deliver_channel, exc)


class UnknownChannelError(LookupError):
    def __init__(self, channel_id: str) -> None:
        super().__init__(
            f"no bridge registered for channel_id {channel_id!r} — "
            f"check the prefix is one of the registered bridges"
        )
        self.channel_id = channel_id


class ChannelRegistry:
    def __init__(self) -> None:
        # Each entry: (prefix, bridge). Sorted by descending prefix length so
        # ``"dm-slack-"`` matches before ``"slack-"``.
        self._entries: list[tuple[str, Bridge]] = []
        self._bridges: list[Bridge] = []

    def register(self, bridge: Bridge) -> None:
        if bridge in self._bridges:
            return
        self._bridges.append(bridge)
        for prefix in bridge.prefixes:
            self._entries.append((prefix, bridge))
        self._entries.sort(key=lambda e: len(e[0]), reverse=True)

    def bridges(self) -> list[Bridge]:
        return list(self._bridges)

    def prefixes(self) -> list[str]:
        """Registered channel-id prefixes (e.g. ``discord-`` / ``dm-slack-``),
        longest-first — the namespaces the connected bridges serve. A
        ``channel_id`` must start with one of these or ``find`` returns None
        (→ ``UnknownChannelError`` on send)."""
        return [prefix for prefix, _ in self._entries]

    def find(self, channel_id: str) -> Bridge | None:
        for prefix, bridge in self._entries:
            if channel_id.startswith(prefix):
                return bridge
        return None

    def find_or_raise(self, channel_id: str) -> Bridge:
        bridge = self.find(channel_id)
        if bridge is None:
            raise UnknownChannelError(channel_id)
        return bridge

    async def send(
        self,
        channel_id: str,
        text: str,
        attachment_paths: list | None = None,
        *,
        final: bool = True,
    ) -> SendResult:
        bridge = self.find_or_raise(channel_id)
        return await bridge.send(
            channel_id, text, attachment_paths, final=final,
        )

    async def react(self, channel_id: str, message_id: str, emoji: str) -> bool:
        bridge = self.find_or_raise(channel_id)
        return await bridge.react(channel_id, message_id, emoji)

    async def fetch_history(
        self,
        channel_id: str,
        *,
        limit: int = 20,
        before: str | None = None,
    ) -> list:
        bridge = self.find_or_raise(channel_id)
        return await bridge.fetch_history(
            channel_id, limit=limit, before=before,
        )

    async def connect_all(self) -> None:
        for bridge in self._bridges:
            try:
                await bridge.connect()
            except Exception:  # noqa: BLE001
                log.exception("bridge %s.connect() failed", bridge.name)

    async def disconnect_all(self) -> None:
        for bridge in self._bridges:
            try:
                await bridge.disconnect()
            except Exception:  # noqa: BLE001
                log.exception("bridge %s.disconnect() failed", bridge.name)
