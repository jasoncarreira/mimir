from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pytest

from mimir import pollers
from mimir_cultivate.tracker.adapters import build_adapter
from mimir_cultivate.tracker.adapters.base import TrackerAdapter
from mimir_cultivate.tracker.adapters.github_issues import build
from mimir_cultivate.tracker.config import TrackerAdapterConfig
from mimir_cultivate.tracker.thread import activity_id, delivery_decision
from mimir_cultivate.tracker.transport import TrackerUnavailable
from tracker_replay import (
    ReplayTransport,
    assert_fixture_inventory,
    assert_outcome,
    forbid_default_transport,  # noqa: F401  (opt-in fixture)
    load_fixture,
    run_contract,
    scenarios_for,
)

ADAPTER = "github_issues"
REF = "acme/widgets#42"
ENV = {"GITHUB_TOKEN": "tok", "MIMIR_GITHUB_SELF_LOGIN": "MimirBot"}
CONFIG = TrackerAdapterConfig(name=ADAPTER, repos=("acme/widgets",))
TRUSTED = {"alice": True, "mallory": False}


@pytest.fixture(autouse=True)
def stub_trust(
    monkeypatch: pytest.MonkeyPatch, forbid_default_transport: None  # noqa: F811
) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    def fake(repo: Any, author: Any, token: str, *, timeout: float | None = None) -> bool | None:
        calls.append((repo, author, token, timeout))
        return TRUSTED.get(author)

    monkeypatch.setattr(pollers, "_github_author_is_trusted", fake)
    return calls


def make(transport: ReplayTransport, env: dict[str, str] | None = None) -> TrackerAdapter:
    return build_adapter(ADAPTER, CONFIG, ENV if env is None else env, transport)


def inline(exchanges: list[dict[str, Any]]) -> tuple[ReplayTransport, TrackerAdapter]:
    transport = ReplayTransport(exchanges)
    return transport, make(transport)


def read_exchange(scenario: str = "attest_trusted") -> list[dict[str, Any]]:
    return copy.deepcopy(load_fixture(ADAPTER, scenario)["exchanges"])


def edited_read(edit: Callable[[dict[str, Any]], None], scenario: str = "attest_trusted"):
    exchanges = read_exchange(scenario)
    edit(exchanges[0]["response"]["json"]["data"]["repository"]["issue"])
    return inline(exchanges)


def test_fixture_inventory_matches_the_scenario_table() -> None:
    assert_fixture_inventory(ADAPTER)


@pytest.mark.parametrize("scenario", scenarios_for(ADAPTER), ids=lambda s: s.name)
def test_contract_scenario(scenario: Any) -> None:
    outcome = run_contract(scenario, ADAPTER, make, REF)
    assert_outcome(scenario, ADAPTER, outcome)


def test_state_scenarios_make_no_request() -> None:
    for scenario in scenarios_for(ADAPTER):
        if scenario.operation == "transition":
            assert run_contract(scenario, ADAPTER, make, REF).values["requests"] == 0


def test_listing_builds_canonical_urls_and_skips_pull_requests() -> None:
    scenario = next(s for s in scenarios_for(ADAPTER) if s.name == "list_assigned")
    transport = ReplayTransport.from_fixture(ADAPTER, "list_assigned")
    issues, complete = make(transport).list_assigned()
    assert complete is True
    assert [(i.ref, i.stable_id, i.url) for i in issues] == [
        ("acme/widgets#42", "9042", "https://github.com/acme/widgets/issues/42"),
        ("acme/widgets#44", "9044", "https://github.com/acme/widgets/issues/44"),
    ]
    assert scenario.name == "list_assigned"
    assert [i.updated_at for i in issues] == ["2026-10-01T12:00:00+00:00"] * 2


@pytest.mark.parametrize("value", [None, "", "yesterday", "2026-10-01T12:00:00", 5])
def test_listing_omits_an_item_without_a_valid_updated_at(value: Any) -> None:
    exchanges = copy.deepcopy(load_fixture(ADAPTER, "list_assigned")["exchanges"])
    exchanges[0]["response"]["json"][0]["updated_at"] = value
    issues, complete = inline(exchanges)[1].list_assigned()
    assert [i.ref for i in issues] == ["acme/widgets#44"]
    assert complete is False


def test_listing_without_updated_at_key_is_incomplete() -> None:
    exchanges = copy.deepcopy(load_fixture(ADAPTER, "list_assigned")["exchanges"])
    del exchanges[0]["response"]["json"][0]["updated_at"]
    issues, complete = inline(exchanges)[1].list_assigned()
    assert [i.ref for i in issues] == ["acme/widgets#44"] and complete is False


def test_listing_omits_a_malformed_item_and_is_incomplete() -> None:
    exchanges = copy.deepcopy(load_fixture(ADAPTER, "list_assigned")["exchanges"])
    exchanges[0]["response"]["json"][0]["number"] = "42"
    issues, complete = inline(exchanges)[1].list_assigned()
    assert [i.ref for i in issues] == ["acme/widgets#44"]
    assert complete is False


def test_listing_that_is_not_a_list_is_unavailable() -> None:
    exchanges = copy.deepcopy(load_fixture(ADAPTER, "list_assigned")["exchanges"])
    exchanges[0]["response"]["json"] = {"message": "nope"}
    with pytest.raises(TrackerUnavailable) as caught:
        inline(exchanges)[1].list_assigned()
    assert caught.value.code == "malformed_payload"


def test_read_normalizes_the_thread() -> None:
    transport = ReplayTransport.from_fixture(ADAPTER, "attest_trusted")
    issue = make(transport).read(REF)
    assert (issue.ref, issue.stable_id, issue.project) == (REF, "9042", None)
    assert issue.url == "https://github.com/acme/widgets/issues/42"
    assert issue.bot_id == "mimirbot"
    assert issue.author_id == "alice"
    assert issue.is_open and issue.assigned_to_bot and not issue.truncated
    assert [(a.kind, a.actor_id) for a in issue.activities] == [
        ("assigned", "alice"),
        ("comment", "alice"),
    ]
    assert [c.body for c in issue.comments] == ["please triage"]


def test_read_records_one_activity_per_rename_and_per_edit() -> None:
    issue = make(ReplayTransport.from_fixture(ADAPTER, "attest_untrusted_title_edit")).read(REF)
    assert [(a.kind, a.actor_id) for a in issue.activities if a.kind == "title_edit"] == [
        ("title_edit", "mallory"),
        ("title_edit", "alice"),
    ]
    issue = make(ReplayTransport.from_fixture(ADAPTER, "attest_untrusted_body_edit_masked")).read(REF)
    assert [a.actor_id for a in issue.activities if a.kind == "body_edit"] == ["mallory", "alice"]
    issue = make(
        ReplayTransport.from_fixture(ADAPTER, "attest_untrusted_comment_edit_masked")
    ).read(REF)
    assert [a.actor_id for a in issue.activities if a.kind == "comment_edit"] == ["mallory", "alice"]


def test_actor_logins_are_casefolded_to_match_the_bot() -> None:
    def edit(issue: dict[str, Any]) -> None:
        issue["timelineItems"]["nodes"][0]["actor"]["login"] = "ALICE"
        issue["timelineItems"]["nodes"][1]["author"]["login"] = "MIMIRBOT"
        issue["timelineItems"]["nodes"][0]["assignee"]["login"] = "mImIrBoT"

    issue = edited_read(edit)[1].read(REF)
    assert [(a.kind, a.actor_id) for a in issue.activities] == [
        ("assigned", "alice"),
        ("comment", "mimirbot"),
    ]
    decision = delivery_decision(issue, lambda actor, _: TRUSTED.get(actor))
    assert decision.kind == "deliver"
    assert decision.cutoff is not None and decision.cutoff.kind == "assigned"


@pytest.mark.parametrize("actor", [None, {"login": "ghost"}])
def test_unattributable_actor_is_none_and_withholds_after_the_cutoff(actor: Any) -> None:
    def edit(issue: dict[str, Any]) -> None:
        issue["timelineItems"]["nodes"][1]["author"] = actor

    issue = edited_read(edit)[1].read(REF)
    assert issue.activities[1].actor_id is None
    assert delivery_decision(issue, lambda actor_id, _: True).reason == "untrusted_latest_activity"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda i: i["timelineItems"]["pageInfo"].update(hasNextPage=True),
        lambda i: i["timelineItems"].update(totalCount=101),
        lambda i: i["userContentEdits"]["pageInfo"].update(hasNextPage=True),
        lambda i: i["userContentEdits"].update(totalCount=1),
        lambda i: i.update(lastEditedAt="2026-10-01T15:00:00Z"),
        lambda i: i["timelineItems"]["nodes"][1]["userContentEdits"]["pageInfo"].update(
            hasNextPage=True
        ),
        lambda i: i["timelineItems"]["nodes"][1].update(lastEditedAt="2026-10-01T15:00:00Z"),
    ],
    ids=[
        "timeline-next-page", "timeline-total", "issue-edits-next-page", "issue-edits-total",
        "issue-edited-without-history", "comment-edits-next-page", "comment-edited-without-history",
    ],
)
def test_every_truncation_condition_marks_the_issue_truncated(mutate: Any) -> None:
    assert edited_read(mutate)[1].read(REF).truncated is True


def test_wrong_issue_in_payload_is_malformed() -> None:
    with pytest.raises(TrackerUnavailable) as caught:
        edited_read(lambda i: i.update(number=43))[1].read(REF)
    assert caught.value.code == "malformed_payload"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda i: i["timelineItems"]["nodes"][0].update(createdAt="2026-10-01T10:00:00"),
        lambda i: i["timelineItems"]["nodes"].append({"__typename": "ClosedEvent", "id": "x", "createdAt": "2026-10-01T10:00:00Z"}),
        lambda i: i.update(state="MERGED"),
        lambda i: i.pop("databaseId"),
    ],
    ids=["naive-timestamp", "unexpected-timeline-item", "unknown-state", "missing-id"],
)
def test_malformed_thread_is_unavailable(mutate: Any) -> None:
    with pytest.raises(TrackerUnavailable) as caught:
        edited_read(mutate)[1].read(REF)
    assert caught.value.code == "malformed_payload"


def test_graphql_errors_are_unavailable_without_leaking_text() -> None:
    exchanges = read_exchange()
    exchanges[0]["response"]["json"] = {"errors": [{"type": "NOT_FOUND", "message": "secret"}]}
    with pytest.raises(TrackerUnavailable) as caught:
        inline(exchanges)[1].read(REF)
    assert caught.value.code == "malformed_payload"
    exchanges[0]["response"]["json"] = {"errors": [{"type": "RATE_LIMITED"}]}
    with pytest.raises(TrackerUnavailable) as caught:
        inline(exchanges)[1].read(REF)
    assert caught.value.code == "rate_limited"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "http_401"),
        (403, "http_403"),
        (429, "rate_limited"),
        (500, "http_500"),
        (503, "http_503"),
    ],
)
def test_failed_statuses_map_to_unavailable_codes(status: int, code: str) -> None:
    listing = read_exchange("list_assigned")
    listing[0]["response"] = {"status": status}
    with pytest.raises(TrackerUnavailable) as caught:
        inline(listing)[1].list_assigned()
    assert caught.value.code == code
    thread = read_exchange()
    thread[0]["response"] = {"status": status}
    with pytest.raises(TrackerUnavailable) as caught:
        inline(thread)[1].read(REF)
    assert caught.value.code == code
    state = read_exchange("close_already")
    state[0]["response"] = {"status": status}
    with pytest.raises(TrackerUnavailable) as caught:
        inline(state)[1].current_state(REF)
    assert caught.value.code == code


@pytest.mark.parametrize(
    ("status", "reason"), [(401, "permission_denied"), (403, "permission_denied"), (500, "api_error")]
)
def test_write_failures_are_reported_not_raised(status: int, reason: str) -> None:
    posting = read_exchange("comment")
    posting[0]["response"] = {"status": status}
    result = inline(posting)[1].comment(REF, "Thanks, filed.")
    assert (result.status, result.reason) == ("unavailable", reason)
    closing = read_exchange("close_default")
    closing[1]["response"] = {"status": status}
    result = inline(closing)[1].close(REF)
    assert (result.status, result.reason) == ("unavailable", reason)


def test_current_state_reports_the_issue_state() -> None:
    transport = ReplayTransport.from_fixture(ADAPTER, "close_already")
    assert make(transport).current_state(REF) == "closed"
    transport.finish()


@pytest.mark.parametrize(
    "ref", ["other/repo#1", "acme/widgets#0", "acme/widgets", "Acme/Widgets#42", "acme/widgets#42/x"]
)
def test_refs_outside_the_configured_repositories_are_refused(ref: str) -> None:
    transport = ReplayTransport([])
    adapter = make(transport)
    for call in (adapter.read, adapter.close, adapter.current_state):
        with pytest.raises(ValueError):
            call(ref)
    with pytest.raises(ValueError):
        adapter.comment(ref, "x")
    with pytest.raises(ValueError):
        adapter.transition(ref, "Done")
    assert transport.requests == []


@pytest.mark.parametrize(
    "env",
    [
        {"GITHUB_TOKEN": "tok"},
        {"GITHUB_TOKEN": "tok", "MIMIR_GITHUB_SELF_LOGIN": "  "},
        {"GITHUB_TOKEN": "tok", "MIMIR_GITHUB_SELF_LOGIN": "has space"},
        {"MIMIR_GITHUB_SELF_LOGIN": "mimirbot"},
    ],
    ids=["no-identity", "blank-identity", "spaced-identity", "no-token"],
)
def test_build_refuses_without_a_bot_identity_or_token(env: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        build(CONFIG, env, ReplayTransport([]))
    with pytest.raises(ValueError):
        build_adapter(ADAPTER, CONFIG, env, ReplayTransport([]))


def test_build_refuses_without_repositories() -> None:
    with pytest.raises(ValueError):
        build(TrackerAdapterConfig(name=ADAPTER), ENV, ReplayTransport([]))


def test_trust_is_strict_and_asks_about_the_issue_repository(
    stub_trust: list[tuple[Any, ...]],
) -> None:
    transport = ReplayTransport.from_fixture(ADAPTER, "attest_trusted")
    adapter = make(transport)
    issue = adapter.read(REF)
    assert adapter.trust("alice", issue) is True
    assert adapter.trust("mallory", issue) is False
    assert adapter.trust("unknown", issue) is None
    assert stub_trust[0][0:3] == ("acme/widgets", "alice", "tok")
    assert stub_trust[0][3] is not None


def test_trust_non_boolean_verdict_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pollers, "_github_author_is_trusted", lambda *a, **k: "yes")
    adapter = make(ReplayTransport.from_fixture(ADAPTER, "attest_trusted"))
    assert adapter.trust("alice", adapter.read(REF)) is None


def test_a_url_that_is_not_a_safe_issue_url_is_never_emitted() -> None:
    repo = f"{'a' * 200}/{'b' * 200}"
    ref = f"{repo}#42"
    listing = read_exchange("list_assigned")
    listing[0]["request"]["url"] = listing[0]["request"]["url"].replace("acme/widgets", repo)
    transport = ReplayTransport(listing)
    adapter = build_adapter(
        ADAPTER, TrackerAdapterConfig(name=ADAPTER, repos=(repo,)), ENV, transport
    )
    assert adapter.list_assigned() == ([], False)
    thread = read_exchange()
    thread[0]["request"]["json"]["variables"].update(owner="a" * 200, name="b" * 200)
    adapter = build_adapter(
        ADAPTER,
        TrackerAdapterConfig(name=ADAPTER, repos=(repo,)),
        ENV,
        ReplayTransport(thread),
    )
    with pytest.raises(TrackerUnavailable) as caught:
        adapter.read(ref)
    assert caught.value.code == "malformed_payload"


def test_edit_activity_ids_use_the_edit_prefixed_formula() -> None:
    issue = make(ReplayTransport.from_fixture(ADAPTER, "attest_untrusted_body_edit_masked")).read(REF)
    edits = [a.id for a in issue.activities if a.kind == "body_edit"]
    assert edits == [activity_id(ADAPTER, "edit:UCE_1"), activity_id(ADAPTER, "edit:UCE_2")]
    issue = make(
        ReplayTransport.from_fixture(ADAPTER, "attest_untrusted_comment_edit_masked")
    ).read(REF)
    ids = [a.id for a in issue.activities if a.kind == "comment_edit"]
    assert len(ids) == 2 and all(i != activity_id(ADAPTER, i) for i in ids)
    assert activity_id(ADAPTER, "edit:UCE_1") not in {a.id for a in issue.activities if a.kind != "comment_edit"}


def test_bot_actors_carry_the_bot_suffix_and_trust_is_asked_about_it(
    stub_trust: list[tuple[Any, ...]],
) -> None:
    def edit(issue: dict[str, Any]) -> None:
        comment = issue["timelineItems"]["nodes"][1]
        comment["author"] = {"__typename": "Bot", "login": "Helper"}
        issue["author"] = {"__typename": "User", "login": "Helper"}

    adapter = edited_read(edit)[1]
    issue = adapter.read(REF)
    assert issue.activities[1].actor_id == "helper[bot]"
    assert issue.author_id == "helper"
    delivery_decision(issue, adapter.trust)
    asked = [call[1] for call in stub_trust]
    assert "helper[bot]" in asked and "helper" not in asked


@pytest.mark.parametrize("login", ["has space", "a" * 40, "bad_name", "x/y", "a[bot]b"])
def test_actor_logins_outside_the_terms_form_are_unattributable(login: str) -> None:
    def edit(issue: dict[str, Any]) -> None:
        issue["timelineItems"]["nodes"][1]["author"] = {"__typename": "User", "login": login}

    assert edited_read(edit)[1].read(REF).activities[1].actor_id is None


def test_assignment_of_someone_else_is_not_an_assignment_activity() -> None:
    def edit(issue: dict[str, Any]) -> None:
        issue["timelineItems"]["nodes"][0]["assignee"]["login"] = "carol"
        issue["timelineItems"]["nodes"][1]["author"]["login"] = "mallory"

    issue = edited_read(edit)[1].read(REF)
    assert [a.kind for a in issue.activities] == ["comment"]
    decision = delivery_decision(issue, lambda actor, _: TRUSTED.get(actor))
    assert decision.kind == "withhold"


def test_bot_not_among_the_assignees_is_skipped() -> None:
    def edit(issue: dict[str, Any]) -> None:
        issue["assignees"]["nodes"][0]["login"] = "carol"

    issue = edited_read(edit)[1].read(REF)
    assert issue.assigned_to_bot is False
    assert delivery_decision(issue, lambda actor, _: True).kind == "skip"


def test_closed_issue_is_skipped() -> None:
    issue = edited_read(lambda i: i.update(state="CLOSED"))[1].read(REF)
    assert issue.is_open is False
    assert delivery_decision(issue, lambda actor, _: True).kind == "skip"
