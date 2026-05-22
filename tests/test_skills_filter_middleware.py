"""Unit tests for :class:`FilterDelegatedSkillsMiddleware`."""
from __future__ import annotations

import asyncio
from typing import Any

from mimir._skills_filter_middleware import FilterDelegatedSkillsMiddleware


def _mw(*names: str) -> FilterDelegatedSkillsMiddleware:
    return FilterDelegatedSkillsMiddleware(delegated_skill_names=names)


def _state(skills: list[dict[str, Any]]) -> dict[str, Any]:
    return {"skills_metadata": skills}


def test_drops_delegated_names_from_catalog():
    state = _state([
        {"name": "weather", "description": "..."},
        {"name": "memory", "description": "..."},
        {"name": "github", "description": "..."},
    ])
    update = _mw("weather", "github").before_agent(state, runtime=None, config=None)
    assert update is not None
    names = [m["name"] for m in update["skills_metadata"]]
    assert names == ["memory"]


def test_no_change_when_no_overlap_returns_none():
    """If nothing would be filtered, return ``None`` so we don't write
    a duplicate state update (langgraph treats a returned ``None`` as
    no-op)."""
    state = _state([
        {"name": "memory", "description": "..."},
        {"name": "wiki", "description": "..."},
    ])
    update = _mw("weather", "github").before_agent(state, runtime=None, config=None)
    assert update is None


def test_no_change_when_no_delegated_names_returns_none():
    """Construction with an empty delegated set is a permitted no-op."""
    state = _state([{"name": "memory", "description": "..."}])
    update = _mw().before_agent(state, runtime=None, config=None)
    assert update is None


def test_no_change_when_metadata_missing_returns_none():
    """SkillsMiddleware hasn't populated state yet (e.g. caller wired
    us before SkillsMiddleware by accident). Return ``None`` rather
    than fabricating an empty list — keeping behavior conservative."""
    state: dict[str, Any] = {}
    update = _mw("weather").before_agent(state, runtime=None, config=None)
    assert update is None


def test_async_path_mirrors_sync():
    """The async variant is just the sync logic wrapped — verify they
    agree on a fixture so future maintainers can't drift them."""
    state = _state([
        {"name": "weather", "description": "..."},
        {"name": "memory", "description": "..."},
    ])
    mw = _mw("weather")
    sync_update = mw.before_agent(state, runtime=None, config=None)
    async_update = asyncio.run(
        mw.abefore_agent(state, runtime=None, config=None),
    )
    assert sync_update == async_update


def test_filter_uses_frozenset_membership_o1():
    """Sanity: large delegated set with a small metadata list is fast."""
    delegated = [f"skill-{i}" for i in range(10_000)]
    mw = FilterDelegatedSkillsMiddleware(delegated_skill_names=delegated)
    state = _state([
        {"name": "skill-7777", "description": "..."},
        {"name": "memory", "description": "..."},
    ])
    update = mw.before_agent(state, runtime=None, config=None)
    assert update is not None
    assert [m["name"] for m in update["skills_metadata"]] == ["memory"]
