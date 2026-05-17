"""Per-turn tool-call budget enforcement.

The SDK build gated tool calls via a ``PreToolUse`` HookMatcher that
checked ``TurnContext.tool_call_count`` against
``ctx.tool_call_budget`` before allowing each invocation. Pre-181-N
the deepagents agent had no equivalent — ``Config.tool_call_budget``
existed but didn't gate anything, so panic-search loops could chew
through token budget indefinitely.

Restoration: ``apply_budget_gate`` wraps every langchain tool with a
pre-invocation check. The wrapper runs synchronously on the tool's
call path (no separate hook chain), so it composes cleanly with the
deepagents middleware surface without requiring custom middleware.

Soft + hard semantics:

* Below ``soft_threshold = max(1, ceil(budget * 0.75))``: silent.
* Between soft and hard: log a one-time-per-turn
  ``tool_call_budget_soft_warning`` event. The tool still runs.
* At or above ``hard_threshold = budget``: refuse the call,
  return a budget-denied string, emit ``tool_call_budget_denied``.

A ``budget`` of 0 disables enforcement entirely (matches main's
contract — operators set MIMIR_TOOL_CALL_BUDGET=0 for benchmarks
that need uncapped exploration).
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import BaseTool

log = logging.getLogger(__name__)


def _resolve_budget_state() -> tuple[Any, int] | None:
    """Return ``(ctx, budget)`` if a TurnContext with a non-zero
    ``tool_call_budget`` is active. ``None`` means: no enforcement
    (no active ctx, or budget=0). Avoids hard-coupling this module
    to the import chain for tests."""
    from .._context import get_current_turn
    ctx = get_current_turn()
    if ctx is None:
        return None
    budget = getattr(ctx, "tool_call_budget", 0) or 0
    if budget <= 0:
        return None
    return ctx, int(budget)


def _emit_event_sync(kind: str, **kwargs: Any) -> None:
    """Fire-and-forget log_event from a sync tool-call wrapper.

    The tool wrapper runs as part of the langchain dispatch; depending
    on the tool's own shape it may be sync or async. log_event is
    async — we schedule it on the running loop when possible.
    """
    try:
        import asyncio
        from ..event_logger import log_event
        loop = asyncio.get_running_loop()
        loop.create_task(log_event(kind, **kwargs))
    except RuntimeError:
        # No running loop (sync caller); silently drop. The denial
        # message in the tool return value is still visible to the
        # model.
        log.debug("budget event %s dropped: no running loop", kind)


def _budget_denied_message(tool_name: str, count: int, budget: int) -> str:
    return (
        f"Tool-call budget exhausted: {count}/{budget} calls used "
        f"this turn. ``{tool_name}`` was refused. Reflect on what "
        f"you have so far and finish the turn rather than firing "
        f"another tool."
    )


def apply_budget_gate(tool: BaseTool) -> BaseTool:
    """Wrap ``tool`` with a per-turn budget check.

    Mutates the tool in-place by replacing its ``coroutine`` / ``func``
    with a budget-aware shim. Returns the same tool for chaining.

    The shim's logic, in order:

      1. Resolve the active TurnContext + non-zero budget. If either
         is missing → no gating, call the original function unchanged.
      2. If ``ctx.tool_call_count >= budget`` → emit
         ``tool_call_budget_denied`` (one per refusal) and return the
         budget-denied message string.
      3. Else increment the count. If we just crossed the soft
         threshold (and haven't warned yet), emit
         ``tool_call_budget_soft_warning`` once per turn.
      4. Delegate to the original function.

    Idempotent: a tool that's already wrapped (carries the
    ``_mimir_budget_wrapped`` marker) is returned unchanged.
    """
    if getattr(tool, "_mimir_budget_wrapped", False):
        return tool

    original_coroutine = tool.coroutine
    original_func = tool.func

    def _check_and_increment(tool_name: str) -> str | None:
        """Returns a denial message (str) if the call should be refused,
        or ``None`` if the call should proceed."""
        state = _resolve_budget_state()
        if state is None:
            return None
        ctx, budget = state
        count = getattr(ctx, "tool_call_count", 0) or 0
        if count >= budget:
            _emit_event_sync(
                "tool_call_budget_denied",
                tool=tool_name,
                count=count,
                budget=budget,
            )
            return _budget_denied_message(tool_name, count, budget)
        # Increment first, then check soft threshold against the new
        # count — easier to reason about than off-by-one.
        new_count = count + 1
        ctx.tool_call_count = new_count
        soft = max(1, int(budget * 0.75))
        if new_count >= soft and not getattr(
            ctx, "_tool_call_soft_warning_emitted", False,
        ):
            ctx._tool_call_soft_warning_emitted = True
            _emit_event_sync(
                "tool_call_budget_soft_warning",
                tool=tool_name,
                count=new_count,
                budget=budget,
                soft_threshold=soft,
            )
        return None

    if original_coroutine is not None:
        async def _gated_coro(**kwargs: Any) -> Any:
            denial = _check_and_increment(tool.name)
            if denial is not None:
                return denial
            return await original_coroutine(**kwargs)
        tool.coroutine = _gated_coro

    if original_func is not None:
        def _gated_sync(**kwargs: Any) -> Any:
            denial = _check_and_increment(tool.name)
            if denial is not None:
                return denial
            return original_func(**kwargs)
        tool.func = _gated_sync

    tool._mimir_budget_wrapped = True
    return tool
