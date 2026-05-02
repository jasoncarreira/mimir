"""Per-turn ``TurnContext`` propagation via ``contextvars`` (SPEC §4.6, §9.3).

The SAGA ``saga_query`` tool needs to auto-append returned ``atom_id``s to the
parent's ``TurnContext.saga_atom_ids`` so the post-message hook can credit
mid-turn retrievals without the agent having to remember (SPEC §9.3 "mid-turn
``saga_query`` tracking"). Tools registered with the SDK are plain functions —
they need a way to find the active turn.

``contextvars`` are the right primitive: each ``query()`` call runs in its own
asyncio task, and we set the ContextVar before invoking ``query()``. Subagent
calls run in distinct tasks with distinct contexts, so a subagent's
``saga_query`` does NOT mutate the parent's ``saga_atom_ids`` — matching SPEC
§9.3 "Subagents do not inherit the parent's ``saga_atom_ids``".
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import TurnContext

_current_turn: ContextVar["TurnContext | None"] = ContextVar(
    "mimir_current_turn", default=None
)


def set_current_turn(ctx: "TurnContext") -> Token:
    return _current_turn.set(ctx)


def reset_current_turn(token: Token) -> None:
    _current_turn.reset(token)


def get_current_turn() -> "TurnContext | None":
    return _current_turn.get()
