"""Per-turn tool-call budget enforcement.

The SDK build gated tool calls via a ``PreToolUse`` HookMatcher that
checked ``TurnContext.tool_call_count`` against
``ctx.tool_call_budget`` before allowing each invocation. The hook ran
on EVERY tool call, including the SDK's built-in tools (read/write/bash).

Post-181 the deepagents agent has a langchain ``AgentMiddleware`` layer
that intercepts every tool invocation via ``wrap_tool_call`` /
``awrap_tool_call``. That's the right level — built-ins included.

Prior implementation (replaced 2026-05-23): we monkey-patched each
mimir tool's ``coroutine``/``func`` via ``apply_budget_gate`` and
added the list to ``create_deep_agent(tools=...)``. That missed
deepagents' built-in tools (``shell_exec``, ``read_file``,
``write_file``, ``glob``, ``edit_file``, ``write_todos``) which are
added by deepagents internally and never went through the mimir
tools list. Production heartbeats hit 142 tool_calls vs a budget of
120 with zero budget events firing — the gap that motivated this
rewrite.

Soft + hard semantics (unchanged):

* Below ``soft_threshold = max(1, int(budget * 0.75))``: silent.
* At soft threshold: log a one-time-per-turn
  ``tool_call_budget_soft_warning`` event. The tool still runs.
* At or above ``hard_threshold = budget``: refuse the call,
  return a ``ToolMessage`` with the denial text, emit
  ``tool_call_budget_denied``.

A ``budget`` of 0 disables enforcement entirely (matches the SDK
contract — operators set ``MIMIR_TOOL_CALL_BUDGET=0`` for benchmarks
that need uncapped exploration).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from .prohibited_action_guard import check_prohibited_bash, is_bash_tool

log = logging.getLogger(__name__)


# Tools exempt from the per-turn cap. They neither consume a slot nor
# get refused after the cap is hit. The driving case is ``send_message``:
# when the budget is exhausted the denial path tells the model to
# "finish the turn", but the final assistant text does NOT auto-deliver
# to channels (an explicit send_message call is the only delivery path
# — see SPEC §7.1). Without exempting it, the agent would hit the cap,
# get told to stop, but have no way to actually tell the operator. ``react``
# is exempt for the same operator-facing-acknowledgement reason.
_BUDGET_EXEMPT_TOOLS = frozenset({"send_message", "react"})


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


# Strong references to fire-and-forget background tasks (chainlink #118).
# Module-level set holds tasks spawned by _emit_event_sync until completion.
# The done-callback discards each entry so the set stays bounded to in-flight
# tasks only.  See cpython docs "Coroutines and Tasks / Important" callout.
_background_tasks: set["asyncio.Task[Any]"] = set()


def _emit_event_sync(kind: str, **kwargs: Any) -> None:
    """Fire-and-forget log_event from inside the middleware sync path.

    The middleware's ``wrap_tool_call`` is sync; ``log_event`` is async.
    We schedule it on the running loop when available, drop otherwise
    (the denial text on the returned ToolMessage is still load-bearing).
    """
    try:
        from ..event_logger import log_event  # lazy: supports monkeypatching in tests
        loop = asyncio.get_running_loop()
        task = loop.create_task(log_event(kind, **kwargs))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    except RuntimeError:
        log.debug("budget event %s dropped: no running loop", kind)


def _budget_denied_message(tool_name: str, count: int, budget: int) -> str:
    return (
        f"Tool-call budget exhausted: {count}/{budget} calls used "
        f"this turn. ``{tool_name}`` was refused. ``send_message`` and "
        f"``react`` remain available so you can still reply or "
        f"acknowledge — use them to wrap up the turn rather than "
        f"firing another tool."
    )


def _check_and_increment_or_deny(tool_name: str) -> str | None:
    """Returns a denial message (str) if the call should be refused,
    or ``None`` if the call should proceed. Shared between the sync
    and async middleware paths so the bookkeeping stays identical."""
    # Exempt tools (send_message, react) bypass both the count
    # increment AND the cap check — see ``_BUDGET_EXEMPT_TOOLS``
    # docstring for why. Free passage, no bookkeeping.
    if tool_name in _BUDGET_EXEMPT_TOOLS:
        return None
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


def _tool_name_from_request(request: ToolCallRequest) -> str:
    """Pull a usable name off the ToolCallRequest. ``request.tool``
    is the BaseTool when registered, ``None`` for un-registered calls
    (e.g. typos the model generates). The ``tool_call`` dict always
    carries the name the model used."""
    tc = getattr(request, "tool_call", None) or {}
    return str(tc.get("name") or "<unknown>")


def _tool_call_id(request: ToolCallRequest) -> str:
    tc = getattr(request, "tool_call", None) or {}
    return str(tc.get("id") or "")


def _check_prohibited(tool_name: str, request: "ToolCallRequest") -> str | None:
    """Return a prohibition message if this bash call is prohibited, else None."""
    if not is_bash_tool(tool_name):
        return None
    tc = getattr(request, "tool_call", None) or {}
    args = tc.get("args") or {}
    command = args.get("command", "")
    if not command:
        return None
    return check_prohibited_bash(command)


class BudgetGateMiddleware(AgentMiddleware):
    """Intercept every tool call (built-in or registered) for per-turn
    budget enforcement. Pairs with ``TurnContext.tool_call_budget`` /
    ``tool_call_count`` set by ``agent.run_turn``.
    """

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        tool_name = _tool_name_from_request(request)

        # Destructive-action guardrail (chainlink #259): an accident
        # deterrent against force-push-to-main/master, NOT a security
        # boundary — the regex screens the command arg and is bypassable
        # (vars, $()); see prohibited_action_guard.py. Catches the honest
        # mistake, doesn't claim to stop a determined caller.
        prohibition = _check_prohibited(tool_name, request)
        if prohibition is not None:
            _emit_event_sync("prohibited_action_blocked", tool=tool_name,
                             reason=prohibition[:200])
            return ToolMessage(
                content=prohibition,
                tool_call_id=_tool_call_id(request),
                name=tool_name,
                status="error",
            )

        denial = _check_and_increment_or_deny(tool_name)
        if denial is not None:
            return ToolMessage(
                content=denial,
                tool_call_id=_tool_call_id(request),
                name=tool_name,
                status="error",
            )
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        tool_name = _tool_name_from_request(request)

        # Destructive-action guardrail (chainlink #259): an accident
        # deterrent against force-push-to-main/master, NOT a security
        # boundary — the regex screens the command arg and is bypassable
        # (vars, $()); see prohibited_action_guard.py. Catches the honest
        # mistake, doesn't claim to stop a determined caller.
        prohibition = _check_prohibited(tool_name, request)
        if prohibition is not None:
            _emit_event_sync("prohibited_action_blocked", tool=tool_name,
                             reason=prohibition[:200])
            return ToolMessage(
                content=prohibition,
                tool_call_id=_tool_call_id(request),
                name=tool_name,
                status="error",
            )

        denial = _check_and_increment_or_deny(tool_name)
        if denial is not None:
            return ToolMessage(
                content=denial,
                tool_call_id=_tool_call_id(request),
                name=tool_name,
                status="error",
            )
        return await handler(request)
