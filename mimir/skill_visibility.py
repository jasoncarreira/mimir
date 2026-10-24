"""Keep skills seeded by a deselected plugin out of the agent's view.

``seed_plugin_defaults`` copies a plugin's default skills into
``<home>/skills/<plugin>-<skill>/``. Those copies outlive the plugin's
selection: after the operator drops it from ``MIMIR_PLUGINS`` they would
otherwise surface as ordinary operator skills, advertising tools and
workflows that no longer exist. Every path that offers home skills to the
agent filters through :func:`hidden_home_skill_dirs`, which decides
ownership from package metadata and the home's seed markers only, so the
deselected provider is never imported.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path, PurePath, PurePosixPath
from typing import TYPE_CHECKING, Any

from deepagents.middleware.skills import SkillsMiddleware

from .plugins.defaults import selected_plugin_owners, unselected_seeded_skills
from .skill_defs import home_skills_dir

if TYPE_CHECKING:
    from .plugins.registry import PolicyRegistry

log = logging.getLogger(__name__)


def hidden_home_skill_dirs(home: Path, policy: "PolicyRegistry | None") -> frozenset[Path]:
    """Withhold deselected and known-stale plugin seeds from every agent loader."""
    hidden = unselected_seeded_skills(home, selected_plugin_owners(policy))
    root = home_skills_dir(home)
    from .plugins.skills import pending_skill_upgrades, skills_for_candidates

    selected = (skills_for_candidates(policy.selected_plugin_candidates)
                if policy is not None else [])
    return frozenset({*(root / name for name in hidden),
                      *(root / skill.dirname for skill in pending_skill_upgrades(home, selected))})


def visible_skill_names(home: Path, names: list[str], policy: "PolicyRegistry | None") -> list[str]:
    """Drop home skill names whose seeding plugin is not selected."""
    hidden = {path.name for path in hidden_home_skill_dirs(home, policy)}
    if not hidden:
        return names
    # A bundled skill of the same name still exists and stays visible.
    from .skill_defs import home_builtin_skills_dir

    builtin = home_builtin_skills_dir(home)
    return [name for name in names
            if name not in hidden or (builtin / name / "SKILL.md").is_file()]


class SeedFilteredSkillsMiddleware(SkillsMiddleware):
    """deepagents' skills catalog, minus home skills seeded by deselected plugins.

    Keeps the ``SkillsMiddleware`` name so ``create_deep_agent`` puts it in the
    default skills slot for the main agent and the general-purpose subagent.
    Filtering also applies to a catalog already held in (checkpointed) state,
    so a session that loaded the catalog under an earlier selection loses the
    seeded entries on its next turn.
    """

    def __init__(self, *, backend: Any, sources: list[str], home: Path,
                 policy: "PolicyRegistry | None") -> None:
        super().__init__(backend=backend, sources=sources)
        self._home = Path(home)
        self._policy = policy

    @property
    def name(self) -> str:
        return "SkillsMiddleware"

    def set_skill_sources(self, sources: list[str]) -> None:
        """Follow the agent's per-turn source list (directories may appear later)."""
        rebuilt = SkillsMiddleware(backend=self._backend, sources=sources)
        self.sources = rebuilt.sources
        self.source_labels = rebuilt.source_labels

    def _filter(self, skills: list[dict[str, Any]]) -> list[dict[str, Any]]:
        hidden = hidden_home_skill_dirs(self._home, self._policy)
        if not hidden:
            return skills
        names = {path.name for path in hidden}
        # The home backend reports home-rooted virtual paths (``/skills/<dir>``);
        # a plain filesystem backend reports host paths. Accept both spellings of
        # the operator skills directory, and never touch the bundled source.
        root = home_skills_dir(self._home)
        operator_roots = {PurePosixPath("/skills").parts, PurePath(root).parts,
                          PurePath(root.resolve(strict=False)).parts}
        kept = []
        for skill in skills:
            skill_dir = PurePath(str(skill.get("path", ""))).parent
            if skill_dir.name in names and skill_dir.parent.parts in operator_roots:
                log.warning(
                    "skill_plugin_seed_hidden: %s — plugin seed is unavailable or awaiting "
                    "CLI upgrade; skill withheld from the agent", skill_dir,
                )
                continue
            kept.append(skill)
        return kept

    def _filtered_update(self, state: Any, update: Any) -> Any:
        current = update["skills_metadata"] if update is not None else state.get("skills_metadata")
        if current is None:
            return update
        kept = self._filter(list(current))
        if update is None:
            if len(kept) == len(current):
                return None
            update = {"skills_load_errors": list(state.get("skills_load_errors") or [])}
        return {**update, "skills_metadata": kept}

    def before_agent(self, state, runtime, config):  # type: ignore[override]
        return self._filtered_update(state, super().before_agent(state, runtime, config))

    async def abefore_agent(self, state, runtime, config):  # type: ignore[override]
        update = await super().abefore_agent(state, runtime, config)
        return await asyncio.to_thread(self._filtered_update, state, update)
