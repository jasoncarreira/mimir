"""Filter delegated skills out of the inline ``Skills System`` catalog.

deepagents' :class:`~deepagents.middleware.skills.SkillsMiddleware`
discovers every ``SKILL.md`` under each configured source path and
injects them into the system prompt under ``## Skills System``. Mimir
also compiles a subset of those skills (the ones with
``subagent: true`` in frontmatter) into :class:`SubAgent` specs surfaced
via deepagents' ``task`` tool. Without filtering, a delegated skill
appears in *both* catalogs — wasted prompt tokens plus an unnecessary
"delegate or read?" decision the parent agent didn't need to make.

This middleware closes that gap by replacing ``state["skills_metadata"]``
(SkillsMiddleware's render-time source of truth) with a filtered list
that excludes any name in :attr:`FilterDelegatedSkillsMiddleware.delegated_skill_names`.

Ordering: the middleware MUST run AFTER ``SkillsMiddleware`` so that
SkillsMiddleware's ``before_agent`` populates the metadata first.
``create_deep_agent`` appends user-passed ``middleware=[...]`` entries
after the framework's built-in middleware stack (graph.py:708-709), so
passing this middleware via that kwarg achieves the right order.

State reducer: ``skills_metadata`` is annotated only with
``PrivateStateAttr`` (a schema annotation, not a reducer), so langgraph's
default REPLACE semantics apply — our update overwrites the unfiltered
value.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware.types import (
    AgentMiddleware,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from langchain_core.runnables import RunnableConfig
    from langgraph.runtime import Runtime

log = logging.getLogger(__name__)


class FilterDelegatedSkillsMiddleware(AgentMiddleware[Any, Any, Any]):
    """Drop delegated skills from ``state["skills_metadata"]`` before the model sees the catalog.

    Args:
        delegated_skill_names: Skill names compiled into SubAgent specs
            (return from :func:`compile_skills_to_subagents`). Any
            metadata entry whose ``name`` matches one of these is
            dropped from the rendered ``Skills System`` block.
    """

    # SkillsMiddleware defines its own state_schema (SkillsState) with
    # ``skills_metadata``; we don't need to redeclare since langgraph
    # merges state schemas across middlewares declared on the same graph.

    def __init__(self, *, delegated_skill_names: Iterable[str]) -> None:
        self._delegated = frozenset(delegated_skill_names)

    def _filter(self, state: dict[str, Any]) -> dict[str, Any] | None:
        """Return a state update with the filtered list, or ``None`` if no change."""
        metadata = state.get("skills_metadata")
        if not metadata or not self._delegated:
            return None
        filtered = [
            entry for entry in metadata
            if entry.get("name") not in self._delegated
        ]
        if len(filtered) == len(metadata):
            return None
        dropped = len(metadata) - len(filtered)
        log.debug(
            "filtered %d delegated skill(s) from the inline catalog "
            "(remaining=%d)", dropped, len(filtered),
        )
        return {"skills_metadata": filtered}

    def before_agent(
        self, state: dict[str, Any], runtime: Runtime, config: RunnableConfig,
    ) -> dict[str, Any] | None:
        """Filter at session start, after SkillsMiddleware populates state."""
        return self._filter(state)

    async def abefore_agent(
        self, state: dict[str, Any], runtime: Runtime, config: RunnableConfig,
    ) -> dict[str, Any] | None:
        """Async variant — same logic; pure dict manipulation, no IO."""
        return self._filter(state)
