"""SubconsciousQueryHook — pre_query SAGA retrieval with background framing."""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from ..sagatools import _atom_ids_from_response, _format_saga_payload
from ..turn_hooks import TurnHook

if TYPE_CHECKING:  # pragma: no cover
    from ..models import AgentEvent, TurnContext
    from ..saga_client import SagaClient

log = logging.getLogger(__name__)

# Avoid circular import: mimir.agent imports mimir.hooks transitively, so we
# cannot import NON_USER_QUERY_TRIGGERS from mimir.agent here.
_NON_USER_QUERY_TRIGGERS: frozenset[str] = frozenset(
    {"saga_session_end", "scheduled_tick", "poller"}
)


class SubconsciousQueryHook(TurnHook):
    """Fire a background-framed SAGA query in ``pre_query`` to retrieve
    episodic context the literal user message might miss.

    The hook is exception-safe: any failure inside the saga round-trip
    is logged at WARNING and silently discarded. ``TurnContext.subconscious_block``
    is only set on success; it stays ``None`` on any error path.

    Gates:
    - ``MIMIR_SUBCONSCIOUS_QUERY=false`` env var disables the hook entirely.
    - Triggers in ``_NON_USER_QUERY_TRIGGERS`` are skipped (synthetic / session-end
      turns don't benefit from background retrieval).
    """

    def __init__(self, saga: "SagaClient") -> None:
        self._saga = saga

    async def pre_query(
        self, ctx: "TurnContext", event: "AgentEvent"
    ) -> None:
        # Gate 1: env-var opt-out.
        if os.environ.get("MIMIR_SUBCONSCIOUS_QUERY", "true").lower() == "false":
            return

        # Gate 2: synthetic / session-end triggers.
        if event.trigger in _NON_USER_QUERY_TRIGGERS:
            return

        refined_query = "Background context and relevant history: " + event.content

        try:
            payload = await self._saga.query(refined_query, top_k=5)

            atom_ids = _atom_ids_from_response(payload)

            # Dedup: suppress the block entirely if every returned ID is
            # already in the main saga retrieval set.
            existing_ids: set[str] = set(ctx.saga_atom_ids)
            new_ids = [aid for aid in atom_ids if aid not in existing_ids]

            if not new_ids and atom_ids:
                # All returned IDs are already seen — skip block.
                ctx.subconscious_block = None
                return

            raw_block = _format_saga_payload(payload)
            if not raw_block or raw_block == "(no atoms)":
                ctx.subconscious_block = None
                return

            ctx.subconscious_block = raw_block

        except Exception:  # noqa: BLE001
            log.warning(
                "SubconsciousQueryHook: saga query failed for turn %s; skipping",
                getattr(ctx, "turn_id", "?"),
                exc_info=True,
            )
