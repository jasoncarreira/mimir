"""Per-turn ``TurnContext`` propagation via ``contextvars`` (SPEC §4.6, §9.3).

The SAGA ``saga_query`` tool needs to auto-append returned ``atom_id``s to the
parent's ``TurnContext.saga_atom_ids`` so the post-message hook can credit
mid-turn retrievals without the agent having to remember (SPEC §9.3 "mid-turn
``saga_query`` tracking"). Tools registered with the SDK are plain functions —
they need a way to find the active turn.

``contextvars`` are the right primitive for in-task lookups: each ``query()``
call runs in its own asyncio task, and we set the ContextVar before invoking
``query()``. Subagent calls run in distinct tasks with distinct contexts, so a
subagent's ``saga_query`` does NOT mutate the parent's ``saga_atom_ids`` —
matching SPEC §9.3 "Subagents do not inherit the parent's ``saga_atom_ids``".

Hook callbacks (PreToolUse / PostToolUse) are dispatched on a different
task — the SDK's control-protocol task, forked at first ``client.connect()``.
That task captured the contextvar value at fork time (``None``) and never
sees subsequent ``set()`` calls in ``run_turn``, so contextvar lookups from
hooks return stale data. The ``_active_turns`` map fixes this: ``run_turn``
registers the turn under its ``turn_id``, hooks pass their incoming
``session_id`` (which is ``ctx.turn_id`` since stage 2 of the ClaudeSDKClient
migration) to ``get_turn_by_session_id`` for a reliable lookup that doesn't
depend on task-fork inheritance.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import TurnContext

_current_turn: ContextVar["TurnContext | None"] = ContextVar(
    "mimir_current_turn", default=None
)

# Registry of active turns keyed by turn_id. Populated by ``run_turn``,
# read by hook callbacks that can't rely on contextvar inheritance.
_active_turns: dict[str, "TurnContext"] = {}


def set_current_turn(ctx: "TurnContext") -> Token:
    _active_turns[ctx.turn_id] = ctx
    return _current_turn.set(ctx)


def reset_current_turn(token: Token) -> None:
    ctx = _current_turn.get()
    if ctx is not None:
        _active_turns.pop(ctx.turn_id, None)
    _current_turn.reset(token)


def get_current_turn() -> "TurnContext | None":
    return _current_turn.get()


def get_turn_by_session_id(session_id: str | None) -> "TurnContext | None":
    """Look up an active turn by its ``turn_id``. Used by hook callbacks
    where contextvar inheritance is unreliable (the hook task forked at
    first connect, captured contextvar=None, and never sees later sets).
    Returns ``None`` if the session is unknown — caller should treat
    that as "no active turn" and skip per-turn enforcement."""
    if not session_id:
        return None
    return _active_turns.get(session_id)
