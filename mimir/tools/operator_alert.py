"""Notify the operator at the single server-configured alert destination."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import ToolException, tool

from ..channel_registry import OPERATOR_CHANNEL_SENTINEL, resolve_deliver_channel


OPERATOR_ALERT_MAX_CHARS = 4000
OPERATOR_ALERT_MAX_PER_TURN = 3

_channel_registry: Any = None
_config: Any = None


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
    try:
        result = await _channel_registry.send(destination, text.strip(), final=False)
    except Exception as exc:
        raise ToolException(f"operator_alert failed: {exc}") from exc
    if not getattr(result, "sent", True):
        error = getattr(result, "error", None)
        detail = f" ({error})" if error else ""
        raise ToolException(f"operator_alert failed: message was not delivered{detail}")

    try:
        turn.delivered_channel_ids.add(destination)
    except (AttributeError, TypeError):
        pass
    return (
        "operator_alert ok: "
        f"message_id={getattr(result, 'message_id', None)}"
    )


operator_alert.handle_tool_error = True
