"""Outsider content must not cross any GitHub poller prompt boundary."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from github_poller_test_support import poller
from tests.withhold_probe import OUTSIDER_MARKER, assert_marker_absent

REPO = "acme/widget"
SINCE = "2026-10-08T00:00:00Z"
STAMP = "2026-10-09T00:00:00Z"


def _issue(number=7, author="outsider"):
    return dict(number=number, title=OUTSIDER_MARKER, body=OUTSIDER_MARKER,
                user={"login": author}, created_at=STAMP,
                html_url=f"https://github.com/{REPO}/issues/{number}")


def _pr(number=8, author="trusted"):
    return dict(number=number, title="Trusted PR", body="trusted body",
                user={"login": author}, state="open", created_at=STAMP,
                html_url=f"https://github.com/{REPO}/pull/{number}",
                head={"sha": "a" * 40, "repo": {"full_name": REPO}},
                base={"sha": "b" * 40}, requested_reviewers=[])


def _comment(author="outsider", number=8):
    return dict(user={"login": author}, body=OUTSIDER_MARKER,
                path=OUTSIDER_MARKER, created_at=STAMP,
                html_url=f"https://github.com/{REPO}/pull/{number}#issuecomment-1",
                issue_url=f"https://api.github.com/repos/{REPO}/issues/{number}",
                pull_request_url=f"https://api.github.com/repos/{REPO}/pulls/{number}")


@pytest.fixture
def trust(monkeypatch):
    calls = []

    def verdict(repo, author, token):
        calls.append(author)
        return {"trusted": True, "outsider": False}.get(author)

    monkeypatch.setattr(poller, "_github_author_is_trusted", verdict)
    return calls


def test_outsider_issue_signal_deduplicates_in_cursor_and_never_leaks(
    monkeypatch, tmp_path, capsys, trust,
):
    monkeypatch.setattr(poller, "STATE_DIR", tmp_path)
    monkeypatch.setattr(poller, "CURSOR_FILE", tmp_path / "cursor.json")
    monkeypatch.setattr(poller, "_resolve_token", lambda: "token")
    monkeypatch.setattr(poller, "_utc_now_iso", lambda: SINCE)
    monkeypatch.setenv("GITHUB_REPOS", REPO)
    poller._save_cursor({"last_checked": SINCE})
    monkeypatch.setattr(poller, "_gh_api", lambda endpoint, token:
                        [_issue()] if "/issues?" in endpoint else [])
    for tick in range(2):
        poller.main()
        records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert_marker_absent(records, poller.CURSOR_FILE)
        signals = [item for item in records if item.get("signal") == "github_outsider_issue_withheld"]
        assert len(signals) == (1 if tick == 0 else 0)
        assert not any("prompt" in item for item in records)
        if signals:
            assert signals[0] == dict(
                poller=poller.POLLER_NAME, signal="github_outsider_issue_withheld",
                repo=REPO, number=7, author="outsider",
                url=f"https://github.com/{REPO}/issues/7",
            )
    assert json.loads(poller.CURSOR_FILE.read_text())["github_outsider_issues"][REPO] == ["7"]


@pytest.mark.parametrize("surface", ["issue_comment", "pr_context", "review_comment", "review"])
def test_outsider_child_events_never_emit_prompt(monkeypatch, capsys, trust, surface):
    pr = _pr()
    child = _comment()

    def api(endpoint, token):
        if endpoint.endswith("/pulls/8"):
            return pr
        if endpoint.endswith("/issues/8"):
            return dict(pr, pull_request={"url": "pr"})
        if endpoint.endswith("/pulls/8/reviews"):
            return [dict(child, submitted_at=STAMP, state="CHANGES_REQUESTED")]
        if "/pulls/comments?" in endpoint or "/issues/comments?" in endpoint:
            return [child]
        if "/pulls?" in endpoint:
            return [pr]
        return []

    monkeypatch.setattr(poller, "_gh_api", api)
    if surface == "issue_comment":
        poller._check_issue_comments(REPO, SINCE, "token", "")
    elif surface == "pr_context":
        _, context = poller._collect_issue_comment_context(REPO, SINCE, "token", "")
        assert "1 comment(s) by non-collaborators withheld" in context["8"]
        assert_marker_absent(context)
    elif surface == "review_comment":
        poller._check_pr_review_comments(REPO, SINCE, "token", "")
    else:
        poller._check_pr_reviews(REPO, SINCE, "token", "")
    assert_marker_absent(capsys.readouterr().out)
    assert not capsys.readouterr().out


def test_mixed_pr_context_keeps_trusted_prose_only(monkeypatch, capsys, trust):
    trusted = dict(_comment("trusted"), body="legitimate review note")
    monkeypatch.setattr(poller, "_gh_api", lambda endpoint, token:
                        [_comment(), trusted] if "/comments?" in endpoint else
                        [_pr()] if "/pulls?" in endpoint else [])
    _, context = poller._collect_issue_comment_context(REPO, SINCE, "token", "")
    assert "legitimate review note" in context["8"]
    assert "1 comment(s) by non-collaborators withheld" in context["8"]
    poller._check_prs(REPO, SINCE, "token", "", review_context=context)
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(records) == 1 and "legitimate review note" in records[0]["prompt"]
    assert "1 comment(s) by non-collaborators withheld" in records[0]["prompt"]
    assert_marker_absent(context, records)


def test_unresolved_author_is_retryable_and_not_cached(monkeypatch, capsys):
    cache = {}
    monkeypatch.setattr(poller, "_gh_api", lambda endpoint, token: [_issue()])
    monkeypatch.setattr(poller, "_github_author_is_trusted", lambda *args: None)
    budget = poller.TickBudget()
    assert poller._check_issues(REPO, SINCE, "token", "", trust_cache=cache,
                               surfaced_outsiders=set(), tick_budget=budget) == 0
    assert cache == {} and budget.hard_truncated
    assert capsys.readouterr().out == ""
    monkeypatch.setattr(poller, "_github_author_is_trusted", lambda *args: False)
    assert poller._check_issues(REPO, SINCE, "token", "", trust_cache=cache,
                               surfaced_outsiders=set()) == 1
    assert_marker_absent(capsys.readouterr().out)


def test_exhausted_attestation_budget_is_not_an_outsider_verdict(monkeypatch, capsys):
    monkeypatch.setattr(poller, "_gh_api", lambda endpoint, token: [_issue()])
    monkeypatch.setattr(poller, "_github_author_is_trusted",
                        lambda *args: pytest.fail("attestation exceeded budget"))
    cache, surfaced = {}, set()
    budget = poller.TickBudget(hard_deadline_seconds=0)
    assert poller._check_issues(REPO, SINCE, "token", "", trust_cache=cache,
                               surfaced_outsiders=surfaced, tick_budget=budget) == 0
    assert budget.hard_truncated and cache == {} and surfaced == set()
    assert capsys.readouterr().out == ""


def test_trusted_issue_prompt_is_unchanged(monkeypatch, capsys, trust):
    issue = dict(_issue(author="trusted"), title="A genuine issue", body="Full report")
    monkeypatch.setattr(poller, "_gh_api", lambda endpoint, token: [issue])
    assert poller._check_issues(REPO, SINCE, "token", "") == 1
    record = json.loads(capsys.readouterr().out)
    assert record["prompt"] == (
        "New issue on acme/widget: #7 A genuine issue (by @trusted)\n"
        "Full report\nhttps://github.com/acme/widget/issues/7"
    )


def test_outsider_requested_pr_and_ci_are_silent(monkeypatch, capsys, trust):
    pr = _pr(author="outsider")
    pr["title"] = OUTSIDER_MARKER
    pr["requested_reviewers"] = [{"login": "mimir"}]
    monkeypatch.setattr(poller, "_pr_author_is_trusted", lambda *args, **kwargs: False)
    monkeypatch.setattr(poller, "_gh_api", lambda endpoint, token:
                        pr if endpoint.endswith("/pulls/8") else [pr])
    poller._check_pr_pushes(REPO, "token", "mimir", {}, surfaced_untrusted=set())
    poller._check_pr_ci_failures(REPO, SINCE, "token", "mimir", {})
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert not any("prompt" in item for item in records)
    assert_marker_absent(records)


def test_outsider_push_commit_is_deferred_without_losing_head(monkeypatch, capsys, trust):
    def api(endpoint, token):
        if "/compare/" in endpoint:
            return {"ahead_by": 1, "commits": [{
                "user": {"login": "outsider"}, "author": {"login": "outsider"},
                "commit": {"message": OUTSIDER_MARKER},
            }]}
        return [_pr()]

    monkeypatch.setattr(poller, "_gh_api", api)
    count, heads, _ = poller._check_pr_pushes(
        REPO, "token", "", {"8": "old-head"},
    )
    assert count == 0 and heads == {"8": "old-head"}
    assert_marker_absent(capsys.readouterr().out, heads)


def test_outsider_blocking_review_cannot_enter_own_pr_reminder(
    monkeypatch, capsys, trust,
):
    pr = _pr(author="mimir")
    review = dict(_comment(), state="CHANGES_REQUESTED", submitted_at=STAMP,
                  commit_id="a" * 40)
    monkeypatch.setattr(poller, "_gh_api", lambda endpoint, token:
                        [review] if endpoint.endswith("/reviews") else [pr])
    count, cursor = poller._check_own_changes_requested(REPO, "token", "mimir", {})
    assert count == 0 and cursor == {}
    assert_marker_absent(capsys.readouterr().out)


def test_mergeability_does_not_render_outsider_review_text(monkeypatch, capsys, trust):
    pr = dict(_pr(author="mimir"), title="Own trusted title", mergeable=False)
    review = dict(_comment(), state="COMMENTED", submitted_at=STAMP,
                  user={"login": OUTSIDER_MARKER})
    monkeypatch.setattr(poller, "_github_author_is_trusted", lambda *args: False)

    def api(endpoint, token):
        if "/compare/" in endpoint:
            return {"behind_by": 0}
        if endpoint.endswith("/reviews"):
            return [review]
        if endpoint.endswith("/pulls/8"):
            return pr
        return [pr]

    monkeypatch.setattr(poller, "_gh_api", api)
    count, _ = poller._check_own_mergeability(REPO, "token", "mimir", {})
    assert count == 1
    assert_marker_absent(capsys.readouterr().out)


def test_outsider_pr_opened_never_emits_title_or_body(monkeypatch, capsys):
    pr = dict(_pr(author="outsider"), title=OUTSIDER_MARKER, body=OUTSIDER_MARKER)
    monkeypatch.setattr(poller, "_pr_author_is_trusted", lambda *args, **kwargs: False)
    monkeypatch.setattr(poller, "_gh_api", lambda endpoint, token: [pr])
    poller._check_prs(REPO, SINCE, "token", "", surfaced_untrusted=set())
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(records) == 1 and records[0]["signal"] == "pr_auto_review_skipped_untrusted_author"
    assert_marker_absent(records)


@pytest.mark.parametrize("surface", ["issue_comment", "review_comment", "review"])
def test_trusted_child_on_outsider_pr_never_spawns_turn(monkeypatch, capsys, trust, surface):
    pr = dict(_pr(author="outsider"), title=OUTSIDER_MARKER)
    child = dict(_comment("trusted"), body="trusted comment")
    monkeypatch.setattr(poller, "_pr_author_is_trusted", lambda *args, **kwargs: False)

    def api(endpoint, token):
        if endpoint.endswith("/pulls/8"):
            return pr
        if endpoint.endswith("/issues/8"):
            return dict(pr, pull_request={"url": "pr"})
        if endpoint.endswith("/pulls/8/reviews"):
            return [dict(child, submitted_at=STAMP, state="COMMENTED")]
        if "/comments?" in endpoint:
            return [child]
        return [pr]

    monkeypatch.setattr(poller, "_gh_api", api)
    if surface == "issue_comment":
        poller._check_issue_comments(REPO, SINCE, "token", "")
    elif surface == "review_comment":
        poller._check_pr_review_comments(REPO, SINCE, "token", "")
    else:
        poller._check_pr_reviews(REPO, SINCE, "token", "")
    assert capsys.readouterr().out == ""


def test_outsider_pr_ci_check_name_does_not_enter_signal(monkeypatch, capsys, tmp_path):
    pr = _pr(author="outsider")
    monkeypatch.setattr(poller, "STATE_DIR", tmp_path)
    monkeypatch.setattr(poller, "_pr_author_is_trusted", lambda *args, **kwargs: False)

    def api(endpoint, token):
        if endpoint.endswith("/pulls/8"):
            return pr
        if "/check-runs?" in endpoint:
            return {"check_runs": [dict(id=101, name=OUTSIDER_MARKER,
                                        status="completed", conclusion="failure",
                                        completed_at=STAMP)]}
        if "/actions/runs?" in endpoint:
            return {"workflow_runs": []}
        return [pr]

    monkeypatch.setattr(poller, "_gh_api", api)
    poller._check_pr_ci_failures(REPO, SINCE, "token", "mimir", {})
    assert capsys.readouterr().out == ""


def test_github_api_error_diagnostics_do_not_repeat_outsider_response(monkeypatch, capsys):
    monkeypatch.setattr(poller.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=403, stdout="", stderr=OUTSIDER_MARKER))
    assert poller._gh_api(f"repos/{REPO}/issues/{OUTSIDER_MARKER}", "token") is None
    assert_marker_absent(capsys.readouterr().err)
