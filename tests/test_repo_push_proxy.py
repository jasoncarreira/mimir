from __future__ import annotations

from dataclasses import replace
from email.message import Message
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from mimir.models import RepoPRAction, RepoPRActionScope
from mimir.repo_push_proxy import PushProxyError, ScopedGitHubPushProxy


OLD = "a" * 40
NEW = "b" * 40
REF = "refs/heads/worklink/7"


def _scope(**changes: object) -> RepoPRActionScope:
    scope = RepoPRActionScope(
        provenance="poller_payload",
        canonical_repo="owner/repo",
        canonical_root="/srv/repo",
        canonical_origin="https://github.com/owner/repo.git",
        principal="mimir-bot",
        event_type="pr_changes_requested_stale",
        allowed_operations=frozenset(action.value for action in RepoPRAction),
        pr_number=7,
        head_repo="owner/repo",
        head_remote="origin",
        destination_ref=REF,
        observed_head_sha=OLD,
        base_ref="main",
        observed_base_sha="c" * 40,
    )
    return replace(scope, **changes)


def _push_body(ref: str = REF, *, old: str = OLD, new: str = NEW) -> bytes:
    command = f"{old} {new} {ref}\0report-status\n".encode()
    return f"{len(command) + 4:04x}".encode() + command + b"0000PACK"


class _Response:
    status = 200
    headers = Message()

    def read(self, _limit: int) -> bytes:
        return b"ok"


def test_proxy_forwards_only_exact_receive_pack_update_without_echoing_token(monkeypatch) -> None:
    captured: list[Request] = []

    def upstream(request: Request, timeout: float):
        captured.append(request)
        return _Response()

    monkeypatch.setattr("mimir.repo_push_proxy.urlopen", upstream)
    with ScopedGitHubPushProxy(_scope(), NEW, token_provider=lambda: "secret-token") as remote:
        request = Request(
            remote + "/git-receive-pack",
            data=_push_body(),
            headers={"Content-Type": "application/x-git-receive-pack-request"},
            method="POST",
        )
        assert urlopen(request).read() == b"ok"

    assert len(captured) == 1
    assert captured[0].full_url == "https://github.com/owner/repo.git/git-receive-pack"
    assert captured[0].get_header("Authorization").startswith("Basic ")
    assert "secret-token" not in captured[0].full_url


@pytest.mark.parametrize(
    "body",
    [
        _push_body("refs/heads/other"),
        _push_body(old="d" * 40),
        _push_body(new="e" * 40),
        _push_body(new="0" * 40),
    ],
)
def test_proxy_refuses_every_out_of_scope_receive_pack_update(monkeypatch, body: bytes) -> None:
    upstream_called = False

    def upstream(_request: Request, timeout: float):
        nonlocal upstream_called
        upstream_called = True
        return _Response()

    monkeypatch.setattr("mimir.repo_push_proxy.urlopen", upstream)
    with ScopedGitHubPushProxy(_scope(), NEW, token_provider=lambda: "secret-token") as remote:
        request = Request(remote + "/git-receive-pack", data=body, method="POST")
        with pytest.raises(HTTPError) as refusal:
            urlopen(request)
        assert refusal.value.code == 403
        assert "secret-token" not in refusal.value.read().decode()
    assert upstream_called is False


def test_proxy_refuses_non_push_operations(monkeypatch) -> None:
    monkeypatch.setattr(
        "mimir.repo_push_proxy.urlopen",
        lambda *_args, **_kwargs: pytest.fail("out-of-scope request reached upstream"),
    )
    with ScopedGitHubPushProxy(_scope(), NEW, token_provider=lambda: "secret-token") as remote:
        with pytest.raises(HTTPError) as refusal:
            urlopen(remote.replace("owner/repo.git", "other/repo.git") + "/info/refs?service=git-upload-pack")
        assert refusal.value.code == 403


@pytest.mark.parametrize(
    "origin",
    [
        "https://github.com/other/repo.git",
        "https://example.com/owner/repo.git",
        "http://github.com/owner/repo.git",
        "https://user@github.com/owner/repo.git",
    ],
)
def test_proxy_origin_binding_is_revalidated_when_credential_is_supplied(origin: str) -> None:
    with pytest.raises(PushProxyError, match="scoped origin"):
        ScopedGitHubPushProxy(_scope(canonical_origin=origin), NEW, token_provider=lambda: "secret")
