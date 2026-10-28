"""Draft CI-gated Worklink PRs do not wake the review bot."""

from github_poller_test_support import poller


def test_draft_not_review_needed(monkeypatch):
    emitted = []
    monkeypatch.setattr(poller, "_emit", lambda *args, **kwargs: emitted.append(args))
    assert not poller._emit_pr_review_needed("review", token="t", reviewer="bot",
        current_head_reviewed=False, repo="acme/widget", number=1, pr_draft=True)
    assert not emitted
