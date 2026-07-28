from __future__ import annotations

import poller


_REAL_PR_AUTHOR_IS_TRUSTED = poller._pr_author_is_trusted


def _pr(
    number: int,
    *,
    author: str = "payload-author",
    requested_reviewers: list[str] | None = None,
) -> dict:
    return {
        "number": number,
        "title": "Untrusted payload title",
        "body": "Untrusted payload body",
        "created_at": "2026-07-28T12:00:00Z",
        "html_url": f"https://github.com/acme/widget/pull/{number}",
        "user": {"login": author},
        "head": {"sha": f"sha-{number}"},
        "requested_reviewers": [
            {"login": login} for login in (requested_reviewers or [])
        ],
    }


def _capture(monkeypatch):
    events: list[dict] = []
    signals: list[dict] = []
    monkeypatch.setattr(
        poller, "_emit", lambda prompt, **extras: events.append({"prompt": prompt, **extras}),
    )
    monkeypatch.setattr(
        poller,
        "_emit_signal",
        lambda signal, **extras: signals.append({"signal": signal, **extras}),
    )
    return events, signals


def _use_real_trust_filter(monkeypatch) -> None:
    monkeypatch.setattr(poller, "_pr_author_is_trusted", _REAL_PR_AUTHOR_IS_TRUSTED)


def test_collaborator_pr_is_reviewed_from_server_attested_author(monkeypatch):
    events, signals = _capture(monkeypatch)
    _use_real_trust_filter(monkeypatch)
    seen: list[tuple[str, object, str]] = []
    monkeypatch.setattr(
        poller,
        "_github_content_author",
        lambda repo, extras, token: "api-collaborator",
    )

    def trusted(repo, author, token):
        seen.append((repo, author, token))
        return author == "api-collaborator"

    monkeypatch.setattr(poller, "_github_author_is_trusted", trusted)
    monkeypatch.setattr(poller, "_gh_api", lambda endpoint, token: [_pr(1, author="impostor")])

    count = poller._check_prs(
        "acme/widget", "2026-07-28T11:00:00Z", "server-token", "",
        trust_cache={}, surfaced_untrusted=set(),
    )

    assert count == 1
    assert [event["event_type"] for event in events] == ["pr_opened"]
    assert signals == []
    assert seen == [("acme/widget", "api-collaborator", "server-token")]


def test_non_collaborator_pr_is_not_reviewed_and_surfaces_once(monkeypatch):
    events, signals = _capture(monkeypatch)
    _use_real_trust_filter(monkeypatch)
    monkeypatch.setattr(poller, "_github_content_author", lambda *args: "outsider")
    monkeypatch.setattr(poller, "_github_author_is_trusted", lambda *args: False)
    monkeypatch.setattr(poller, "_gh_api", lambda endpoint, token: [_pr(2)])
    surfaced: set[str] = set()

    for _ in range(2):
        poller._check_pr_pushes(
            "acme/widget", "token", "", {}, trust_cache={},
            surfaced_untrusted=surfaced,
        )

    assert events == []
    assert signals == [{
        "signal": "pr_auto_review_skipped_untrusted_author",
        "repo": "acme/widget",
        "number": 2,
        "url": "https://github.com/acme/widget/pull/2",
    }]
    assert surfaced == {"2"}


def test_active_org_member_pr_is_reviewed_via_existing_trust_primitive(monkeypatch):
    events, signals = _capture(monkeypatch)
    _use_real_trust_filter(monkeypatch)
    monkeypatch.setattr(poller, "_github_content_author", lambda *args: "org-member")
    calls: list[str] = []

    def github_api(endpoint: str, token: str):
        calls.append(endpoint)
        if endpoint.startswith("orgs/"):
            return 200, {"state": "active"}
        return 404, None

    monkeypatch.setattr("mimir.pollers._github_api_attestation", github_api)
    monkeypatch.setattr(poller, "_gh_api", lambda endpoint, token: [_pr(3)])

    poller._check_prs(
        "acme/widget", "2026-07-28T11:00:00Z", "token", "",
        trust_cache={}, surfaced_untrusted=set(),
    )

    assert [event["event_type"] for event in events] == ["pr_opened"]
    assert signals == []
    assert calls == [
        "repos/acme/widget/collaborators/org-member",
        "orgs/acme/memberships/org-member",
    ]


def test_trust_lookup_failure_fails_closed(monkeypatch):
    events, signals = _capture(monkeypatch)
    _use_real_trust_filter(monkeypatch)
    monkeypatch.setattr(poller, "_github_content_author", lambda *args: None)
    monkeypatch.setattr(poller, "_gh_api", lambda endpoint, token: [_pr(4)])

    poller._check_prs(
        "acme/widget", "2026-07-28T11:00:00Z", "token", "",
        trust_cache={}, surfaced_untrusted=set(),
    )

    assert events == []
    assert [signal["signal"] for signal in signals] == [
        "pr_auto_review_skipped_untrusted_author",
    ]


def test_explicit_review_request_bypasses_failed_author_trust(monkeypatch):
    events, signals = _capture(monkeypatch)
    _use_real_trust_filter(monkeypatch)
    monkeypatch.setattr(poller, "_github_content_author", lambda *args: None)
    pr = _pr(5, requested_reviewers=["mimir-bot"])

    def github_api(endpoint: str, token: str):
        return None if endpoint.endswith("/reviews") else [pr]

    monkeypatch.setattr(poller, "_gh_api", github_api)

    poller._check_pr_pushes(
        "acme/widget", "token", "mimir-bot", {"5": "sha-5"},
        trust_cache={}, surfaced_untrusted=set(),
    )

    assert [event["event_type"] for event in events] == ["pr_review_requested"]
    assert signals == []


def test_trust_lookup_is_cached_per_author_per_poll_cycle(monkeypatch):
    _use_real_trust_filter(monkeypatch)
    authors = {1: "same-author", 2: "same-author"}
    content_calls: list[int] = []
    trust_calls: list[str] = []

    def content_author(repo, extras, token):
        number = int(extras["url"].rsplit("/", 1)[1])
        content_calls.append(number)
        return authors[number]

    def trusted(repo, author, token):
        trust_calls.append(author)
        return True

    monkeypatch.setattr(poller, "_github_content_author", content_author)
    monkeypatch.setattr(poller, "_github_author_is_trusted", trusted)
    cache: dict[tuple[str, object], object] = {}

    assert _REAL_PR_AUTHOR_IS_TRUSTED(
        "acme/widget", 1, "https://github.com/acme/widget/pull/1", "token", cache,
    )
    assert _REAL_PR_AUTHOR_IS_TRUSTED(
        "acme/widget", 2, "https://github.com/acme/widget/pull/2", "token", cache,
    )
    assert _REAL_PR_AUTHOR_IS_TRUSTED(
        "acme/widget", 1, "https://github.com/acme/widget/pull/1", "token", cache,
    )

    assert content_calls == [1, 2]
    assert trust_calls == ["same-author"]
