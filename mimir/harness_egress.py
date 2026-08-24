"""Observability and enforcement for harness-owned final egress sinks."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from .access_control import SinkGate, get_sink_category
from .event_logger import log_event_sync

log = logging.getLogger(__name__)


def record_harness_shadow_block(
    sink_name: str,
    target_channel: str | None,
    reason: str,
) -> None:
    """Record a shadow denial already decided by a harness-owned predicate."""
    try:
        log_event_sync(
            "sink_blocked",
            sink=sink_name,
            reason=reason,
            sink_category=get_sink_category(sink_name).value,
            target_channel=target_channel,
            allowed=True,
            status="would_block",
            enforcement_enabled=False,
            is_shadow_decision=True,
        )
    except Exception:  # noqa: BLE001 - observability must not break delivery
        log.debug("harness shadow decision event failed", exc_info=True)
    log.warning("harness IFC sink would block: sink=%s reason=%s", sink_name, reason)


def harness_sink_allowed(
    sink_name: str,
    target_channel: str | None,
    ifc_labels: Any,
    auth_context: Any,
    *,
    on_refusal: Callable[[str], None] | None = None,
) -> bool:
    """Check one harness sink and record enforced or shadow denials."""
    if auth_context is not None:
        ifc_labels = auth_context.ifc_state.current(ifc_labels)
    enforcement_enabled = bool(
        getattr(auth_context, "enforcement_enabled", False)
    )
    decision = SinkGate.check_sink_flow(
        sink_name,
        target_channel,
        ifc_labels,
        auth_context,
        enforce=enforcement_enabled,
    )
    if decision.allowed and not decision.is_shadow_decision:
        return True

    if not decision.allowed and on_refusal is not None:
        try:
            on_refusal(decision.reason)
        except Exception:  # noqa: BLE001 - failure reporting must not break delivery
            log.debug("harness sink refusal callback failed", exc_info=True)

    try:
        log_event_sync(
            "sink_blocked",
            sink=sink_name,
            reason=decision.reason,
            sink_category=get_sink_category(sink_name).value,
            target_channel=target_channel,
            allowed=decision.allowed,
            status="would_block" if decision.is_shadow_decision else "denied",
            enforcement_enabled=decision.enforcement_enabled,
            is_shadow_decision=decision.is_shadow_decision,
        )
    except Exception:  # noqa: BLE001 - observability must not break delivery
        log.debug("harness sink decision event failed", exc_info=True)
    log.warning(
        "harness IFC sink %s: sink=%s reason=%s",
        "would block" if decision.is_shadow_decision else "blocked",
        sink_name,
        decision.reason,
    )
    return decision.allowed
