"""Tests for github-ci-watch's failure detection + seen-set dedup.

Mocks ``_gh`` (the ``gh run list`` wrapper) to return canned run JSON and
captures ``_emit`` calls. Asserts only NEW, *completed* failures emit,
that already-seen runs are skipped, and that every observed run id is
returned for the seen-set (regardless of whether it emitted).
"""
from __future__ import annotations

import pytest

import poller


def _run(run_id, conclusion="success", status="completed", workflow="CI"):
    return {
        "databaseId": run_id,
        "status": status,
        "conclusion": conclusion,
        "workflowName": workflow,
        "createdAt": "2026-05-31T00:00:00Z",
        "url": f"https://github.com/o/r/actions/runs/{run_id}",
    }


@pytest.fixture
def captured(monkeypatch):
    """Capture every ``_emit`` payload."""
    events: list[dict] = []
    monkeypatch.setattr(poller, "_emit", lambda ev: events.append(ev))
    return events


def test_emits_only_new_completed_failures(monkeypatch, captured):
    runs = [
        _run(1, "success"),                          # green → ignore
        _run(2, "failure"),                          # NEW failure → emit
        _run(3, "timed_out"),                        # NEW failure → emit
        _run(4, "failure", status="in_progress"),    # not completed → ignore
    ]
    monkeypatch.setattr(poller, "_gh", lambda *a: runs)

    newly = poller._check_repo("o/r", seen=set())

    emitted = {(e["event_type"], e["run_id"], e["conclusion"]) for e in captured}
    assert ("ci_failure", 2, "failure") in emitted
    assert ("ci_failure", 3, "timed_out") in emitted
    assert {e["run_id"] for e in captured} == {2, 3}  # not 1 (green) or 4 (running)
    # url is populated (regression: poller used to read the wrong JSON field)
    assert all(e["url"].endswith(str(e["run_id"])) for e in captured)
    # Only COMPLETED run ids are returned for the seen-set; the in-progress
    # run (4) is intentionally left UNSEEN so its eventual failure can still
    # emit on a later poll (chainlink #307).
    assert set(newly) == {1, 2, 3}


def test_skips_already_seen_failures(monkeypatch, captured):
    monkeypatch.setattr(poller, "_gh", lambda *a: [_run(2, "failure")])
    newly = poller._check_repo("o/r", seen={2})
    assert captured == []          # run 2 was already reported
    assert set(newly) == {2}       # still observed → stays in the seen-set


def test_gh_error_yields_no_events(monkeypatch, captured):
    monkeypatch.setattr(poller, "_gh", lambda *a: None)  # gh CLI failed
    assert poller._check_repo("o/r", seen=set()) == []
    assert captured == []


def test_in_progress_run_failure_emits_on_later_poll(monkeypatch, captured):
    """chainlink #307: a run first observed while in-progress must NOT be
    marked seen — so when it later completes as a failure, the failure still
    emits. Pre-fix the in-progress observation recorded it as seen and its
    eventual failure was silently skipped as already-reported."""
    # Poll 1: run 7 is in-progress → no emit, and NOT added to the seen-set.
    monkeypatch.setattr(
        poller, "_gh", lambda *a: [_run(7, "failure", status="in_progress")]
    )
    newly1 = poller._check_repo("o/r", seen=set())
    assert captured == []
    assert set(newly1) == set()  # in-progress → left unseen for re-check

    # Poll 2: run 7 has now completed as a failure. Because it was never
    # recorded as seen, the failure emits.
    monkeypatch.setattr(
        poller, "_gh", lambda *a: [_run(7, "failure", status="completed")]
    )
    newly2 = poller._check_repo("o/r", seen=set(newly1))
    assert {(e["event_type"], e["run_id"]) for e in captured} == {("ci_failure", 7)}
    assert set(newly2) == {7}
