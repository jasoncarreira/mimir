from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from mimir.poller_tracker import safe_delivery_key, safe_event_id
from mimir_cultivate.tracker.thread import Activity

from tracker_poller_test_support import (
    ENTRY, REF, T0, Rig, event, external_description, issue, items, poller, signals,
)

COMMENT_AT = T0 + timedelta(hours=1)


def seeded(rig: Rig, **fields):
    rig.adapter.put(issue(**fields))


def first_item(rig: Rig):
    [item] = items(rig.fire())
    return item


def deliver(rig: Rig):
    item = first_item(rig)
    rig.receipt(item["delivery_key"])
    return item


def followup(rig: Rig, updated_at: str = "2026-10-01T14:00:00+00:00"):
    base = rig.adapter.full[REF]
    activities = (*base.activities, Activity("comment", event("IC_2"), T0 + timedelta(hours=2), "alice"))
    rig.adapter.put(replace(base, activities=activities, updated_at=updated_at))


def test_new_issue_emits_one_triage_item_with_safe_identifiers(rig: Rig) -> None:
    seeded(rig)
    item = first_item(rig)
    assert item["kind"] == "triage" and item["adapter"] == "github_issues"
    assert (item["ref"], item["attested_event_id"]) == (REF, event("AE_1"))
    assert safe_delivery_key(item["delivery_key"]) and safe_event_id(item["attested_event_id"])
    assert rig.entry()["pending"]["delivery_key"] == item["delivery_key"]
    assert rig.entry()["triage"]["state"] == "new"


def test_filed_issue_repoll_emits_nothing(rig: Rig) -> None:
    seeded(rig)
    deliver(rig)
    rig.chainlink.add(external_description())
    assert items(rig.fire()) == []
    assert rig.entry()["triage"]["state"] == "filed"
    reads = len(rig.adapter.reads)
    assert items(rig.fire()) == []
    assert len(rig.adapter.reads) == reads


def test_receipt_moves_new_to_pending_and_commits_updated_at(rig: Rig) -> None:
    seeded(rig)
    item = deliver(rig)
    assert items(rig.fire()) == []
    entry = rig.entry()
    assert entry["triage"]["state"] == "pending" and entry["pending"] is None
    assert entry["last_delivered_event_id"] == item["attested_event_id"]
    assert entry["updated_at"] == "2026-10-01T12:00:00+00:00"


def test_updated_at_committed_only_after_receipt(rig: Rig) -> None:
    seeded(rig)
    first_item(rig)
    assert rig.entry()["updated_at"] is None
    rig.fire()
    assert rig.entry()["updated_at"] is None


def test_core_drop_without_receipt_reemits_same_key(rig: Rig) -> None:
    seeded(rig)
    first = first_item(rig)
    second = first_item(rig)
    assert second["delivery_key"] == first["delivery_key"]


def test_trusted_followup_is_update_kind(rig: Rig) -> None:
    seeded(rig)
    rig.chainlink.add(external_description())
    rig.fire()
    followup(rig)
    item = first_item(rig)
    assert item["kind"] == "update" and item["attested_event_id"] == event("IC_2")
    assert rig.entry()["triage"]["state"] == "filed"


def test_comment_then_file_race_ends_filed_and_followup_is_update(rig: Rig) -> None:
    seeded(rig)
    deliver(rig)
    rig.fire()
    base = rig.adapter.full[REF]
    reply = Activity("comment", event("BOT_1"), COMMENT_AT, "bot")
    rig.adapter.put(replace(base, activities=(*base.activities, reply), updated_at="2026-10-01T13:00:00+00:00"))
    rig.fire()
    assert rig.entry()["triage"]["state"] == "responded"
    rig.chainlink.add(external_description())
    rig.fire()
    assert rig.entry()["triage"]["state"] == "filed"
    followup(rig)
    assert first_item(rig)["kind"] == "update"


def test_unavailable_commits_nothing(rig: Rig) -> None:
    seeded(rig)
    rig.adapter.trusted["alice"] = None
    assert rig.fire() == []
    entry = rig.entry()
    assert entry["updated_at"] is None and entry["pending"] is None
    assert entry["last_delivered_event_id"] is None


def test_read_failure_commits_nothing(rig: Rig) -> None:
    seeded(rig)
    rig.adapter.read_error = "timeout"
    assert rig.fire() == []
    assert rig.entry()["updated_at"] is None


def test_unchanged_updated_at_skips_the_read(rig: Rig) -> None:
    seeded(rig)
    deliver(rig)
    rig.fire()
    rig.adapter.reads.clear()
    assert rig.fire() == [] and rig.adapter.reads == []


def test_missing_listed_updated_at_never_skips_reads(rig: Rig) -> None:
    seeded(rig, updated_at=None)
    deliver(rig)
    rig.fire()
    rig.adapter.reads.clear()
    rig.fire()
    assert rig.adapter.reads == [REF] and rig.entry()["updated_at"] is None


def test_retry_after_24h_once(rig: Rig) -> None:
    seeded(rig)
    deliver(rig)
    rig.fire()
    assert items(rig.fire(now=rig.later(hours=23))) == []
    retry = items(rig.fire(now=rig.later(hours=25)))
    [retry] = retry
    assert retry["kind"] == "triage" and retry["attested_event_id"] == event("AE_1")
    assert rig.entry()["pending"]["retry"] is True
    rig.receipt(retry["delivery_key"])
    assert items(rig.fire(now=rig.later(hours=26))) == []
    assert rig.entry()["triage"]["reemitted"] is True
    assert items(rig.fire(now=rig.later(hours=80))) == []


def test_retry_key_differs_from_the_item_key(rig: Rig) -> None:
    seeded(rig)
    first = deliver(rig)
    rig.fire()
    [retry] = items(rig.fire(now=rig.later(hours=25)))
    assert retry["delivery_key"] != first["delivery_key"]


def test_no_retry_when_the_bot_replied(rig: Rig) -> None:
    seeded(rig)
    deliver(rig)
    rig.fire()
    base = rig.adapter.full[REF]
    reply = Activity("comment", event("BOT_1"), COMMENT_AT, "bot")
    rig.adapter.put(replace(base, activities=(*base.activities, reply)))
    assert items(rig.fire(now=rig.later(hours=30))) == []
    assert rig.entry()["triage"]["state"] == "responded"


def test_capped_listing_never_drops_entries(rig: Rig) -> None:
    seeded(rig)
    rig.fire()
    other = issue(ref="acme/widgets#43", stable_id="9043")
    rig.adapter.listed = [replace(other, body="", activities=(), comments=())]
    rig.adapter.full[other.ref] = other
    rig.adapter.complete = False
    rig.fire()
    assert ENTRY in rig.cursor()["issues"]


def test_failed_listing_never_drops_entries(rig: Rig) -> None:
    seeded(rig)
    rig.fire()
    rig.adapter.list_error = "timeout"
    rig.fire()
    assert ENTRY in rig.cursor()["issues"]


def test_complete_listing_drops_unlisted_entries(rig: Rig) -> None:
    seeded(rig)
    rig.fire()
    rig.adapter.listed = []
    rig.fire()
    assert rig.cursor()["issues"] == {}


@pytest.mark.parametrize("state", ["new", "pending", "responded"])
def test_adoption_runs_for_new_pending_responded(rig: Rig, state: str) -> None:
    seeded(rig)
    rig.fire()
    cursor = rig.cursor()
    cursor["issues"][ENTRY]["triage"]["state"] = state
    cursor["issues"][ENTRY]["pending"] = None
    (rig.state / "cursor.json").write_text(__import__("json").dumps(cursor))
    rig.chainlink.add(external_description())
    rig.fire()
    assert rig.entry()["triage"]["state"] == "filed"


def test_lost_create_adopted_and_armed_once(rig: Rig) -> None:
    seeded(rig)
    chainlink_id = rig.chainlink.add(external_description())
    rig.fire()
    rig.fire()
    assert rig.chainlink.issues[chainlink_id]["labels"] == ["worklink:ready"]
    assert len(rig.chainlink.labels_calls()) == 1
    assert rig.entry()["triage"]["state"] == "filed"


@pytest.mark.parametrize("fields", [
    {"labels": ["worklink:in-progress"]},
    {"labels": ["worklink:blocked"]},
    {"comments": [
        'WORKLINK_CLAIM {"agent_id": "a", "attempt": 1, "claimed_at": '
        '"2026-10-01T00:00:00+00:00", "issue_id": 9137}'
    ]},
    {"status": "closed"},
])
def test_adoption_never_rearms_claimed_or_labeled(rig: Rig, fields) -> None:
    seeded(rig)
    rig.chainlink.add(external_description(), **fields)
    rig.fire()
    assert rig.chainlink.labels_calls() == []
    assert rig.entry()["triage"]["state"] == "filed"


def test_search_failure_skips_adoption_this_fire(rig: Rig) -> None:
    seeded(rig)
    rig.chainlink.add(external_description())
    rig.chainlink.fail.add("search")
    [item] = items(rig.fire())
    assert item["kind"] == "triage" and rig.entry()["triage"]["state"] == "new"


def test_adoption_records_the_filed_transition_request(rig: Rig, monkeypatch) -> None:
    seeded(rig)
    rig.chainlink.add(external_description())
    seen = []
    monkeypatch.setattr(poller, "request_transition", lambda home, d, event: seen.append((d, event)))
    rig.fire()
    assert [event for _, event in seen] == ["filed"]


def test_ambiguous_adoption_is_diagnosed_and_never_arms(rig: Rig, capsys) -> None:
    seeded(rig)
    rig.chainlink.add(external_description())
    rig.chainlink.add(external_description())
    rig.fire()
    assert capsys.readouterr().err.splitlines() == ["adoption_ambiguous"]
    assert rig.chainlink.labels_calls() == []
    assert rig.entry()["triage"]["state"] == "filed"
