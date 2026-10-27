"""Notify the operator at the single server-configured alert destination."""

from __future__ import annotations

from typing import Any
import logging
import uuid

from langchain_core.tools import ToolException, tool

from ..channel_registry import OPERATOR_CHANNEL_SENTINEL, resolve_deliver_channel


OPERATOR_ALERT_MAX_CHARS = 4000
OPERATOR_ALERT_MAX_PER_TURN = 3

_channel_registry: Any = None
_config: Any = None
_log = logging.getLogger(__name__)


async def send_social_proposal_ping(text: str) -> None:
    """Server-owned proposal notification; no model-selected channel or text."""
    channel = (getattr(_config, "operator_alert_channel", "") or "").strip()
    if not channel:
        _log.warning("social-outbox proposal: operator alert channel is unset")
        return
    if _channel_registry is None:
        _log.warning("social-outbox proposal: channel registry is unavailable")
        return
    try:
        result = await _channel_registry.send(channel, text, final=True)
        if not getattr(result, "sent", True):
            _log.warning("social-outbox proposal: operator notification not delivered")
    except Exception:
        _log.exception("social-outbox proposal: operator notification failed")


def set_operator_alert_dependencies(channel_registry: Any, config: Any) -> None:
    """Install runtime-owned dependencies for ``operator_alert``."""
    global _channel_registry, _config
    _channel_registry = channel_registry
    _config = config


@tool
async def operator_alert(text: str) -> str:
    """Send a bounded alert to the operator-configured notification channel.

    Use this from autonomous turns when a judgement is worth surfacing but the
    turn cannot write. The destination is fixed by server configuration and
    cannot be selected by this call.

    Args:
        text: Alert text for the operator, at most 4000 characters.
    """
    from .._context import get_current_turn

    destination = resolve_deliver_channel(
        OPERATOR_CHANNEL_SENTINEL,
        getattr(_config, "operator_alert_channel", "") if _config is not None else "",
    )
    if destination is None:
        raise ToolException(
            "operator_alert refused: MIMIR_OPERATOR_ALERT_CHANNEL is not configured"
        )
    if _channel_registry is None:
        raise ToolException("operator_alert refused: no channel registry configured")
    if not text or not text.strip():
        raise ToolException("operator_alert refused: text must not be empty")
    if len(text) > OPERATOR_ALERT_MAX_CHARS:
        raise ToolException(
            "operator_alert refused: text exceeds the "
            f"{OPERATOR_ALERT_MAX_CHARS}-character limit"
        )

    turn = get_current_turn()
    if turn is None:
        raise ToolException("operator_alert refused: no active turn context")
    count = getattr(turn, "operator_alert_count", 0) or 0
    if count >= OPERATOR_ALERT_MAX_PER_TURN:
        raise ToolException(
            "operator_alert refused: per-turn limit of "
            f"{OPERATOR_ALERT_MAX_PER_TURN} alerts reached"
        )

    # Reserve the slot before awaiting delivery so parallel tool calls cannot
    # exceed the per-turn bound.
    turn.operator_alert_count = count + 1
    # For web, history is delivery; SSE is only a best-effort live projection.
    # Persist before fan-out so a failed write cannot cause a duplicate retry.
    if destination.startswith("web-"):
        from ..history import get_global_buffer

        buffer = get_global_buffer()
        if buffer is None:
            turn.operator_alert_count -= 1
            raise ToolException("operator_alert failed: web history is unavailable")
        try:
            from ..agent import _effective_persisted_integrity

            integrity = (
                _effective_persisted_integrity(turn.ifc_labels, turn.auth_context)
                if turn.ifc_labels is not None else "untrusted"
            )
            record = buffer.make_message(
                channel_id=destination, kind="assistant_message", content=text.strip(),
                msg_id=uuid.uuid4().hex[:12],
                source="web", integrity=integrity,
            )
            await buffer.append(record, require_disk=True)
        except Exception as exc:
            turn.operator_alert_count -= 1
            raise ToolException("operator_alert failed: web history write failed") from exc
        message_id = record.msg_id
        try:
            await _channel_registry.send(
                destination, text.strip(), final=False, message_id=message_id,
            )
        except Exception:
            # Already delivered durably: never invite a retry for SSE failure.
            _log.warning("operator alert persisted; live web fan-out failed")
    else:
        try:
            result = await _channel_registry.send(destination, text.strip(), final=False)
        except Exception as exc:
            turn.operator_alert_count -= 1
            raise ToolException(f"operator_alert failed: {exc}") from exc
        if not getattr(result, "sent", True):
            turn.operator_alert_count -= 1
            error = getattr(result, "error", None)
            detail = f" ({error})" if error else ""
            raise ToolException(f"operator_alert failed: message was not delivered{detail}")
        message_id = getattr(result, "message_id", None)

    try:
        turn.delivered_channel_ids.add(destination)
    except (AttributeError, TypeError):
        pass
    return (
        "operator_alert ok: "
        f"message_id={message_id}"
    )


operator_alert.handle_tool_error = True
