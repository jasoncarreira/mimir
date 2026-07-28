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

from mimir.worklink.claims import (
    CLAIM_PREFIX,
    CLAIM_RESET_PREFIX,
    MAX_CLAIM_RESETS,
    ChainlinkClaims,
    claim_records_from_comments,
)


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


def test_exhausted_budget_without_a_reset_stays_exhausted():
    comments = [_claim(1019, 1), _claim(1019, 2), _claim(1019, 3)]
    assert _claims().next_attempt(comments) == 4  # > max_attempts(3) -> exhausted


def test_reset_forgives_the_attempts_before_it():
    comments = [_claim(1019, 1), _claim(1019, 2), _claim(1019, 3), _reset()]
    assert _claims().next_attempt(comments) == 1


def test_attempts_after_a_reset_count_again():
    comments = [_claim(1019, 1), _claim(1019, 2), _claim(1019, 3),
                _reset(), _claim(1019, 1), _claim(1019, 2)]
    assert _claims().next_attempt(comments) == 3


def test_reset_forgiveness_is_bounded_so_a_stuck_leaf_cannot_loop_forever():
    """Past the cap the budget re-asserts permanently and a human must decide."""
    comments: list[str] = []
    for _ in range(MAX_CLAIM_RESETS):
        comments += [_claim(1019, 1), _claim(1019, 2), _claim(1019, 3), _reset()]
    # Both resets honoured: the budget is clean here.
    assert _claims().next_attempt(comments) == 1

    # One reset too many, after another exhausted round: no longer forgiven.
    comments += [_claim(1019, 1), _claim(1019, 2), _claim(1019, 3), _reset()]
    assert _claims().next_attempt(comments) == 4, (
        "an unbounded reset would let a stuck leaf retry forever, which is what "
        "max_attempts exists to prevent"
    )


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
    ) == 1
