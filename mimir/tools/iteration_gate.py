"""Per-turn model-iteration ceiling — 3-tier (chainlink #511).

``BudgetGateMiddleware`` caps how many *tools* a turn fires; this caps how many
model *iterations* it runs (one per ``before_model`` boundary). A loop that does
little or no tool work per step could otherwise spin within the tool budget.

Three escalating tiers, mirroring the tool budget's soft-warn → hard-deny shape:

* **75%** — a gentle wrap-up nudge (one ``HumanMessage``). No event.
* **90%** — an urgent "last warning" nudge (one ``HumanMessage``) + an
  ``iteration_budget_warning`` event.
* **100%** — a **hard stop**: force the agent loop to end (``jump_to: "end"``)
  with a final ``AIMessage``, set ``ctx.iteration_hard_stopped`` (so ``run_turn``
  sends a cap notice to the channel — the model never got to deliver), and emit
  an ``iteration_budget_reached`` event.

The 75% tier deliberately emits no event (only 90% and 100% do). Off when
``iteration_budget`` is 0. Pairs with ``TurnContext.iteration_budget`` /
``iteration_count`` set by ``agent.run_turn``.
"""

from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import hook_config
from langchain_core.messages import AIMessage, HumanMessage

from ..event_logger import emit_event_background as _emit_event_sync


def _nudge_75(count: int, budget: int) -> str:
    return (
        f"You're {count}/{budget} model steps into this turn (~75% of the "
        f"iteration budget). Start converging: finish the current step, avoid "
        f"opening new lines of work, and aim to give your final response soon."
    )


def _nudge_90(count: int, budget: int) -> str:
    return (
        f"Last warning: {count}/{budget} model steps used this turn (~90%). "
        f"Give your final response NOW — summarize what you did or found and any "
        f"next step, and stop calling tools. The turn will be force-stopped at "
        f"{budget}. (If this turn needs to reach a channel, use send_message.)"
    )


def _hard_stop_message(count: int, budget: int) -> str:
    return (
        f"Turn force-stopped at the per-turn iteration limit "
        f"({count}/{budget} model steps)."
    )


class IterationGateMiddleware(AgentMiddleware):
    """Count model iterations per turn and enforce the 3-tier ceiling."""

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime):  # noqa: ANN001 — langchain hook shape
        from .._context import get_current_turn

        ctx = get_current_turn()
        if ctx is None:
            return None
        budget = getattr(ctx, "iteration_budget", 0) or 0
        if budget <= 0:
            return None  # disabled

        ctx.iteration_count = (getattr(ctx, "iteration_count", 0) or 0) + 1
        count = ctx.iteration_count
        t75 = max(1, int(budget * 0.75))
        t90 = max(1, int(budget * 0.90))

        # Highest tier first so each count lands in exactly one branch; the
        # hard stop persists on graph re-entry; only its event is one-shot.
        if count >= budget:
            ctx.iteration_hard_stopped = True  # run_turn sends the channel notice
            if not getattr(ctx, "_iteration_cap_emitted", False):
                ctx._iteration_cap_emitted = True
                _emit_event_sync("iteration_budget_reached", count=count, budget=budget)
            # Force the agent loop to terminate cleanly with a final message.
            return {
                "jump_to": "end",
                "messages": [AIMessage(content=_hard_stop_message(count, budget))],
            }
        if count >= t90 and not getattr(ctx, "_iteration_warn_90_emitted", False):
            ctx._iteration_warn_90_emitted = True
            _emit_event_sync(
                "iteration_budget_warning", count=count, budget=budget, threshold=t90,
            )
            return {"messages": [HumanMessage(content=_nudge_90(count, budget))]}
        if count >= t75 and not getattr(ctx, "_iteration_warn_75_emitted", False):
            ctx._iteration_warn_75_emitted = True  # no event at 75%
            return {"messages": [HumanMessage(content=_nudge_75(count, budget))]}
        return None
