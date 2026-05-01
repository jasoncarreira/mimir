"""``send_message`` + ``react`` MCP tools (SPEC §7.1, §7.2.4).

Both dispatch through the ``ChannelRegistry`` based on ``channel_id`` prefix.
``send_message`` is wrapped by the per-turn ``LoopDetector`` (SPEC §7.2.4):
near-duplicate spam triggers a soft warning at ``MIMIR_SEND_LOOP_SOFT_LIMIT``
and a hard refusal at ``MIMIR_SEND_LOOP_HARD_LIMIT``.

Defaults derive from the active ``TurnContext`` (via the contextvar) — the
turn's ``channel_id`` becomes the default ``channel_id``, and the agent's
loop detector is read from the context. Subagents inherit a fresh contextvar
copy so their sends use their own (typically inert) detector.
"""

from __future__ import annotations

import logging
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

from ._context import get_current_turn
from ._tool_helpers import _content_block, _need, _safe
from .channel_registry import ChannelRegistry, UnknownChannelError
from .event_logger import log_event
from .history import MessageBuffer
from .loop_detector import (
    ERROR_REACTION,
    WARNING_REACTION,
    BreakerVerdict,
    LoopDetector,
)
from .msam_client import MsamClient, MsamError

log = logging.getLogger(__name__)


def build_channel_tools(
    registry: ChannelRegistry,
    msam_client: MsamClient | None = None,
    message_buffer: MessageBuffer | None = None,
) -> list[SdkMcpTool]:
    """Build the channel-aware send_message + react tools, closed over a
    shared ``ChannelRegistry``.

    When ``msam_client`` is provided, every successful ``send_message`` fires
    an MSAM ``mark_contributions`` pass (POST ``/v1/feedback``) with the
    sent text and the union of pre-injected + mid-turn-queried atom IDs in
    ``TurnContext.msam_atom_ids``. Crediting at send-time is more accurate
    than the agent-level ``_post_message_hook`` fallback because the
    delivered text is exactly what the user sees, whereas the SDK's "final
    output" can be empty (agent only used send_message) or include
    reasoning/scratch the user never receives. The post hook becomes a
    fallback for turns that don't ``send_message`` (scheduled ticks,
    bookkeeping turns).

    When ``message_buffer`` is provided, the delivered text is also written
    to chat_history as an ``assistant_message`` so the agent sees its own
    prior replies in the next turn's Recent activity. The agent-level
    ``_record_outbound`` is the fallback when no send_message fires."""

    @tool(
        "send_message",
        "Emit a message to a channel. If channel_id is omitted, uses the "
        "current turn's channel. Subject to a per-turn loop-detection "
        "circuit breaker — repeated near-duplicates first warn, then refuse. "
        "Returns 'send_message complete (sent=..., chunks=..., message_id=...)' "
        "or an is_error response on validation/breaker/dispatch failure.",
        # Explicit JSON schema so `channel_id` is optional — without this, the
        # SDK's dict-style schema marks every key required (claude_agent_sdk
        # __init__.py:_build_schema) and the MCP layer 422-rejects calls that
        # rely on the prompt's "channel_id defaults to the current turn"
        # guidance before our handler ever runs.
        {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "channel_id": {"type": "string"},
            },
            "required": ["text"],
        },
    )
    @_safe("send_message")
    async def send_message(args: dict[str, Any]) -> dict[str, Any]:
        text = args.get("text", "")
        if not isinstance(text, str):
            return _content_block("send_message failed: text must be a string", is_error=True)
        if not text.strip():
            return _content_block("send_message failed: text is empty", is_error=True)

        ctx = get_current_turn()
        channel_id = (args.get("channel_id") or "").strip()
        if not channel_id:
            channel_id = (ctx.channel_id if ctx else "") or ""
        if not channel_id:
            return _content_block(
                "send_message failed: no channel_id and no current turn channel",
                is_error=True,
            )

        # Loop-detection circuit breaker (SPEC §7.2.4).
        detector: LoopDetector | None = getattr(ctx, "loop_detector", None) if ctx else None
        if detector is not None:
            decision = detector.check(text)
            if decision.verdict == BreakerVerdict.HARD_STOP:
                # Refuse the send. Try to drop a ❌ on the channel for the
                # human watching — best-effort; ignore failures.
                try:
                    bridge = registry.find_or_raise(channel_id)
                    if ctx and ctx.last_assistant_message_id:
                        await bridge.react(channel_id, ctx.last_assistant_message_id, ERROR_REACTION)
                except UnknownChannelError:
                    pass
                except Exception:  # noqa: BLE001
                    log.exception("breaker hard-stop reaction failed")
                await log_event(
                    "send_message_loop_hard_stop",
                    channel_id=channel_id,
                    streak=decision.streak,
                    similarity=round(decision.similarity, 4),
                )
                return _content_block(
                    "send_message hard stop: repeated near-duplicate loop. "
                    "This send is refused. Reflect on what's wrong with the "
                    "approach before sending again — try a completely different "
                    "tactic or finish the turn.",
                    is_error=True,
                )
            if decision.verdict == BreakerVerdict.SOFT_WARN:
                if detector.mark_warning_emitted():
                    try:
                        bridge = registry.find_or_raise(channel_id)
                        if ctx and ctx.last_assistant_message_id:
                            await bridge.react(
                                channel_id, ctx.last_assistant_message_id, WARNING_REACTION
                            )
                    except UnknownChannelError:
                        pass
                    except Exception:  # noqa: BLE001
                        log.exception("breaker soft-warn reaction failed")
                await log_event(
                    "send_message_loop_warning",
                    channel_id=channel_id,
                    streak=decision.streak,
                    similarity=round(decision.similarity, 4),
                )
                # Soft warn doesn't refuse the send — fall through to dispatch.

        # Mark the attempt up-front so a failed dispatch (UnknownChannel,
        # bridge error, sent=False) still suppresses the agent-level
        # outbound fallback — otherwise the SDK's final assistant text
        # would be persisted to chat_history as if the user had received
        # it. Failures are visible in events.jsonl; chat_history shouldn't
        # claim a delivery that didn't happen.
        if ctx is not None:
            ctx.send_message_attempts += 1

        try:
            result = await registry.send(channel_id, text)
        except UnknownChannelError as exc:
            await log_event(
                "send_message_unknown_channel",
                channel_id=channel_id,
            )
            return _content_block(f"send_message failed: {exc}", is_error=True)
        except Exception as exc:  # noqa: BLE001
            log.exception("send_message dispatch failed")
            return _content_block(
                f"send_message failed: {type(exc).__name__}: {exc}",
                is_error=True,
            )

        if not result.sent:
            return _content_block(
                f"send_message failed: {result.error or 'bridge reported sent=False'}",
                is_error=True,
            )

        if ctx is not None and result.message_id:
            ctx.last_assistant_message_id = result.message_id

        # MSAM mark_contributions for the atoms in flight on this turn,
        # against the actual delivered text. Skipped on synthesis turns —
        # the agent calls msam_feedback per-atom there. Errors are logged
        # but never block the send (MSAM down ≠ user-visible failure).
        if (
            msam_client is not None
            and ctx is not None
            and ctx.trigger != "msam_session_end"
            and ctx.msam_atom_ids
        ):
            atom_ids_for_feedback = list(dict.fromkeys(ctx.msam_atom_ids))
            try:
                await msam_client.feedback(
                    atom_ids_for_feedback,  # de-dup, keep order
                    text,
                    session_id=ctx.msam_session_id,
                )
                await log_event(
                    "msam_feedback_sent",
                    where="send_message",
                    channel_id=channel_id,
                    n_atoms=len(atom_ids_for_feedback),
                    text_len=len(text),
                )
            except MsamError as exc:
                await log_event(
                    "msam_feedback_error",
                    where="send_message",
                    channel_id=channel_id,
                    error=str(exc),
                )

        if ctx is not None:
            ctx.send_message_count += 1

        # Persist the delivered text to chat_history as an assistant_message
        # so the agent sees its own prior reply in the next turn's Recent
        # activity. Source defaults to the inbound trigger's source when we
        # have a turn context; bridges that route by their own conventions
        # may pass through here even on outbound-only turns. agent.py's
        # _record_outbound (end of turn) is gated on send_message_count to
        # avoid duplicating this entry.
        if message_buffer is not None:
            try:
                msg = message_buffer.make_message(
                    channel_id=channel_id,
                    kind="assistant_message",
                    content=text,
                    msg_id=result.message_id,
                    source=ctx.channel_source if ctx is not None else None,
                )
                await message_buffer.append(msg)
            except Exception:  # noqa: BLE001
                log.exception("send_message: chat_history append failed")

        # Record the text on the event so adapters / the viewer can read what
        # was actually delivered. Cap to 4KB to keep events.jsonl small.
        text_for_log = text if len(text) <= 4096 else text[:4096] + "…[truncated]"
        await log_event(
            "send_message",
            channel_id=channel_id,
            ok=True,
            chunks=result.chunks,
            message_id=result.message_id,
            text=text_for_log,
        )
        return _content_block(
            f"send_message complete (sent=True, chunks={result.chunks}, "
            f"message_id={result.message_id})"
        )

    @tool(
        "react",
        "React to a message with an emoji. Defaults to the most recent "
        "assistant message on the current channel. Bridges that don't "
        "support native reactions (e.g. Bluesky) log a no-op.",
        {"emoji": str, "message_id": str, "channel_id": str},
    )
    @_safe("react")
    async def react(args: dict[str, Any]) -> dict[str, Any]:
        emoji = _need(args, "emoji")
        ctx = get_current_turn()
        channel_id = (args.get("channel_id") or "").strip()
        if not channel_id:
            channel_id = (ctx.channel_id if ctx else "") or ""
        if not channel_id:
            return _content_block(
                "react failed: no channel_id and no current turn channel",
                is_error=True,
            )

        message_id = (args.get("message_id") or "").strip()
        if not message_id and ctx is not None:
            message_id = ctx.last_assistant_message_id or ""
        if not message_id:
            return _content_block("react failed: no message_id and no recent message", is_error=True)

        try:
            ok = await registry.react(channel_id, message_id, emoji)
        except UnknownChannelError as exc:
            return _content_block(f"react failed: {exc}", is_error=True)
        except Exception as exc:  # noqa: BLE001
            log.exception("react dispatch failed")
            return _content_block(
                f"react failed: {type(exc).__name__}: {exc}",
                is_error=True,
            )
        await log_event(
            "react",
            channel_id=channel_id,
            message_id=message_id,
            emoji=emoji,
            ok=ok,
        )
        return _content_block(
            f"react ok={ok} (message_id={message_id}, emoji={emoji})"
        )

    return [send_message, react]


def channel_tool_names() -> list[str]:
    return ["mcp__mimir__send_message", "mcp__mimir__react"]
