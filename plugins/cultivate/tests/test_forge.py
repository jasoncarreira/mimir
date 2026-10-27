"""Cultivate's independently owned, scoped forge tool surface."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest
import requests
from langchain.tools import ToolRuntime

from mimir import access_control
from mimir_cultivate.forge import (
    CheckProjection, CommentProjection, FileProjection, IssueTarget,
    PullRequestProjection, PullRequestSummary, ReviewProjection,
    ReviewRequestProjection, ReviewVerdict,
)
from mimir.models import AuthContext, InformationFlowLabels
from mimir_cultivate.policy import (
    RepoPRAction, RepoPRActionScope, RepoPRScopeRegistry, RepoReviewState,
)
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


def test_forge_surface_has_all_scoped_tools() -> None:
    assert [item.name for item in forge.FORGE_TOOLS] == [
        "pr_list", "pr_metadata", "pr_spec", "pr_files", "pr_diff",
        "pr_file_content", "pr_checks", "pr_job_log", "ci_run_jobs", "ci_run",
        "ci_recent_runs", "pr_reviews", "pr_comments", "pr_review_requests",
        "pr_submit_review", "pr_inline_review_comment", "pr_comment",
        "pr_edit_body", "issue_comment", "pr_rerequest_review",
        "unsupported_operation",
    ]
    assert all(callable(item.func) and callable(item.coroutine) for item in forge.FORGE_TOOLS)


def test_provider_forge_has_no_core_repository_fallback() -> None:
    tree = ast.parse(Path(forge.__file__).read_text(encoding="utf-8"))
    imports = (
        node.module for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )
    assert "mimir.repo_tools" not in imports


def test_provider_refuses_accidental_pr_body_shrink(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_REPOS", "owner/repo")
    client = _Forge()
    original = client.get_pull_request

    def current(scope):
        return replace(original(scope), body="Existing description and Closes #1949" * 4)

    client.get_pull_request = current
    forge.set_forge_client(client)
    try:
        args = ("owner/repo", 17, "replacement")
        with pytest.raises(ToolPolicyRefusal, match="under 50%"):
            forge.pr_edit_body.func(*args, runtime=_runtime())
        assert not any(name == "edit" for name, _ in client.calls)
        assert forge.pr_edit_body.func(*args, allow_shrink=True, runtime=_runtime()) == {
            "status": "body_updated",
        }
        assert any(name == "edit" for name, _ in client.calls)
    finally:
        forge.set_forge_client(None)


def test_provider_pr_metadata_bounds_body_and_preserves_merged_at(monkeypatch) -> None:
    class Session:
        def request(self, *args, **kwargs):
            payload = {
                "number": 17, "merged_at": "2026-10-19T12:00:00Z",
                "body": "é" * 60_000,
            }
            return SimpleNamespace(
                status_code=200, headers={"Content-Type": "application/json"},
                iter_content=lambda **_: (json.dumps(payload).encode("utf-8"),),
                close=lambda: None,
            )

    scope = _runtime().context.repo_pr_action_scope
    monkeypatch.setattr(forge.GitHubForgeClient, "_target", lambda self, _: ("owner/repo", 17))
    result = forge.GitHubForgeClient(token="token", session=Session()).get_pull_request(scope)
    assert result.merged_at == "2026-10-19T12:00:00Z"
    assert len(result.body.encode("utf-8")) <= forge._MAX_BODY_BYTES
    assert result.body and result.body.endswith("é")


def test_non_pr_number_refusal_names_issue_remedy() -> None:
    class Session:
        def request(self, *args, **kwargs):
            return SimpleNamespace(
                status_code=404, iter_content=lambda **_: (b'{"message":"Not Found"}',),
                close=lambda: None,
            )

    client = forge.GitHubForgeClient(token="token", session=Session())
    with pytest.raises(forge.ForgeError) as error:
        client._request("GET", "/repos/owner/repo/pulls/358")
    assert "issue, not a PR" in str(error.value)
    assert "issues aren't handled on service turns yet" in str(error.value)


@pytest.mark.parametrize("url", [
    "https://github.com/other/trellis/actions/runs/40411654556/job/1",
    "https://github.com/SquashMerge/other/actions/runs/40411654556/job/1",
    "https://github.com.evil/SquashMerge/trellis/actions/runs/1",
    "https://api.github.com/SquashMerge/trellis/actions/runs/1",
    "http://github.com/SquashMerge/trellis/actions/runs/1",
    " HTTPS://github.com/SquashMerge/trellis/actions/runs/1",
    "https://x@github.com/SquashMerge/trellis/actions/runs/1",
    "https://github.com/SquashMerge/trellis/../other/actions/runs/1",
    "https://github.com/SquashMerge/trellis/%2e%2e/other/actions/runs/1",
    "https://github.com/SquashMerge/trellis/%252e%252e/other/actions/runs/1",
    "https://github.com/SquashMerge/trellis/%2e%2e%2fother/actions/runs/1",
    "https://github.com/SquashMerge/trellis.evil/actions/runs/1",
])
def test_check_url_outside_exact_repo_is_unsafe(url) -> None:
    scope = replace(_runtime().context.repo_pr_action_scope,
                    canonical_repo="squashmerge/trellis")
    check = CheckProjection("test (ubuntu-latest)", "completed", "success", None, None, url)
    assert forge._safe_check_projection(check, scope, 0)["details_url"] is None


def test_check_url_preserves_path_case_and_allows_repo_case() -> None:
    scope = replace(_runtime().context.repo_pr_action_scope,
                    canonical_repo="squashmerge/trellis")
    url = "https://github.com/SquashMerge/trellis/actions/runs/40411654556/job/123"
    check = CheckProjection("test (macos-latest)", "completed", "success", None, None, url)
    assert forge._safe_check_projection(check, scope, 0)["details_url"] == url
    assert check.details_url == url


def test_github_origin_repo_comparison_ignores_slug_case() -> None:
    scope = replace(_runtime().context.repo_pr_action_scope,
                    canonical_repo="squashmerge/trellis",
                    canonical_origin="https://github.com/SquashMerge/trellis.git")
    assert forge._github_origin_matches(scope)
    assert not forge._github_origin_matches(replace(
        scope, canonical_origin="https://github.com/SquashMerge/other.git",
    ))


@pytest.mark.parametrize("url", [
    "https://github.com/other/repo/actions/runs/1",
    "https://github.com.evil/owner/repo/actions/runs/1",
    "https://api.github.com/owner/repo/actions/runs/1",
    "http://github.com/owner/repo/actions/runs/1",
    "https://x@github.com/owner/repo/actions/runs/1",
    "https://github.com/owner/repo/../other/actions/runs/1",
    "https://github.com/owner/repo/%2e%2e/other/actions/runs/1",
])
def test_pr_checks_unsafe_url_is_withheld_without_downgrading_attestation(url, monkeypatch) -> None:
    import mimir.event_logger as event_logger

    events = []
    monkeypatch.setattr(event_logger, "log_event_sync", lambda *a, **kw: events.append((a, kw)))
    runtime = _runtime()
    client = _Forge()
    client.author_is_trusted = lambda *_: True
    client.list_checks = lambda _: (
        CheckProjection("test", "completed", "success", None, None, url),
    )
    forge.set_forge_client(client)
    try:
        token = access_control.begin_protected_result_capture()
        try:
            result = forge.pr_checks.func("owner/repo", 17, runtime=runtime)
        finally:
            provenance = access_control.end_protected_result_capture(token)
    finally:
        forge.set_forge_client(None)
    assert result[0]["details_url"] is None
    assert url not in str(result)
    assert [source.integrity for source in provenance.sources] == ["trusted"]
    assert events == [(("pr_checks_field_withheld",), {"field": "details_url", "check_index": 0})]


@pytest.mark.parametrize("name", [
    "candidate\nunsafe", "candidate:unsafe", "candidate`unsafe", "candidate<unsafe",
    "a" * 129,
])
def test_pr_checks_withholds_invalid_name_without_taint(name, monkeypatch) -> None:
    import mimir.event_logger as event_logger

    events = []
    monkeypatch.setattr(event_logger, "log_event_sync", lambda *a, **kw: events.append((a, kw)))
    runtime = _runtime()
    client = _Forge()
    client.author_is_trusted = lambda *_: True
    client.list_checks = lambda _: (
        CheckProjection("valid", "completed", "success", None, None),
        CheckProjection(name, "completed", "unexpected conclusion", None, None),
    )
    forge.set_forge_client(client)
    try:
        token = access_control.begin_protected_result_capture()
        try:
            result = forge.pr_checks.func("owner/repo", 17, runtime=runtime)
        finally:
            provenance = access_control.end_protected_result_capture(token)
        # The following write can still proceed after ingesting the checks.
        assert forge.pr_submit_review.func(
            "owner/repo", 17, ReviewVerdict.COMMENT, "reviewed", runtime=runtime,
        )["body"] == "reviewed"
    finally:
        forge.set_forge_client(None)
    assert result[0]["name"] == "valid"
    assert result[1]["name"] == "<withheld-check-name>"
    assert result[1]["conclusion"] == "unknown"
    assert name not in str(result)
    assert "unexpected conclusion" not in str(result)
    assert [source.integrity for source in provenance.sources] == ["trusted"]
    assert events == [
        (("pr_checks_field_withheld",), {"field": "name", "check_index": 1}),
        (("pr_checks_field_withheld",), {"field": "conclusion", "check_index": 1}),
    ]


def test_pr_checks_unknown_status_is_withheld(monkeypatch) -> None:
    import mimir.event_logger as event_logger

    events = []
    monkeypatch.setattr(event_logger, "log_event_sync", lambda *a, **kw: events.append((a, kw)))
    client = _Forge()
    client.author_is_trusted = lambda *_: True
    client.list_checks = lambda _: (CheckProjection("valid", "invalid status", None, None, None),)
    forge.set_forge_client(client)
    try:
        token = access_control.begin_protected_result_capture()
        try:
            result = forge.pr_checks.func("owner/repo", 17, runtime=_runtime())
        finally:
            provenance = access_control.end_protected_result_capture(token)
    finally:
        forge.set_forge_client(None)
    assert result[0]["status"] == "unknown"
    assert "invalid status" not in str(result)
    assert [source.integrity for source in provenance.sources] == ["trusted"]
    assert events == [(("pr_checks_field_withheld",), {"field": "status", "check_index": 0})]


@pytest.mark.parametrize("method,warning", [
    ("GET", False), ("POST", True), ("PATCH", True),
])
def test_provider_transport_warning_distinguishes_ambiguous_writes(method, warning) -> None:
    class FailingSession:
        def request(self, *args, **kwargs):
            raise requests.ConnectionError("private transport detail")

    client = forge.GitHubForgeClient(token="token", session=FailingSession())
    with pytest.raises(forge.ForgeError) as error:
        client._request(method, "/repos/owner/repo/pulls/17")
    assert ("write may have been applied" in str(error.value)) is warning
    assert "private transport detail" not in str(error.value)


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
    forge.set_forge_client(client)
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
        forge.set_forge_client(None)


@pytest.mark.asyncio
async def test_plugin_call_site_preserves_scope_head_and_untrusted_taint() -> None:
    client = _Forge()
    forge.set_forge_client(client)
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
        ("metadata", runtime.context.repo_pr_action_scope),
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
        forge.set_forge_client(None)


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


def test_forge_provider_loads_without_retired_forge_sources() -> None:
    program = """
import importlib.abc
import os
import sys
os.environ.pop('MIMIR_HOME', None)
os.environ['GITHUB_REPOS'] = 'owner/repo'

class RetiredSources(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'mimir.forge' or fullname.startswith('mimir.forge.') or fullname == 'mimir.tools.forge':
            raise ImportError(f'retired source imported: {fullname}')

sys.meta_path.insert(0, RetiredSources())
from mimir_cultivate import forge
assert len(forge.FORGE_TOOLS) == 21
assert forge._issue_bound_to_pr is not None
class Client:
    def list_pull_requests(self, repository, **filters):
        assert repository == 'owner/repo'
        return ()
forge.set_forge_client(Client())
assert forge.pr_list.func('owner/repo') == []
"""
    result = subprocess.run(
        [sys.executable, "-c", program], env=os.environ.copy(), capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_github_identity_binding_and_degraded_latch(monkeypatch) -> None:
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "reviewer")
    monkeypatch.setattr(forge, "_github_identity_degraded", False)
    monkeypatch.setattr(forge, "_github_identity_degraded_error", None)
    monkeypatch.setattr(forge, "_github_identity_degraded_callback", None)
    monkeypatch.setattr(forge, "_default_client", None)
    verified = []

    class IdentityClient:
        def verify_identity(self, login):
            verified.append(login)

    monkeypatch.setattr(forge, "GitHubForgeClient", IdentityClient)
    assert forge.initialize_github_forge_identity()
    assert verified == ["reviewer"]
    assert isinstance(forge._client_for_repository("owner/repo"), IdentityClient)
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "")
    failures = []
    forge.set_github_identity_degraded_callback(failures.append)
    assert not forge.initialize_github_forge_identity()
    assert forge.github_identity_is_degraded()
    assert isinstance(failures[0], forge.GitHubIdentityVerificationError)
    with pytest.raises(ToolPolicyRefusal, match="disabled"):
        forge.confirm_github_tool_identity("reviewer")


def test_armed_spec_is_bound_to_server_evidence_and_scoped_head(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setenv("GITHUB_REPOS", "owner/repo")
    evidence = tmp_path / "state" / "worklink" / "evidence"
    evidence.mkdir(parents=True)
    (evidence / "1949-run.json").write_text(json.dumps({
        "pr_url": "https://github.com/owner/repo/pull/17",
    }), encoding="utf-8")
    payload = {
        "id": 1949, "title": "Build safely", "status": "open",
        "labels": ["worklink:armed"], "description": "Authoritative spec",
    }

    def show_issue(args, *, cwd, stdout, **kwargs):
        assert args[1:] == ["issue", "show", "1949", "--json"]
        assert cwd == str(tmp_path)
        stdout.write(json.dumps(payload).encode())
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(forge.subprocess, "run", show_issue)
    runtime = _runtime()
    tool = forge.pr_spec
    token = access_control.begin_protected_result_capture()
    try:
        assert tool.func("owner/repo", 17, runtime=runtime) == payload
    finally:
        provenance = access_control.end_protected_result_capture(token)
    assert provenance.sources[0].resource_id == "owner/repo#pull/17@" + "a" * 40
    with pytest.raises(ToolPolicyRefusal, match="outside this turn's scope"):
        tool.func("owner/repo", 18, runtime=runtime)
    (evidence / "1949-run.json").write_text(json.dumps({
        "pr_url": "https://github.com/other/repo/pull/17",
    }), encoding="utf-8")
    assert tool.func("owner/repo", 17, runtime=runtime) == {"error": "no_bound_spec"}


def test_unsupported_operation_escalates_once_and_persists(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setenv("GITHUB_REPOS", "owner/repo")
    events = []
    import mimir.event_logger as event_logger

    monkeypatch.setattr(event_logger, "log_durable_event_sync", lambda *a, **kw: events.append((a, kw)))
    runtime = _runtime()
    arguments = {
        "repository": "owner/repo", "pull_request": 17,
        "description": "Need a new operation", "attempted_operations": ["Checked review"],
        "runtime": runtime,
    }
    result = forge.unsupported_operation.func(**arguments)
    assert result["status"] == "unsupported_operation"
    assert result["escalated"] is True
    assert result["attempted_operations"] == ["Checked review"]
    assert events == [(('unsupported_operation',), {
        "repository": "owner/repo", "pull_request": 17,
        "operation": result["operation"], "description": "Need a new operation",
        "attempted_operations": ["Checked review"],
        "scope_id": runtime.context.repo_pr_action_scope.scope_id,
        "operator_visible": True,
    })]
    persisted = tmp_path / "state" / "unsupported_operations.json"
    assert json.loads(persisted.read_text(encoding="utf-8")) == {
        "owner/repo#17": (
            runtime.context.repo_pr_action_scope.observed_head_sha + ":" + result["operation"]
        ),
    }
    assert forge.unsupported_operation.func(**arguments)["escalated"] is False
    assert len(events) == 1
    with pytest.raises(ToolPolicyRefusal, match="outside this turn's scope"):
        forge.unsupported_operation.func(**{**arguments, "pull_request": 18})
    assert len(events) == 1
