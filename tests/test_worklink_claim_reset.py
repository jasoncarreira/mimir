"""An attempt budget consumed by infrastructure failure can be forgiven, once or twice.

The budget is derived from `WORKLINK_CLAIM` comments, so a fault that fails every
attempt — a broken base-repo fetch, a reclaimed git object store — exhausts a leaf
that was never itself at fault. Relabelling does not help: the count is recomputed
from comment history on each dispatch, which is why #1019, #1020 and #1023
re-exhausted within seconds of being re-promoted on 2026-07-28.

The reset marker forgives prior attempts. It is bounded, because the comment
carries no author and an unbounded marker would let a genuinely stuck leaf loop
forever by resetting itself — precisely what `max_attempts` exists to prevent.

It also must not disturb liveness. `claim_records_from_comments` still reports
every claim, because the duplicate-run guard and the stale-claim reaper judge live
runs from those records; forgetting them would let a second run claim an issue a
live run already holds.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Sequence

from mimir.worklink.autonomy import _find_latest_evidence_file_for_issue
from mimir.worklink.claims import (
    CLAIM_PREFIX,
    CLAIM_RESET_PREFIX,
    MAX_CLAIM_RESETS,
    ChainlinkClaims,
    claim_records_from_comments,
)
from mimir.worklink.checkout import create_isolated_checkout


def _claim(issue_id: int, attempt: int) -> str:
    return CLAIM_PREFIX + json.dumps({
        "issue_id": issue_id,
        "attempt": attempt,
        "agent_id": "mimir-worklink",
        "claimed_at": "2026-07-28T00:00:00+00:00",
    }, sort_keys=True)


def _reset(reason: str = "base repo fetch broken by reclaimed alternate") -> str:
    return CLAIM_RESET_PREFIX + json.dumps({"reason": reason})


def _claims() -> ChainlinkClaims:
    return ChainlinkClaims.__new__(ChainlinkClaims)  # only next_attempt is exercised


def _completed(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(args), 0, stdout="", stderr="")


def _repo_with_main(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
    for args in (
        ("config", "user.email", "t@example.com"),
        ("config", "user.name", "Test"),
        ("checkout", "-q", "-b", "main"),
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "base.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "base"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "push", "-q", "origin", "HEAD:main"], check=True
    )
    subprocess.run(
        ["git", "-C", str(origin), "symbolic-ref", "HEAD", "refs/heads/main"],
        check=True,
    )
    return repo


def test_exhausted_budget_without_a_reset_stays_exhausted():
    comments = [_claim(1019, 1), _claim(1019, 2), _claim(1019, 3)]
    assert _claims().next_attempt(comments) == 4  # > max_attempts(3) -> exhausted


def test_reset_forgives_the_attempts_before_it():
    comments = [_claim(1019, 1), _claim(1019, 2), _claim(1019, 3), _reset()]
    assert _claims().next_attempt(comments) == 4


def test_attempts_after_a_reset_count_again():
    comments = [_claim(1019, 1), _claim(1019, 2), _claim(1019, 3),
                _reset(), _claim(1019, 4), _claim(1019, 5)]
    claims = _claims()
    assert claims.next_attempt(comments) == 6
    assert claims.attempts_used(comments) == 2


def test_reset_dispatch_uses_fresh_checkout_ordinal_and_keeps_old_attempts(
    tmp_path: Path,
) -> None:
    issue_id = 1216
    repo = _repo_with_main(tmp_path)
    checkout_root = repo.parent / ".worklink" / repo.name
    retained = [checkout_root / f"{issue_id}-{attempt}" for attempt in range(1, 4)]
    for path in retained:
        path.mkdir(parents=True)
        (path / "post-mortem.txt").write_text("retained\n", encoding="utf-8")

    comments = [_claim(issue_id, attempt) for attempt in range(1, 4)] + [_reset()]
    claims = ChainlinkClaims(agent_id="mimir-worklink", runner=_completed, max_attempts=3)
    result = claims.claim_issue(issue_id, comments, labels=["worklink:ready"])

    assert result.claimed is True
    assert result.record is not None
    assert result.record.attempt == 4
    assert result.record.budget_attempt == 1
    lease = create_isolated_checkout(repo, issue_id=issue_id, attempt=result.record.attempt)
    assert lease.path == checkout_root / f"{issue_id}-4"
    assert (lease.path / ".git").is_dir()
    assert all((path / "post-mortem.txt").is_file() for path in retained)

    post_reset_comments = [*comments, result.record.to_comment()]
    assert claims.attempts_used(post_reset_comments) == 1
    assert claims.next_attempt(post_reset_comments) == 5

    evidence_dir = tmp_path / "home" / "state" / "worklink" / "evidence"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / f"{issue_id}-3.json").write_text(
        json.dumps({"attempt": 3, "status": "failed"}), encoding="utf-8"
    )
    newest = evidence_dir / f"{issue_id}-{result.record.attempt}.json"
    newest.write_text(
        json.dumps({"attempt": result.record.attempt, "status": "running"}),
        encoding="utf-8",
    )
    selected = _find_latest_evidence_file_for_issue(tmp_path / "home", issue_id)
    assert selected is not None
    assert selected[0] == newest


def test_reset_forgiveness_is_bounded_so_a_stuck_leaf_cannot_loop_forever():
    """Past the cap the budget re-asserts permanently and a human must decide."""
    comments: list[str] = []
    next_attempt = 1
    for _ in range(MAX_CLAIM_RESETS):
        comments += [
            _claim(1019, next_attempt),
            _claim(1019, next_attempt + 1),
            _claim(1019, next_attempt + 2),
            _reset(),
        ]
        next_attempt += 3
    # Both resets honoured: the budget is clean here.
    claims = _claims()
    assert claims.attempts_used(comments) == 0
    assert claims.next_attempt(comments) == next_attempt

    # One reset too many, after another exhausted round: no longer forgiven.
    comments += [
        _claim(1019, next_attempt),
        _claim(1019, next_attempt + 1),
        _claim(1019, next_attempt + 2),
        _reset(),
    ]
    assert claims.attempts_used(comments) == 3, (
        "an unbounded reset would let a stuck leaf retry forever, which is what "
        "max_attempts exists to prevent"
    )
    assert claims.next_attempt(comments) == next_attempt + 3


def test_reset_does_not_hide_claims_from_liveness_or_the_reaper():
    """The duplicate-run guard and stale-claim reaper must still see every claim."""
    comments = [_claim(1019, 1), _claim(1019, 2), _claim(1019, 3), _reset()]
    records = claim_records_from_comments(comments)
    assert [r.attempt for r in records] == [1, 2, 3], (
        "forgetting claims here would let a second run claim an issue that a live "
        "run already holds"
    )


def test_a_malformed_reset_payload_is_still_a_reset():
    """The marker is the signal; the payload is documentation for humans."""
    assert _claims().next_attempt(
        [_claim(1019, 1), _claim(1019, 2), _claim(1019, 3), CLAIM_RESET_PREFIX + "not json"]
    ) == 4
