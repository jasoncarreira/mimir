"""Structured subagent role definitions for the deepagents task tool."""

from __future__ import annotations

from pathlib import Path

from deepagents.middleware.subagents import GENERAL_PURPOSE_SUBAGENT

from mimir.tools.budget_gate import BudgetGateMiddleware
from mimir.tools.fetched_content_inject import FetchedContentReminderMiddleware


def build_mimir_subagents(*, home: Path | None = None) -> list[dict]:
    """Build explicit Mimir subagent specs for ``create_deep_agent``.

    The child agent explicitly carries the authorization/budget gate because
    DeepAgents does not inherit the parent agent's middleware into subagents.
    Registering ``general-purpose`` here suppresses its otherwise ungated
    auto-added equivalent while preserving its standard prompt and tool inheritance.
    """

    ingestion_middleware = [FetchedContentReminderMiddleware(home)] if home else []
    return [{
        **GENERAL_PURPOSE_SUBAGENT,
        "middleware": [BudgetGateMiddleware(), *ingestion_middleware],
    }]
