from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from mimir.poller_tracker import NOTICE_REASONS, safe_event_id, safe_url
from mimir_cultivate.tracker.adapters import build_adapter
from mimir_cultivate.tracker.adapters import linear as linear_adapter
from mimir_cultivate.tracker.config import TrackerAdapterConfig
from mimir_cultivate.tracker.thread import activity_id, delivery_decision
from mimir_cultivate.tracker.transport import HttpResponse, TrackerUnavailable
from tracker_replay import (
    ReplayTransport,
    assert_fixture_inventory,
    assert_outcome,
    forbid_default_transport,  # noqa: F401  (opt-in fixture)
    run_contract,
    scenarios_for,
)

REF = "ENG-123"
CONFIG = TrackerAdapterConfig(name="linear")
ENV = {"LINEAR_API_KEY": "lin_oauth_test"}


def _build(transport: Any, config: TrackerAdapterConfig = CONFIG) -> Any:
    return build_adapter("linear", config, ENV, transport)


class StubTransport:
    """Answers each request with the next canned response and records the requests."""

    def __init__(self, *responses: HttpResponse | Mapping[str, Any]) -> None:
        self._responses = [
            r if isinstance(r, HttpResponse) else HttpResponse(200, {"data": r}) for r in responses
        ]
        self.requests: list[tuple[str, Any]] = []

    def request(self, method: str, url: str, *, headers: Mapping[str, str], json: Any = None, timeout: float):
        self.requests.append((url, json))
        return self._responses.pop(0)


VIEWER = {"viewer": {"id": "usr-bot"}}


def _issue(url: str = "https://linear.app/acme/issue/ENG-123/fix", **overrides: Any) -> dict[str, Any]:
    issue = {
        "id": "iss-123", "identifier": "ENG-123", "url": url, "title": "t", "description": "d",
        "createdAt": "2026-10-20T09:00:00.000Z", "creator": {"id": "usr-alice"}, "delegate": {"id": "usr-bot"},
        "state": {"type": "started"}, "team": {"key": "ENG"},
        "history": {"pageInfo": {"hasNextPage": False}, "nodes": []},
        "comments": {"pageInfo": {"hasNextPage": False}, "nodes": []},
    }
    issue.update(overrides)
    return {"issue": issue}


def _entry(entry_id: str, hour: int, actor: str | None, **changes: Any) -> dict[str, Any]:
    return {
        "id": entry_id, "createdAt": f"2026-10-20T{hour:02d}:00:00.000Z",
        "actor": {"id": actor} if actor else None, "toDelegate": None, "fromTitle": None,
        "toTitle": None, "updatedDescription": False, **changes,
    }


def _history(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"pageInfo": {"hasNextPage": False}, "nodes": list(entries)}


def test_fixture_set_equals_scenario_table() -> None:
    assert_fixture_inventory("linear")


@pytest.mark.parametrize("scenario", scenarios_for("linear"), ids=lambda s: s.name)
def test_contract_scenario(scenario, forbid_default_transport) -> None:  # noqa: F811
    outcome = run_contract(scenario, "linear", _build, REF)
    assert_outcome(scenario, "linear", outcome)


def test_slugged_url_is_listed_canonical(forbid_default_transport) -> None:  # noqa: F811
    transport = ReplayTransport.from_fixture("linear", "list_slugged_url")
    issues, complete = _build(transport).list_assigned()
    assert complete
    assert [i.url for i in issues] == ["https://linear.app/acme/issue/ENG-123"]
    assert safe_url(issues[0].url, issues[0].ref) == issues[0].url


@pytest.mark.parametrize(
    "api_url, expected",
    [
        ("https://linear.app/acme/issue/ENG-123/fix-login", "https://linear.app/acme/issue/ENG-123"),
        ("https://linear.app/acme/issue/ENG-123", "https://linear.app/acme/issue/ENG-123"),
        ("https://linear.app/acme/issue/ENG-123/a/b?x=1#frag", "https://linear.app/acme/issue/ENG-123"),
        ("http://linear.app/acme/issue/ENG-123/x", None),
        ("https://evil.example/acme/issue/ENG-123/x", None),
        ("https://linear.app.evil.example/acme/issue/ENG-123/x", None),
        ("https://user@linear.app/acme/issue/ENG-123/x", None),
        ("https://linear.app:8443/acme/issue/ENG-123/x", None),
        ("https://linear.app/acme/issue/ENG-999/x", None),
        ("https://linear.app/acme/project/ENG-123/x", None),
        ("https://linear.app/ac_me/issue/ENG-123/x", None),
        ("https://linear.app/issue/ENG-123", None),
        (None, None),
        (7, None),
    ],
)
def test_canonical_url(api_url: object, expected: str | None) -> None:
    assert linear_adapter._canonical_url(api_url, "ENG-123") == expected


def test_read_with_mismatched_url_is_unavailable() -> None:
    transport = StubTransport(VIEWER, _issue(url="https://linear.app/acme/issue/ENG-999/x"))
    with pytest.raises(TrackerUnavailable) as raised:
        _build(transport).read(REF)
    assert raised.value.code == "malformed_payload"


@pytest.mark.parametrize("env", [{}, {"LINEAR_API_KEY": ""}, {"LINEAR_API_KEY": "  "}])
def test_build_refuses_without_credential(env: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        build_adapter("linear", CONFIG, env, StubTransport(VIEWER))


@pytest.mark.parametrize("viewer", [{"viewer": {"id": None}}, {"viewer": {"id": ""}}, {"viewer": {}}, {"viewer": None}, {}])
def test_build_refuses_without_bot_identity(viewer: dict[str, Any]) -> None:
    with pytest.raises((ValueError, TrackerUnavailable)):
        _build(StubTransport(viewer))


def test_requests_carry_the_bearer_credential() -> None:
    transport = ReplayTransport.from_fixture("linear", "list_assigned")
    _build(transport).list_assigned()
    assert {r.headers["Authorization"] for r in transport.requests} == {"Bearer lin_oauth_test"}


@pytest.mark.parametrize(
    "status, code",
    [(401, "http_403"), (403, "http_403"), (429, "rate_limited"), (500, "http_500"), (503, "http_503")],
)
def test_unavailable_status_codes(status: int, code: str) -> None:
    transport = StubTransport(VIEWER, HttpResponse(status, None))
    with pytest.raises(TrackerUnavailable) as raised:
        _build(transport).read(REF)
    assert raised.value.code == code


def test_graphql_errors_fail_closed() -> None:
    transport = StubTransport(VIEWER, HttpResponse(200, {"data": _issue(), "errors": [{"message": "x"}]}))
    with pytest.raises(TrackerUnavailable) as raised:
        _build(transport).read(REF)
    assert raised.value.code == "malformed_payload"


@pytest.mark.parametrize(
    "user, expected",
    [
        ({"id": "usr-a", "active": True, "guest": False, "app": False}, True),
        ({"id": "usr-a", "active": False, "guest": False, "app": False}, False),
        ({"id": "usr-a", "active": True, "guest": True, "app": False}, False),
        ({"id": "usr-a", "active": True, "guest": False, "app": True}, False),
        ({"id": "usr-a", "active": True, "guest": False}, False),
        ({"id": "usr-other", "active": True, "guest": False, "app": False}, False),
        (None, False),
    ],
)
def test_trust_requires_active_non_guest_member(user: dict[str, Any] | None, expected: bool) -> None:
    adapter = _build(StubTransport(VIEWER, {"user": user}))
    assert adapter.trust("usr-a", None) is expected


def test_trust_lookup_is_cached_and_failure_is_unavailable() -> None:
    user = {"user": {"id": "usr-a", "active": True, "guest": False, "app": False}}
    adapter = _build(StubTransport(VIEWER, user))
    assert adapter.trust("usr-a", None) is True
    assert adapter.trust("usr-a", None) is True
    failing = _build(StubTransport(VIEWER, HttpResponse(500, None)))
    with pytest.raises(TrackerUnavailable):
        failing.trust("usr-a", None)


def test_history_entries_become_one_activity_per_change() -> None:
    both = _entry("h2", 11, "usr-mallory", fromTitle="a", toTitle="b", updatedDescription=True)
    assigned = _entry("h1", 10, "usr-alice", toDelegate={"id": "usr-bot"})
    other_delegate = _entry("h0", 9, "usr-alice", toDelegate={"id": "usr-someone"})
    transport = StubTransport(VIEWER, _issue(history=_history(other_delegate, assigned, both)))
    issue = _build(transport).read(REF)
    assert {(a.kind, a.actor_id) for a in issue.activities} == {
        ("assigned", "usr-alice"),
        ("title_edit", "usr-mallory"),
        ("body_edit", "usr-mallory"),
    }
    assert len(issue.activities) == 3
    assert issue.bot_id == "usr-bot"
    assert issue.assigned_to_bot and issue.is_open and issue.project == "ENG"
    assert issue.url == "https://linear.app/acme/issue/ENG-123"


def test_unattributable_actor_withholds_after_the_cutoff() -> None:
    entries = [
        _entry("h1", 10, "usr-alice", toDelegate={"id": "usr-bot"}),
        _entry("h2", 11, None, updatedDescription=True),
    ]
    adapter = _build(StubTransport(VIEWER, _issue(history=_history(*entries))))
    issue = adapter.read(REF)
    assert [a.actor_id for a in issue.activities if a.kind == "body_edit"] == [None]
    decision = delivery_decision(issue, adapter.trust)
    assert (decision.kind, decision.reason) == ("withhold", "untrusted_latest_activity")


def test_comment_edit_is_attributed_to_the_comment_user_and_null_user_is_none() -> None:
    comments = {
        "pageInfo": {"hasNextPage": False},
        "nodes": [
            {"id": "c1", "createdAt": "2026-10-20T10:00:00.000Z", "editedAt": "2026-10-20T10:30:00.000Z",
             "body": "x", "user": {"id": "usr-mallory"}},
            {"id": "c2", "createdAt": "2026-10-20T10:05:00.000Z", "editedAt": None, "body": "y", "user": None},
        ],
    }
    issue = _build(StubTransport(VIEWER, _issue(comments=comments))).read(REF)
    found = {(a.kind, a.actor_id) for a in issue.activities}
    assert found == {("comment", None), ("comment", "usr-mallory"), ("comment_edit", "usr-mallory")}
    assert [c.author_id for c in issue.comments] == ["usr-mallory", None]


def test_bot_own_activity_is_skipped_by_the_delivery_rule() -> None:
    entries = [
        _entry("h1", 10, "usr-alice", toDelegate={"id": "usr-bot"}),
        _entry("h2", 11, "usr-bot", fromTitle="a", toTitle="b"),
    ]
    user = {"user": {"id": "usr-alice", "active": True, "guest": False, "app": False}}
    adapter = _build(StubTransport(VIEWER, _issue(history=_history(*entries)), user))
    decision = delivery_decision(adapter.read(REF), adapter.trust)
    assert decision.kind == "deliver"


def test_closed_or_unassigned_issue_is_skipped() -> None:
    adapter = _build(StubTransport(VIEWER, _issue(state={"type": "completed"}), _issue(delegate=None)))
    assert delivery_decision(adapter.read(REF), adapter.trust).kind == "skip"
    assert delivery_decision(adapter.read(REF), adapter.trust).kind == "skip"


def test_listing_filters_by_team_keys_and_omits_unmappable_nodes() -> None:
    nodes = [
        {"id": "i1", "identifier": "ENG-1", "url": "https://linear.app/acme/issue/ENG-1/a", "updatedAt": "2026-10-20T10:00:00.000Z"},
        {"id": "i2", "identifier": "OPS-2", "url": "https://linear.app/acme/issue/OPS-2/a", "updatedAt": "2026-10-20T11:00:00.000Z"},
        {"id": "i3", "identifier": "ENG-3", "url": "https://linear.app/acme/issue/ENG-3/a", "updatedAt": "2026-10-20T12:00:00.000Z"},
        {"id": "i4", "identifier": "bad", "url": "https://linear.app/acme/issue/bad/a", "updatedAt": "2026-10-20T12:00:00.000Z"},
    ]
    transport = StubTransport(VIEWER, {"issues": {"nodes": nodes, "pageInfo": {"hasNextPage": False}}})
    adapter = _build(transport, TrackerAdapterConfig(name="linear", team_keys=("ENG",)))
    issues, complete = adapter.list_assigned()
    assert [i.ref for i in issues] == ["ENG-3", "ENG-1"]
    assert complete is False
    sent = transport.requests[1][1]["variables"]["filter"]
    assert sent["team"] == {"key": {"in": ["ENG"]}}
    assert sent["delegate"] == {"id": {"eq": "usr-bot"}}
    assert sent["state"] == {"type": {"nin": ["completed", "canceled"]}}


_STATES = [
    {"id": "s1", "name": "In Progress", "type": "started"},
    {"id": "s2", "name": "Done", "type": "completed"},
    {"id": "s3", "name": "Shipped", "type": "completed"},
]


def _workflow(state: dict[str, str], states: list[dict[str, str]] = _STATES) -> dict[str, Any]:
    return {"issue": {"id": "iss-123", "state": state, "team": {"states": {"nodes": states}}}}


def test_close_with_ambiguous_completed_states_requires_a_mapping() -> None:
    adapter = _build(StubTransport(VIEWER, _workflow({"name": "In Progress", "type": "started"})))
    result = adapter.close(REF)
    assert (result.status, result.reason) == ("unavailable", "merged_state_required")


def test_close_uses_the_project_merged_mapping() -> None:
    config = TrackerAdapterConfig(
        name="linear", transitions={"merged": "Done"}, project_transitions={"ENG": {"merged": "Shipped"}}
    )
    transport = StubTransport(
        VIEWER, _workflow({"name": "In Progress", "type": "started"}), {"issueUpdate": {"success": True}}
    )
    assert _build(transport, config).close(REF).status == "applied"
    assert transport.requests[-1][1]["variables"] == {"id": REF, "stateId": "s3"}


def test_close_already_in_the_mapped_state() -> None:
    config = TrackerAdapterConfig(name="linear", transitions={"merged": "Shipped"})
    adapter = _build(StubTransport(VIEWER, _workflow({"name": "Shipped", "type": "completed"})), config)
    assert adapter.close(REF).status == "already"


def test_close_with_duplicate_mapped_state_is_ambiguous() -> None:
    config = TrackerAdapterConfig(name="linear", transitions={"merged": "Done"})
    duplicated = _STATES + [{"id": "s4", "name": "Done", "type": "completed"}]
    adapter = _build(StubTransport(VIEWER, _workflow({"name": "In Progress", "type": "started"}, duplicated)), config)
    result = adapter.close(REF)
    assert (result.status, result.reason) == ("unavailable", "ambiguous_state")


def test_rejected_writes_report_api_error() -> None:
    started = _workflow({"name": "In Progress", "type": "started"})
    comment = _build(StubTransport(VIEWER, started, {"commentCreate": {"success": False}})).comment(REF, "x")
    state = _build(StubTransport(VIEWER, started, {"issueUpdate": {"success": False}})).transition(REF, "Done")
    for result in (comment, state):
        assert (result.status, result.reason) == ("unavailable", "api_error")
        assert result.reason in NOTICE_REASONS


def test_transition_to_the_current_state_is_already_and_duplicate_names_are_not_found() -> None:
    adapter = _build(StubTransport(VIEWER, _workflow({"name": "Done", "type": "completed"})))
    assert adapter.transition(REF, "Done").status == "already"
    duplicated = _STATES + [{"id": "s4", "name": "Done", "type": "completed"}]
    adapter = _build(StubTransport(VIEWER, _workflow({"name": "In Progress", "type": "started"}, duplicated)))
    result = adapter.transition(REF, "Done")
    assert (result.status, result.reason) == ("unavailable", "ambiguous_state")
    adapter = _build(StubTransport(VIEWER, _workflow({"name": "In Progress", "type": "started"})))
    result = adapter.transition(REF, "Missing")
    assert (result.status, result.reason) == ("unavailable", "state_not_found")


def test_current_state_returns_the_state_name() -> None:
    adapter = _build(StubTransport(VIEWER, _workflow({"name": "In Progress", "type": "started"})))
    assert adapter.current_state(REF) == "In Progress"


def _comments(*nodes: dict[str, Any], more: bool = False) -> dict[str, Any]:
    return {"pageInfo": {"hasNextPage": more}, "nodes": list(nodes)}


def test_more_comment_pages_withhold_as_thread_too_long() -> None:
    entries = _history(_entry("h1", 10, "usr-alice", toDelegate={"id": "usr-bot"}))
    adapter = _build(StubTransport(VIEWER, _issue(history=entries, comments=_comments(more=True))))
    issue = adapter.read(REF)
    assert issue.truncated
    decision = delivery_decision(issue, adapter.trust)
    assert (decision.kind, decision.reason) == ("withhold", "thread_too_long")


def test_issue_delegated_to_another_user_is_skipped() -> None:
    adapter = _build(StubTransport(VIEWER, _issue(delegate={"id": "usr-other"})))
    issue = adapter.read(REF)
    assert issue.assigned_to_bot is False
    assert delivery_decision(issue, adapter.trust).kind == "skip"


@pytest.mark.parametrize("state_type", ["completed", "canceled"])
def test_closed_state_types_are_skipped_while_still_delegated(state_type: str) -> None:
    adapter = _build(StubTransport(VIEWER, _issue(state={"type": state_type})))
    issue = adapter.read(REF)
    assert issue.assigned_to_bot and not issue.is_open
    assert delivery_decision(issue, adapter.trust).kind == "skip"


def test_activity_ids_are_pinned_to_the_documented_raw_ids() -> None:
    entries = _history(
        _entry("h1", 10, "usr-alice", toDelegate={"id": "usr-bot"}, fromTitle="a", toTitle="b", updatedDescription=True)
    )
    edited = "2026-10-20T10:30:00.000Z"
    comments = _comments(
        {"id": "c1", "createdAt": "2026-10-20T10:00:00.000Z", "editedAt": edited, "body": "x", "user": {"id": "usr-alice"}}
    )
    issue = _build(StubTransport(VIEWER, _issue(history=entries, comments=comments))).read(REF)
    assert {a.kind: a.id for a in issue.activities} == {
        "assigned": activity_id("linear", "h1:assigned"),
        "title_edit": activity_id("linear", "h1:title_edit"),
        "body_edit": activity_id("linear", "h1:body_edit"),
        "comment": activity_id("linear", "c1"),
        "comment_edit": activity_id("linear", f"c1:edited:{edited}"),
    }


@pytest.mark.parametrize(
    "scenario", [s for s in scenarios_for("linear") if s.operation == "attest"], ids=lambda s: s.name
)
def test_delivered_cutoff_ids_are_safe_event_ids(scenario, forbid_default_transport) -> None:  # noqa: F811
    adapter = _build(ReplayTransport.from_fixture("linear", scenario.name))
    try:
        decision = delivery_decision(adapter.read(REF), adapter.trust)
    except TrackerUnavailable:
        return
    if decision.kind == "deliver":
        assert safe_event_id(decision.cutoff.id) == decision.cutoff.id


def test_delivered_scenarios_exist() -> None:
    delivered = [s.name for s in scenarios_for("linear") if s.expectation("linear").get("decision") == "deliver"]
    assert delivered == ["attest_trusted"]


def test_listing_populates_updated_at_in_utc() -> None:
    issues, _ = _build(ReplayTransport.from_fixture("linear", "list_assigned")).list_assigned()
    assert {i.ref: i.updated_at for i in issues} == {
        "ENG-2": "2026-10-20T12:00:00+00:00",
        "ENG-1": "2026-10-20T11:00:00+00:00",
    }


def test_listing_normalizes_offsets_to_utc() -> None:
    node = {"id": "i1", "identifier": "ENG-1", "url": "https://linear.app/acme/issue/ENG-1/a", "updatedAt": "2026-10-20T14:00:00+02:00"}
    issues, complete = _build(StubTransport(VIEWER, {"issues": _comments(node)})).list_assigned()
    assert complete and issues[0].updated_at == "2026-10-20T12:00:00+00:00"


@pytest.mark.parametrize("updated", [None, "", "yesterday", 7, "2026-10-20T10:00:00"])
def test_listing_omits_items_without_a_valid_updated_at(updated: object) -> None:
    good = {"id": "i1", "identifier": "ENG-1", "url": "https://linear.app/acme/issue/ENG-1/a", "updatedAt": "2026-10-20T10:00:00.000Z"}
    bad = {"id": "i2", "identifier": "ENG-2", "url": "https://linear.app/acme/issue/ENG-2/a", "updatedAt": updated}
    issues, complete = _build(StubTransport(VIEWER, {"issues": _comments(good, bad)})).list_assigned()
    assert [i.ref for i in issues] == ["ENG-1"]
    assert complete is False


def test_trust_of_an_entity_not_found_user_is_false_not_unavailable() -> None:
    error = {"errors": [{"message": "Entity not found: User", "extensions": {"type": "invalid input"}}]}
    adapter = _build(StubTransport(VIEWER, HttpResponse(200, error)))
    assert adapter.trust("usr-gone", None) is False


def test_other_graphql_errors_on_trust_lookup_stay_unavailable() -> None:
    mixed = {"errors": [{"message": "Entity not found: User"}, {"message": "boom"}]}
    with pytest.raises(TrackerUnavailable):
        _build(StubTransport(VIEWER, HttpResponse(200, mixed))).trust("usr-a", None)


def test_ratelimited_graphql_error_maps_to_rate_limited() -> None:
    body = {"errors": [{"message": "slow down", "extensions": {"code": "RATELIMITED"}}]}
    with pytest.raises(TrackerUnavailable) as raised:
        _build(StubTransport(VIEWER, HttpResponse(200, body))).read(REF)
    assert raised.value.code == "rate_limited"


@pytest.mark.parametrize("odd", ["bad id!", "x" * 65, "", 7, ["usr-a"]])
def test_odd_shaped_actor_id_is_none(odd: object) -> None:
    entry = _entry("h1", 10, "usr-alice", toDelegate={"id": "usr-bot"})
    entry["actor"] = {"id": odd}
    issue = _build(StubTransport(VIEWER, _issue(history=_history(entry)))).read(REF)
    assert [a.actor_id for a in issue.activities] == [None]
