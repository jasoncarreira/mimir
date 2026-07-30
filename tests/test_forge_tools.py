from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from langchain.tools import ToolRuntime
from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import ToolException
from langgraph.prebuilt import ToolNode
from langgraph.runtime import Runtime

import mimir.access_control as access_control
from mimir.event_logger import _reset_logger_for_tests, init_logger
from mimir.forge import (
    CheckProjection,
    CommentProjection,
    FileProjection,
    PullRequestProjection,
    ReviewProjection,
    ReviewRequestProjection,
    ReviewVerdict,
)
from mimir.models import (
    AuthContext, InformationFlowLabels, NormalizedPullRequestSnapshot,
    RepoPRAction, RepoPRActionScope,
    RepoPRScopeRegistry,
    RepoReviewState,
)
from mimir.tools.forge import (
    FORGE_TOOLS,
    pr_checks,
    pr_comment,
    pr_comments,
    pr_diff,
    pr_files,
    pr_inline_review_comment,
    pr_metadata,
    pr_rerequest_review,
    pr_review_requests,
    pr_reviews,
    pr_submit_review,
    set_forge_client,
    unsupported_operation,
)
from mimir.tools.repo import repo_test
from mimir.tools.budget_gate import BudgetGateMiddleware


def _scope(
    *actions: RepoPRAction,
    number: int = 17,
    head_sha: str = "a" * 40,
) -> RepoPRActionScope:
    return RepoPRActionScope(
        provenance="poller_payload",
        canonical_repo="owner/repo",
        canonical_root="/tmp/repo",
        canonical_origin="ssh://forge.invalid/owner/repo",
        principal="reviewer",
        event_type="pr_review_requested",
        allowed_operations=frozenset(action.value for action in actions),
        pr_number=number,
        head_repo="contributor/repo",
        head_remote="source",
        destination_ref="refs/heads/change",
        observed_head_sha=head_sha,
        base_ref="main",
        observed_base_sha="b" * 40,
    )


def _runtime(scope: RepoPRActionScope) -> ToolRuntime[AuthContext]:
    return _runtime_for_scopes(scope)


def _runtime_for_scopes(*scopes: RepoPRActionScope) -> ToolRuntime[AuthContext]:
    states = tuple(RepoReviewState(scope) for scope in scopes)
    context = AuthContext(
        principal="service:poller",
        canonical_principal="poller",
        roles=("service",),
        event_ingress=None,
        trigger="poller",
        channel_id="poller:forge",
        interactivity=None,
        enforcement_enabled=True,
        ifc_labels=InformationFlowLabels(),
        repo_pr_scope_registry=RepoPRScopeRegistry(states),
        repo_review_state=states[0] if len(states) == 1 else None,
        repo_pr_action_scope=scopes[0] if len(scopes) == 1 else None,
    )
    return ToolRuntime(
        state={}, context=context, config={}, stream_writer=lambda _: None,
        tool_call_id="forge-tool-test", store=None,
    )


class FakeForge:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def get_pull_request(self, scope):
        self.calls.append(("metadata", scope))
        return PullRequestProjection(
            17, "Title", "open", "author", False, "main", "change",
            "a" * 40, True, "created", "updated",
        )

    def get_pull_request_snapshot(self, repository, number):
        self.calls.append(("snapshot", repository, number))
        return NormalizedPullRequestSnapshot(
            state="open", number=number, author="untrusted-author",
            head_repo="contributor/repo", head_remote="source",
            head_ref="server-head", head_sha="c" * 40,
            base_ref="server-base", base_sha="d" * 40,
        )

    def list_files(self, scope):
        self.calls.append(("files", scope))
        return (FileProjection("src/app.py", "modified", 2, 1, 3, "@@ patch"),)

    def get_diff(self, scope):
        self.calls.append(("diff", scope))
        return "diff --git a/src/app.py b/src/app.py"

    def list_checks(self, scope):
        self.calls.append(("checks", scope))
        return (CheckProjection("test", "completed", "success", "now", "now"),)

    def list_reviews(self, scope):
        self.calls.append(("reviews", scope))
        return (ReviewProjection("1", "reviewer", "approve", "LGTM", "now", "a" * 40),)

    def list_comments(self, scope):
        self.calls.append(("comments", scope))
        return (CommentProjection("1", "reviewer", "note", "now", "now"),)

    def list_review_requests(self, scope):
        self.calls.append(("review_requests", scope))
        return (ReviewRequestProjection("reviewer", "user"),)

    def submit_review(self, scope, verdict, body):
        self.calls.append(("review", scope, verdict, body))
        return ReviewProjection("1", "reviewer", verdict.value, body, "now", "a" * 40)

    def add_inline_review_comment(self, scope, *, path, line, body):
        self.calls.append(("inline", scope, path, line, body))
        return CommentProjection("1", "reviewer", body, "now", "now", path, line)

    def add_pull_request_comment(self, scope, body):
        self.calls.append(("comment", scope, body))
        return CommentProjection("1", "reviewer", body, "now", "now")

    def rerequest_review(self, scope, reviewer):
        self.calls.append(("rerequest", scope, reviewer))


@pytest.fixture(autouse=True)
def _reset_client() -> None:
    set_forge_client(None)
    yield
    set_forge_client(None)
    _reset_logger_for_tests()


def test_tool_surface_requires_exact_repository_and_pr_selectors() -> None:
    for forge_tool in FORGE_TOOLS:
        properties = forge_tool.tool_call_schema.model_json_schema()["properties"]
        assert {"repository", "pull_request"} <= set(properties)
        assert not ({"repo", "pr_number", "url", "host"} & set(properties))
        assert "runtime" not in properties
        assert forge_tool._injected_args_keys == frozenset({"runtime"})


def test_every_tool_class_invokes_through_langchain_with_injected_runtime(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeForge()
    set_forge_client(client)
    runtime = _runtime(_scope(*RepoPRAction))
    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    init_logger(home / "events.jsonl", "test")
    invocations = (
        (pr_metadata, {}), (pr_files, {}), (pr_diff, {}), (pr_checks, {}),
        (pr_reviews, {}), (pr_comments, {}), (pr_review_requests, {}),
        (pr_submit_review, {"verdict": "approve", "body": "Looks good"}),
        (pr_inline_review_comment, {"path": "src/app.py", "line": 1, "body": "Fix"}),
        (pr_comment, {"body": "Fixed"}),
        (pr_rerequest_review, {"reviewer": "reviewer"}),
        (unsupported_operation, {
            "description": "Resolve an inline review thread",
            "attempted_operations": ["Listed review comments", "Looked for a resolve tool"],
        }),
    )

    node = ToolNode(list(FORGE_TOOLS))
    for index, (forge_tool, arguments) in enumerate(invocations):
        arguments = {"repository": "owner/repo", "pull_request": 17, **arguments}
        tool_call = {
            "name": forge_tool.name, "args": arguments,
            "id": f"forge-{index}", "type": "tool_call",
        }
        injected = node._inject_tool_args(tool_call, runtime)
        assert injected["args"]["runtime"] is runtime, forge_tool.name
        assert forge_tool.invoke(injected["args"]) is not None, forge_tool.name

    assert [call[0] for call in client.calls] == [
        "metadata", "files", "diff", "checks", "reviews", "comments",
        "review_requests", "review", "inline", "comment", "rerequest",
    ]


def test_read_uses_only_immutable_scope_target() -> None:
    client = FakeForge()
    set_forge_client(client)
    scope = _scope(RepoPRAction.INSPECT)

    result = pr_metadata.func(
        repository="owner/repo", pull_request=17, runtime=_runtime(scope),
    )

    assert result["number"] == 17
    assert client.calls == [("metadata", scope)]


def test_review_scope_cannot_rerequest_review() -> None:
    client = FakeForge()
    set_forge_client(client)
    scope = _scope(RepoPRAction.INSPECT, RepoPRAction.PR_REVIEW)
    runtime = _runtime(scope)

    authorization = access_control.ToolRegistry().authorize_tool(
        "pr_rerequest_review", runtime.context, enforce=True,
        arguments={"repository": "owner/repo", "pull_request": 17},
    )
    assert authorization.allowed is False
    assert authorization.reason == "repo_pr_scope_denied"
    # The tool implementation resolves execution scope only; middleware serves
    # the gate's refusal before this callable can reach the forge adapter.
    assert client.calls == []


def test_batched_turn_resolves_each_exact_existing_scope() -> None:
    client = FakeForge()
    set_forge_client(client)
    first = _scope(RepoPRAction.INSPECT)
    second = _scope(RepoPRAction.INSPECT, number=18, head_sha="c" * 40)
    runtime = _runtime_for_scopes(first, second)

    pr_metadata.func(repository="OWNER/REPO", pull_request=17, runtime=runtime)
    pr_metadata.func(repository="owner/repo", pull_request=18, runtime=runtime)

    assert [call[1] for call in client.calls] == [first, second]


def test_forge_refusal_names_unconfigured_repository() -> None:
    context = AuthContext(
        principal=None, canonical_principal=None, roles=(), event_ingress=None,
        trigger="poller", channel_id="poller:forge", interactivity=None,
    )
    runtime = ToolRuntime(
        state={}, context=context, config={}, stream_writer=lambda _: None,
        tool_call_id="forge-no-scope", store=None,
    )

    with pytest.raises(ToolException, match="not configured in GITHUB_REPOS"):
        pr_metadata.func(
            repository="owner/repo", pull_request=17, runtime=runtime,
        )


def test_review_and_remediation_actions_do_not_widen_each_other() -> None:
    client = FakeForge()
    set_forge_client(client)
    review = _runtime(_scope(RepoPRAction.INSPECT, RepoPRAction.PR_REVIEW))
    remediation = _runtime(
        _scope(RepoPRAction.INSPECT, RepoPRAction.PR_COMMENT, RepoPRAction.PR_REREQUEST)
    )

    pr_submit_review.func(
        repository="owner/repo", pull_request=17,
        verdict=ReviewVerdict.APPROVE, body="Looks good", runtime=review,
    )
    pr_comment.func(
        repository="owner/repo", pull_request=17, body="Fixed", runtime=remediation,
    )
    arguments = {"repository": "owner/repo", "pull_request": 17}
    registry = access_control.ToolRegistry()
    assert registry.authorize_tool(
        "pr_submit_review", remediation.context, enforce=True, arguments=arguments,
    ).reason == "repo_pr_scope_denied"
    assert registry.authorize_tool(
        "pr_comment", review.context, enforce=True, arguments=arguments,
    ).reason == "repo_pr_scope_denied"


def test_review_scope_has_no_event_or_requested_reviewer_gate_and_exact_safe_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "reviewer")
    monkeypatch.setattr(
        access_control,
        "_canonical_repo_binding",
        lambda repo: ("/tmp/repo", "ssh://forge.invalid/owner/repo"),
    )
    fields = dict(
        provenance="poller_payload",
        repo="owner/repo",
        principal="author",
        event_type="pr_review_requested",
        number=17,
        head_repo="fork/repo",
        head_remote="source",
        head_ref="change",
        head_sha="a" * 40,
        base_ref="main",
        base_sha="b" * 40,
    )

    scope = access_control._repo_pr_scope(
        **fields,
    )

    assert scope.allowed_operations == frozenset({
        "repo.inspect", "repo.checkout", "repo.test", "pr.review", "pr.comment",
    })
    for denied in (
        RepoPRAction.WRITE, RepoPRAction.COMMIT, RepoPRAction.PUSH,
        RepoPRAction.PR_EDIT, RepoPRAction.PR_REREQUEST,
    ):
        assert denied.value not in scope.allowed_operations

    for event_type in ("pr_review", "pr_opened", "pr_synchronize"):
        candidate = access_control._repo_pr_scope(**{**fields, "event_type": event_type})
        assert candidate is not None
        assert candidate.allowed_operations == scope.allowed_operations


@dataclass(frozen=True)
class _TestResult:
    status: str


def test_operator_turn_discovers_live_review_scope_and_reaches_repo_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeForge()
    set_forge_client(client)
    monkeypatch.setenv("GITHUB_REPOS", "owner/repo")
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "reviewer")
    monkeypatch.setattr(
        access_control, "_canonical_repo_binding",
        lambda repo: ("/server/configured/repo", "git@github.com:owner/repo.git"),
    )
    monkeypatch.setattr(
        "mimir.tools.repo.RepoProjectTests",
        lambda state: type("Tests", (), {"execute": lambda self, selectors: _TestResult("ok")})(),
    )
    context = AuthContext(
        principal="operator", canonical_principal="operator", roles=("admin",),
        event_ingress=None, trigger="message", channel_id="operator", interactivity=None,
    )
    runtime = ToolRuntime(
        state={}, context=context, config={}, stream_writer=lambda _: None,
        tool_call_id="operator-review", store=None,
    )

    assert repo_test.func(
        repository="OWNER/REPO", pull_request=1291, runtime=runtime,
    ) == {"status": "ok"}
    state = context.server_discovered_pr_states.resolve("owner/repo", 1291)
    assert state is not None
    scope = state.action_scope
    assert scope.provenance == "server_discovered"
    assert scope.principal == "reviewer"
    assert scope.head_repo == "contributor/repo"
    assert scope.head_ref == "server-head"
    assert scope.observed_head_sha == "c" * 40
    assert scope.base_ref == "server-base"
    assert scope.observed_base_sha == "d" * 40
    assert scope.checkout_ref == "refs/pull/1291/head"
    assert client.calls == [("snapshot", "owner/repo", 1291)]


def test_standing_review_refuses_unconfigured_repo_before_live_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeForge()
    set_forge_client(client)
    monkeypatch.setenv("GITHUB_REPOS", "owner/repo")
    context = AuthContext(
        principal="operator", canonical_principal="operator", roles=("admin",),
        event_ingress=None, trigger="message", channel_id="operator", interactivity=None,
    )
    runtime = ToolRuntime(
        state={}, context=context, config={}, stream_writer=lambda _: None,
        tool_call_id="operator-review", store=None,
    )

    with pytest.raises(ToolException, match="not configured in GITHUB_REPOS"):
        pr_metadata.func(
            repository="attacker/other", pull_request=1, runtime=runtime,
        )
    assert client.calls == []


@pytest.mark.asyncio
async def test_enforced_middleware_resolves_standing_review_before_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeForge()
    set_forge_client(client)
    monkeypatch.setenv("GITHUB_REPOS", "owner/repo")
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "reviewer")
    monkeypatch.setenv("MIMIR_ACCESS_CONTROL_ENFORCED", "1")
    monkeypatch.setattr(
        access_control, "_canonical_repo_binding",
        lambda repo: ("/server/configured/repo", "git@github.com:owner/repo.git"),
    )
    context = AuthContext(
        principal="operator", canonical_principal="operator", roles=("admin",),
        event_ingress=None, trigger="message", channel_id="operator", interactivity=None,
        enforcement_enabled=True, ifc_labels=InformationFlowLabels(),
    )
    request = ToolCallRequest(
        tool_call={
            "name": "repo_test",
            "args": {"repository": "owner/repo", "pull_request": 1291},
            "id": "standing-review", "type": "tool_call",
        },
        tool=None, state=None, runtime=Runtime(context=context),
    )
    called = False

    async def handler(request):
        nonlocal called
        called = True
        return ToolMessage(content="tested", tool_call_id="standing-review")

    result = await BudgetGateMiddleware().awrap_tool_call(request, handler)

    assert result.status != "error"
    assert called is True
    assert context.server_discovered_pr_states.resolve("owner/repo", 1291) is not None
    assert client.calls == [("snapshot", "owner/repo", 1291)]


def test_body_and_inline_path_injection_are_rejected_before_adapter() -> None:
    client = FakeForge()
    set_forge_client(client)
    runtime = _runtime(_scope(RepoPRAction.PR_REVIEW, RepoPRAction.PR_COMMENT))

    with pytest.raises(ToolException, match="65536-byte"):
        pr_comment.func(
            repository="owner/repo", pull_request=17,
            body="x" * 65_537, runtime=runtime,
        )
    with pytest.raises(ToolException, match="relative repository path"):
        pr_inline_review_comment.func(
            repository="owner/repo", pull_request=17,
            path="../../secret", line=1, body="x", runtime=runtime,
        )
    with pytest.raises(ToolException, match="null byte"):
        pr_comment.func(
            repository="owner/repo", pull_request=17,
            body="hello\x00world", runtime=runtime,
        )
    assert client.calls == []


def test_unsupported_operation_is_durable_and_deduped(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    events = home / "events.jsonl"
    init_logger(events, "test")
    runtime = _runtime(_scope(RepoPRAction.INSPECT))

    first = unsupported_operation.func(
        repository="owner/repo", pull_request=17,
        description="Resolve an inline review thread",
        attempted_operations=["pr_comments", "pr_inline_review_comment"],
        runtime=runtime,
    )
    second = unsupported_operation.func(
        repository="owner/repo", pull_request=17,
        description="Resolve an inline review thread",
        attempted_operations=["pr_comments", "pr_inline_review_comment"],
        runtime=runtime,
    )

    assert first["status"] == "unsupported_operation"
    assert first["escalated"] is True
    assert second["escalated"] is False
    assert first["description"] == "Resolve an inline review thread"
    assert first["attempted_operations"] == ["pr_comments", "pr_inline_review_comment"]
    assert first["operation"].startswith("resolve_an_inline_review_thread:")
    records = [json.loads(line) for line in events.read_text().splitlines()]
    assert len(records) == 1
    assert records[0] | {
        "repository": "owner/repo",
        "pull_request": 17,
        "operation": first["operation"],
        "description": "Resolve an inline review thread",
        "attempted_operations": ["pr_comments", "pr_inline_review_comment"],
        "operator_visible": True,
    } == records[0]


def test_malformed_escalation_is_normalized_bounded_and_non_fatal(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("MIMIR_HOME", str(home))
    events = home / "events.jsonl"
    init_logger(events, "test")

    result = unsupported_operation.func(
        repository="owner/repo",
        pull_request=17,
        description=None,
        attempted_operations={"bad\nfield": "ghp_abcdefghijklmnopqrstuvwxyz"},
        runtime=_runtime(_scope(RepoPRAction.INSPECT)),
    )

    assert result["escalated"] is True
    assert result["description"].startswith("The caller did not provide")
    assert "\n" not in result["attempted_operations"][0]
    assert "ghp_" not in result["attempted_operations"][0]
    record = json.loads(events.read_text())
    assert len(record["description"].encode()) <= 4_096
    assert len(record["attempted_operations"][0].encode()) <= 512


def test_distinct_escalations_with_same_slug_words_are_not_deduplicated(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("MIMIR_HOME", str(home))
    init_logger(home / "events.jsonl", "test")
    runtime = _runtime(_scope(RepoPRAction.INSPECT))

    first = unsupported_operation.func(
        repository="owner/repo", pull_request=17,
        description="Resolve thread after submitting a review alpha",
        runtime=runtime,
    )
    second = unsupported_operation.func(
        repository="owner/repo", pull_request=17,
        description="Resolve thread after submitting a review beta",
        runtime=runtime,
    )

    assert first["escalated"] is True
    assert second["escalated"] is True
    assert first["operation"] != second["operation"]
