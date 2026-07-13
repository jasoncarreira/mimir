from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import subprocess
from typing import Sequence

import pytest

from mimir.worklink.claims import ChainlinkClaims, ClaimRecord, claim_records_from_comments


def completed(args: Sequence[str], returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(args), returncode, stdout="", stderr="")


def test_claim_records_round_trip_and_next_attempt() -> None:
    record = ClaimRecord(
        issue_id=439,
        attempt=2,
        agent_id="mimir-a",
        claimed_at=datetime(2026, 6, 11, 5, tzinfo=UTC),
    )
    comments = ["noise", record.to_comment()]

    parsed = claim_records_from_comments(comments)

    assert parsed == [record]
    claims = ChainlinkClaims(agent_id="mimir-b")
    assert claims.next_attempt(comments) == 3


def test_claim_issue_records_attempt_and_labels_transition() -> None:
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return completed(args)

    claims = ChainlinkClaims(
        chainlink_bin="chainlink",
        agent_id="mimir-a",
        runner=runner,
        clock=lambda: datetime(2026, 6, 11, 5, tzinfo=UTC),
    )

    result = claims.claim_issue(439)

    assert result.claimed is True
    assert result.record is not None
    assert result.record.attempt == 1
    assert calls[0] == ["chainlink", "locks", "claim", "439"]
    assert ["chainlink", "issue", "unlabel", "439", "worklink:ready"] in calls
    assert ["chainlink", "issue", "label", "439", "worklink:in-progress"] in calls
    comment_calls = [call for call in calls if call[:3] == ["chainlink", "issue", "comment"]]
    assert comment_calls and "WORKLINK_CLAIM" in comment_calls[0][-1]


def test_heartbeat_issue_appends_fresh_claim_record() -> None:
    calls: list[list[str]] = []
    heartbeat_at = datetime(2026, 6, 11, 5, 30, tzinfo=UTC)

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return completed(args)

    claims = ChainlinkClaims(
        chainlink_bin="chainlink",
        agent_id="mimir-a",
        runner=runner,
        clock=lambda: heartbeat_at,
    )
    record = ClaimRecord(
        issue_id=439,
        attempt=1,
        agent_id="mimir-a",
        claimed_at=datetime(2026, 6, 11, 5, tzinfo=UTC),
    )

    updated = claims.heartbeat_issue(record)

    assert updated.heartbeat_at == heartbeat_at
    assert calls == [["chainlink", "issue", "comment", "439", updated.to_comment()]]
    parsed = claim_records_from_comments([calls[0][-1]])
    assert parsed == [updated]


def test_claim_issue_enforces_max_active_locks_after_reservation() -> None:
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args = list(args)
        calls.append(args)
        if args[1:4] == ["locks", "list", "--json"]:
            # The new issue lock is already reserved; cap=1 means this would be
            # the second active worker, so claim_issue must release it before
            # mutating labels/comments.
            return subprocess.CompletedProcess(args, 0, stdout='{"locks":{"old":{},"new":{}}}', stderr="")
        return completed(args)

    claims = ChainlinkClaims(agent_id="mimir-a", runner=runner)
    result = claims.claim_issue(8, max_active_locks=1)

    assert result.claimed is False
    assert result.reason and "concurrency cap reached" in result.reason
    assert ["chainlink", "locks", "claim", "8"] in calls
    assert ["chainlink", "locks", "release", "8"] in calls
    assert not any(call[:3] == ["chainlink", "issue", "label"] for call in calls)
    assert not any(call[:3] == ["chainlink", "issue", "comment"] for call in calls)


def test_list_issue_ids_falls_back_when_id_is_null() -> None:
    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if list(args)[1:3] == ["issue", "list"]:
            return subprocess.CompletedProcess(
                list(args),
                0,
                stdout=json.dumps([{"id": None, "number": 81}, {"id": "82"}]),
                stderr="",
            )
        return completed(args)

    claims = ChainlinkClaims(agent_id="t", runner=runner)
    assert claims.issue_ids_with_label("worklink:ready") == [81, 82]


def test_claim_issue_blocks_when_attempts_exhausted() -> None:
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return completed(args)

    old = [
        ClaimRecord(7, 1, "a", datetime(2026, 6, 11, tzinfo=UTC)).to_comment(),
        ClaimRecord(7, 2, "a", datetime(2026, 6, 11, tzinfo=UTC)).to_comment(),
        ClaimRecord(7, 3, "a", datetime(2026, 6, 11, tzinfo=UTC)).to_comment(),
    ]
    claims = ChainlinkClaims(agent_id="mimir-a", runner=runner, max_attempts=3)

    result = claims.claim_issue(7, old)

    assert result.claimed is False
    assert result.attempts_exhausted is True
    # chainlink #825: exhaustion is judged AFTER lock ownership is established
    # (so a duplicate bouncing off a live run can never mislabel it); the
    # transient lock is released before the blocked transition.
    assert ["chainlink", "locks", "claim", "7"] in calls
    assert ["chainlink", "locks", "release", "7"] in calls
    assert ["chainlink", "issue", "label", "7", "worklink:blocked"] in calls


def test_reaper_enforces_own_ttl_before_steal() -> None:
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        if list(args)[1:3] == ["locks", "list"]:
            return subprocess.CompletedProcess(
                list(args),
                0,
                stdout=json.dumps({"locks": {"2": {"issue_id": 2}}}),
                stderr="",
            )
        return completed(args)

    now = datetime(2026, 6, 11, 5, tzinfo=UTC)
    claims = ChainlinkClaims(agent_id="mimir-a", runner=runner, clock=lambda: now)
    fresh = ClaimRecord(1, 1, "live", now - timedelta(minutes=5))
    stale = ClaimRecord(2, 1, "stale", now - timedelta(hours=3))

    reaped = claims.reap_stale_claims([fresh, stale], ttl=timedelta(hours=1))

    assert reaped == [stale]
    assert ["chainlink", "locks", "steal", "1"] not in calls
    assert ["chainlink", "locks", "steal", "2"] in calls
    assert ["chainlink", "locks", "release", "2"] in calls
    assert ["chainlink", "issue", "label", "2", "worklink:ready"] in calls
    comments = [call[-1] for call in calls if call[:3] == ["chainlink", "issue", "comment"]]
    assert any("stale_agent_id" in comment and "stale" in comment for comment in comments)


def test_reaper_blocks_after_max_attempts() -> None:
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        if list(args)[1:3] == ["locks", "list"]:
            return subprocess.CompletedProcess(
                list(args),
                0,
                stdout=json.dumps({"locks": {"2": {"issue_id": 2}}}),
                stderr="",
            )
        return completed(args)

    now = datetime(2026, 6, 11, 5, tzinfo=UTC)
    claims = ChainlinkClaims(agent_id="mimir-a", runner=runner, clock=lambda: now, max_attempts=3)
    stale = ClaimRecord(2, 3, "stale", now - timedelta(hours=3))

    reaped = claims.reap_stale_claims([stale], ttl=timedelta(hours=1))

    assert reaped == [stale]
    assert ["chainlink", "issue", "label", "2", "worklink:blocked"] in calls
    assert ["chainlink", "issue", "label", "2", "worklink:ready"] not in calls


def test_transition_failed_attempt_with_retries_returns_to_ready() -> None:
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return completed(args)

    claims = ChainlinkClaims(agent_id="mimir-a", runner=runner, max_attempts=3)

    claims.transition_issue(
        2, status="failed", review_ready=False, attempt=1, reason="tests_failed"
    )

    assert ["chainlink", "issue", "label", "2", "worklink:ready"] in calls
    assert ["chainlink", "issue", "label", "2", "worklink:blocked"] not in calls
    assert any(
        call[:3] == ["chainlink", "issue", "comment"] and "tests_failed" in call[-1]
        for call in calls
    )


def test_transition_blocked_comment_uses_blocked_prefix() -> None:
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return completed(args)

    claims = ChainlinkClaims(agent_id="mimir-a", runner=runner, max_attempts=3)

    # A backend-signalled block on attempt 1: labels worklink:blocked and the
    # reason posts under WORKLINK_BLOCKED, not the misleading WORKLINK_FAILED.
    claims.transition_issue(
        2, status="blocked", review_ready=False, attempt=1, reason="acceptance criteria contradict #438"
    )

    assert ["chainlink", "issue", "label", "2", "worklink:blocked"] in calls
    assert ["chainlink", "issue", "label", "2", "worklink:ready"] not in calls
    assert any(
        call[:3] == ["chainlink", "issue", "comment"]
        and call[-1] == "WORKLINK_BLOCKED acceptance criteria contradict #438"
        for call in calls
    )


def test_transition_failed_exhausted_attempt_blocks() -> None:
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return completed(args)

    claims = ChainlinkClaims(agent_id="mimir-a", runner=runner, max_attempts=3)

    claims.transition_issue(2, status="failed", review_ready=False, attempt=3)

    assert ["chainlink", "issue", "label", "2", "worklink:blocked"] in calls
    assert ["chainlink", "issue", "label", "2", "worklink:ready"] not in calls


def _reclaim_runner(calls, stdout="You already hold the lock on issue #783"):
    def runner(args):
        calls.append(list(args))
        if list(args)[1:3] == ["locks", "claim"]:
            return subprocess.CompletedProcess(list(args), 0, stdout=stdout, stderr="")
        return subprocess.CompletedProcess(list(args), 0, stdout="", stderr="")
    return runner


def test_same_agent_reclaim_with_fresh_heartbeat_is_refused():
    """chainlink #822: 'You already hold the lock' rc=0 must not admit a
    duplicate process while the owner's claim heartbeat is fresh."""
    now = datetime(2026, 7, 3, 19, 0, tzinfo=UTC)
    calls: list[list[str]] = []
    claims = ChainlinkClaims(agent_id="mimir-worklink-epic", runner=_reclaim_runner(calls), clock=lambda: now)
    fresh = ClaimRecord(issue_id=783, attempt=1, agent_id="mimir-worklink-epic", claimed_at=now, heartbeat_at=now)

    result = claims.claim_issue(783, [fresh.to_comment()])

    assert result.claimed is False
    assert result.reason == "duplicate_run_live"
    # refused WITHOUT mutating state: reads (issue show) are fine, but no
    # steal and no label/comment writes.
    assert not any(
        call[1:3] == ["locks", "steal"]
        or (call[1] == "issue" and call[2] in ("label", "unlabel", "comment"))
        for call in calls
        if len(call) > 2
    )


def test_same_agent_reclaim_with_stale_heartbeat_steals_and_proceeds():
    now = datetime(2026, 7, 3, 19, 0, tzinfo=UTC)
    calls: list[list[str]] = []
    claims = ChainlinkClaims(agent_id="mimir-worklink-epic", runner=_reclaim_runner(calls), clock=lambda: now)
    stale = ClaimRecord(issue_id=783, attempt=1, agent_id="mimir-worklink-epic", claimed_at=now - timedelta(hours=2), heartbeat_at=now - timedelta(hours=1))

    result = claims.claim_issue(783, [stale.to_comment()])

    assert result.claimed is True
    assert any(call[1:3] == ["locks", "steal"] for call in calls)


def test_duplicate_vs_live_final_attempt_never_labels_blocked():
    """chainlink #825 (run-15 regression): a duplicate bouncing off a LIVE
    final-attempt claim must refuse as duplicate_run_live — the old ordering
    declared attempts_exhausted and mislabeled the healthy epic blocked."""
    now = datetime(2026, 7, 3, 22, 20, tzinfo=UTC)
    calls: list[list[str]] = []
    claims = ChainlinkClaims(agent_id="mimir-worklink-epic", runner=_reclaim_runner(calls), clock=lambda: now)
    live_final = ClaimRecord(
        issue_id=783, attempt=3, agent_id="mimir-worklink-epic",
        claimed_at=now - timedelta(minutes=10), heartbeat_at=now - timedelta(minutes=1),
    )

    result = claims.claim_issue(783, [live_final.to_comment()])

    assert result.claimed is False
    assert result.reason == "duplicate_run_live"
    assert result.attempts_exhausted is False
    assert not any(call[1:3] == ["issue", "label"] for call in calls)


def test_claim_issue_refuses_worklink_review_label() -> None:
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return completed(args)

    claims = ChainlinkClaims(agent_id="mimir-a", runner=runner)

    result = claims.claim_issue(100, labels=["worklink:review", "worklink:ready"])

    assert result.claimed is False
    assert result.reason == "lifecycle_state_incompatible"
    assert not any(call[1:3] == ["locks", "claim"] for call in calls)


def test_claim_issue_accepts_worklink_ready_label() -> None:
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return completed(args)

    claims = ChainlinkClaims(agent_id="mimir-a", runner=runner)

    result = claims.claim_issue(102, labels=["worklink:ready"])

    assert result.claimed is True
    assert result.record is not None


def test_claim_issue_accepts_worklink_in_progress_label() -> None:
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return completed(args)

    claims = ChainlinkClaims(agent_id="mimir-a", runner=runner)

    result = claims.claim_issue(103, labels=["worklink:in-progress"])

    assert result.claimed is True
    assert result.record is not None


def test_claim_issue_refuses_when_review_ready_evidence_exists(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return completed(args)

    evidence_dir = tmp_path / "state" / "worklink" / "evidence"
    evidence_dir.mkdir(parents=True)
    evidence_file = evidence_dir / "200-1.json"
    evidence_file.write_text(
        json.dumps({
            "issue": 200,
            "attempt": 1,
            "status": "completed",
            "review_ready": True,
            "pr_url": "https://github.com/owner/repo/pull/123",
        }),
        encoding="utf-8",
    )

    claims = ChainlinkClaims(agent_id="mimir-a", runner=runner)

    result = claims.claim_issue(200, labels=["worklink:ready"], home_path=str(tmp_path))

    assert result.claimed is False
    assert result.reason == "review_ready_evidence_exists"
    assert not any(call[1:3] == ["locks", "claim"] for call in calls)


def test_claim_issue_allows_without_evidence_when_worklink_ready(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return completed(args)

    evidence_dir = tmp_path / "state" / "worklink" / "evidence"
    evidence_dir.mkdir(parents=True)

    claims = ChainlinkClaims(agent_id="mimir-a", runner=runner)

    result = claims.claim_issue(201, labels=["worklink:ready"], home_path=str(tmp_path))

    assert result.claimed is True
    assert result.record is not None
