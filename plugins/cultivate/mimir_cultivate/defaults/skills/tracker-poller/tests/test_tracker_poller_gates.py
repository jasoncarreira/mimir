from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from mimir_cultivate.tracker.evidence import evidence_transaction
from mimir_cultivate.tracker.thread import Activity

from tracker_poller_test_support import (
    ENTRY, REF, T0, Rig, event, issue, items, poller, signals,
)

MALLORY_AT = T0 + timedelta(hours=3)


def withheld_issue(**fields):
    activities = (
        Activity("assigned", event("AE_1"), T0, "alice"),
        Activity("comment", event("IC_9"), MALLORY_AT, "mallory"),
    )
    return issue(activities=activities, title="SECRET TITLE", **fields)


def test_untrusted_latest_activity_emits_only_a_withheld_signal(rig: Rig) -> None:
    rig.adapter.put(withheld_issue())
    records = rig.fire()
    [signal] = signals(records)
    assert items(records) == []
    assert signal["signal"] == "tracker_issue_withheld"
    assert signal["reason"] == "untrusted_latest_activity"
    assert signal["title"] == "SECRET TITLE"
    assert rig.entry()["pending"] is None and rig.entry()["updated_at"] is None


def test_withheld_title_never_reaches_the_cursor_or_other_stdout(rig: Rig) -> None:
    rig.adapter.put(withheld_issue(body="BODY TEXT"))
    records = rig.fire()
    cursor_text = (rig.state / "cursor.json").read_text()
    assert "SECRET TITLE" not in cursor_text and "BODY TEXT" not in cursor_text
    assert "BODY TEXT" not in json.dumps(records)


def test_withheld_signal_is_not_repeated_after_its_receipt(rig: Rig) -> None:
    rig.adapter.put(withheld_issue())
    [signal] = signals(rig.fire())
    rig.receipt(signal["delivery_key"])
    assert rig.fire() == []
    assert rig.entry()["updated_at"] == "2026-10-01T12:00:00+00:00"


def test_withheld_signal_reemits_without_receipt_and_new_activity_changes_key(rig: Rig) -> None:
    rig.adapter.put(withheld_issue())
    [first] = signals(rig.fire())
    [again] = signals(rig.fire())
    assert again["delivery_key"] == first["delivery_key"]
    base = rig.adapter.full[REF]
    later = Activity("comment", event("IC_10"), MALLORY_AT + timedelta(hours=1), "mallory")
    rig.adapter.put(replace(base, activities=(*base.activities, later)))
    [other] = signals(rig.fire())
    assert other["delivery_key"] != first["delivery_key"]


def test_withhold_clears_a_pending_item(rig: Rig) -> None:
    rig.adapter.put(issue())
    rig.fire()
    assert rig.entry()["pending"] is not None
    rig.adapter.put(withheld_issue())
    rig.fire()
    assert rig.entry()["pending"] is None


def test_item_cap_emits_nothing_and_changes_nothing_beyond_it(rig: Rig) -> None:
    for number in range(40, 48):
        rig.adapter.put(issue(ref=f"acme/widgets#{number}", stable_id=str(9000 + number)))
    assert len(items(rig.fire())) == 5
    unemitted = [e for e in rig.cursor()["issues"].values() if e["pending"] is None]
    assert len(unemitted) == 3
    assert all(e["updated_at"] is None for e in unemitted)


def test_read_cap_limits_reads_per_fire(rig: Rig) -> None:
    for number in range(40, 60):
        rig.adapter.put(issue(ref=f"acme/widgets#{number}", stable_id=str(9000 + number),
                              activities=(Activity("assigned", event(f"A{number}"), T0, "bot"),)))
    rig.fire()
    assert len(rig.adapter.reads) == poller.READ_CAP


def test_find_cap_limits_adoption_searches(rig: Rig) -> None:
    for number in range(40, 70):
        rig.adapter.put(issue(ref=f"acme/widgets#{number}", stable_id=str(9000 + number),
                              activities=(Activity("assigned", event(f"A{number}"), T0, "bot"),)))
    rig.fire()
    searches = [c for c in rig.chainlink.calls if c[1] == "search"]
    assert len(searches) == poller.FIND_CAP


def test_listing_beyond_the_cap_is_incomplete_and_drops_nothing(rig: Rig) -> None:
    rig.adapter.put(issue())
    rig.fire()
    rig.adapter.listed = []
    for n in range(100, 100 + poller.LIST_CAP + 1):
        rig.adapter.put(issue(ref=f"acme/widgets#{n}", stable_id=str(5000 + n),
                              activities=(Activity("assigned", event(f"A{n}"), T0, "bot"),)))
    rig.adapter.complete = True
    rig.fire()
    assert ENTRY in rig.cursor()["issues"]


def test_deadline_stops_reads_but_saves_the_cursor(rig: Rig) -> None:
    rig.adapter.put(issue())
    rig.fire(deadline=0.0)
    assert rig.adapter.reads == [] and ENTRY in rig.cursor()["issues"]


def test_read_that_returns_another_issue_is_ignored(rig: Rig) -> None:
    rig.adapter.put(issue())
    rig.adapter.full[REF] = issue(stable_id="1")
    assert rig.fire() == []
    assert rig.entry()["updated_at"] is None


def test_notices_from_the_drain_become_signals(rig: Rig) -> None:
    with evidence_transaction(rig.home) as data:
        data["notices"].append({
            "delivery_key": "trk-" + "1" * 24, "adapter": "github_issues", "ref": REF,
            "url": "https://github.com/acme/widgets/issues/42", "lifecycle_event": "agent",
            "reason": "state_not_found", "created_at": "2026-10-01T00:00:00+00:00",
            "notified": False,
        })
    [signal] = signals(rig.fire())
    assert signal == {
        "signal": "tracker_transition_unavailable", "poller": "tracker-poller",
        "delivery_key": "trk-" + "1" * 24, "adapter": "github_issues", "ref": REF,
        "url": "https://github.com/acme/widgets/issues/42", "lifecycle_event": "agent",
        "reason": "state_not_found",
    }
    rig.receipt("trk-" + "1" * 24)
    assert signals(rig.fire()) == []
    from mimir_cultivate.tracker.evidence import read_evidence

    assert read_evidence(rig.home)["notices"][0]["notified"] is True


def test_other_drain_emissions_are_not_forwarded(rig: Rig) -> None:
    fire = poller._Fire(
        home=rig.home, config=rig.config, env={}, transport=object(), work=None,
        gh_runner=None, now=rig.now, deadline=1e12,
    )
    complete = {
        "delivery_key": "trk-" + "1" * 24, "adapter": "github_issues", "ref": REF,
        "url": "https://github.com/acme/widgets/issues/42", "lifecycle_event": "agent",
        "reason": "failed",
    }
    fire._notice({**complete, "kind": "something_else"})
    fire._notice({"kind": "tracker_transition_unavailable", "delivery_key": complete["delivery_key"]})
    fire._notice({**complete, "kind": "tracker_transition_unavailable", "url": None})
    assert fire.out == []


@pytest.mark.parametrize("text", ["{", "[]", '{"version": 2, "issues": {}}',
                                  '{"version": 1, "issues": {"bad key": {}}}'])
def test_untrusted_cursor_is_never_overwritten(rig: Rig, text: str) -> None:
    (rig.state / "cursor.json").write_text(text)
    rig.adapter.put(issue())
    with pytest.raises(poller.CursorError):
        rig.fire()
    assert (rig.state / "cursor.json").read_text() == text


def test_diag_refuses_free_text(capsys) -> None:
    poller._diag("Some tracker text!")
    poller._diag("adapter_unavailable:jira:http_403")
    assert capsys.readouterr().err.splitlines() == ["diag_invalid", "adapter_unavailable:jira:http_403"]


def test_main_does_nothing_without_configured_adapters(tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "worklink.yaml").write_text("repository: acme/widgets\n")
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr(poller, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(poller, "CURSOR_FILE", tmp_path / "state" / "cursor.json")
    poller.main()
    out = capsys.readouterr()
    assert out.out == "" and not (tmp_path / "state" / "cursor.json").exists()
