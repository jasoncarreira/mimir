"""Notify the operator at the single server-configured alert destination."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from langchain_core.tools import ToolException, tool

from ..channel_registry import OPERATOR_CHANNEL_SENTINEL, resolve_deliver_channel


OPERATOR_ALERT_MAX_CHARS = 4000
OPERATOR_ALERT_MAX_PER_TURN = 3

_channel_registry: Any = None
_config: Any = None


@dataclass(frozen=True)
class OperatorAlertReceipt:
    destination: str
    message_id: str | None
    delivered_at: str
    text_sha256: str


def set_operator_alert_dependencies(channel_registry: Any, config: Any) -> None:
    """Install runtime-owned dependencies for ``operator_alert``."""
    global _channel_registry, _config
    _channel_registry = channel_registry
    _config = config


async def deliver_operator_alert(text: str, *, authorize: bool = False) -> OperatorAlertReceipt:
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
    if authorize:
        from ..access_control import (
            ToolRegistry,
            get_trusted_service_from_auth_context,
            service_can_invoke_operation,
        )

        auth = getattr(turn, "auth_context", None)
        service = get_trusted_service_from_auth_context(auth)
        labels = auth.ifc_state.current(auth.ifc_labels) if auth is not None else None
        authorized = (
            service_can_invoke_operation(service, "operator_alert")
            and ToolRegistry().authorize_tool(
                "operator_alert",
                auth,
                enforce=True,
                target_channel=destination,
                ifc_labels=labels,
            ).allowed
        )
        if not authorized:
            raise ToolException("operator_alert refused: trusted service capability required")
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
        turn.operator_alert_count -= 1
        raise ToolException(f"operator_alert failed: {exc}") from exc
    if not getattr(result, "sent", True):
        turn.operator_alert_count -= 1
        error = getattr(result, "error", None)
        detail = f" ({error})" if error else ""
        raise ToolException(f"operator_alert failed: message was not delivered{detail}")

    try:
        turn.delivered_channel_ids.add(destination)
    except (AttributeError, TypeError):
        pass
    clean = text.strip()
    return OperatorAlertReceipt(
        destination=destination,
        message_id=getattr(result, "message_id", None),
        delivered_at=datetime.now(UTC).isoformat(),
        text_sha256=hashlib.sha256(clean.encode("utf-8")).hexdigest(),
    )


@tool
async def operator_alert(text: str) -> str:
    """Send a bounded alert to the operator-configured notification channel."""
    receipt = await deliver_operator_alert(text)
    return f"operator_alert ok: message_id={receipt.message_id}"


operator_alert.handle_tool_error = True
