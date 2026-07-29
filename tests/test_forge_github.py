from __future__ import annotations

from dataclasses import replace

import pytest

from mimir.forge import ForgeError, ForgeResponseTooLarge, ReviewVerdict
from mimir.forge.github import GitHubForgeClient
from mimir.models import RepoPRActionScope


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


def test_submit_review_uses_json_transport_and_scope_head() -> None:
    session = Session([Response({
        "id": 9, "user": {"login": "reviewer"}, "state": "APPROVED",
        "body": "body", "commit_id": "a" * 40,
    })])
    client = GitHubForgeClient(session=session)

    client.submit_review(_scope(), ReviewVerdict.APPROVE, 'body "quoted"\nnext')

    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert url.endswith("/repos/owner/repo/pulls/17/reviews")
    assert kwargs["json"] == {
        "commit_id": "a" * 40,
        "event": "APPROVE",
        "body": 'body "quoted"\nnext',
    }


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
