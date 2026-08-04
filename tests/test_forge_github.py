from __future__ import annotations

from dataclasses import replace

import pytest

from mimir.forge import ForgeError, ForgeResponseTooLarge, ReviewVerdict
from mimir.forge.github import GitHubForgeClient
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


def test_live_snapshot_normalizes_all_authority_facts() -> None:
    session = Session([Response({
        "number": 17, "state": "open", "user": {"login": "author"},
        "base": {"ref": "main", "sha": "b" * 40},
        "head": {
            "ref": "feature", "sha": "a" * 40,
            "repo": {"full_name": "contributor/fork"},
        },
    })])

    snapshot = GitHubForgeClient(session=session).get_pull_request_snapshot(
        "owner/repo", 17,
    )

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
        "base": {"ref": "main", "sha": "b" * 40},
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


def test_midflight_forge_identity_change_returns_policy_refusal_and_latches(monkeypatch) -> None:
    from mimir.tools import forge as forge_tools
    from mimir.tools.refusals import ToolPolicyRefusal

    error = github_module.GitHubIdentityVerificationError(
        "github identity verification cache does not match active credential",
        declared_login="reviewer",
        authenticated_login="reviewer",
    )

    with pytest.raises(ToolPolicyRefusal, match="active credential"):
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
            raise ForgeError(
                f"github identity mismatch: authenticated as other-bot, declared as {declared_login}"
            )

    observed: list[str] = []
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "reviewer")
    monkeypatch.setattr(github_module, "GitHubForgeClient", MismatchedClient)
    monkeypatch.setattr(forge_tools, "_github_identity_degraded", False)
    monkeypatch.setattr(forge_tools, "_github_identity_degraded_error", None)
    forge_tools.set_github_identity_degraded_callback(lambda exc: observed.append(str(exc)))

    assert initialize_github_forge_identity() is False
    assert forge_tools.github_identity_is_degraded() is True
    assert observed == ["github identity mismatch: authenticated as other-bot, declared as reviewer"]


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
