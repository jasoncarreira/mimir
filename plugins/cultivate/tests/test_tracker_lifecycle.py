"""Tracker evidence store and lifecycle request writers."""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import pytest
import yaml

from mimir_cultivate.tracker.evidence import (
    EvidenceError,
    evidence_path,
    evidence_transaction,
    read_evidence,
)
from mimir_cultivate.tracker.lifecycle import request_merge_closure, request_transition
from mimir_cultivate.tracker.record import ExternalRecord, render_external_record

REC = ExternalRecord("linear", "ENG-1", "https://linear.app/a/issue/ENG-1")
DESCRIPTION = f"Goal: x\n\n{render_external_record(REC)}"
CLOCK = "2026-10-29T00:00:00+00:00"
PR = "https://github.com/acme/app/pull/5"


def _config(home: Path, *, transitions: dict | None = None, close: bool = False) -> None:
    section = {
        "close_on_merge": close,
        "adapters": {"linear": {"transitions": transitions or {}}},
    }
    (home / "worklink.yaml").write_text(yaml.safe_dump({"tracker": section}), encoding="utf-8")


def _items(home: Path) -> dict:
    return read_evidence(home)["items"]


def test_transaction_creates_schema_and_persists(tmp_path: Path) -> None:
    with evidence_transaction(tmp_path) as data:
        assert data == {"version": 1, "items": {}, "notices": []}
        data["notices"].append({"delivery_key": "k"})
    assert json.loads(evidence_path(tmp_path).read_text())["notices"] == [{"delivery_key": "k"}]
    assert not list(evidence_path(tmp_path).parent.glob("*.tmp"))


def test_transaction_exception_writes_nothing(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        with evidence_transaction(tmp_path) as data:
            data["notices"].append({"delivery_key": "k"})
            raise RuntimeError("boom")
    assert not evidence_path(tmp_path).exists()


@pytest.mark.parametrize("text", ["{", "[]", '{"version": 2, "items": {}, "notices": []}'])
def test_untrusted_file_is_refused_and_kept(tmp_path: Path, text: str) -> None:
    path = evidence_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(EvidenceError):
        with evidence_transaction(tmp_path):
            pass
    assert path.read_text(encoding="utf-8") == text


def _bump(home: str) -> None:
    for _ in range(10):
        with evidence_transaction(Path(home)) as data:
            data["notices"].append({"n": len(data["notices"])})


def test_transactions_serialize_across_processes(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("fork")
    procs = [ctx.Process(target=_bump, args=(str(tmp_path),)) for _ in range(3)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join()
        assert proc.exitcode == 0
    assert [n["n"] for n in read_evidence(tmp_path)["notices"]] == list(range(30))


def test_request_transition_records_requested_stage(tmp_path: Path) -> None:
    _config(tmp_path, transitions={"build_started": "In Progress"})
    assert request_transition(tmp_path, DESCRIPTION, "build_started", clock=lambda: CLOCK)
    assert _items(tmp_path)["linear:ENG-1"]["transitions"]["build_started"] == {
        "stage": "requested",
        "state": "In Progress",
        "result": None,
        "reason": None,
        "attempts": 0,
        "requested_at": CLOCK,
        "recorded_at": None,
    }


def test_unset_mapping_writes_nothing(tmp_path: Path) -> None:
    _config(tmp_path, transitions={"pr_opened": "In Review"})
    assert not request_transition(tmp_path, DESCRIPTION, "build_started")
    assert not evidence_path(tmp_path).exists()


def test_no_worklink_yaml_writes_nothing(tmp_path: Path) -> None:
    assert not request_transition(tmp_path, DESCRIPTION, "filed")
    assert not evidence_path(tmp_path).exists()


def test_no_record_writes_nothing(tmp_path: Path) -> None:
    _config(tmp_path, transitions={"filed": "Triaged"})
    assert not request_transition(tmp_path, "Goal: x", "filed")
    assert not request_transition(tmp_path, "External: bogus", "filed")
    assert not evidence_path(tmp_path).exists()


def test_existing_transition_entry_is_left_alone(tmp_path: Path) -> None:
    _config(tmp_path, transitions={"filed": "Triaged"})
    assert request_transition(tmp_path, DESCRIPTION, "filed", clock=lambda: CLOCK)
    with evidence_transaction(tmp_path) as data:
        data["items"]["linear:ENG-1"]["transitions"]["filed"]["stage"] = "done"
    assert not request_transition(tmp_path, DESCRIPTION, "filed")
    assert _items(tmp_path)["linear:ENG-1"]["transitions"]["filed"]["stage"] == "done"


def test_unknown_event_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        request_transition(tmp_path, DESCRIPTION, "nope")


def test_closure_requires_close_on_merge(tmp_path: Path) -> None:
    _config(tmp_path, close=False)
    assert not request_merge_closure(
        tmp_path, description=DESCRIPTION, chainlink_issue_id=9, pr_url=PR
    )
    assert not evidence_path(tmp_path).exists()


def test_closure_without_record_writes_nothing(tmp_path: Path) -> None:
    _config(tmp_path, close=True)
    assert not request_merge_closure(
        tmp_path, description="Goal: x", chainlink_issue_id=9, pr_url=PR
    )
    assert not evidence_path(tmp_path).exists()


def test_closure_requested_once(tmp_path: Path) -> None:
    _config(tmp_path, close=True)
    assert request_merge_closure(
        tmp_path, description=DESCRIPTION, chainlink_issue_id=9, pr_url=PR, clock=lambda: CLOCK
    )
    assert _items(tmp_path)["linear:ENG-1"]["closure"] == {
        "stage": "requested",
        "chainlink_issue": 9,
        "pr_url": PR,
        "result": None,
        "reason": None,
        "attempts": 0,
        "merge_commit_sha": None,
        "base_ref": None,
        "requested_at": CLOCK,
        "recorded_at": None,
    }
    assert not request_merge_closure(
        tmp_path, description=DESCRIPTION, chainlink_issue_id=10, pr_url=PR + "6"
    )
    assert _items(tmp_path)["linear:ENG-1"]["closure"]["chainlink_issue"] == 9


def test_evidence_holds_no_tracker_text(tmp_path: Path) -> None:
    _config(tmp_path, transitions={"filed": "Triaged"}, close=True)
    secret = DESCRIPTION + "\nSECRET-PROSE"
    request_transition(tmp_path, secret, "filed")
    request_merge_closure(tmp_path, description=secret, chainlink_issue_id=9, pr_url=PR)
    assert "SECRET-PROSE" not in evidence_path(tmp_path).read_text(encoding="utf-8")
