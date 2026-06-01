"""Shared assembly for the Resource usage stats block.

Both ``Agent._assemble_usage_block`` and the ``mimir stats`` CLI need
to aggregate the same five inputs (turns.jsonl cost windows, plan-
quota snapshot, off-pace projection, subagent token spend, the
optional 1M-context beta flag) and feed them to
``usage_stats.render_usage_block``. Pre-refactor (code-review-
2026-05-09 CR2-#6) the assembly was duplicated across ``agent.py``
and ``cli.py`` with subtle drift (the CLI skipped billing-mode
evaluation, the agent passed ``betas`` to the renderer, etc.). This
module is the single source of truth.

Callers differ in their post-aggregation concerns:
- ``Agent`` runs inside ``asyncio.to_thread`` and emits cooldown-gated
  events for ``cost_rate_alert`` / ``cost_rate_advisory`` /
  ``rate_limit_off_pace`` from the aggregated state.
- ``mimir stats`` runs synchronously, prints the body plus a
  billing-mode diagnostic so the operator sees which event WOULD
  have been emitted on the agent loop.

``StatsBlockResult`` exposes the underlying state (alert, off_pace,
report) so each caller can do its own thing without re-aggregating.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .rate_limits import (
    RateLimitStore,
    off_pace_buckets,
    render_off_pace_warning,
    render_plan_quota_lines,
)
from .subagent_stats import (
    aggregate as aggregate_subagents,
    render_subagent_block,
)
from .usage_stats import (
    CONTEXT_1M_BETA,
    CostRateAlert,
    UsageReport,
    aggregate,
    evaluate_cost_rate,
    render_usage_block,
)

if TYPE_CHECKING:
    from .config import Config
    from .jsonl_snapshot import JsonlSnapshot

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StatsBlockResult:
    """Aggregated state + rendered body for the Resource usage section.

    Fields:
    - ``body``: the rendered block as a string, or None when there
      are no turns recorded yet (the renderer's "skip" sentinel).
    - ``alert``: the cost-rate spike check result, or None if no
      spike is currently tripped.
    - ``off_pace``: ``rate_limits.off_pace_buckets`` output (worst-
      first); empty list when all plan windows are on-pace.
    - ``rate_limit_current``: the SDK rate-limit snapshot dict that
      was passed in (echoed for caller convenience).
    - ``report``: the underlying ``UsageReport`` from
      ``usage_stats.aggregate``; useful for callers that want to
      inspect window-by-window breakdowns without re-running the
      JSONL scan.
    """

    body: str | None
    alert: CostRateAlert | None
    off_pace: list[Any]
    rate_limit_current: dict[str, Any]
    report: UsageReport


def assemble_stats_block(
    cfg: "Config",
    rate_limits: RateLimitStore,
    *,
    turns_snapshot: "JsonlSnapshot | None" = None,
    events_snapshot: "JsonlSnapshot | None" = None,
    betas: list[str] | None = None,
) -> StatsBlockResult:
    """Aggregate usage stats + rate-limit projection + subagent spend,
    return a ``StatsBlockResult`` with the rendered body and the
    underlying state.

    ``rate_limits`` is the ``RateLimitStore`` from either the agent's
    ``self._rate_limits`` (worker-loop path) or a per-CLI-invocation
    ``RateLimitStore(path=...)``. The helper calls ``.current()``
    INSIDE the rate-limits try/except so a corrupt rate_limits.json
    or transient stat() error degrades to empty plan lines instead
    of taking down the whole block (PR #116 review-fix).

    ``turns_snapshot`` / ``events_snapshot`` are JsonlSnapshot caches
    used by the agent path to avoid re-scanning turns.jsonl /
    events.jsonl every turn; the CLI passes None.

    ``betas`` defaults to ``[CONTEXT_1M_BETA]`` when
    ``cfg.context_1m`` is true. Pre-refactor the CLI didn't pass
    betas; defaulting from cfg matches the agent's rendering so
    ``mimir stats`` output matches what the agent sees.

    Partial-failure shape (matches pre-refactor agent behavior):
    - aggregate() / evaluate_cost_rate() exceptions BUBBLE — the
      whole block goes away (caller catches + skips).
    - rate-limits exception (``.current()`` raise OR projection
      raise) → ``off_pace = []`` + empty plan/off_pace lines;
      ``rate_limit_current = {}`` echoed on the result; the block
      still renders.
    - subagent_stats exception → ``subagent_block = None``; the
      block still renders.
    """
    if betas is None:
        betas = []
        if getattr(cfg, "context_1m", False):
            betas.append(CONTEXT_1M_BETA)

    report = aggregate(
        cfg.turns_log,
        fallback_model=cfg.model,
        snapshot=turns_snapshot,
    )

    alert = evaluate_cost_rate(
        report,
        hourly_limit_usd=cfg.cost_hourly_limit_usd or None,
        spike_ratio=cfg.cost_rate_spike_ratio or None,
        spike_floor_usd_per_hour=cfg.cost_rate_spike_floor_usd or None,
    )

    plan_lines: list[str] = []
    off_pace_lines: list[str] = []
    off_pace: list[Any] = []
    rate_limit_current: dict[str, Any] = {}
    try:
        rate_limit_current = rate_limits.current()
        # Render/project only the ACTIVE quota provider's keys so stale
        # keys a now-disabled poller left in the store (e.g. Anthropic keys
        # after a Codex cutover) don't pollute the view or trigger phantom
        # off-pace warnings (chainlink #301). The full unfiltered dict is
        # still echoed on the result below.
        from .providers import provider_for_quota
        from .rate_limits import filter_to_active_provider

        active_provider = provider_for_quota(
            getattr(cfg, "model_spec", ""),
            getattr(cfg, "anthropic_base_url", ""),
        ).quota_provider_key
        visible = filter_to_active_provider(rate_limit_current, active_provider)
        plan_lines = render_plan_quota_lines(visible)
        off_pace = off_pace_buckets(visible)
        off_pace_lines = render_off_pace_warning(off_pace)
    except Exception:  # noqa: BLE001
        log.exception("rate_limits read/projection failed")

    subagent_body: str | None = None
    try:
        subagent_body = render_subagent_block(
            aggregate_subagents(cfg.events_log)
        )
    except Exception:  # noqa: BLE001
        log.exception("subagent_stats aggregate failed")

    body = render_usage_block(
        report,
        fallback_model=cfg.model,
        budget_5h_usd=cfg.usage_5h_limit_usd or None,
        budget_weekly_usd=cfg.usage_weekly_limit_usd or None,
        alert=alert,
        plan_quota_lines=plan_lines,
        off_pace_warning=off_pace_lines,
        subagent_block=subagent_body,
        betas=betas or None,
    )

    return StatsBlockResult(
        body=body,
        alert=alert,
        off_pace=off_pace,
        rate_limit_current=rate_limit_current,
        report=report,
    )
