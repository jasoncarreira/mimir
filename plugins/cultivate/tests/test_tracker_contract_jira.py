"""Contract and adapter-specific tests for the Jira tracker adapter, over recorded exchanges."""

from __future__ import annotations

from typing import Any

import pytest

from mimir_cultivate.tracker.adapters import build_adapter
from mimir_cultivate.tracker.adapters.jira import JiraAdapter
from mimir_cultivate.tracker.config import TrackerAdapterConfig
from mimir_cultivate.tracker.thread import delivery_decision
from mimir_cultivate.tracker.transport import TrackerUnavailable
from tracker_replay import (
    ReplayTransport,
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


def test_listed_issue_url_is_canonical() -> None:
    body = {"issues": [{"id": "1", "key": "PROJ-1"}, {"id": "2", "key": "OTHER-2"}], "isLast": True}
    jql = 'assignee = "acct-bot" AND statusCategory != Done AND project in (PROJ) ORDER BY updated DESC'
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
