from __future__ import annotations

import pytest

from mimir.ci_attention import classify_cancelled_run


@pytest.mark.parametrize("projection", ["rest", "gh"])
@pytest.mark.parametrize("replacement, expected", [
    ({}, "SUPERSEDED"),
    ({"workflow_id": 2, "status": "completed", "conclusion": "success"}, "OVERTAKEN_BY_SUCCESS"),
    ({"head_sha": "other"}, "UNKNOWN"),
    ({"head_sha": "other", "status": "completed", "conclusion": "success"}, "UNKNOWN"),
    ({"id": 9}, "UNKNOWN"),
    ({"id": 10}, "UNKNOWN"),
    ({"workflow_id": 2}, "UNKNOWN"),
    ({"workflow_id": 2, "conclusion": "success"}, "UNKNOWN"),
    ({"workflow_id": 2, "status": "completed", "conclusion": "failure"}, "UNKNOWN"),
])
def test_replacement_classification(projection, replacement, expected):
    run = dict(id=10, head_sha="abc", workflow_id=1, status="completed", conclusion="cancelled")
    other = dict(run, id=11, status="queued", conclusion=None)
    other.update(replacement)
    if projection == "gh":
        names = {"id": "databaseId", "head_sha": "headSha", "workflow_id": "workflowDatabaseId"}
        run, other = [{names.get(k, k): v for k, v in item.items()} for item in (run, other)]
    assert classify_cancelled_run(run, [other, run]) == expected
    assert classify_cancelled_run(run, [run, other]) == expected


@pytest.mark.parametrize("missing", ["id", "head_sha", "workflow_id"])
def test_missing_identity_cannot_prove_supersession(missing):
    run = dict(id=10, head_sha="abc", workflow_id=1)
    del run[missing]
    assert classify_cancelled_run(run, [dict(run, id=11)]) == "UNKNOWN"


def test_supersession_takes_precedence_over_success():
    run = dict(id=10, head_sha="abc", workflow_id=1)
    success = dict(run, id=11, workflow_id=2, status="completed", conclusion="success")
    newer = dict(run, id=12, status="queued")
    assert classify_cancelled_run(run, [success, newer]) == "SUPERSEDED"
