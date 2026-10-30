"""Contract and adapter-specific tests for the Jira tracker adapter, over recorded exchanges."""

from __future__ import annotations

from typing import Any

import pytest

from mimir.poller_tracker import safe_event_id
from mimir_cultivate.tracker.adapters import build_adapter
from mimir_cultivate.tracker.adapters.jira import JiraAdapter
from mimir_cultivate.tracker.config import TrackerAdapterConfig
from mimir_cultivate.tracker.thread import activity_id, delivery_decision
from mimir_cultivate.tracker.transport import TrackerUnavailable
from tracker_replay import (
    ReplayTransport,
    forbid_default_transport,  # noqa: F401  (opt-in fixture)
    assert_fixture_inventory,
    assert_outcome,
    run_contract,
    scenarios_for,
)

SITE = "acme.atlassian.net"
ENV = {"JIRA_SITE": SITE, "JIRA_EMAIL": "bot@acme.test", "JIRA_API_TOKEN": "token"}
REF = "PROJ-45"
API = f"https://{SITE}/rest/api/3/"
READ = (
    API + f"issue/{REF}?expand=changelog"
    "&fields=summary,description,status,assignee,project,creator,comment,updated"
)
JQL = (
    'assignee = "acct-bot" AND statusCategory != Done '
    'AND project in ("PROJ") ORDER BY updated DESC'
)


def config(**overrides: Any) -> TrackerAdapterConfig:
    fields: dict[str, Any] = dict(
        name="jira", account_id="acct-bot", projects=("PROJ",), trusted_groups=("jira-trusted",)
    )
    fields.update(overrides)
    return TrackerAdapterConfig(**fields)


def adapter_over(transport: ReplayTransport, cfg: TrackerAdapterConfig | None = None) -> JiraAdapter:
    built = build_adapter("jira", cfg or config(), ENV, transport)
    assert isinstance(built, JiraAdapter)
    return built


def exchange(method: str, url: str, status: int, response: Any = None, body: Any = None) -> dict[str, Any]:
    request: dict[str, Any] = {"method": method, "url": url}
    if body is not None:
        request["json"] = body
    reply: dict[str, Any] = {"status": status}
    if response is not None:
        reply["json"] = response
    return {"request": request, "response": reply}


def issue_payload(histories: list[Any], comments: list[Any]) -> dict[str, Any]:
    return {
        "id": "10045",
        "key": REF,
        "fields": {
            "summary": "T",
            "description": None,
            "status": {"name": "Open", "statusCategory": {"key": "new"}},
            "assignee": {"accountId": "acct-bot"},
            "project": {"key": "PROJ"},
            "creator": {"accountId": "acct-alice"},
            "comment": {"comments": comments, "total": len(comments)},
        },
        "changelog": {"histories": histories, "total": len(histories)},
    }


ASSIGNED = {
    "id": "1",
    "author": {"accountId": "acct-alice"},
    "created": "2026-10-01T09:00:00.000+0000",
    "items": [{"field": "assignee", "to": "acct-bot"}],
}


@pytest.fixture(autouse=True)
def _no_default_transport(forbid_default_transport) -> None:  # noqa: F811
    return None


def test_fixture_inventory_matches_scenarios() -> None:
    assert_fixture_inventory("jira")


@pytest.mark.parametrize("scenario", scenarios_for("jira"), ids=lambda s: s.name)
def test_contract_scenario(scenario: Any) -> None:
    merged = {"merged": "Done"} if "jira" in scenario.merged_mapping else {}

    def build(transport: ReplayTransport) -> JiraAdapter:
        return adapter_over(transport, config(transitions=merged))

    outcome = run_contract(scenario, "jira", build, REF)
    assert_outcome(scenario, "jira", outcome)


@pytest.mark.parametrize(
    "env",
    [
        {**ENV, "JIRA_SITE": "Acme.Atlassian.net"},
        {**ENV, "JIRA_SITE": "evil.example/x"},
        {**ENV, "JIRA_SITE": ""},
        {**ENV, "JIRA_EMAIL": ""},
        {**ENV, "JIRA_API_TOKEN": ""},
    ],
)
def test_build_refuses_bad_credentials(env: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        build_adapter("jira", config(), env, ReplayTransport([]))


@pytest.mark.parametrize("account_id", ["", 'x" OR project = "Y', "a b"])
def test_build_refuses_missing_or_unsafe_bot_identity(account_id: str) -> None:
    with pytest.raises(ValueError):
        build_adapter("jira", config(account_id=account_id), ENV, ReplayTransport([]))


def test_requests_carry_basic_auth() -> None:
    transport = ReplayTransport([exchange("GET", READ, 503)])
    with pytest.raises(TrackerUnavailable):
        adapter_over(transport).read(REF)
    assert transport.requests[0].headers["Authorization"].startswith("Basic ")


def test_read_url_is_canonical_and_ref_must_be_configured() -> None:
    transport = ReplayTransport([exchange("GET", READ, 200, issue_payload([ASSIGNED], []))])
    issue = adapter_over(transport).read(REF)
    assert issue.url == f"https://{SITE}/browse/{REF}"
    assert issue.bot_id == "acct-bot"
    with pytest.raises(ValueError):
        adapter_over(ReplayTransport([])).read("OTHER-1")
    with pytest.raises(ValueError):
        adapter_over(ReplayTransport([])).read("PROJ-45/../x")


UPDATED = "2026-10-01T12:00:00.000+0000"


def test_listed_issue_url_is_canonical() -> None:
    body = {"issues": [{"id": "1", "key": "PROJ-1", "fields": {"updated": UPDATED}}, {"id": "2", "key": "OTHER-2"}], "isLast": True}
    jql = JQL
    transport = ReplayTransport(
        [exchange("POST", API + "search/jql", 200, body, {"jql": jql, "maxResults": 50, "fields": ["updated"]})]
    )
    issues, complete = adapter_over(transport).list_assigned()
    assert [i.url for i in issues] == [f"https://{SITE}/browse/PROJ-1"]
    assert complete is False


def test_mismatched_key_is_malformed() -> None:
    payload = issue_payload([ASSIGNED], [])
    payload["key"] = "PROJ-99"
    transport = ReplayTransport([exchange("GET", READ, 200, payload)])
    with pytest.raises(TrackerUnavailable) as caught:
        adapter_over(transport).read(REF)
    assert caught.value.code == "malformed_payload"


def test_bot_edit_comment_is_not_skipped_as_the_bot() -> None:
    comment = {
        "id": "7",
        "author": {"accountId": "acct-bot"},
        "body": None,
        "created": "2026-10-01T10:00:00.000+0000",
        "updated": "2026-10-01T10:05:00.000+0000",
    }
    transport = ReplayTransport([exchange("GET", READ, 200, issue_payload([ASSIGNED], [comment]))])
    issue = adapter_over(transport).read(REF)
    edits = [a for a in issue.activities if a.kind == "comment_edit"]
    assert [a.actor_id for a in edits] == [None]


def role_exchanges(actors: list[dict[str, Any]], user_groups: list[str]) -> list[dict[str, Any]]:
    out = [exchange("GET", API + "user?accountId=acct-alice", 200, {"active": True})]
    out.append(
        exchange("GET", API + "project/PROJ/role", 200, {"Developers": API + "project/10000/role/10002"})
    )
    out.append(exchange("GET", API + "project/PROJ/role/10002", 200, {"actors": actors}))
    if any("actorGroup" in a for a in actors):
        out.append(
            exchange("GET", API + "user/groups?accountId=acct-alice", 200, [{"name": n} for n in user_groups])
        )
    return out


def trust_over(exchanges: list[dict[str, Any]]) -> bool | None:
    transport = ReplayTransport([exchange("GET", READ, 200, issue_payload([ASSIGNED], []))] + exchanges)
    adapter = adapter_over(transport, config(trusted_groups=(), trusted_project_roles=("Developers",)))
    issue = adapter.read(REF)
    verdict = adapter.trust("acct-alice", issue)
    transport.finish()
    return verdict


def test_role_user_actor_is_trusted() -> None:
    actors = [{"type": "atlassian-user-role-actor", "actorUser": {"accountId": "acct-alice"}}]
    assert trust_over(role_exchanges(actors, [])) is True


def test_role_group_actor_is_trusted_through_membership() -> None:
    actors = [{"type": "atlassian-group-role-actor", "actorGroup": {"name": "devs"}}]
    assert trust_over(role_exchanges(actors, ["devs"])) is True
    assert trust_over(role_exchanges(actors, ["other"])) is False


def test_user_outside_the_role_is_untrusted() -> None:
    actors = [{"type": "atlassian-user-role-actor", "actorUser": {"accountId": "acct-zed"}}]
    assert trust_over(role_exchanges(actors, [])) is False


def test_inactive_user_is_untrusted_without_group_lookup() -> None:
    transport = ReplayTransport(
        [
            exchange("GET", READ, 200, issue_payload([ASSIGNED], [])),
            exchange("GET", API + "user?accountId=acct-alice", 200, {"active": False}),
        ]
    )
    adapter = adapter_over(transport)
    issue = adapter.read(REF)
    assert adapter.trust("acct-alice", issue) is False
    transport.finish()


def test_trust_lookups_are_cached_per_adapter() -> None:
    transport = ReplayTransport(
        [
            exchange("GET", READ, 200, issue_payload([ASSIGNED], [])),
            exchange("GET", API + "user?accountId=acct-alice", 200, {"active": True}),
            exchange("GET", API + "user/groups?accountId=acct-alice", 200, [{"name": "jira-trusted"}]),
        ]
    )
    adapter = adapter_over(transport)
    issue = adapter.read(REF)
    assert adapter.trust("acct-alice", issue) is True
    assert adapter.trust("acct-alice", issue) is True
    transport.finish()


@pytest.mark.parametrize("status", [401, 403])
def test_trust_lookup_denied_fails_closed_with_http_403(status: int) -> None:
    transport = ReplayTransport(
        [
            exchange("GET", READ, 200, issue_payload([ASSIGNED], [])),
            exchange("GET", API + "user?accountId=acct-alice", status),
        ]
    )
    adapter = adapter_over(transport)
    issue = adapter.read(REF)
    with pytest.raises(TrackerUnavailable) as caught:
        adapter.trust("acct-alice", issue)
    assert caught.value.code == "http_403"


def test_group_lookup_denied_fails_closed() -> None:
    transport = ReplayTransport(
        [
            exchange("GET", READ, 200, issue_payload([ASSIGNED], [])),
            exchange("GET", API + "user?accountId=acct-alice", 200, {"active": True}),
            exchange("GET", API + "user/groups?accountId=acct-alice", 403),
        ]
    )
    adapter = adapter_over(transport)
    issue = adapter.read(REF)
    assert delivery_decision(issue, adapter.trust).kind == "unavailable"


def test_actions_refuse_unconfigured_projects() -> None:
    adapter = adapter_over(ReplayTransport([]))
    assert adapter.comment("OTHER-1", "x").status == "refused"
    assert adapter.transition("OTHER-1", "Done").status == "refused"
    assert adapter.close("OTHER-1").status == "refused"


def test_issue_assigned_to_someone_else_is_not_the_bots() -> None:
    payload = issue_payload([ASSIGNED], [])
    payload["fields"]["assignee"] = {"accountId": "acct-other"}
    transport = ReplayTransport([exchange("GET", READ, 200, payload)])
    issue = adapter_over(transport).read(REF)
    assert issue.assigned_to_bot is False
    assert delivery_decision(issue, lambda *_: True).kind == "skip"


def test_only_assignments_to_the_bot_are_assigned_activities() -> None:
    elsewhere = {**ASSIGNED, "id": "2", "items": [{"field": "assignee", "to": "acct-other"}]}
    transport = ReplayTransport([exchange("GET", READ, 200, issue_payload([ASSIGNED, elsewhere], []))])
    issue = adapter_over(transport).read(REF)
    assert [a.kind for a in issue.activities] == ["assigned"]


def comment_payload(**overrides: Any) -> dict[str, Any]:
    return {
        "id": "7",
        "author": {"accountId": "acct-alice"},
        "body": None,
        "created": "2026-10-01T10:00:00.000+0000",
        "updated": "2026-10-01T10:00:00.000+0000",
        **overrides,
    }


def test_activity_ids_follow_the_documented_formulas() -> None:
    edited_text = "2026-10-01T10:05:00.000+0000"
    unedited = comment_payload(id="8")
    edited = comment_payload(updated=edited_text)
    history = {
        "id": "5",
        "author": {"accountId": "acct-alice"},
        "created": "2026-10-01T11:00:00.000+0000",
        "items": [{"field": "summary"}, {"field": "description"}],
    }
    transport = ReplayTransport(
        [exchange("GET", READ, 200, issue_payload([ASSIGNED, history], [unedited, edited]))]
    )
    issue = adapter_over(transport).read(REF)
    ids = {(a.kind, a.id) for a in issue.activities}
    assert ids == {
        ("assigned", activity_id("jira", "1:assigned")),
        ("title_edit", activity_id("jira", "5:title_edit")),
        ("body_edit", activity_id("jira", "5:body_edit")),
        ("comment", activity_id("jira", "7")),
        ("comment", activity_id("jira", "8")),
        ("comment_edit", activity_id("jira", f"7:edited:{edited_text}")),
    }


def test_delivered_cutoff_id_is_a_safe_event_id() -> None:
    scenario = next(s for s in scenarios_for("jira") if s.name == "attest_trusted")
    transport = ReplayTransport.from_fixture("jira", scenario.name)
    adapter = adapter_over(transport, config())
    issue = adapter.read(REF)
    decision = delivery_decision(issue, adapter.trust)
    assert decision.kind == "deliver" and decision.cutoff is not None
    assert safe_event_id(decision.cutoff.id) == decision.cutoff.id


def read_with(payload: dict[str, Any]) -> Any:
    return adapter_over(ReplayTransport([exchange("GET", READ, 200, payload)])).read(REF)


def test_closed_issue_is_skipped_and_open_control_is_not() -> None:
    closed = issue_payload([ASSIGNED], [])
    closed["fields"]["status"] = {"name": "Done", "statusCategory": {"key": "done"}}
    issue = read_with(closed)
    assert issue.is_open is False
    assert delivery_decision(issue, lambda *_: True).kind == "skip"
    control = read_with(issue_payload([ASSIGNED], []))
    assert control.is_open is True
    assert delivery_decision(control, lambda *_: True).kind == "deliver"


def test_truncated_comments_withhold_even_with_a_complete_changelog() -> None:
    payload = issue_payload([ASSIGNED], [comment_payload()])
    payload["fields"]["comment"]["total"] = 2
    issue = read_with(payload)
    decision = delivery_decision(issue, lambda *_: True)
    assert (decision.kind, decision.reason) == ("withhold", "thread_too_long")
    assert delivery_decision(read_with(issue_payload([ASSIGNED], [comment_payload()])), lambda *_: True).kind == (
        "deliver"
    )


def test_malformed_account_id_has_no_actor_and_is_never_looked_up() -> None:
    bad = {**ASSIGNED, "author": {"accountId": "bad id!"}}
    issue = read_with(issue_payload([bad], []))
    assert [a.actor_id for a in issue.activities] == [None]
    lookups: list[str] = []

    def trust(actor_id: str, _issue: Any) -> bool:
        lookups.append(actor_id)
        return True

    decision = delivery_decision(issue, trust)
    assert (decision.kind, decision.reason) == ("withhold", "untrusted_latest_activity")
    assert lookups == []


def test_close_per_project_transition_overrides_the_global_one() -> None:
    cfg = config(transitions={"merged": "Done"}, project_transitions={"PROJ": {"merged": "Shipped"}})
    status = exchange("GET", API + f"issue/{REF}?fields=status", 200, {"fields": {"status": {"name": "Open"}}})
    listing = exchange(
        "GET",
        API + f"issue/{REF}/transitions",
        200,
        {"transitions": [{"id": "1", "to": {"name": "Done"}}, {"id": "2", "to": {"name": "Shipped"}}]},
    )
    post = exchange("POST", API + f"issue/{REF}/transitions", 204, None, {"transition": {"id": "2"}})
    transport = ReplayTransport([status, listing, post])
    assert adapter_over(transport, cfg).close(REF).status == "applied"
    transport.finish()


def test_transition_status_preread_403_is_permission_denied() -> None:
    cfg = config(transitions={"merged": "Done"})
    for act in (lambda a: a.close(REF), lambda a: a.transition(REF, "Done")):
        transport = ReplayTransport([exchange("GET", API + f"issue/{REF}?fields=status", 403)])
        result = act(adapter_over(transport, cfg))
        assert (result.status, result.reason) == ("unavailable", "permission_denied")
        transport.finish()


def test_listed_issues_carry_the_parsed_utc_update_time() -> None:
    transport = ReplayTransport.from_fixture("jira", "list_assigned")
    issues, complete = adapter_over(transport).list_assigned()
    assert complete is True
    assert [(i.ref, i.updated_at) for i in issues] == [
        ("PROJ-46", "2026-10-01T13:00:00+00:00"),
        ("PROJ-45", "2026-10-01T12:00:00+00:00"),
    ]
    zoned = {"issues": [{"id": "1", "key": "PROJ-1", "fields": {"updated": "2026-10-01T14:00:00.000+0200"}}], "isLast": True}
    body = {"jql": JQL, "maxResults": 50, "fields": ["updated"]}
    transport = ReplayTransport([exchange("POST", API + "search/jql", 200, zoned, body)])
    (issue,), _ = adapter_over(transport).list_assigned()
    assert issue.updated_at == "2026-10-01T12:00:00+00:00"


@pytest.mark.parametrize("fields", [{}, {"updated": None}, {"updated": "yesterday"}, {"updated": "2026-10-01T12:00:00"}])
def test_listed_issue_without_a_valid_update_time_is_omitted_and_incomplete(fields: dict[str, Any]) -> None:
    body = {
        "issues": [{"id": "1", "key": "PROJ-1", "fields": fields}, {"id": "2", "key": "PROJ-2", "fields": {"updated": UPDATED}}],
        "isLast": True,
    }
    request = {"jql": JQL, "maxResults": 50, "fields": ["updated"]}
    transport = ReplayTransport([exchange("POST", API + "search/jql", 200, body, request)])
    issues, complete = adapter_over(transport).list_assigned()
    assert [i.ref for i in issues] == ["PROJ-2"]
    assert complete is False


def test_project_keys_are_quoted_in_the_jql() -> None:
    cfg = config(projects=("PROJ", "IN", "OR"))
    jql = (
        'assignee = "acct-bot" AND statusCategory != Done '
        'AND project in ("PROJ", "IN", "OR") ORDER BY updated DESC'
    )
    request = {"jql": jql, "maxResults": 50, "fields": ["updated"]}
    transport = ReplayTransport([exchange("POST", API + "search/jql", 200, {"issues": [], "isLast": True}, request)])
    assert adapter_over(transport, cfg).list_assigned() == ([], True)
    transport.finish()


def test_equal_instants_in_different_zones_are_not_a_comment_edit() -> None:
    same_instant = comment_payload(updated="2026-10-01T11:00:00.000+0100")
    issue = read_with(issue_payload([ASSIGNED], [same_instant]))
    assert [a.kind for a in issue.activities if a.kind == "comment_edit"] == []
    edited = comment_payload(updated="2026-10-01T11:01:00.000+0100")
    assert [a.kind for a in read_with(issue_payload([ASSIGNED], [edited])).activities if a.kind == "comment_edit"] == [
        "comment_edit"
    ]
