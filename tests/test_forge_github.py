from __future__ import annotations

from dataclasses import replace

import pytest
from langchain_core.tools import ToolException
import requests

from mimir.forge import ForgeError, ForgeResponseTooLarge, ReviewVerdict
from mimir.forge.github import (
    GitHubForgeClient,
    GitHubIdentityFailureKind,
    GitHubIdentityVerificationError,
    bound_diff,
)
from mimir.forge import github as github_module
from mimir.models import RepoPRActionScope
from mimir.tools.forge import initialize_github_forge_identity


def _scope() -> RepoPRActionScope:
    return RepoPRActionScope(
        provenance="poller_payload",
        canonical_repo="owner/repo",
        canonical_root="/tmp/repo",
        canonical_origin="ssh://forge.invalid/owner/repo",
        principal="reviewer",
        event_type="pr_review_requested",
        allowed_operations=frozenset({"repo.inspect", "pr.review"}),
        pr_number=17,
        head_repo="fork/repo",
        head_remote="source",
        destination_ref="refs/heads/change",
        observed_head_sha="a" * 40,
        base_ref="main",
        observed_base_sha="b" * 40,
    )


class Response:
    def __init__(self, payload, *, status=200, content_type="application/json") -> None:
        import json

        self._payload = payload
        self.status_code = status
        self.headers = {"Content-Type": content_type}
        self.content = (
            payload.encode() if isinstance(payload, str)
            else json.dumps(payload).encode()
        )

    def json(self):
        return self._payload


class Session:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def reset_verified_identity(monkeypatch) -> None:
    from mimir.tools import forge as forge_tools

    monkeypatch.setattr(github_module, "_verified_identity", None)
    monkeypatch.setattr(forge_tools, "_github_identity_degraded", False)
    monkeypatch.setattr(forge_tools, "_github_identity_degraded_error", None)
    monkeypatch.setattr(forge_tools, "_github_identity_degraded_callback", None)


def test_metadata_target_and_auth_are_adapter_constructed() -> None:
    session = Session([Response({
        "number": 17,
        "title": "Title",
        "state": "open",
        "user": {"login": "author"},
        "base": {"ref": "main"},
        "head": {"ref": "change", "sha": "a" * 40},
        "created_at": "created",
        "updated_at": "updated",
    })])
    client = GitHubForgeClient(token="secret", session=session)

    result = client.get_pull_request(_scope())

    assert result.author == "author"
    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert url == "https://api.github.com/repos/owner/repo/pulls/17"
    assert kwargs["headers"]["Authorization"] == "Bearer secret"


def _job_metadata():
    return {
        "id": 456, "run_id": 123, "head_sha": "a" * 40,
        "run_url": "https://api.github.com/repos/owner/repo/actions/runs/123",
        "status": "completed", "conclusion": "failure",
    }


def _run_metadata():
    return {
        "id": 123, "repository": {"full_name": "owner/repo"},
        "head_sha": "a" * 40, "status": "completed",
    }


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "startup_failure", "action_required"])
@pytest.mark.parametrize("run_id", [None, 123])
@pytest.mark.parametrize("repo_case", ["owner/repo", "Owner/Repo"])
def test_job_log_binds_metadata_before_capture(monkeypatch, conclusion, run_id, repo_case):
    from mimir import ci_logs

    job = {**_job_metadata(), "conclusion": conclusion}
    job["run_url"] = f"https://api.github.com/repos/{repo_case}/actions/runs/123"
    run = {**_run_metadata(), "repository": {"full_name": repo_case}}
    session = Session([Response(job), Response(run)])
    calls = []

    def capture(repo, job_id, **kwargs):
        assert len(session.calls) == 2
        calls.append((repo, job_id, kwargs))
        return b"selected [REDACTED] excerpt", ""

    monkeypatch.setattr(ci_logs, "capture_job_log", capture)
    result = GitHubForgeClient(token="secret", session=session).get_job_log(_scope(), 456, run_id)
    assert result == "selected [REDACTED] excerpt"
    assert calls == [("owner/repo", 456, {
        "token": "secret", "limit": ci_logs.LOG_EXCERPT_BYTES, "timeout": 20.0,
    })]
    assert [call[1] for call in session.calls] == [
        "https://api.github.com/repos/owner/repo/actions/jobs/456",
        "https://api.github.com/repos/owner/repo/actions/runs/123",
    ]


@pytest.mark.parametrize("target,field,value", [
    ("job", "id", 457), ("job", "id", "456"), ("job", "id", 456.0), ("job", "id", None),
    ("job", "run_id", None), ("job", "run_id", True), ("job", "run_id", 0), ("job", "run_id", 123.0),
    ("job", "head_sha", "c" * 40), ("job", "head_sha", None),
    ("job", "run_url", "https://api.github.com/repos/other/repo/actions/runs/123"),
    ("job", "run_url", "https://api.github.com/repos/owner/repo/actions/runs/999"),
    ("job", "run_url", "https://api.github.com/repos/owner/repo/actions/runs/123/extra"),
    ("job", "run_url", "https://evil.example/repos/owner/repo/actions/runs/123"),
    ("job", "run_url", None),
    ("job", "status", "in_progress"), ("job", "status", None),
    *[("job", "conclusion", value) for value in ["success", "cancelled", "neutral", "skipped", "stale", None]],
    ("run", "id", 124), ("run", "id", "123"), ("run", "id", 123.0), ("run", "id", None),
    ("run", "repository", {"full_name": "other/repo"}),
    ("run", "repository", None), ("run", "repository", {}),
    ("run", "head_sha", "c" * 40), ("run", "head_sha", None),
    ("run", "status", "in_progress"), ("run", "status", "queued"), ("run", "status", None),
])
def test_job_log_rejects_each_independent_binding_or_state(monkeypatch, target, field, value):
    from mimir import ci_logs

    job, run = _job_metadata(), _run_metadata()
    (job if target == "job" else run)[field] = value
    monkeypatch.setattr(ci_logs, "capture_job_log", lambda *a, **k: pytest.fail("capture before validation"))
    session = Session([Response(job), Response(run)])
    message = "run is still in progress" if target == "run" and field == "status" else None
    with pytest.raises(ForgeError, match=message):
        GitHubForgeClient(session=session).get_job_log(_scope(), 456)


@pytest.mark.parametrize("job_id,run_id", [
    (None, None),
    (True, None), (0, None), (-1, None), ("456", None), (1.5, None),
    (456, True), (456, 0), (456, -1), (456, "123"), (456, 1.5),
])
def test_job_log_invalid_ids_do_not_fetch(job_id, run_id):
    session = Session([])
    with pytest.raises(ForgeError, match="positive integer"):
        GitHubForgeClient(session=session).get_job_log(_scope(), job_id, run_id)
    assert session.calls == []


@pytest.mark.parametrize("target", ["job", "run"])
@pytest.mark.parametrize("payload", [None, [], "invalid"])
def test_job_log_rejects_non_mapping_metadata(monkeypatch, target, payload):
    from mimir import ci_logs

    monkeypatch.setattr(ci_logs, "capture_job_log", lambda *a, **k: pytest.fail("invalid metadata captured"))
    responses = [Response(payload)] if target == "job" else [Response(_job_metadata()), Response(payload)]
    with pytest.raises(ForgeError, match=f"invalid {target} metadata"):
        GitHubForgeClient(session=Session(responses)).get_job_log(_scope(), 456)


@pytest.mark.parametrize("observed_run", [0, -1, True, 123.0])
def test_job_log_rejects_self_consistent_invalid_run_id(monkeypatch, observed_run):
    from mimir import ci_logs

    job = {**_job_metadata(), "run_id": observed_run,
           "run_url": f"https://api.github.com/repos/owner/repo/actions/runs/{observed_run}"}
    run = {**_run_metadata(), "id": int(observed_run)}
    session = Session([Response(job), Response(run)])
    monkeypatch.setattr(ci_logs, "capture_job_log", lambda *a, **k: pytest.fail("invalid run ID captured"))
    with pytest.raises(ForgeError, match="outside"):
        GitHubForgeClient(session=session).get_job_log(_scope(), 456)
    assert len(session.calls) == 1


def test_job_log_explicit_run_mismatch_does_not_fetch_run():
    session = Session([Response(_job_metadata())])
    with pytest.raises(ForgeError, match="outside"):
        GitHubForgeClient(session=session).get_job_log(_scope(), 456, 999)
    assert len(session.calls) == 1


@pytest.mark.parametrize("status,message", [(404, "job not found"), (401, "authentication failed")])
def test_job_log_metadata_refusals_are_distinct(status, message):
    session = Session([Response({}, status=status)])
    with pytest.raises(ForgeError, match=message):
        GitHubForgeClient(session=session).get_job_log(_scope(), 456)


def test_job_log_capture_error_not_returned_as_evidence(monkeypatch):
    from mimir import ci_logs

    monkeypatch.setattr(ci_logs, "capture_job_log", lambda *a, **k: (b"", "unauthenticated gh"))
    session = Session([Response(_job_metadata()), Response(_run_metadata())])
    with pytest.raises(ForgeError, match="unauthenticated gh"):
        GitHubForgeClient(session=session).get_job_log(_scope(), 456)


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "action_required", "success"])
@pytest.mark.parametrize("url_field", ["details_url", "html_url", None])
def test_checks_preserve_actionable_url(conclusion, url_field) -> None:
    log_url = "https://github.com/owner/repo/actions/runs/123/job/456"
    row = {
        "name": "tests", "status": "completed", "conclusion": conclusion,
        "started_at": "started", "completed_at": "completed",
    }
    if url_field:
        row[url_field] = log_url
    if url_field == "details_url":
        row["html_url"] = "https://github.com/owner/repo/runs/456"
    session = Session([Response({"check_runs": [row]})])

    result, = GitHubForgeClient(session=session).list_checks(_scope())

    assert result.name == "tests"
    assert result.status == "completed"
    assert result.conclusion == conclusion
    assert result.details_url == (log_url if url_field else None)
    assert len(session.calls) == 1
    assert session.calls[0][0] == "GET"
    assert session.calls[0][1] == (
        f"https://api.github.com/repos/owner/repo/commits/{'a' * 40}/check-runs?per_page=100"
    )


def test_live_snapshot_normalizes_all_authority_facts() -> None:
    session = Session([Response({
        "number": 17, "state": "open", "user": {"login": "author"},
        "base": {
            "ref": "main", "sha": "b" * 40,
            "repo": {"full_name": "owner/repo"},
        },
        "head": {
            "ref": "feature", "sha": "a" * 40,
            "repo": {"full_name": "contributor/fork"},
        },
    })])

    snapshot = GitHubForgeClient(session=session).get_pull_request_snapshot(
        "owner/repo", 17,
    )

    assert snapshot.repo == "owner/repo"
    assert snapshot.state == "open"
    assert snapshot.number == 17
    assert snapshot.author == "author"
    assert snapshot.head_repo == "contributor/fork"
    assert snapshot.head_remote == "source"
    assert snapshot.head_ref == "feature"
    assert snapshot.head_sha == "a" * 40
    assert snapshot.base_ref == "main"
    assert snapshot.base_sha == "b" * 40
    assert session.calls[0][1].endswith("/repos/owner/repo/pulls/17")


def test_live_snapshot_uses_origin_for_same_repository_head() -> None:
    session = Session([Response({
        "number": 17, "state": "open", "user": {"login": "author"},
        "base": {
            "ref": "main", "sha": "b" * 40,
            "repo": {"full_name": "owner/repo"},
        },
        "head": {
            "ref": "feature", "sha": "a" * 40,
            "repo": {"full_name": "owner/repo"},
        },
    })])

    snapshot = GitHubForgeClient(session=session).get_pull_request_snapshot(
        "owner/repo", 17,
    )

    assert snapshot.head_remote == "origin"


def test_submit_review_uses_json_transport_and_scope_head() -> None:
    session = Session([Response({"login": "reviewer"}), Response({
        "id": 9, "user": {"login": "reviewer"}, "state": "APPROVED",
        "body": "body", "commit_id": "a" * 40,
    })])
    client = GitHubForgeClient(session=session)
    client.verify_identity("reviewer")

    client.submit_review(_scope(), ReviewVerdict.APPROVE, 'body "quoted"\nnext')

    method, url, kwargs = session.calls[1]
    assert method == "POST"
    assert url.endswith("/repos/owner/repo/pulls/17/reviews")
    assert kwargs["json"] == {
        "commit_id": "a" * 40,
        "event": "APPROVE",
        "body": 'body "quoted"\nnext',
    }


def test_edit_pull_request_body_patches_only_body_at_exact_bound_pulls_endpoint() -> None:
    session = Session([Response({"login": "reviewer"}), Response({})])
    client = GitHubForgeClient(token="secret", session=session)
    client.verify_identity("reviewer")
    scope = replace(_scope(), canonical_repo="bound/project", pr_number=29)
    body = 'Description "quoted"\n@/tmp/body.md'

    assert client.edit_pull_request_body(scope, body) is None

    assert [(method, url) for method, url, _ in session.calls] == [
        ("GET", "https://api.github.com/user"),
        ("PATCH", "https://api.github.com/repos/bound/project/pulls/29"),
    ]
    assert session.calls[1][2]["json"] == {"body": body}


@pytest.mark.parametrize("identity", ["unverified", "wrong-principal", "changed-credential"])
def test_edit_pull_request_body_identity_refusal_without_patch(identity: str) -> None:
    session = Session([Response({"login": "reviewer"})])
    client = GitHubForgeClient(token="secret", session=session)
    scope = _scope()
    if identity != "unverified":
        client.verify_identity("reviewer")
        if identity == "wrong-principal":
            scope = replace(scope, principal="other-bot")
        else:
            client = GitHubForgeClient(token="different-secret", session=session)
    before = list(session.calls)

    with pytest.raises(GitHubIdentityVerificationError):
        client.edit_pull_request_body(scope, "Updated description")

    assert session.calls == before


def test_mismatched_authenticated_identity_refuses_effect_without_post() -> None:
    session = Session([Response({"login": "other-bot"})])
    client = GitHubForgeClient(token="secret", session=session)

    with pytest.raises(
        ForgeError,
        match="authenticated as other-bot, declared as reviewer",
    ):
        client.verify_identity("reviewer")
    with pytest.raises(ForgeError, match="cache is empty"):
        client.submit_review(_scope(), ReviewVerdict.APPROVE, "body")

    assert [(method, url.rsplit("/", 1)[-1]) for method, url, _ in session.calls] == [
        ("GET", "user"),
    ]


def test_midflight_forge_identity_change_returns_execution_fault_and_latches(monkeypatch) -> None:
    from mimir.tools import forge as forge_tools

    error = github_module.GitHubIdentityVerificationError(
        "github identity verification cache does not match active credential",
        declared_login="reviewer",
        authenticated_login="reviewer",
    )

    with pytest.raises(ToolException, match="active credential"):
        forge_tools._call(lambda: (_ for _ in ()).throw(error))

    assert forge_tools.github_identity_is_degraded() is True


def test_matching_identity_is_cached_for_multiple_effects() -> None:
    session = Session([
        Response({"login": "reviewer"}),
        Response({
            "id": 9, "user": {"login": "reviewer"}, "state": "APPROVED",
            "body": "body", "commit_id": "a" * 40,
        }),
        Response({
            "id": 10, "user": {"login": "reviewer"}, "body": "comment",
            "created_at": "now", "updated_at": "now",
        }),
    ])
    client = GitHubForgeClient(token="secret", session=session)

    assert client.verify_identity("reviewer") == "reviewer"
    client.submit_review(_scope(), ReviewVerdict.APPROVE, "body")
    client.add_pull_request_comment(_scope(), "comment")

    assert [url for method, url, _ in session.calls if method == "GET"] == [
        "https://api.github.com/user",
    ]


def test_startup_identity_verification_degrades_coding_on_mismatch(monkeypatch) -> None:
    from mimir.tools import forge as forge_tools

    class MismatchedClient:
        def verify_identity(self, declared_login):
            raise GitHubIdentityVerificationError(
                "provider wording may change",
                declared_login=declared_login,
                authenticated_login="other-bot",
            )

    observed: list[str] = []
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "reviewer")
    monkeypatch.setattr(github_module, "GitHubForgeClient", MismatchedClient)
    monkeypatch.setattr(forge_tools, "_github_identity_degraded", False)
    monkeypatch.setattr(forge_tools, "_github_identity_degraded_error", None)
    forge_tools.set_github_identity_degraded_callback(lambda exc: observed.append(str(exc)))

    assert initialize_github_forge_identity() is False
    assert forge_tools.github_identity_is_degraded() is True
    assert observed == ["provider wording may change"]


@pytest.mark.parametrize("status", [429, 503])
def test_identity_http_outage_has_typed_transient_provenance(status: int) -> None:
    client = GitHubForgeClient(session=Session([Response({}, status=status)]))

    with pytest.raises(GitHubIdentityVerificationError) as caught:
        client.verify_identity("reviewer")

    assert caught.value.failure_kind == GitHubIdentityFailureKind.TRANSIENT


def test_identity_transport_failure_has_typed_transient_provenance() -> None:
    class FailingSession:
        def request(self, *args, **kwargs):
            raise requests.ConnectionError("wording is not part of policy")

    client = GitHubForgeClient(session=FailingSession())

    with pytest.raises(GitHubIdentityVerificationError) as caught:
        client.verify_identity("reviewer")

    assert caught.value.failure_kind == GitHubIdentityFailureKind.TRANSIENT


@pytest.mark.parametrize("status", [401, 403])
def test_invalid_github_credential_is_permanent(status: int) -> None:
    client = GitHubForgeClient(session=Session([Response({}, status=status)]))

    with pytest.raises(GitHubIdentityVerificationError) as caught:
        client.verify_identity("reviewer")

    assert caught.value.failure_kind == GitHubIdentityFailureKind.PERMANENT


def test_startup_identity_verification_registers_matching_client(monkeypatch) -> None:
    from mimir.tools import forge as forge_tools

    monkeypatch.setattr(forge_tools, "_github_identity_degraded", False)
    monkeypatch.setattr(forge_tools, "_github_identity_degraded_error", None)
    verified: list[str] = []
    registered: list[object] = []

    class MatchingClient:
        def verify_identity(self, declared_login):
            verified.append(declared_login)
            return declared_login

    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "reviewer")
    monkeypatch.setattr(github_module, "GitHubForgeClient", MatchingClient)
    monkeypatch.setattr("mimir.tools.forge.set_forge_client", registered.append)

    assert initialize_github_forge_identity() is True

    assert verified == ["reviewer"]
    assert len(registered) == 1
    assert isinstance(registered[0], MatchingClient)


def test_invalid_scope_cannot_construct_account_or_cross_repo_target() -> None:
    client = GitHubForgeClient(session=Session([]))
    malformed = replace(_scope(), canonical_repo="users/account")
    object.__setattr__(malformed, "canonical_repo", "../users/account")

    with pytest.raises(ForgeError, match="invalid immutable"):
        client.get_pull_request(malformed)


def test_response_size_and_pagination_are_bounded() -> None:
    oversized = Response("x" * 1_048_577, content_type="text/plain")
    client = GitHubForgeClient(session=Session([oversized]))
    with pytest.raises(ForgeResponseTooLarge):
        client.get_pull_request(_scope())

    page = [{"filename": f"file-{index}"} for index in range(50)]
    client = GitHubForgeClient(session=Session([Response(page) for _ in range(10)]))
    with pytest.raises(ForgeResponseTooLarge, match="page limit"):
        client.list_files(_scope())


def _file_diff(path: str, body_bytes: int) -> str:
    header = f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
    return header + "+" + "x" * (body_bytes - len(header.encode("utf-8")) - 2) + "\n"


def test_diff_under_cap_is_byte_identical() -> None:
    diff = _file_diff("small.txt", 1_024)

    assert bound_diff(diff).encode("utf-8") == diff.encode("utf-8")


def test_diff_omits_one_file_over_cap_but_keeps_complete_sibling() -> None:
    small = _file_diff("small.txt", 1_024)
    huge = _file_diff("huge.txt", github_module._MAX_DIFF_BYTES + 1)

    result = bound_diff(huge + small)

    assert len(result.encode("utf-8")) <= github_module._MAX_DIFF_BYTES
    assert result.startswith(small)
    assert "diff --git a/huge.txt" not in result
    assert "per_file_byte_limit" in result
    assert "whole_diff_byte_limit" not in result
    assert "omitted_file_count: 1" in result
    assert '"huge.txt"' in result


def test_many_files_over_whole_diff_cap_stop_on_file_boundaries() -> None:
    files = [_file_diff(f"file-{index}.txt", 200_000) for index in range(3)]

    result = bound_diff("".join(files))

    assert len(result.encode("utf-8")) <= github_module._MAX_DIFF_BYTES
    assert result.startswith(files[0] + files[1])
    assert files[2] not in result
    assert "whole_diff_byte_limit" in result
    assert "omitted_file_count: 1" in result
    assert '"file-2.txt"' in result


def test_single_file_larger_than_whole_budget_returns_only_actionable_marker() -> None:
    result = bound_diff(_file_diff("only-huge.txt", github_module._MAX_DIFF_BYTES + 1))

    assert len(result.encode("utf-8")) <= github_module._MAX_DIFF_BYTES
    assert "diff --git" not in result
    assert "per_file_byte_limit" in result
    assert "omitted_file_count: 1" in result
    assert '"only-huge.txt"' in result


def test_many_tiny_files_fit_a_bounded_marker_without_raising() -> None:
    diff = "".join(
        f"diff --git a/f{index} b/f{index}\n@@ -1 +1 @@\n-a\n+b\n"
        for index in range(12_000)
    )

    result = bound_diff(diff)

    assert len(result.encode("utf-8")) <= github_module._MAX_DIFF_BYTES
    assert "[pr_diff truncated]" in result
    assert "whole_diff_byte_limit" in result
    assert "omitted_file_count:" in result
    assert "additional paths not shown" in result


def test_github_diff_over_generic_response_cap_uses_diff_truncation() -> None:
    diff = _file_diff("provider-huge.txt", github_module._MAX_RESPONSE_BYTES + 1)
    session = Session([Response(diff, content_type="text/plain")])
    client = GitHubForgeClient(session=session)

    result = client.get_diff(_scope())

    assert len(result.encode("utf-8")) <= github_module._MAX_DIFF_BYTES
    assert "per_file_byte_limit" in result
    assert '"provider-huge.txt"' in result
    assert session.calls[0][2]["timeout"] == client._timeout


def test_github_diff_fetch_keeps_a_finite_input_ceiling() -> None:
    client = GitHubForgeClient(session=Session([
        Response("x" * (github_module._MAX_DIFF_FETCH_BYTES + 1), content_type="text/plain"),
    ]))

    with pytest.raises(ForgeResponseTooLarge, match="size limit"):
        client.get_diff(_scope())


def test_provider_errors_are_mapped_without_response_payload() -> None:
    client = GitHubForgeClient(session=Session([
        Response({"message": "token secret details"}, status=403),
    ]))

    with pytest.raises(ForgeError, match="operation forbidden") as raised:
        client.get_pull_request(_scope())
    assert "secret details" not in str(raised.value)


def test_adapter_rejects_oversized_bodies_and_path_injection() -> None:
    client = GitHubForgeClient(session=Session([]))

    with pytest.raises(ForgeError, match="oversized body"):
        client.add_pull_request_comment(_scope(), "x" * 65_537)
    with pytest.raises(ForgeError, match="repository path"):
        client.add_inline_review_comment(
            _scope(), path="../secret", line=1, body="comment",
        )


def _issue_payload(*, state="open", repository="owner/repo", number=220, pr=False):
    payload = {
        "number": number,
        "state": state,
        "repository_url": f"https://api.github.com/repos/{repository}",
    }
    if pr:
        payload["pull_request"] = {"url": "https://api.github.com/pulls/220"}
    return payload


def test_issue_comment_posts_only_after_server_resolves_exact_open_issue() -> None:
    session = Session([
        Response(_issue_payload()),
        Response({
            "id": 5, "user": {"login": "mimir"}, "body": "analysis",
            "created_at": "now", "updated_at": "now",
        }),
    ])

    result = GitHubForgeClient(session=session).add_issue_comment(
        "owner/repo", 220, "analysis",
    )

    assert result.body == "analysis"
    assert [(method, url) for method, url, _ in session.calls] == [
        ("GET", "https://api.github.com/repos/owner/repo/issues/220"),
        ("POST", "https://api.github.com/repos/owner/repo/issues/220/comments"),
    ]


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (Response(_issue_payload(pr=True)), "pull request; use the pull-request"),
        (Response(_issue_payload(state="closed")), "issue is not open"),
        (Response({"message": "missing"}, status=404), "issue not found"),
        (Response(_issue_payload(repository="other/repo")), "mismatched issue identity"),
    ],
)
def test_issue_comment_refuses_invalid_server_target_before_post(response, message) -> None:
    session = Session([response])

    with pytest.raises(ForgeError, match=message):
        GitHubForgeClient(session=session).add_issue_comment(
            "owner/repo", 220, "analysis",
        )

    assert [method for method, _url, _kwargs in session.calls] == ["GET"]
