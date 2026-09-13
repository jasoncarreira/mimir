"""Cancellation notification policy, never a source of remediation authority."""
from __future__ import annotations


def classify_cancelled_run(run: dict, runs: list[dict]) -> str:
    """Classify replacement metadata, not the unknowable cause of cancellation.

    Accept the REST and gh run-list projections. Run IDs establish creation
    order, independent of list ordering or completion time. Missing identity
    cannot prove replacement and therefore leaves operator attention enabled.
    """
    run_id = run.get("id", run.get("databaseId"))
    head = run.get("head_sha", run.get("headSha"))
    workflow = run.get("workflow_id", run.get("workflowDatabaseId"))
    if type(run_id) is not int or run_id <= 0 or not head:
        return "UNKNOWN"
    overtaken = False
    for other in runs:
        other_id = other.get("id", other.get("databaseId"))
        if (type(other_id) is not int or other_id <= run_id
                or other.get("head_sha", other.get("headSha")) != head):
            continue
        if workflow and other.get("workflow_id", other.get("workflowDatabaseId")) == workflow:
            return "SUPERSEDED"
        if other.get("status") == "completed" and other.get("conclusion") == "success":
            overtaken = True
    return "OVERTAKEN_BY_SUCCESS" if overtaken else "UNKNOWN"
