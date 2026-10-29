"""Local requests to move a tracker issue; nothing here calls a tracker."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .config import TRANSITION_EVENTS, resolve_transition
from .evidence import evidence_transaction
from .record import ExternalRecord, ExternalRecordError, parse_external_record


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _record(description: str) -> ExternalRecord | None:
    try:
        return parse_external_record(description)
    except ExternalRecordError:
        return None


def _config(home: Path):
    from ..worklink.backends.registry import WorklinkConfig

    return WorklinkConfig.load(home / "worklink.yaml").tracker


def request_transition(
    home: Path,
    description: str,
    event: str,
    *,
    clock: Callable[[], str] = _now,
) -> bool:
    """Record a transition request; return False when nothing was written."""

    if event not in TRANSITION_EVENTS:
        raise ValueError(f"unknown tracker transition event: {event}")
    record = _record(description)
    if record is None:
        return False
    state = resolve_transition(_config(home), record, event)
    if state is None:
        return False
    with evidence_transaction(home) as data:
        if event in data["items"].get(record.external_ref(), {}).get("transitions", {}):
            return False
        item = data["items"].setdefault(record.external_ref(), {})
        transitions = item.setdefault("transitions", {})
        transitions[event] = {
            "stage": "requested",
            "state": state,
            "result": None,
            "reason": None,
            "attempts": 0,
            "requested_at": clock(),
            "recorded_at": None,
        }
    return True


def request_merge_closure(
    home: Path,
    *,
    description: str,
    chainlink_issue_id: int,
    pr_url: str,
    clock: Callable[[], str] = _now,
) -> bool:
    """Record a closure request after a merge; return False when nothing was written."""

    record = _record(description)
    if record is None or not _config(home).close_on_merge:
        return False
    with evidence_transaction(home) as data:
        if "closure" in data["items"].get(record.external_ref(), {}):
            return False
        item = data["items"].setdefault(record.external_ref(), {})
        item["closure"] = {
            "stage": "requested",
            "chainlink_issue": chainlink_issue_id,
            "pr_url": pr_url,
            "result": None,
            "reason": None,
            "attempts": 0,
            "merge_commit_sha": None,
            "base_ref": None,
            "requested_at": clock(),
            "recorded_at": None,
        }
    return True
