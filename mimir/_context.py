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

**MCP tool dispatch hits the same pattern (chainlink #23).** Every MCP
``tools/call`` control request lands in
``Query._spawn_control_request_handler`` (SDK internals,
``claude_agent_sdk/_internal/query.py:232``), which calls
``spawn_detached`` to run the handler on a fresh asyncio task. That task
captures contextvars from the SDK's read-loop task — forked at connect
time, where ``_current_turn`` was ``None``. So the same staleness affects
MCP-dispatched tools (``saga_query``, ``saga_store``, ``saga_feedback``,
``saga_end_session``) as PreToolUse / PostToolUse hooks.

The hook fix uses ``input_data["session_id"]`` which the SDK forwards on
every hook callback. The MCP path is **asymmetric** — the SDK only
forwards ``(server_name, mcp_message)`` to the MCP handler; per-call
session_id is dropped at the boundary. So MCP tools can't use the same
fix shape as hooks.

The two helpers below cover the lookups available to MCP tool handlers:

- ``get_turn_by_saga_session_id(saga_session_id)`` — for tools whose args
  carry the saga_session_id (currently just ``saga_end_session``). Iterates
  ``_active_turns`` matching ``ctx.saga_session_id``.
- ``get_only_active_turn()`` — best-effort heuristic for tools whose args
  don't carry any per-turn key. Returns the single active turn if exactly
  one is registered, else ``None``. Works in single-channel deployments;
  multi-active cases must be surfaced via observability events rather than
  silently picking one.

See ``state/spec/chainlink-23-saga-mcp-context-resolution.md`` for the
full design and the per-tool migration sequence.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Any

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


def get_turn_by_saga_session_id(saga_session_id: str | None) -> "TurnContext | None":
    """Look up an active turn by its ``saga_session_id``. Used by MCP
    tool handlers whose args carry a ``saga_session_id`` (currently
    ``saga_end_session``) where the SDK's task-fork dispatch breaks
    contextvar inheritance (chainlink #23).

    Iterates ``_active_turns.values()`` rather than maintaining a parallel
    registry — active_turns is bounded by the dispatcher's per-channel
    queue size (typically 1-3 in production), so the linear scan is cheap.

    Returns ``None`` when ``saga_session_id`` is empty / None, or when no
    active turn matches. Caller should fall back to ``get_current_turn``
    (which works for direct-handler-call paths, e.g. unit tests) or
    treat as "no active turn" and skip per-turn bookkeeping."""
    if not saga_session_id:
        return None
    for ctx in _active_turns.values():
        if ctx.saga_session_id == saga_session_id:
            return ctx
    return None


def get_only_active_turn() -> "TurnContext | None":
    """Return the unique active turn if exactly one is registered, else
    ``None``. Best-effort heuristic for MCP tool handlers whose args
    don't carry any per-turn lookup key (``saga_query``, ``saga_store``,
    ``saga_feedback``) — works in single-channel deployments where
    concurrent turns are serialized by the dispatcher.

    Multi-active cases (multiple channels with concurrent in-flight
    turns) return ``None`` rather than guessing — callers should emit a
    ``resolution_path`` observability event so the rate at which the
    heuristic punts is visible. See chainlink #23 design doc."""
    if len(_active_turns) == 1:
        return next(iter(_active_turns.values()))
    return None


def resolve_active_ctx(args: dict[str, Any]) -> tuple["TurnContext | None", str]:
    """Standard three-level lookup chain for MCP tool handlers running
    on a forked task that can't see ``_current_turn``.

    Tries:

    1. ``args["session_id"]`` (model-passed via Option P) → match against
       ``ctx.saga_session_id`` in ``_active_turns``. Multi-channel safe.
    2. ``get_only_active_turn()`` heuristic — the unique active turn if
       exactly one is registered. Works in single-channel deployments;
       returns None when 0 or >1 turns are active.
    3. ``get_current_turn()`` contextvar — works for the direct-handler-
       call test path. Won't fire under SDK dispatch.

    Returns ``(ctx, resolution_path)`` where resolution_path is one of
    ``"saga_session_id" | "single_active" | "contextvar" | "missing"``.
    The path is logged via per-tool ``<tool>_ctx_resolution`` events so
    the rate of each path is visible in events.jsonl.

    Mirrors the chainlink #23 sagatools resolution chain; lifted here
    so any new MCP-dispatched tool (currently the bash_async family)
    can use the same shape without duplicating the logic.
    """
    sid = args.get("session_id") if args else None
    ctx = get_turn_by_saga_session_id(sid) if sid else None
    if ctx is not None:
        return ctx, "saga_session_id"
    ctx = get_only_active_turn()
    if ctx is not None:
        return ctx, "single_active"
    ctx = get_current_turn()
    if ctx is not None:
        return ctx, "contextvar"
    return None, "missing"
