"""Trusted comments on the bot's own PRs start scoped remediation, once per id."""
from __future__ import annotations

import json

import pytest

from github_poller_test_support import poller
from mimir import access_control
from mimir.plugins.core import policy_scope
from mimir.plugins.manager import load_plugins
from mimir_cultivate.policy import RepoPRAction, RepoPRScopeProvenance

REPO = "owner/repo"
BOT = "mimir-bot"
SINCE = "2026-10-27T18:00:00Z"
CREATED = "2026-10-27T18:15:41Z"
PR_URL = f"https://github.com/{REPO}/pull/372"


def _pr(author=BOT, state="open"):
    return {
        "number": 372, "title": "README fixes", "state": state,
        "html_url": PR_URL, "user": {"login": author},
        "head": {"ref": "worklink/1", "sha": "a" * 40,
                 "repo": {"full_name": REPO}},
        "base": {"ref": "main", "sha": "b" * 40},
        "requested_reviewers": [],
    }


def _feedback(kind, author="collaborator", id=101):
    common = {"id": id, "user": {"login": author}, "body": f"Fix README {id}",
              "created_at": CREATED}
    if kind == "issue_comment":
        return dict(common, html_url=f"{PR_URL}#issuecomment-{id}",
                    issue_url=f"https://api.github.com/repos/{REPO}/issues/372")
    if kind == "pr_review_comment":
        return dict(common, html_url=f"{PR_URL}#discussion_r{id}",
                    pull_request_url=f"https://api.github.com/repos/{REPO}/pulls/372",
                    path="README.md")
    return dict(common, html_url=f"{PR_URL}#pullrequestreview-{id}",
                submitted_at=CREATED, state="COMMENTED")


def _drive(monkeypatch, tmp_path, capsys, items, *, pr=None, since=SINCE):
    pr = pr or _pr()
    monkeypatch.setattr(poller, "_PROCESS_START", poller.time.monotonic())
    monkeypatch.setattr(poller, "STATE_DIR", tmp_path)
    monkeypatch.setattr(poller, "CURSOR_FILE", tmp_path / "cursor.json")
    monkeypatch.setattr(poller, "_resolve_token", lambda: "token")
    monkeypatch.setattr(poller, "_utc_now_iso", lambda: SINCE)
    monkeypatch.setenv("GITHUB_REPOS", REPO)
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", BOT)
    monkeypatch.setattr(poller, "_github_author_is_trusted",
                        lambda repo, author, token: author == "collaborator")
    if not poller.CURSOR_FILE.exists():
        poller._save_cursor({"last_checked": since})

    def api(endpoint, token):
        if endpoint.endswith("/issues/372"):
            return {"pull_request": {"url": PR_URL}, "state": pr["state"]}
        if endpoint.endswith("/pulls/372"):
            return pr
        if "/issues/comments?" in endpoint:
            return [item for item in items if "issue_url" in item]
        if "/pulls/comments?" in endpoint:
            return [item for item in items if "pull_request_url" in item]
        if endpoint.endswith("/pulls/372/reviews"):
            return [item for item in items if "submitted_at" in item]
        if "/pulls?" in endpoint:
            return [pr] if pr["state"] == "open" else []
        if "/issues?" in endpoint:
            return []
        return []

    monkeypatch.setattr(poller, "_gh_api", api)
    poller.main()
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def _scope(event, monkeypatch):
    monkeypatch.setattr(
        access_control, "_canonical_repo_binding_resolution",
        lambda repo: access_control.RepoBindingResolution(
            ("/server/repo", f"git@github.com:{REPO}.git"), ("/server/repo",), 1,
        ),
    )
    with policy_scope(load_plugins(enable="cultivate").policy):
        return access_control._repo_pr_scope(
            provenance=RepoPRScopeProvenance.POLLER_PAYLOAD,
            repo=event["repo"], principal=event["author"],
            event_type=event["event_type"], review_state=event.get("state"),
            actor=event["actor"], author_is_trusted=True,
            pr_state=event["pr_state"], number=event["number"],
            head_repo=event["head_repo"], head_remote=event["head_remote"],
            head_ref=event["head_ref"], head_sha=event["head_sha"],
            base_ref=event["base_ref"], base_sha=event["base_sha"],
        )


@pytest.mark.parametrize("kind", ["issue_comment", "pr_review_comment", "pr_review"])
def test_each_trusted_feedback_routes_to_remediation(
    monkeypatch, tmp_path, capsys, kind,
):
    events = _drive(monkeypatch, tmp_path, capsys, [_feedback(kind)])
    [event] = [item for item in events if "prompt" in item]
    assert event["event_type"] == kind
    assert event["author"] == BOT and event["actor"] == "collaborator"
    assert event["head_remote"] == "origin"
    assert event["head_ref"] == "worklink/1" and event["base_ref"] == "main"
    assert event["head_sha"] == "a" * 40 and event["base_sha"] == "b" * 40
    assert "Fix README 101" in event["prompt"]
    assert "push the fixes to the PR branch" in event["prompt"]
    assert "reply" in event["prompt"] and "without pushing" in event["prompt"]
    scope = _scope(event, monkeypatch)
    assert scope is not None
    for action in (RepoPRAction.WRITE, RepoPRAction.COMMIT, RepoPRAction.PUSH):
        assert action.value in scope.allowed_operations
    assert not any(item.get("event_type") in poller.REVIEW_NEEDED_EVENT_TYPES
                   for item in events)


def test_two_comments_share_one_turn_and_cursor_survives_restart(monkeypatch, tmp_path, capsys):
    items = [_feedback("issue_comment", id=101), _feedback("issue_comment", id=102),
             _feedback("pr_review_comment", id=201), _feedback("pr_review", id=301)]
    first = _drive(monkeypatch, tmp_path, capsys, items)
    [event] = [item for item in first if "prompt" in item]
    for id in (101, 102, 201, 301):
        assert f"Fix README {id}" in event["prompt"]
    assert len([item for item in first if "prompt" in item]) == 1
    assert set(json.loads(poller.CURSOR_FILE.read_text())["pr_feedback_seen"][REPO]) == {
        "issue_comment:101", "issue_comment:102", "pr_review_comment:201", "pr_review:301",
    }
    second = _drive(monkeypatch, tmp_path, capsys, items)
    assert not any("prompt" in item for item in second)


@pytest.mark.parametrize("kind", ["issue_comment", "pr_review_comment", "pr_review"])
def test_outsider_on_own_pr_gets_only_notice(monkeypatch, tmp_path, capsys, kind):
    events = _drive(monkeypatch, tmp_path, capsys, [_feedback(kind, author="outsider")])
    assert not any("prompt" in item for item in events)
    assert [item["signal"] for item in events] == ["github_content_withheld"]
    assert "Fix README" not in str(events)


@pytest.mark.parametrize("kind", ["issue_comment", "pr_review_comment", "pr_review"])
def test_bot_own_feedback_does_not_wake_it(monkeypatch, tmp_path, capsys, kind):
    events = _drive(monkeypatch, tmp_path, capsys, [_feedback(kind, author=BOT)])
    assert events == []


@pytest.mark.parametrize("kind", ["issue_comment", "pr_review_comment", "pr_review"])
def test_closed_pr_comment_is_ignored(monkeypatch, tmp_path, capsys, kind):
    assert _drive(monkeypatch, tmp_path, capsys, [_feedback(kind)],
                  pr=_pr(state="closed")) == []


def test_other_authors_pr_comment_still_requests_review(monkeypatch, tmp_path, capsys):
    events = _drive(monkeypatch, tmp_path, capsys, [_feedback("issue_comment")],
                    pr=_pr(author="collaborator"))
    [event] = [item for item in events if "prompt" in item]
    assert event["event_type"] == "issue_comment"
    assert "New comment" in event["prompt"]
    assert "Trusted feedback on your open PR" not in event["prompt"]


def test_requested_reviewer_on_own_pr_never_starts_review(monkeypatch, tmp_path, capsys):
    pr = _pr()
    pr["requested_reviewers"] = [{"login": BOT}]
    events = _drive(monkeypatch, tmp_path, capsys, [], pr=pr)
    assert not any("prompt" in item for item in events)


def test_empty_commented_review_on_own_pr_is_not_actionable(monkeypatch, tmp_path, capsys):
    review = _feedback("pr_review")
    review["body"] = "  "
    assert not any("prompt" in item for item in _drive(
        monkeypatch, tmp_path, capsys, [review],
    ))


def test_review_choke_point_rejects_own_pr_even_with_no_prior_review(monkeypatch, capsys):
    monkeypatch.setattr(poller, "_gh_api", lambda endpoint, token: _pr())
    assert poller._emit_pr_review_needed(
        "review this", token="token", reviewer=BOT, event_type="issue_comment",
        repo=REPO, number=372, author="collaborator",
    ) is False
    assert capsys.readouterr().out == ""


def test_forked_own_pr_cannot_get_a_push_scope(monkeypatch, tmp_path, capsys):
    pr = _pr()
    pr["head"]["repo"]["full_name"] = "other/repo"
    assert not any("prompt" in item for item in _drive(
        monkeypatch, tmp_path, capsys, [_feedback("issue_comment")], pr=pr,
    ))


def test_missing_feedback_id_cannot_enter_remediation():
    pending = {}
    poller._queue_own_feedback(
        pending, set(), REPO, BOT, _pr(), _feedback("issue_comment", id=None),
        "issue_comment", "fix README",
    )
    assert pending == {}
