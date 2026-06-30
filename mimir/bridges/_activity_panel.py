"""Passive activity panel subscriber for live turn events.

The panel is presentation-only: it consumes the drop-allowed TurnEventBus from
its own task, posts one bridge message at turn start, and edits that message at
a coarse cadence as safe step summaries accumulate.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ..channel_registry import ChannelRegistry
from ..turn_event_bus import TurnEventBus
from .base import MessageUpdate

log = logging.getLogger(__name__)


def channel_enabled(channel_id: str, allowlist: tuple[str, ...]) -> bool:
    if not channel_id or not allowlist:
        return False
    if "*" in allowlist:
        return True
    return any(channel_id.startswith(prefix) for prefix in allowlist)


# Activity panels are user-facing bridge UI, so start events must opt into a
# documented set of work-producing triggers. Unknown triggers are skipped by
# default: adding a new framework/session-management trigger must not surface a
# panel until it is classified here.
ACTIVITY_PANEL_INCLUDED_TRIGGERS = frozenset(
    {
        "user_message",  # Direct user interaction; always show progress.
        "poller",  # Autonomous external work operators may allow-list.
        "scheduled_tick",  # Scheduled autonomous work on allow-listed channels.
        "shell_job_complete",  # Async job continuation the user/work initiated.
    }
)

ACTIVITY_PANEL_EXCLUDED_TRIGGERS = frozenset(
    {
        "saga_session_end",  # Idle-session synthesis; internal housekeeping.
        "upgrade",  # Defaults/version maintenance; framework-owned.
        "claude_code_spawn",  # Spawn bookkeeping; not a user-facing turn.
        "react_received",  # Reaction bookkeeping/follow-up routing.
        "reflect",  # Internal reflection/introspection work.
        "unknown",  # Missing/unclean trigger metadata is not user-facing.
    }
)


def trigger_enabled(trigger: Any) -> bool:
    cleaned = _clean(trigger, limit=80) or "unknown"
    if cleaned in ACTIVITY_PANEL_EXCLUDED_TRIGGERS:
        return False
    return cleaned in ACTIVITY_PANEL_INCLUDED_TRIGGERS


@dataclass
class ActivityStep:
    label: str
    status: str = "running"


@dataclass
class FoldedInput:
    source_id: str | None = None
    author: str | None = None
    author_display: str | None = None
    source: str | None = None


@dataclass
class ActivityPanelModel:
    turn_id: str
    channel_id: str
    reply_to_message_id: str | None = None
    thread_ts: str | None = None
    message_id: str | None = None
    posted: bool = False
    finalized: bool = False
    outbound_message_sent: bool = False
    steps: list[ActivityStep] = field(default_factory=list)
    in_flight: ActivityStep | None = None
    folded_inputs: list[FoldedInput] = field(default_factory=list)

    @property
    def completed_count(self) -> int:
        return len(self.steps)


class ActivityPanel:
    """Bridge-agnostic TurnEventBus subscriber with platform renderers."""

    def __init__(
        self,
        bus: TurnEventBus,
        channels: ChannelRegistry,
        allowlist: tuple[str, ...],
        *,
        debounce_seconds: float = 1.0,
    ) -> None:
        self._bus = bus
        self._channels = channels
        self._allowlist = allowlist
        self._debounce_seconds = max(0.0, debounce_seconds)
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._task: asyncio.Task[Any] | None = None
        self._models: dict[str, ActivityPanelModel] = {}
        self._last_edit_by_channel: dict[str, float] = {}
        self._pending: dict[str, asyncio.Task[Any]] = {}

    @property
    def models(self) -> dict[str, ActivityPanelModel]:
        return self._models

    def start(self) -> asyncio.Task[Any] | None:
        if not self._allowlist:
            return None
        if self._task is not None and not self._task.done():
            return self._task
        self._queue = self._bus.subscribe("*")
        self._task = asyncio.create_task(self.run(), name="mimir-activity-panel")
        return self._task

    async def stop(self) -> None:
        if self._queue is not None:
            self._bus.unsubscribe("*", self._queue)
            self._queue = None
        for task in list(self._pending.values()):
            task.cancel()
        self._pending.clear()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def run(self) -> None:
        if self._queue is None:
            self._queue = self._bus.subscribe("*")
        try:
            while True:
                event = await self._queue.get()
                try:
                    await self.handle_event(event)
                except Exception:  # noqa: BLE001
                    log.debug("activity panel event handling failed", exc_info=True)
        finally:
            if self._queue is not None:
                self._bus.unsubscribe("*", self._queue)
                self._queue = None

    async def handle_event(self, event: dict[str, Any]) -> None:
        channel_id = str(event.get("channel_id") or "")
        if not channel_enabled(channel_id, self._allowlist):
            return
        turn_id = str(event.get("turn_id") or "")
        if not turn_id:
            return
        if event.get("type") == "turn" and event.get("phase") == "start":
            if not trigger_enabled(event.get("trigger")):
                return
            model = ActivityPanelModel(
                turn_id=turn_id,
                channel_id=channel_id,
                reply_to_message_id=_clean(event.get("reply_to_message_id")),
                thread_ts=_clean(event.get("thread_ts")),
            )
            model.in_flight = ActivityStep("Working")
            self._models[turn_id] = model
            await self._post(model)
            return

        model = self._models.get(turn_id)
        if model is None or not model.posted:
            return

        typ = event.get("type")
        phase = event.get("phase")
        if typ in ("reasoning", "tool_call", "tool_result"):
            self._apply_span(model, event)
            if phase != "chunk":
                await self._schedule_edit(model)
        elif typ == "injected_input":
            for item in event.get("inputs") or []:
                if isinstance(item, dict):
                    model.folded_inputs.append(
                        FoldedInput(
                            source_id=_clean(item.get("source_id")),
                            author=_clean(item.get("author")),
                            author_display=_clean(item.get("author_display")),
                            source=_clean(item.get("source")),
                        )
                    )
            await self._schedule_edit(model)
        elif typ == "outbound_message" and event.get("sent"):
            model.outbound_message_sent = True
        elif typ == "turn" and phase == "end":
            if event.get("outbound_message_sent"):
                model.outbound_message_sent = True
            final_folded = event.get("injected_input_count")
            if isinstance(final_folded, int) and final_folded > len(model.folded_inputs):
                for _ in range(final_folded - len(model.folded_inputs)):
                    model.folded_inputs.append(FoldedInput())
            self._complete_in_flight(model)
            model.finalized = True
            await self._flush(model)

    def _apply_span(self, model: ActivityPanelModel, event: dict[str, Any]) -> None:
        phase = event.get("phase")
        if phase == "chunk":
            return
        label = _step_label(event)
        if phase == "start":
            model.in_flight = ActivityStep(label)
        elif phase == "end":
            self._complete_in_flight(model, fallback=label)

    def _complete_in_flight(
        self,
        model: ActivityPanelModel,
        *,
        fallback: str | None = None,
    ) -> None:
        step = model.in_flight or ActivityStep(fallback or "Working")
        if fallback:
            step.label = fallback
        step.status = "done"
        model.steps.append(step)
        model.in_flight = None

    async def _post(self, model: ActivityPanelModel) -> None:
        bridge = self._channels.find(model.channel_id)
        if bridge is None:
            return
        text, blocks, embed = _render_for_bridge(bridge, model)
        kwargs: dict[str, Any] = {
            "final": False,
            "reply_to_message_id": model.thread_ts or model.reply_to_message_id,
        }
        if getattr(bridge, "name", "") == "slack":
            kwargs["blocks"] = blocks
        elif getattr(bridge, "name", "") == "discord":
            kwargs["embed"] = embed
        try:
            try:
                result = await bridge.send(model.channel_id, text, **kwargs)
            except TypeError:
                result = await bridge.send(model.channel_id, text, final=False)
        except Exception:  # noqa: BLE001
            log.debug("activity panel post failed", exc_info=True)
            return
        if getattr(result, "sent", False) and getattr(result, "message_id", None):
            model.message_id = result.message_id
            model.posted = True

    async def _schedule_edit(self, model: ActivityPanelModel) -> None:
        if model.finalized:
            return
        loop = asyncio.get_running_loop()
        last_edit = self._last_edit_by_channel.get(model.channel_id)
        if last_edit is None:
            await self._flush(model)
            return
        elapsed = loop.time() - last_edit
        if elapsed >= self._debounce_seconds:
            await self._flush(model)
            return
        if model.turn_id in self._pending:
            return
        delay = self._debounce_seconds - elapsed
        self._pending[model.turn_id] = asyncio.create_task(
            self._flush_later(model.turn_id, delay),
            name=f"mimir-activity-panel-edit-{model.turn_id}",
        )

    async def _flush_later(self, turn_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            model = self._models.get(turn_id)
            if model is not None and not model.finalized:
                await self._flush(model)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.debug("activity panel delayed edit failed", exc_info=True)
        finally:
            self._pending.pop(turn_id, None)

    async def _flush(self, model: ActivityPanelModel) -> None:
        if not model.message_id:
            return
        current = asyncio.current_task()
        pending = self._pending.pop(model.turn_id, None)
        if pending is not None and pending is not current and not pending.done():
            pending.cancel()
        bridge = self._channels.find(model.channel_id)
        if bridge is None:
            return
        text, blocks, embed = _render_for_bridge(bridge, model)
        update = MessageUpdate(text=text, blocks=blocks, embed=embed)
        try:
            await bridge.edit_message(model.channel_id, model.message_id, update)
            self._last_edit_by_channel[model.channel_id] = asyncio.get_running_loop().time()
        except Exception:  # noqa: BLE001
            log.debug("activity panel edit failed", exc_info=True)


def _render_for_bridge(
    bridge: Any,
    model: ActivityPanelModel,
) -> tuple[str, list[dict[str, Any]] | None, Any | None]:
    if getattr(bridge, "name", "") == "discord":
        text, embed = render_discord_panel(model)
        return text, None, embed
    text, blocks = render_slack_panel(model)
    return text, blocks, None


def render_slack_panel(model: ActivityPanelModel) -> tuple[str, list[dict[str, Any]]]:
    text = render_panel_text(model)
    return text, [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


def render_discord_panel(model: ActivityPanelModel) -> tuple[str, dict[str, Any]]:
    title = "Done" if model.finalized else "Working"
    description = render_discord_panel_description(model)
    return "", {
        "title": title,
        "description": description,
        "color": 0x2ECC71 if model.finalized else 0x5865F2,
    }


def render_discord_panel_description(model: ActivityPanelModel) -> str:
    if model.finalized:
        if model.outbound_message_sent:
            return "Done Reply posted"
        suffix = _folded_summary(model)
        return f"Done {model.completed_count} steps{suffix}"

    lines: list[str] = []
    for step in model.steps[-8:]:
        lines.append(f"[x] {step.label}")
    if model.in_flight is not None:
        lines.append(f"[ ] {model.in_flight.label}")
    folded = _folded_live_lines(model)
    if folded:
        lines.extend(folded)
    return "\n".join(lines) or "[ ] Working"


def render_panel_text(model: ActivityPanelModel) -> str:
    if model.finalized:
        if model.outbound_message_sent:
            return "✓ Reply posted"
        suffix = _folded_summary(model)
        return f"✓ {model.completed_count} steps{suffix}"

    lines = ["*Working*"]
    for step in model.steps[-8:]:
        lines.append(f"✓ {step.label}")
    if model.in_flight is not None:
        lines.append(f"◌ {model.in_flight.label}")
    folded = _folded_live_lines(model)
    if folded:
        lines.extend(folded)
    return "\n".join(lines)


def _folded_summary(model: ActivityPanelModel) -> str:
    count = len(model.folded_inputs)
    if count == 0:
        return ""
    label = "follow-up" if count == 1 else "follow-ups"
    return f" · +{count} {label} folded"


def _folded_live_lines(model: ActivityPanelModel) -> list[str]:
    count = len(model.folded_inputs)
    if count == 0:
        return []
    last = model.folded_inputs[-1]
    who = last.author_display or last.author
    if count == 1 and who:
        return [f"↳ folded in {who}'s follow-up"]
    label = "message" if count == 1 else "messages"
    return [f"↳ +{count} mid-turn {label} folded"]


def _step_label(event: dict[str, Any]) -> str:
    typ = event.get("type")
    phase = event.get("phase")
    if typ == "reasoning":
        return "Thought"
    if typ == "tool_call":
        name = _tool_name(event.get("tool_name"))
        if phase == "start":
            return f"Working {name}" if name else "Working"
        return f"Skill {name}" if name else "Skill"
    if typ == "tool_result":
        name = _tool_name(event.get("tool_name"))
        return f"Ran {name}" if name else "Ran skill"
    return "Working"


def _tool_name(value: Any) -> str:
    text = _clean(value, limit=48) or ""
    return re.sub(r"[^A-Za-z0-9_.:-]", "", text)[:48]


def _clean(value: Any, *, limit: int = 240) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]
