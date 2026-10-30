from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from mimir_cultivate.tracker.thread import (
    Activity,
    Decision,
    TrackerIssue,
    activity_id,
    delivery_decision,
    parse_timestamp,
)
from mimir_cultivate.tracker.transport import TrackerUnavailable

BOT = "bot"
BASE = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
TRUST = {"alice": True, "carol": True, "mallory": False, "flaky": None, BOT: True}


def at(minutes: int) -> datetime:
    return BASE + timedelta(minutes=minutes)


def act(kind: str, ident: str, minutes: int, actor: str | None) -> Activity:
    return Activity(kind, ident, at(minutes), actor)  # type: ignore[arg-type]


def make_issue(*activities: Activity, **overrides: object) -> TrackerIssue:
    fields: dict[str, object] = dict(
        adapter="linear",
        stable_id="1",
        ref="ENG-1",
        url="https://linear.app/acme/issue/ENG-1",
        project="ENG",
        title="Title",
        body="Body",
        author_id="alice",
        is_open=True,
        assigned_to_bot=True,
        activities=tuple(activities),
        comments=(),
        truncated=False,
        bot_id=BOT,
    )
    fields.update(overrides)
    return TrackerIssue(**fields)  # type: ignore[arg-type]


def trust(actor_id: str, issue: TrackerIssue) -> bool | None:
    return TRUST[actor_id]


def decide(*activities: Activity, **overrides: object) -> Decision:
    return delivery_decision(make_issue(*activities, **overrides), trust)


ASSIGN = act("assigned", "ev-a", 1, "alice")


def test_trusted_assignment_delivers_with_it_as_cutoff() -> None:
    decision = decide(ASSIGN)

    assert decision == Decision("deliver", cutoff=ASSIGN)


@pytest.mark.parametrize("overrides", [{"is_open": False}, {"assigned_to_bot": False}])
def test_closed_or_unassigned_issue_is_skipped(overrides: dict[str, object]) -> None:
    assert decide(ASSIGN, **overrides) == Decision("skip")


def test_outsider_after_trusted_assignment_withholds() -> None:
    decision = decide(ASSIGN, act("comment", "ev-c", 2, "mallory"))

    assert decision == Decision("withhold", "untrusted_latest_activity")


def test_trusted_comment_after_outsider_delivers_full_thread() -> None:
    reply = act("comment", "ev-r", 3, "alice")

    decision = decide(ASSIGN, act("comment", "ev-c", 2, "mallory"), reply)

    assert decision == Decision("deliver", cutoff=reply)


@pytest.mark.parametrize("kind", ["body_edit", "title_edit", "comment_edit"])
def test_untrusted_edit_after_cutoff_withholds(kind: str) -> None:
    decision = decide(ASSIGN, act(kind, "ev-e", 2, "mallory"))

    assert decision == Decision("withhold", "untrusted_latest_activity")


def test_untrusted_title_edit_after_cutoff_withholds() -> None:
    assert decide(ASSIGN, act("title_edit", "ev-t", 2, "mallory")).reason == (
        "untrusted_latest_activity"
    )


def test_untrusted_comment_edit_after_cutoff_withholds() -> None:
    comment = act("comment", "ev-c", 2, "alice")

    decision = decide(ASSIGN, comment, act("comment_edit", "ev-ce", 3, "mallory"))

    assert decision == Decision("withhold", "untrusted_latest_activity")


@pytest.mark.parametrize("kind", ["body_edit", "comment_edit", "title_edit"])
def test_untrusted_edit_masked_by_later_trusted_edit(kind: str) -> None:
    decision = decide(
        ASSIGN, act(kind, "ev-bad", 2, "mallory"), act(kind, "ev-good", 3, "carol")
    )

    assert decision == Decision("withhold", "untrusted_latest_activity")


def test_unattributable_edit_after_cutoff_withholds() -> None:
    decision = decide(ASSIGN, act("comment_edit", "ev-e", 2, None))

    assert decision == Decision("withhold", "untrusted_latest_activity")


def test_unattributable_edit_before_cutoff_is_attested() -> None:
    cutoff = act("assigned", "ev-a", 5, "alice")

    decision = decide(act("comment_edit", "ev-e", 2, None), cutoff)

    assert decision == Decision("deliver", cutoff=cutoff)


def test_unattributable_actor_is_never_looked_up() -> None:
    looked_up: list[str] = []

    def spy(actor_id: str, issue: TrackerIssue) -> bool | None:
        looked_up.append(actor_id)
        return True

    delivery_decision(make_issue(ASSIGN, act("body_edit", "ev-e", 2, None)), spy)

    assert looked_up == []


def test_same_timestamp_untrusted_activity_withholds() -> None:
    decision = decide(
        act("comment", "ev-z", 2, "alice"), act("body_edit", "ev-a", 2, "mallory"), ASSIGN
    )

    assert decision == Decision("withhold", "untrusted_latest_activity")


@pytest.mark.parametrize(("assign_id", "comment_id"), [("ev-1", "ev-9"), ("ev-9", "ev-1")])
def test_untrusted_comment_tied_with_trusted_assignment_withholds(
    assign_id: str, comment_id: str
) -> None:
    decision = decide(act("assigned", assign_id, 2, "alice"), act("comment", comment_id, 2, "mallory"))

    assert decision == Decision("withhold", "untrusted_latest_activity")


@pytest.mark.parametrize("verdict", [1, "yes", [True], object()])
def test_truthy_non_bool_verdict_does_not_attest(verdict: object) -> None:
    decision = delivery_decision(make_issue(ASSIGN), lambda actor_id, issue: verdict)  # type: ignore[arg-type,return-value]

    assert decision == Decision("withhold", "untrusted_latest_activity")


def test_same_timestamp_group_cutoff_is_the_greatest_id() -> None:
    low = act("assigned", "ev-1", 2, "alice")
    high = act("comment", "ev-9", 2, "carol")

    assert decide(low, high).cutoff == high


def test_trusted_edits_only_continue_to_cutoff() -> None:
    decision = decide(ASSIGN, act("body_edit", "ev-e", 2, "carol"))

    assert decision == Decision("deliver", cutoff=ASSIGN)


def test_bot_activity_neither_attests_nor_withholds() -> None:
    decision = decide(
        ASSIGN, act("comment", "ev-b", 2, BOT), act("body_edit", "ev-b2", 3, BOT)
    )

    assert decision == Decision("deliver", cutoff=ASSIGN)


def test_bot_self_assignment_has_no_trusted_attestation() -> None:
    decision = decide(act("assigned", "ev-s", 1, BOT), act("comment", "ev-b", 2, BOT))

    assert decision == Decision("withhold", "no_trusted_attestation")


def test_no_activity_has_no_trusted_attestation() -> None:
    assert decide() == Decision("withhold", "no_trusted_attestation")


def test_unavailable_trust_fails_closed() -> None:
    assert decide(ASSIGN, act("comment", "ev-c", 2, "flaky")) == Decision("unavailable")


def test_trust_lookup_error_is_unavailable() -> None:
    def failing(actor_id: str, issue: TrackerIssue) -> bool | None:
        raise TrackerUnavailable("http_403")

    decision = delivery_decision(make_issue(ASSIGN), failing)

    assert decision == Decision("unavailable")


def test_untrusted_wins_over_unavailable_in_one_group() -> None:
    decision = decide(
        ASSIGN, act("comment", "ev-c", 2, "flaky"), act("body_edit", "ev-e", 2, "mallory")
    )

    assert decision == Decision("withhold", "untrusted_latest_activity")


def test_untrusted_activity_older_than_the_cutoff_is_attested() -> None:
    cutoff = act("assigned", "ev-a", 5, "alice")

    decision = decide(act("comment", "ev-c", 2, "mallory"), cutoff)

    assert decision == Decision("deliver", cutoff=cutoff)


def test_truncated_withholds_thread_too_long() -> None:
    assert decide(ASSIGN, truncated=True) == Decision("withhold", "thread_too_long")


def test_truncation_does_not_override_skip() -> None:
    assert decide(ASSIGN, truncated=True, is_open=False) == Decision("skip")


def test_activity_id_is_a_stable_prefixed_digest() -> None:
    first = activity_id("linear", "c1")

    assert first == activity_id("linear", "c1")
    assert first != activity_id("jira", "c1")
    assert len(first) == 26 and first.startswith("ev")


def test_parse_timestamp_normalizes_offsets_to_utc() -> None:
    assert parse_timestamp("2026-10-01T14:00:00.000+0200") == BASE
    assert parse_timestamp("2026-10-01T12:00:00Z") == BASE


@pytest.mark.parametrize("value", ["2026-10-01T12:00:00", "nope", None, 5])
def test_parse_timestamp_rejects_naive_and_malformed(value: object) -> None:
    with pytest.raises(ValueError):
        parse_timestamp(value)
