"""ChainlinkWork: search, create, arm and the never-armed adoption check."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from mimir_cultivate.tracker.record import ExternalRecord, render_external_record
from mimir_cultivate.tracker.work import ChainlinkWork, WorkUnavailable
from mimir_cultivate.worklink.claims import ClaimRecord
from tracker_fakes import FakeChainlink

REC = ExternalRecord("github_issues", "acme/app#1", "https://github.com/acme/app/issues/1")
OTHER = ExternalRecord("github_issues", "acme/app#12", "https://github.com/acme/app/issues/12")


def _work(fake: FakeChainlink) -> ChainlinkWork:
    return ChainlinkWork("chainlink", None, fake)


def _described(record: ExternalRecord) -> str:
    return f"Goal: x\n\n{render_external_record(record)}"


def test_find_returns_only_exact_record_matches() -> None:
    fake = FakeChainlink()
    wanted = fake.add(_described(REC))
    fake.add(_described(OTHER))
    fake.add("mentions github_issues:acme/app#1 in prose only")
    assert _work(fake).find(REC.external_ref()) == [wanted]


def test_find_returns_ids_sorted_when_duplicated() -> None:
    fake = FakeChainlink()
    first = fake.add(_described(REC))
    second = fake.add(_described(REC))
    assert _work(fake).find(REC.external_ref()) == [first, second]


def test_find_with_no_match_is_empty() -> None:
    assert _work(FakeChainlink()).find(REC.external_ref()) == []


def test_find_search_failure_raises_not_empty() -> None:
    fake = FakeChainlink()
    fake.fail.add("search")
    with pytest.raises(WorkUnavailable):
        _work(fake).find(REC.external_ref())


def test_find_reads_description_from_show_when_search_omits_it() -> None:
    fake = FakeChainlink()
    issue_id = fake.add(_described(REC))

    def runner(argv):
        result = fake(argv)
        if argv[2] == "search":
            result.stdout = f'[{{"id": {issue_id}, "title": "t"}}]'
        return result

    assert ChainlinkWork("chainlink", None, runner).find(REC.external_ref()) == [issue_id]


def test_find_skips_malformed_record_lines() -> None:
    fake = FakeChainlink()
    fake.add(f"External: github_issues:acme/app#1 not-a-url\n{REC.external_ref()}")
    assert _work(fake).find(REC.external_ref()) == []


def test_create_appends_record_and_arms_with_ready_label() -> None:
    fake = FakeChainlink()
    issue_id = _work(fake).create(title="T", description="Goal: x", record=REC)
    assert issue_id is not None
    issue = fake.issues[issue_id]
    assert issue["description"] == f"Goal: x\n\n{render_external_record(REC)}"
    assert issue["labels"] == ["worklink:ready"]


def test_create_epic_adds_epic_label() -> None:
    fake = FakeChainlink()
    issue_id = _work(fake).create(title="T", description="d", record=REC, epic=True)
    assert fake.issues[issue_id]["labels"] == ["worklink:ready", "worklink:epic"]


def test_create_without_reported_id_returns_none() -> None:
    fake = FakeChainlink()
    fake.silent_create = True
    assert _work(fake).create(title="T", description="d", record=REC) is None
    assert _work(fake).find(REC.external_ref()) != []


def test_create_failure_raises() -> None:
    fake = FakeChainlink()
    fake.fail.add("create")
    with pytest.raises(WorkUnavailable):
        _work(fake).create(title="T", description="d", record=REC)


def test_runner_oserror_is_unavailable() -> None:
    def broken(argv):
        raise FileNotFoundError("chainlink")

    with pytest.raises(WorkUnavailable):
        ChainlinkWork("chainlink", None, broken).find(REC.external_ref())


def test_arm_labels_ready_and_is_idempotent() -> None:
    fake = FakeChainlink()
    issue_id = fake.add(_described(REC))
    work = _work(fake)
    work.arm(issue_id)
    work.arm(issue_id)
    assert fake.issues[issue_id]["labels"] == ["worklink:ready"]


def test_arm_failure_raises() -> None:
    fake = FakeChainlink()
    issue_id = fake.add(_described(REC))
    fake.fail.add("label")
    with pytest.raises(WorkUnavailable):
        _work(fake).arm(issue_id)


def _claim_comment() -> str:
    return ClaimRecord(
        issue_id=1, attempt=1, agent_id="a", claimed_at=datetime(2026, 1, 1, tzinfo=UTC)
    ).to_comment()


def test_never_armed_for_open_unlabeled_unclaimed_issue() -> None:
    fake = FakeChainlink()
    issue_id = fake.add(_described(REC), labels=["triage"], comments=["note"])
    assert _work(fake).never_armed(fake.issues[issue_id]) is True


@pytest.mark.parametrize(
    "label",
    [
        "worklink:ready",
        "worklink:in-progress",
        "worklink:review",
        "worklink:blocked",
        "worklink:failed",
    ],
)
def test_never_armed_false_for_lifecycle_labels(label: str) -> None:
    fake = FakeChainlink()
    issue_id = fake.add(_described(REC), labels=[label])
    assert _work(fake).never_armed(fake.issues[issue_id]) is False


def test_never_armed_false_when_closed() -> None:
    fake = FakeChainlink()
    issue_id = fake.add(_described(REC), status="closed")
    assert _work(fake).never_armed(fake.issues[issue_id]) is False


def test_never_armed_false_when_claim_record_exists() -> None:
    fake = FakeChainlink()
    issue_id = fake.add(_described(REC), comments=[_claim_comment()])
    assert _work(fake).never_armed(fake.issues[issue_id]) is False


@pytest.mark.parametrize(
    "item",
    [
        {},
        {"status": "open", "labels": "x", "comments": []},
        {"status": "open", "labels": [], "comments": "x"},
        {"status": "open", "labels": [3], "comments": []},
        {"status": "open", "labels": [], "comments": [3]},
    ],
)
def test_never_armed_false_for_malformed_snapshot(item: dict) -> None:
    assert _work(FakeChainlink()).never_armed(item) is False
