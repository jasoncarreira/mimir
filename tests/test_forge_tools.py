from __future__ import annotations

import json

import pytest
from langchain.tools import ToolRuntime
from langchain_core.tools import ToolException
from langgraph.prebuilt import ToolNode

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
    AuthContext, RepoPRAction, RepoPRActionScope, RepoPRScopeRegistry,
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
        (unsupported_operation, {"operation": "pr.resolve_thread"}),
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


def test_batched_turn_resolves_each_exact_scope_and_refuses_unlisted_pr() -> None:
    client = FakeForge()
    set_forge_client(client)
    first = _scope(RepoPRAction.INSPECT)
    second = _scope(RepoPRAction.INSPECT, number=18, head_sha="c" * 40)
    runtime = _runtime_for_scopes(first, second)

    pr_metadata.func(repository="OWNER/REPO", pull_request=17, runtime=runtime)
    pr_metadata.func(repository="owner/repo", pull_request=18, runtime=runtime)
    with pytest.raises(ToolException, match="not authorized for this turn") as denied:
        pr_metadata.func(repository="owner/repo", pull_request=19, runtime=runtime)

    assert [call[1] for call in client.calls] == [first, second]
    assert "owner/repo" not in str(denied.value)
    assert "19" not in str(denied.value)


def test_forge_refusal_distinguishes_turn_with_no_authorized_prs() -> None:
    context = AuthContext(
        principal=None, canonical_principal=None, roles=(), event_ingress=None,
        trigger="poller", channel_id="poller:forge", interactivity=None,
    )
    runtime = ToolRuntime(
        state={}, context=context, config={}, stream_writer=lambda _: None,
        tool_call_id="forge-no-scope", store=None,
    )

    with pytest.raises(ToolException, match="this turn carries no authorized pull requests"):
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
    with pytest.raises(ToolException, match="pr.review not granted"):
        pr_submit_review.func(
            repository="owner/repo", pull_request=17,
            verdict=ReviewVerdict.APPROVE, body="No", runtime=remediation,
        )
    with pytest.raises(ToolException, match="pr.comment not granted"):
        pr_comment.func(
            repository="owner/repo", pull_request=17, body="No", runtime=review,
        )


def test_review_request_scope_grants_read_and_review_only(
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
        **fields, requested_reviewer="reviewer",
    )

    assert scope.allowed_operations == frozenset({"repo.inspect", "pr.review"})
    assert access_control._repo_pr_scope(
        **fields, requested_reviewer="attacker",
    ) is None


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
        operation="pr.resolve_thread", runtime=runtime,
    )
    second = unsupported_operation.func(
        repository="owner/repo", pull_request=17,
        operation="pr.resolve_thread", runtime=runtime,
    )

    assert first["status"] == "unsupported_operation"
    assert first["escalated"] is True
    assert second["escalated"] is False
    records = [json.loads(line) for line in events.read_text().splitlines()]
    assert len(records) == 1
    assert records[0] | {
        "repository": "owner/repo",
        "pull_request": 17,
        "operation": "pr.resolve_thread",
        "operator_visible": True,
    } == records[0]
