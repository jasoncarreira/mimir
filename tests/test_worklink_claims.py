from __future__ import annotations

from datetime import UTC, datetime, timedelta
import subprocess
from typing import Sequence

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
    assert ["chainlink", "locks", "claim", "7"] not in calls
    assert ["chainlink", "issue", "label", "7", "worklink:blocked"] in calls


def test_reaper_enforces_own_ttl_before_steal() -> None:
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
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
