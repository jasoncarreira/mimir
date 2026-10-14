"""Forge compatibility surface until the host's coding registration is retired."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from langchain.tools import ToolRuntime

from mimir import access_control
from mimir.forge import (
    CheckProjection, CommentProjection, FileProjection, IssueTarget,
    PullRequestProjection, PullRequestSummary, ReviewProjection,
    ReviewRequestProjection, ReviewVerdict,
)
from mimir.models import (
    AuthContext, InformationFlowLabels, RepoPRAction, RepoPRActionScope,
    RepoPRScopeRegistry, RepoReviewState,
)
from mimir.tools import forge as legacy
from mimir.tools.refusals import ToolPolicyRefusal
from mimir_cultivate import forge


def _runtime() -> ToolRuntime[AuthContext]:
    scope = RepoPRActionScope(
        provenance="poller_payload", canonical_repo="owner/repo",
        canonical_root="/tmp/repo", canonical_origin="ssh://forge.invalid/owner/repo",
        principal="reviewer", event_type="pr_review_requested",
        allowed_operations=frozenset(action.value for action in RepoPRAction),
        pr_number=17, head_repo="contributor/repo", head_remote="source",
        destination_ref="refs/heads/change", observed_head_sha="a" * 40,
        base_ref="main", observed_base_sha="b" * 40,
    )
    state = RepoReviewState(scope)
    context = AuthContext(
        principal="service:poller", canonical_principal="poller", roles=("service",),
        event_ingress=None, trigger="poller", channel_id="poller:forge",
        interactivity=None, enforcement_enabled=True,
        ifc_labels=InformationFlowLabels(),
        repo_pr_scope_registry=RepoPRScopeRegistry((state,)),
        repo_review_state=state, repo_pr_action_scope=scope,
        ci_run_targets=frozenset({("owner/repo", 42)}),
        ci_branch_targets=frozenset({("owner/repo", "main", 7)}),
    )
    return ToolRuntime(
        state={}, context=context, config={}, stream_writer=lambda _: None,
        tool_call_id="cultivate-forge-test", store=None,
    )


class _Forge:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def get_pull_request(self, scope: RepoPRActionScope) -> PullRequestProjection:
        self.calls.append(("metadata", scope))
        return PullRequestProjection(
            17, "Title", "open", "author", False, "main", "change",
            "a" * 40, True, "created", "updated",
        )

    def author_is_trusted(self, repository: str, author: str) -> bool:
        self.calls.append(("attest", (repository, author)))
        return False

    def add_pull_request_comment(self, scope: RepoPRActionScope, body: str):
        self.calls.append(("comment", (scope, body)))
        return CommentProjection("1", "author", body, "now", "now")

    def list_pull_requests(self, repository, **filters):
        self.calls.append(("list", repository))
        return (PullRequestSummary(
            17, "Title", "open", "author", "change", "main", "a" * 40,
            "created", None, "https://github.com/owner/repo/pull/17",
        ),)

    def list_files(self, scope):
        self.calls.append(("files", scope))
        return (FileProjection("src/app.py", "modified", 2, 1, 3, "@@ patch"),)

    def get_diff(self, scope):
        self.calls.append(("diff", scope))
        return "diff --git a/src/app.py b/src/app.py"

    def get_file_content(self, scope, path):
        self.calls.append(("file_content", (scope, path)))
        return "file at scoped head"

    def list_checks(self, scope):
        self.calls.append(("checks", scope))
        return (CheckProjection("test", "completed", "success", "now", "now"),)

    def get_job_log(self, scope, job_id, run_id):
        self.calls.append(("job_log", (scope, job_id, run_id)))
        return "redacted excerpt"

    def get_run(self, repository, run_id):
        self.calls.append(("run", (repository, run_id)))
        return {"id": run_id, "name": "CI"}

    def list_run_jobs(self, repository, run_id):
        self.calls.append(("jobs", (repository, run_id)))
        return [{"id": 1, "name": "test"}]

    def list_runs(self, repository, branch, workflow_id, limit):
        self.calls.append(("runs", (repository, branch, workflow_id, limit)))
        return [{"id": 42, "name": "CI", "head_branch": branch, "workflow_id": workflow_id}]

    def list_reviews(self, scope):
        self.calls.append(("reviews", scope))
        return (ReviewProjection("1", "author", "COMMENTED", "review text", "now", "a" * 40),)

    def list_comments(self, scope):
        self.calls.append(("comments", scope))
        return (CommentProjection("1", "author", "comment text", "now", "now"),)

    def list_review_requests(self, scope):
        self.calls.append(("requests", scope))
        return (ReviewRequestProjection("reviewer", "user"),)

    def submit_review(self, scope, verdict, body):
        self.calls.append(("review", (scope, verdict, body)))
        return ReviewProjection("2", "author", verdict.value, body, "now", "a" * 40)

    def add_inline_review_comment(self, scope, *, path, line, body):
        self.calls.append(("inline", (scope, path, line, body)))
        return CommentProjection("2", "author", body, "now", "now", path, line)

    def edit_pull_request_body(self, scope, body):
        self.calls.append(("edit", (scope, body)))

    def get_open_issue_target(self, repository, issue):
        self.calls.append(("issue_target", (repository, issue)))
        return IssueTarget(repository, issue)

    def add_issue_comment(self, repository, issue, body):
        self.calls.append(("issue_comment", (repository, issue, body)))
        return CommentProjection("2", "author", body, "now", "now")

    def rerequest_review(self, scope, reviewer):
        self.calls.append(("rerequest", (scope, reviewer)))


def test_forge_surface_matches_all_legacy_call_contracts() -> None:
    assert len(forge.FORGE_TOOLS) == 21
    assert [item.name for item in forge.FORGE_TOOLS] == [item.name for item in legacy.FORGE_TOOLS]
    for wrapped, original in zip(forge.FORGE_TOOLS, legacy.FORGE_TOOLS):
        assert wrapped is not original
        assert wrapped.func is not original.func
        assert wrapped.args_schema is original.args_schema
        assert wrapped.response_format == original.response_format
        assert wrapped.return_direct == original.return_direct
        assert callable(wrapped.coroutine)


@pytest.mark.parametrize("name,arguments,operation,expected", [
    ("pr_list", {"repository": "owner/repo"}, "list", "withheld"),
    ("pr_metadata", {"pull_request": 17}, "metadata", "Title"),
    ("pr_spec", {"pull_request": 17}, None, "no_bound_spec"),
    ("pr_files", {"pull_request": 17}, "files", "src/app.py"),
    ("pr_diff", {"pull_request": 17}, "diff", "diff --git"),
    ("pr_file_content", {"pull_request": 17, "path": "src/app.py"}, "file_content", "file at scoped head"),
    ("pr_checks", {"pull_request": 17}, "checks", "success"),
    ("pr_job_log", {"pull_request": 17, "job_id": 12}, "job_log", "redacted excerpt"),
    ("ci_run_jobs", {"run_id": 42}, "jobs", "test"),
    ("ci_run", {"run_id": 42}, "run", "CI"),
    ("ci_recent_runs", {"branch": "main", "workflow_id": 7}, "runs", "42"),
    ("pr_reviews", {"pull_request": 17}, "reviews", "withheld"),
    ("pr_comments", {"pull_request": 17}, "comments", "withheld"),
    ("pr_review_requests", {"pull_request": 17}, "requests", "reviewer"),
    ("pr_submit_review", {"pull_request": 17, "verdict": ReviewVerdict.COMMENT, "body": "posted"}, "review", "posted"),
    ("pr_inline_review_comment", {"pull_request": 17, "path": "src/app.py", "line": 2, "body": "posted"}, "inline", "posted"),
    ("pr_comment", {"pull_request": 17, "body": "posted"}, "comment", "posted"),
    ("pr_edit_body", {"pull_request": 17, "body": "posted"}, "edit", "body_updated"),
    ("issue_comment", {"issue": 23, "body": "posted"}, "issue_comment", "posted"),
    ("pr_rerequest_review", {"pull_request": 17, "reviewer": "reviewer"}, "rerequest", "review_rerequested"),
    ("unsupported_operation", {"pull_request": 17, "description": "unavailable"}, None, "MIMIR_HOME"),
])
def test_every_plugin_forge_tool_executes_scoped_behavior(
    monkeypatch, name, arguments, operation, expected,
) -> None:
    monkeypatch.setenv("GITHUB_REPOS", "owner/repo")
    monkeypatch.delenv("MIMIR_HOME", raising=False)
    client = _Forge()
    runtime = _runtime()
    legacy.set_forge_client(client)
    tool = next(item for item in forge.FORGE_TOOLS if item.name == name)
    inputs = {"repository": "owner/repo", **arguments, "runtime": runtime}
    try:
        token = access_control.begin_protected_result_capture()
        try:
            if name == "unsupported_operation":
                with pytest.raises(ToolPolicyRefusal, match=expected):
                    tool.func(**inputs)
            else:
                result = tool.func(**inputs)
                assert expected in str(result)
        finally:
            provenance = access_control.end_protected_result_capture(token)
        if name in {"pr_metadata", "pr_files", "pr_diff", "pr_file_content", "pr_checks",
                    "pr_job_log", "pr_review_requests", "pr_comment", "pr_edit_body",
                    "pr_inline_review_comment", "pr_submit_review"}:
            assert provenance.sources[0].resource_id == "owner/repo#pull/17@" + "a" * 40
            assert provenance.sources[0].integrity == "untrusted"
        if operation is not None:
            assert operation in [call[0] for call in client.calls]
        if "pull_request" in arguments and name not in {"pr_spec", "unsupported_operation"}:
            assert any(
                call[1] is runtime.context.repo_pr_action_scope
                or isinstance(call[1], tuple) and runtime.context.repo_pr_action_scope in call[1]
                for call in client.calls if call[0] == operation
            )
        before = list(client.calls)
        if "pull_request" in arguments:
            with pytest.raises(ToolPolicyRefusal, match="outside this turn's scope"):
                tool.func(**{**inputs, "pull_request": 18})
        elif name == "issue_comment":
            with pytest.raises(ToolPolicyRefusal, match="positive integer"):
                tool.func(**{**inputs, "issue": 0})
        elif name == "pr_list":
            with pytest.raises(ToolPolicyRefusal, match="not configured"):
                tool.func(**{**inputs, "repository": "other/repo"})
        else:
            with pytest.raises(ToolPolicyRefusal, match="outside this turn's poller scope"):
                tool.func(**{**inputs, "run_id": 43} if name != "ci_recent_runs" else
                          {**inputs, "branch": "other"})
        assert client.calls == before
    finally:
        legacy.set_forge_client(None)


@pytest.mark.asyncio
async def test_plugin_call_site_preserves_scope_head_and_untrusted_taint() -> None:
    client = _Forge()
    legacy.set_forge_client(client)
    runtime = _runtime()
    metadata = next(tool for tool in forge.FORGE_TOOLS if tool.name == "pr_metadata")
    comment = next(tool for tool in forge.FORGE_TOOLS if tool.name == "pr_comment")
    token = access_control.begin_protected_result_capture()
    try:
        result = await metadata.coroutine("owner/repo", 17, runtime=runtime)
    finally:
        provenance = access_control.end_protected_result_capture(token)
    assert result["title"] == "Title"
    assert client.calls == [
        ("metadata", runtime.context.repo_pr_action_scope),
        ("attest", ("owner/repo", "author")),
    ]
    assert provenance.sources[0].resource_id == "owner/repo#pull/17@" + "a" * 40
    assert provenance.sources[0].integrity == "untrusted"
    try:
        assert comment.func("owner/repo", 17, "posted", runtime=runtime)["body"] == "posted"
        with pytest.raises(ToolPolicyRefusal, match="not configured"):
            comment.func("other/repo", 18, "no", runtime=runtime)
        assert [call for call in client.calls if call[0] == "comment"] == [
            ("comment", (runtime.context.repo_pr_action_scope, "posted")),
        ]
    finally:
        legacy.set_forge_client(None)


def test_disabled_provider_is_not_imported_by_host() -> None:
    program = (
        "import json, sys; from mimir.plugins.manager import load_plugins; "
        "manager = load_plugins(enable=sys.argv[1]); "
        "print(json.dumps([p.candidate.name for p in manager.plugins])); "
        "print('mimir_cultivate' in sys.modules)"
    )
    for selection, names, imported in (("", [], False), ("cultivate", ["cultivate"], True)):
        env = dict(os.environ, MIMIR_PLUGINS=selection)
        result = subprocess.run(
            [sys.executable, "-c", program, selection], env=env,
            capture_output=True, text=True, check=True,
        )
        actual_names, actual_imported = result.stdout.strip().splitlines()
        assert json.loads(actual_names) == names
        assert (actual_imported == "True") is imported
