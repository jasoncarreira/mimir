"""Fail-closed CI evidence for reviews bound to an immutable PR head."""

from __future__ import annotations

from collections.abc import Mapping

_PASSING = frozenset({"success", "neutral", "skipped"})


def approval_blocker(check_data: object, status_data: object) -> str | None:
    """Return a reason approval must wait, or None if all head checks passed."""
    if not isinstance(check_data, Mapping) or not isinstance(status_data, Mapping):
        return "CI checks could not be read"
    checks = check_data.get("check_runs")
    total = check_data.get("total_count")
    statuses = status_data.get("statuses")
    if (
        not isinstance(checks, list) or not isinstance(statuses, list)
        or type(total) is not int or total != len(checks)
        or any(not isinstance(row, Mapping) for row in [*checks, *statuses])
    ):
        return "CI checks could not be read completely"
    if not checks and not statuses:
        return "no CI checks or commit statuses exist on this head"
    blockers = []
    for row in checks:
        name, state, conclusion = row.get("name"), row.get("status"), row.get("conclusion")
        if not isinstance(name, str) or not name or not isinstance(state, str):
            return "CI check response is malformed"
        if state != "completed" or not isinstance(conclusion, str) or conclusion not in _PASSING:
            blockers.append(f"{name} ({conclusion if state == 'completed' and conclusion else state})")
    for row in statuses:
        name, state = row.get("context"), row.get("state")
        if not isinstance(name, str) or not name or not isinstance(state, str):
            return "CI status response is malformed"
        if state not in _PASSING:
            blockers.append(f"{name} ({state})")
    return "CI checks not green: " + ", ".join(blockers[:20]) if blockers else None
