"""Orchestrator request points for tracker lifecycle events."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from mimir_cultivate.tracker.evidence import read_evidence
from mimir_cultivate.tracker.record import ExternalRecord, render_external_record
from mimir_cultivate.worklink import orchestrator

REC = ExternalRecord("linear", "ENG-1", "https://linear.app/a/issue/ENG-1")
DESCRIPTION = f"Goal: x\n\n{render_external_record(REC)}"


def _issue(description: str = DESCRIPTION) -> orchestrator.IssueContext:
    return orchestrator.IssueContext(700, "leaf", description, set())


def _config(home: Path, transitions: dict[str, str]) -> None:
    section = {"adapters": {"linear": {"transitions": transitions}}}
    (home / "worklink.yaml").write_text(yaml.safe_dump({"tracker": section}), encoding="utf-8")


@pytest.mark.parametrize("event", ["build_started", "pr_opened"])
def test_unset_mapping_writes_nothing(tmp_path: Path, event: str) -> None:
    _config(tmp_path, {})
    orchestrator._request_tracker_transition(tmp_path, _issue(), event)
    assert read_evidence(tmp_path)["items"] == {}


@pytest.mark.parametrize("event", ["build_started", "pr_opened"])
def test_record_free_issue_writes_nothing(tmp_path: Path, event: str) -> None:
    _config(tmp_path, {event: "In Progress"})
    orchestrator._request_tracker_transition(tmp_path, _issue("Goal: x"), event)
    assert read_evidence(tmp_path)["items"] == {}


@pytest.mark.parametrize("event", ["build_started", "pr_opened"])
def test_mapped_event_is_requested_once(tmp_path: Path, event: str) -> None:
    _config(tmp_path, {event: "In Progress"})
    orchestrator._request_tracker_transition(tmp_path, _issue(), event)
    orchestrator._request_tracker_transition(tmp_path, _issue(), event)
    transitions = read_evidence(tmp_path)["items"]["linear:ENG-1"]["transitions"]
    assert transitions[event]["stage"] == "requested"
    assert transitions[event]["state"] == "In Progress"


def test_request_failure_is_logged_without_text_and_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(orchestrator, "_log_event", lambda name, **kw: events.append((name, kw)))

    def boom(*args, **kwargs):
        raise RuntimeError("secret tracker text")

    monkeypatch.setattr(orchestrator, "request_transition", boom)
    orchestrator._request_tracker_transition(tmp_path, _issue(), "build_started")
    assert events == [(
        "tracker_transition_request_failed",
        {"issue_id": 700, "event": "build_started", "error": "RuntimeError"},
    )]
    assert "secret" not in json.dumps(events)
