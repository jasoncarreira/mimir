"""The ``tracker:`` section of ``worklink.yaml``."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from .record import REF_PATTERNS, ExternalRecord

ADAPTER_IDS = ("github_issues", "linear", "jira")
TRANSITION_EVENTS = ("filed", "build_started", "pr_opened", "merged")
MAX_STATE_LENGTH = 64

_REPOSITORY_SLUG = re.compile(r"[a-z0-9_.-]+/[a-z0-9_.-]+")
_PROJECT_KEYS = {
    "linear": re.compile(r"[A-Z][A-Z0-9]{0,9}"),
    "jira": re.compile(r"[A-Z][A-Z0-9_]{1,9}"),
}
_ADAPTER_KEYS = {
    "github_issues": {"repos"},
    "linear": {"team_keys", "transitions", "project_transitions"},
    "jira": {
        "account_id",
        "projects",
        "trusted_groups",
        "trusted_project_roles",
        "transitions",
        "project_transitions",
    },
}


@dataclass(frozen=True)
class TrackerAdapterConfig:
    name: str
    repos: tuple[str, ...] = ()
    team_keys: tuple[str, ...] = ()
    account_id: str = ""
    projects: tuple[str, ...] = ()
    trusted_groups: tuple[str, ...] = ()
    trusted_project_roles: tuple[str, ...] = ()
    transitions: Mapping[str, str] = field(default_factory=dict)
    project_transitions: Mapping[str, Mapping[str, str]] = field(default_factory=dict)


@dataclass(frozen=True)
class TrackerConfig:
    close_on_merge: bool = False
    adapters: Mapping[str, TrackerAdapterConfig] = field(default_factory=dict)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return value


def _reject_unknown(data: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(str(key) for key in data if key not in allowed)
    if unknown:
        raise ValueError(f"{label} has unknown keys: {', '.join(unknown)}")


def _printable(value: Any, label: str, *, max_length: int | None = None) -> str:
    if (
        not isinstance(value, str)
        or not value
        or not value.isprintable()
        or (max_length is not None and len(value) > max_length)
    ):
        raise ValueError(f"{label} must be a printable string")
    return value


def _string_list(
    value: Any, label: str, *, pattern: re.Pattern[str] | None = None
) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    items = tuple(_printable(item, f"{label} entry") for item in value)
    if pattern is not None:
        for item in items:
            if pattern.fullmatch(item) is None:
                raise ValueError(f"{label} entry {item!r} is not a valid key")
    return items


def _transitions(value: Any, label: str) -> dict[str, str]:
    data = _mapping(value, label)
    _reject_unknown(data, set(TRANSITION_EVENTS), label)
    return {
        event: _printable(state, f"{label}.{event}", max_length=MAX_STATE_LENGTH)
        for event, state in data.items()
    }


def _project_transitions(
    value: Any, label: str, pattern: re.Pattern[str]
) -> dict[str, dict[str, str]]:
    data = _mapping(value, label)
    parsed: dict[str, dict[str, str]] = {}
    for project, transitions in data.items():
        if not isinstance(project, str) or pattern.fullmatch(project) is None:
            raise ValueError(f"{label} key {project!r} is not a valid project key")
        parsed[project] = _transitions(transitions, f"{label}.{project}")
    return parsed


def _parse_adapter(
    name: str, raw: Any, repository: str | None
) -> TrackerAdapterConfig:
    label = f"worklink tracker.adapters.{name}"
    data = _mapping(raw, label)
    _reject_unknown(data, _ADAPTER_KEYS[name], label)
    if name == "github_issues":
        repos = _string_list(data.get("repos"), f"{label}.repos", pattern=_REPOSITORY_SLUG)
        if not repos:
            if repository is None:
                raise ValueError(f"{label}.repos is required when worklink repository is unset")
            repos = (repository,)
        return TrackerAdapterConfig(name=name, repos=repos)
    pattern = _PROJECT_KEYS[name]
    transitions = _transitions(data.get("transitions"), f"{label}.transitions")
    project_transitions = _project_transitions(
        data.get("project_transitions"), f"{label}.project_transitions", pattern
    )
    if name == "linear":
        return TrackerAdapterConfig(
            name=name,
            team_keys=_string_list(data.get("team_keys"), f"{label}.team_keys", pattern=pattern),
            transitions=transitions,
            project_transitions=project_transitions,
        )
    account_id = data.get("account_id")
    if account_id is None:
        raise ValueError(f"{label}.account_id is required")
    projects = _string_list(data.get("projects"), f"{label}.projects", pattern=pattern)
    if not projects:
        raise ValueError(f"{label}.projects must name at least one project")
    groups = _string_list(data.get("trusted_groups"), f"{label}.trusted_groups")
    roles = _string_list(data.get("trusted_project_roles"), f"{label}.trusted_project_roles")
    if not groups and not roles:
        raise ValueError(
            f"{label} needs a non-empty trusted_groups or trusted_project_roles"
        )
    return TrackerAdapterConfig(
        name=name,
        account_id=_printable(account_id, f"{label}.account_id"),
        projects=projects,
        trusted_groups=groups,
        trusted_project_roles=roles,
        transitions=transitions,
        project_transitions=project_transitions,
    )


def parse_tracker_config(data: Any, repository: str | None) -> TrackerConfig:
    """Parse and validate ``worklink.yaml`` ``tracker:``; unknown keys raise ValueError."""

    section = _mapping(data, "worklink tracker")
    _reject_unknown(section, {"close_on_merge", "adapters"}, "worklink tracker")
    close_on_merge = section.get("close_on_merge", False)
    if not isinstance(close_on_merge, bool):
        raise ValueError("worklink tracker.close_on_merge must be a boolean")
    raw_adapters = _mapping(section.get("adapters"), "worklink tracker.adapters")
    _reject_unknown(raw_adapters, set(ADAPTER_IDS), "worklink tracker.adapters")
    return TrackerConfig(
        close_on_merge=close_on_merge,
        adapters={
            name: _parse_adapter(name, raw, repository)
            for name, raw in raw_adapters.items()
        },
    )


def _project_of(record: ExternalRecord) -> str | None:
    if record.adapter == "github_issues":
        return None
    return record.ref.rsplit("-", 1)[0]


def resolve_transition(
    config: TrackerConfig, record: ExternalRecord, event: str
) -> str | None:
    """Return the state configured for ``event`` on this issue, or None when unset."""

    adapter = config.adapters.get(record.adapter)
    if adapter is None:
        return None
    project = _project_of(record)
    scoped = adapter.project_transitions.get(project, {}) if project else {}
    return scoped.get(event) or adapter.transitions.get(event)


def configured_states(config: TrackerConfig, record: ExternalRecord) -> frozenset[str]:
    """Return every state this issue may be moved to."""

    return frozenset(
        state
        for event in TRANSITION_EVENTS
        if (state := resolve_transition(config, record, event)) is not None
    )
