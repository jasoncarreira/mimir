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
    IssueTarget,
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
    issue_comment,
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
from mimir.tools.repo import repo_status, repo_test
from mimir.tools.budget_gate import BudgetGateMiddleware


@pytest.fixture(autouse=True)
def _isolate_operator_repository_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy GITHUB_REPOS cases must not inherit the operator's inventory."""
    monkeypatch.delenv("MIMIR_HOME", raising=False)


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
        self.snapshot_author = "untrusted-author"
        self.reviews = (
            ReviewProjection("1", "reviewer", "approve", "LGTM", "now", "a" * 40),
        )

    def get_pull_request(self, scope):
        self.calls.append(("metadata", scope))
        return PullRequestProjection(
            17, "Title", "open", "author", False, "main", "change",
            "a" * 40, True, "created", "updated",
        )

    def get_pull_request_snapshot(self, repository, number):
        self.calls.append(("snapshot", repository, number))
        return NormalizedPullRequestSnapshot(
            state="open", number=number, author=self.snapshot_author,
            head_repo=(
                repository if self.snapshot_author == "reviewer" else "contributor/repo"
            ),
            head_remote="origin" if self.snapshot_author == "reviewer" else "source",
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
        return self.reviews

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

    def get_open_issue_target(self, repository, issue):
        self.calls.append(("issue_target", repository, issue))
        return IssueTarget(repository, issue)

    def add_issue_comment(self, repository, issue, body):
        self.calls.append(("issue_comment", repository, issue, body))
        return CommentProjection("2", "reviewer", body, "now", "now")

    def rerequest_review(self, scope, reviewer):
        self.calls.append(("rerequest", scope, reviewer))


@pytest.fixture(autouse=True)
def _reset_client() -> None:
    set_forge_client(None)
    yield
    set_forge_client(None)
    _reset_logger_for_tests()


def test_tool_surface_requires_exact_repository_and_resource_selectors() -> None:
    for forge_tool in FORGE_TOOLS:
        properties = forge_tool.tool_call_schema.model_json_schema()["properties"]
        selectors = {"pull_request", "issue"} & set(properties)
        assert "repository" in properties
        assert len(selectors) == 1, forge_tool.name
        assert not ({"repo", "pr_number", "issue_number", "url", "host"} & set(properties))
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
    monkeypatch.setenv("GITHUB_REPOS", "owner/repo")
    init_logger(home / "events.jsonl", "test")
    invocations = (
        (pr_metadata, {}), (pr_files, {}), (pr_diff, {}), (pr_checks, {}),
        (pr_reviews, {}), (pr_comments, {}), (pr_review_requests, {}),
        (pr_submit_review, {"verdict": "approve", "body": "Looks good"}),
        (pr_inline_review_comment, {"path": "src/app.py", "line": 1, "body": "Fix"}),
        (pr_comment, {"body": "Fixed"}),
        (issue_comment, {"issue": 220, "body": "Analysis"}),
        (pr_rerequest_review, {"reviewer": "reviewer"}),
        (unsupported_operation, {
            "description": "Resolve an inline review thread",
            "attempted_operations": ["Listed review comments", "Looked for a resolve tool"],
        }),
    )

    node = ToolNode(list(FORGE_TOOLS))
    for index, (forge_tool, arguments) in enumerate(invocations):
        arguments = {"repository": "owner/repo", **arguments}
        if forge_tool is not issue_comment:
            arguments["pull_request"] = 17
        tool_call = {
            "name": forge_tool.name, "args": arguments,
            "id": f"forge-{index}", "type": "tool_call",
        }
        injected = node._inject_tool_args(tool_call, runtime)
        assert injected["args"]["runtime"] is runtime, forge_tool.name
        assert forge_tool.invoke(injected["args"]) is not None, forge_tool.name

    assert [call[0] for call in client.calls] == [
        "metadata", "files", "diff", "checks", "reviews", "comments",
        "review_requests", "review", "inline", "comment", "issue_target",
        "issue_comment", "rerequest",
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
        "_canonical_repo_binding_resolution",
        lambda repo: access_control.RepoBindingResolution(
            ("/tmp/repo", "ssh://forge.invalid/owner/repo"), ("/tmp/repo",), 1,
        ),
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


def test_ci_remediation_scope_requires_checkout_before_every_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "reviewer")
    monkeypatch.setattr(
        access_control,
        "_canonical_repo_binding_resolution",
        lambda _repo: access_control.RepoBindingResolution(
            ("/tmp/repo", "ssh://forge.invalid/owner/repo"), ("/tmp/repo",), 1,
        ),
    )

    scope = access_control._repo_pr_scope(
        provenance="poller_payload",
        repo="owner/repo",
        principal="reviewer",
        event_type="pr_ci_failure",
        number=17,
        head_repo="owner/repo",
        head_remote="origin",
        head_ref="worklink/17",
        head_sha="a" * 40,
        base_ref="main",
        base_sha="b" * 40,
    )

    assert scope.allowed_operations == frozenset({
        "repo.inspect", "repo.checkout", "repo.test", "repo.write",
        "repo.commit", "repo.push",
    })


@dataclass(frozen=True)
class _TestResult:
    status: str


@pytest.mark.asyncio
async def test_operator_turn_discovers_live_review_scope_and_reaches_repo_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeForge()
    set_forge_client(client)
    monkeypatch.setenv("GITHUB_REPOS", "owner/repo")
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "reviewer")
    monkeypatch.setattr(
        access_control, "_canonical_repo_binding_resolution",
        lambda repo: access_control.RepoBindingResolution(
            ("/server/configured/repo", "git@github.com:owner/repo.git"),
            ("/server/configured/repo",), 1,
        ),
    )
    class Tests:
        async def execute(self, selectors):
            return _TestResult("ok")

    monkeypatch.setattr("mimir.tools.repo.RepoProjectTests", lambda state: Tests())
    context = AuthContext(
        principal="operator", canonical_principal="operator", roles=("admin",),
        event_ingress=None, trigger="message", channel_id="operator", interactivity=None,
    )
    runtime = ToolRuntime(
        state={}, context=context, config={}, stream_writer=lambda _: None,
        tool_call_id="operator-review", store=None,
    )

    assert await repo_test.coroutine(
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


def test_server_discovered_changes_requested_review_reaches_repo_write_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeForge()
    client.snapshot_author = "reviewer"
    client.reviews = (
        ReviewProjection(
            "1", "jasoncarreira", "CHANGES_REQUESTED", "fix", "now", "c" * 40,
        ),
    )
    set_forge_client(client)
    monkeypatch.setenv("GITHUB_REPOS", "owner/repo")
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "reviewer")
    monkeypatch.setattr(
        access_control, "_canonical_repo_binding_resolution",
        lambda repo: access_control.RepoBindingResolution(
            ("/server/configured/repo", "git@github.com:owner/repo.git"),
            ("/server/configured/repo",), 1,
        ),
    )
    context = AuthContext(
        principal="operator", canonical_principal="operator", roles=("admin",),
        event_ingress=None, trigger="message", channel_id="operator", interactivity=None,
        enforcement_enabled=True, ifc_labels=InformationFlowLabels(),
    )

    from mimir.tools.forge import resolve_review_state_for_context

    state = resolve_review_state_for_context(context, "owner/repo", 1291)

    assert state.action_scope.provenance == "server_discovered"
    assert state.action_scope.event_type == "pr_review"
    for tool_name, action in (
        ("repo_commit", RepoPRAction.COMMIT),
        ("repo_push", RepoPRAction.PUSH),
    ):
        assert action.value in state.action_scope.allowed_operations
        decision = access_control.ToolRegistry().authorize_tool(
            tool_name, context, enforce=True,
            arguments={"repository": "owner/repo", "pull_request": 1291},
        )
        assert decision.allowed is True
        assert decision.reason is None
    assert [call[0] for call in client.calls] == ["snapshot", "reviews"]


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
        access_control, "_canonical_repo_binding_resolution",
        lambda repo: access_control.RepoBindingResolution(
            ("/server/configured/repo", "git@github.com:owner/repo.git"),
            ("/server/configured/repo",), 1,
        ),
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


@pytest.mark.parametrize(
    ("forge_tool", "arguments", "missing_field"),
    [
        (
            pr_submit_review,
            {"repository": "owner/repo", "pull_request": 7, "verdict": "approve"},
            "body",
        ),
        (repo_status, {"repository": "owner/repo"}, "pull_request"),
    ],
)
@pytest.mark.parametrize("is_async", [False, True])
@pytest.mark.asyncio
async def test_schema_invalid_standing_review_call_is_recoverable(
    forge_tool,
    arguments: dict[str, object],
    missing_field: str,
    is_async: bool,
) -> None:
    context = AuthContext(
        principal="operator", canonical_principal="operator", roles=("admin",),
        event_ingress=None, trigger="message", channel_id="operator", interactivity=None,
        enforcement_enabled=True, ifc_labels=InformationFlowLabels(),
    )
    request = ToolCallRequest(
        tool_call={
            "name": forge_tool.name, "args": arguments,
            "id": "invalid-standing-review", "type": "tool_call",
        },
        tool=forge_tool, state={}, runtime=Runtime(context=context),
    )
    node = ToolNode([forge_tool])
    middleware = BudgetGateMiddleware()

    if is_async:
        async def handler(tool_request):
            return await node._execute_tool_async(tool_request, "tool_calls", {})

        result = await middleware.awrap_tool_call(request, handler)
    else:
        result = middleware.wrap_tool_call(
            request,
            lambda tool_request: node._execute_tool_sync(tool_request, "tool_calls", {}),
        )

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert missing_field in str(result.content)
    assert "field required" in str(result.content).lower()
    assert "fix the error and try again" in str(result.content).lower()


@pytest.mark.parametrize("arguments", [None, [], "not-a-mapping"])
def test_standing_review_resolution_ignores_non_mapping_arguments(arguments) -> None:
    from mimir.tools.budget_gate import _resolve_standing_review

    context = AuthContext(
        principal="operator", canonical_principal="operator", roles=("admin",),
        event_ingress=None, trigger="message", channel_id="operator", interactivity=None,
    )

    assert _resolve_standing_review("pr_metadata", context, arguments) is None


@pytest.mark.parametrize(
    ("match_count", "mode"),
    [(0, "zero roots matched"), (2, "ambiguous: 2 roots matched")],
)
def test_standing_review_distinguishes_repo_binding_failures(
    monkeypatch: pytest.MonkeyPatch,
    match_count: int,
    mode: str,
) -> None:
    client = FakeForge()
    set_forge_client(client)
    monkeypatch.setenv("GITHUB_REPOS", "owner/repo")
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "reviewer")
    monkeypatch.setattr(
        access_control,
        "_canonical_repo_binding_resolution",
        lambda _repo: access_control.RepoBindingResolution(
            None, ("/configured/root-a", "/configured/root-b"), match_count,
        ),
    )
    context = AuthContext(
        principal="operator", canonical_principal="operator", roles=("admin",),
        event_ingress=None, trigger="message", channel_id="operator", interactivity=None,
    )
    runtime = ToolRuntime(
        state={}, context=context, config={}, stream_writer=lambda _: None,
        tool_call_id="binding-refusal", store=None,
    )

    with pytest.raises(ToolException) as refused:
        pr_metadata.func(
            repository="owner/repo", pull_request=1300, runtime=runtime,
        )

    reason = str(refused.value)
    assert "no unique writable root matched repository 'owner/repo'" in reason
    assert mode in reason
    assert "MIMIR_FILE_TOOL_ROOTS" in reason
    assert "checkout directory itself, not its parent" in reason
    assert "/configured/" not in reason


@pytest.mark.parametrize(
    "snapshot",
    [
        NormalizedPullRequestSnapshot(
            state="closed", number=1300, author="author",
            head_repo="contributor/repo", head_remote="source",
            head_ref="change", head_sha="a" * 40,
            base_ref="main", base_sha="b" * 40,
        ),
        NormalizedPullRequestSnapshot(
            state="open", number=1300, author="author",
            head_repo="contributor/repo", head_remote="source",
            head_ref="invalid..branch", head_sha="a" * 40,
            base_ref="main", base_sha="b" * 40,
        ),
    ],
    ids=["closed", "field-predicate"],
)
def test_pr_state_failures_keep_existing_reason(
    monkeypatch: pytest.MonkeyPatch,
    snapshot: NormalizedPullRequestSnapshot,
) -> None:
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "reviewer")
    monkeypatch.setattr(
        access_control,
        "_canonical_repo_binding_resolution",
        lambda _repo: pytest.fail("PR-state failure reached repository binding"),
    )

    resolution = access_control.resolve_server_discovered_review_scope(
        "owner/repo", snapshot,
    )

    assert resolution.scope is None
    assert resolution.refusal_reason == (
        "pull-request operation rejected: live pull request is closed or invalid"
    )


@pytest.mark.asyncio
async def test_pr_refusal_event_identifies_repository_and_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeForge()
    set_forge_client(client)
    monkeypatch.setenv("GITHUB_REPOS", "owner/repo")
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "reviewer")
    monkeypatch.setattr(
        access_control,
        "_canonical_repo_binding_resolution",
        lambda _repo: access_control.RepoBindingResolution(None, ("/configured",), 0),
    )
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "mimir.tools.budget_gate._emit_event_sync",
        lambda kind, **fields: events.append((kind, fields)),
    )
    context = AuthContext(
        principal="operator", canonical_principal="operator", roles=("admin",),
        event_ingress=None, trigger="message", channel_id="operator",
        interactivity=None, enforcement_enabled=True,
        ifc_labels=InformationFlowLabels(),
    )
    request = ToolCallRequest(
        tool_call={
            "name": "pr_reviews",
            "args": {"repository": "owner/repo", "pull_request": 1300},
            "id": "binding-refusal", "type": "tool_call",
        },
        tool=None, state=None, runtime=Runtime(context=context),
    )

    async def handler(_request):
        pytest.fail("refused request reached handler")

    result = await BudgetGateMiddleware().awrap_tool_call(request, handler)

    assert result.status == "error"
    tool_call = next(fields for kind, fields in events if kind == "tool_call")
    assert tool_call["repository"] == "owner/repo"
    assert tool_call["pull_request"] == 1300
    assert tool_call["denied"] is True


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


def test_issue_comment_posts_to_a_server_resolved_configured_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeForge()
    set_forge_client(client)
    monkeypatch.setenv("GITHUB_REPOS", "owner/repo")

    result = issue_comment.func(repository="owner/repo", issue=220, body="analysis")

    assert result["body"] == "analysis"
    assert client.calls == [
        ("issue_target", "owner/repo", 220),
        ("issue_comment", "owner/repo", 220, "analysis"),
    ]


@pytest.mark.parametrize("issue", [0, -1, True])
def test_issue_comment_refuses_invalid_issue_numbers_before_adapter_call(
    issue: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeForge()
    set_forge_client(client)
    monkeypatch.setenv("GITHUB_REPOS", "owner/repo")

    with pytest.raises(ToolException, match="positive integer"):
        issue_comment.func(repository="owner/repo", issue=issue, body="analysis")

    assert client.calls == []


def test_issue_comment_registration_and_capability_preflights() -> None:
    assert "issue_comment" in {forge_tool.name for forge_tool in FORGE_TOOLS}
    assert access_control._SINK_CATEGORY_MAP["issue_comment"] is access_control.SinkCategory.FORGE
    assert access_control._TOOL_FLOW_MAP["issue_comment"] is access_control.ToolFlowDirection.SINK
    assert access_control.TRIGGER_CAPABILITY_TIERS["issue_comment"] is (
        access_control.CapabilityTier.SCOPED_WITH_PROVENANCE
    )
    assert access_control._OPERATION_SINK_DESTINATION["issue_comment"] == (
        "configured_repository_issue"
    )
    assert "issue_comment" in access_control.TRIGGER_AUTHORITY_PROFILES["github"]
    assert all(
        "issue_comment" not in capabilities
        for profile, capabilities in access_control.TRIGGER_AUTHORITY_PROFILES.items()
        if profile != "github"
    )
    assert "issue_comment" not in access_control._TYPED_REPO_PR_TOOL_ACTIONS
    assert access_control.get_operation_catalog().get_decision("issue_comment") is (
        access_control.OperationDecision.ADMIN_REQUIRED
    )
    access_control.assert_capability_matrix_complete()
    access_control.assert_model_tool_inventory_cataloged()
