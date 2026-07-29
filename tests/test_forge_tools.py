from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from langchain_core.tools import ToolException

import mimir.access_control as access_control
from mimir.event_logger import _reset_logger_for_tests, init_logger
from mimir.forge import (
    CommentProjection,
    PullRequestProjection,
    ReviewProjection,
    ReviewVerdict,
)
from mimir.models import AuthContext, RepoPRAction, RepoPRActionScope
from mimir.tools.forge import (
    FORGE_TOOLS,
    pr_comment,
    pr_inline_review_comment,
    pr_metadata,
    pr_submit_review,
    set_forge_client,
    unsupported_operation,
)


def _scope(*actions: RepoPRAction) -> RepoPRActionScope:
    return RepoPRActionScope(
        provenance="poller_payload",
        canonical_repo="owner/repo",
        canonical_root="/tmp/repo",
        canonical_origin="ssh://forge.invalid/owner/repo",
        principal="reviewer",
        event_type="pr_review_requested",
        allowed_operations=frozenset(action.value for action in actions),
        pr_number=17,
        head_repo="contributor/repo",
        head_remote="source",
        destination_ref="refs/heads/change",
        observed_head_sha="a" * 40,
        base_ref="main",
        observed_base_sha="b" * 40,
    )


def _runtime(scope: RepoPRActionScope) -> SimpleNamespace:
    context = AuthContext(
        principal="service:poller",
        canonical_principal="poller",
        roles=("service",),
        event_ingress=None,
        trigger="poller",
        channel_id="poller:forge",
        interactivity=None,
        enforcement_enabled=True,
        repo_pr_action_scope=scope,
    )
    return SimpleNamespace(context=context)


class FakeForge:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def get_pull_request(self, scope):
        self.calls.append(("metadata", scope))
        return PullRequestProjection(
            17, "Title", "open", "author", False, "main", "change",
            "a" * 40, True, "created", "updated",
        )

    def submit_review(self, scope, verdict, body):
        self.calls.append(("review", scope, verdict, body))
        return ReviewProjection("1", "reviewer", verdict.value, body, "now", "a" * 40)

    def add_inline_review_comment(self, scope, *, path, line, body):
        self.calls.append(("inline", scope, path, line, body))
        return CommentProjection("1", "reviewer", body, "now", "now", path, line)

    def add_pull_request_comment(self, scope, body):
        self.calls.append(("comment", scope, body))
        return CommentProjection("1", "reviewer", body, "now", "now")


@pytest.fixture(autouse=True)
def _reset_client() -> None:
    set_forge_client(None)
    yield
    set_forge_client(None)
    _reset_logger_for_tests()


def test_tool_surface_has_no_repository_pr_or_request_target_selectors() -> None:
    for forge_tool in FORGE_TOOLS:
        properties = forge_tool.tool_call_schema.model_json_schema()["properties"]
        assert not (
            {"repository", "repo", "pull_request", "pr_number", "url", "host"}
            & set(properties)
        )
        assert "runtime" not in properties


def test_read_uses_only_immutable_scope_target() -> None:
    client = FakeForge()
    set_forge_client(client)
    scope = _scope(RepoPRAction.INSPECT)

    result = pr_metadata.func(runtime=_runtime(scope))

    assert result["number"] == 17
    assert client.calls == [("metadata", scope)]


def test_review_and_remediation_actions_do_not_widen_each_other() -> None:
    client = FakeForge()
    set_forge_client(client)
    review = _runtime(_scope(RepoPRAction.INSPECT, RepoPRAction.PR_REVIEW))
    remediation = _runtime(
        _scope(RepoPRAction.INSPECT, RepoPRAction.PR_COMMENT, RepoPRAction.PR_REREQUEST)
    )

    pr_submit_review.func(
        verdict=ReviewVerdict.APPROVE, body="Looks good", runtime=review,
    )
    pr_comment.func(body="Fixed", runtime=remediation)
    with pytest.raises(ToolException, match="pr.review not granted"):
        pr_submit_review.func(
            verdict=ReviewVerdict.APPROVE, body="No", runtime=remediation,
        )
    with pytest.raises(ToolException, match="pr.comment not granted"):
        pr_comment.func(body="No", runtime=review)


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
        pr_comment.func(body="x" * 65_537, runtime=runtime)
    with pytest.raises(ToolException, match="relative repository path"):
        pr_inline_review_comment.func(
            path="../../secret", line=1, body="x", runtime=runtime,
        )
    with pytest.raises(ToolException, match="null byte"):
        pr_comment.func(body="hello\x00world", runtime=runtime)
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

    first = unsupported_operation.func(operation="pr.resolve_thread", runtime=runtime)
    second = unsupported_operation.func(operation="pr.resolve_thread", runtime=runtime)

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
