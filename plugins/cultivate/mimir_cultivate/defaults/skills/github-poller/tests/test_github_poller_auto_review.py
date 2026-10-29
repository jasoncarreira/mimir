"""Repository auto-review mode gates only unsolicited PR review turns."""
from __future__ import annotations

import json

import pytest
import yaml

from github_poller_test_support import poller
from mimir_cultivate.repository import RepositoryInventory


REPO = "o/r"
BOT = "bot"
SINCE = "2026-10-27T18:00:00Z"


def _pr(author="alice", *, requested=False, sha="new"):
    return {
        "number": 42, "title": "A PR", "state": "open",
        "created_at": "2026-10-27T18:15:00Z",
        "html_url": f"https://github.com/{REPO}/pull/42",
        "user": {"login": author},
        "head": {"sha": sha, "ref": "feature", "repo": {"full_name": REPO}},
        "base": {"sha": "base", "ref": "main"},
        "requested_reviewers": [{"login": BOT}] if requested else [],
    }


def _drive(monkeypatch, tmp_path, capsys, *, mode, pr, heads=None, comment=False, ci_failure=False):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    inventory_path = home / "repositories.yaml"
    inventory_path.write_text(yaml.safe_dump({"repositories": [{
        "slug": REPO, "root": str(tmp_path / "checkout"), "mode": "rw",
        "origin": f"https://github.com/{REPO}.git", "base_branch": "main",
        **({"auto_review": mode} if mode is not None else {}),
    }]}))
    assert RepositoryInventory.load(inventory_path).repository(REPO).auto_review == (mode or "all")
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.setenv("GITHUB_REPOS", REPO)
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", BOT)
    monkeypatch.setattr(poller, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(poller, "CURSOR_FILE", tmp_path / "state" / "cursor.json")
    monkeypatch.setattr(poller, "_PROCESS_START", poller.time.monotonic())
    monkeypatch.setattr(poller, "_resolve_token", lambda: "token")
    monkeypatch.setattr(poller, "_pr_author_is_trusted",
                        lambda *args, **kw: pr["user"]["login"] != "outsider")
    monkeypatch.setattr(poller, "_pr_author_trust",
                        lambda *args, **kw: (pr["user"]["login"] != "outsider", None))
    monkeypatch.setattr(poller, "_github_author_is_trusted",
                        lambda repo, author, token: author != "outsider")
    if not poller.CURSOR_FILE.exists():
        poller._save_cursor({"last_checked": SINCE, "pr_heads": {REPO: heads or {}}})

    def api(endpoint, token):
        if endpoint.startswith(f"repos/{REPO}/pulls?state=open"):
            return [pr]
        if endpoint == f"repos/{REPO}/pulls/42":
            return pr
        if endpoint.endswith("/pulls/42/reviews"):
            return []
        if "/issues/comments?" in endpoint:
            return ([{"id": 123, "user": {"login": "alice"},
                     "body": "@bot please look", "created_at": "2026-10-27T18:20:00Z",
                     "html_url": pr["html_url"] + "#issuecomment-123",
                     "issue_url": f"https://api.github.com/repos/{REPO}/issues/42"}]
                    if comment else [])
        if endpoint.endswith("/issues/42"):
            return {"pull_request": {"url": pr["html_url"]}, "state": "open"}
        if endpoint.endswith("/check-runs?per_page=100"):
            return {"check_runs": [{"id": 99, "name": "tests", "status": "completed",
                                    "conclusion": "failure", "completed_at": "2026-10-27T18:20:00Z",
                                    "html_url": f"https://github.com/{REPO}/runs/99"}], "total_count": 1}
        if endpoint.endswith("/status"):
            return {"statuses": []}
        if "/actions/runs?" in endpoint:
            return {"workflow_runs": [], "total_count": 0}
        if "compare/" in endpoint:
            return None
        return []

    monkeypatch.setattr(poller, "_gh_api", api)
    # Other reconciliation passes are unrelated to automatic review; keep the
    # real issue-comment pass to prove a mention still creates a turn.
    for name in ("_check_pr_review_comments", "_check_pr_reviews"):
        monkeypatch.setattr(poller, name, lambda *a, **kw: 0)
    for name in ("_check_own_changes_requested", "_check_own_mergeability"):
        monkeypatch.setattr(poller, name, lambda *a, **kw: (0, {}))
    if not ci_failure:
        monkeypatch.setattr(poller, "_check_pr_ci_failures", lambda *a, **kw: (0, {}))
    poller.main()
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    return events, json.loads(poller.CURSOR_FILE.read_text())


def test_requested_only_suppresses_open_and_push_but_advances_head(monkeypatch, tmp_path, capsys):
    pr = _pr()
    events, cursor = _drive(monkeypatch, tmp_path, capsys, mode="requested-only", pr=pr,
                            heads={"42": "old"})
    assert events == []
    assert cursor["pr_heads"][REPO] == {"42": "new"}
    assert cursor["last_checked"] != SINCE
    events, _ = _drive(monkeypatch, tmp_path, capsys, mode="all", pr=pr)
    assert events == []  # Switching modes cannot replay the old push.
    pr["head"]["sha"] = "later"
    events, _ = _drive(monkeypatch, tmp_path, capsys, mode="all", pr=pr)
    assert [e["event_type"] for e in events] == ["pr_synchronize"]


def test_requested_only_request_and_later_push(monkeypatch, tmp_path, capsys):
    pr = _pr(requested=True)
    events, cursor = _drive(monkeypatch, tmp_path, capsys, mode="requested-only", pr=pr,
                            heads={"42": "new"})
    assert [e["event_type"] for e in events] == ["pr_review_requested"]
    assert cursor["pr_review_requests"][REPO] == {"42": 1}
    pr["head"]["sha"] = "later"
    events, cursor = _drive(monkeypatch, tmp_path, capsys, mode="requested-only", pr=pr)
    assert [e["event_type"] for e in events] == ["pr_synchronize"]
    assert cursor["pr_heads"][REPO] == {"42": "later"}


def test_default_all_reviews_open_and_push(monkeypatch, tmp_path, capsys):
    events, _ = _drive(monkeypatch, tmp_path, capsys, mode=None, pr=_pr(), heads={"42": "old"})
    assert [e["event_type"] for e in events] == ["pr_opened", "pr_synchronize"]


def test_requested_only_preserves_mentions_and_untrusted_notices(monkeypatch, tmp_path, capsys):
    events, _ = _drive(monkeypatch, tmp_path, capsys, mode="requested-only", pr=_pr(),
                       comment=True)
    assert [e["event_type"] for e in events] == ["issue_comment"]


def test_requested_only_preserves_untrusted_notices(monkeypatch, tmp_path, capsys):
    events, _ = _drive(monkeypatch, tmp_path, capsys, mode="requested-only", pr=_pr("outsider"))
    assert [e["signal"] for e in events] == ["pr_auto_review_skipped_untrusted_author"]


def test_requested_only_preserves_own_pr_feedback(monkeypatch, tmp_path, capsys):
    events, _ = _drive(monkeypatch, tmp_path, capsys, mode="requested-only", pr=_pr(BOT),
                       comment=True)
    assert [e["event_type"] for e in events] == ["issue_comment"]
    assert events[0]["author"] == BOT
    assert "push the fixes to the PR branch" in events[0]["prompt"]


def test_requested_only_preserves_own_pr_ci_remediation(monkeypatch, tmp_path, capsys):
    events, _ = _drive(monkeypatch, tmp_path, capsys, mode="requested-only", pr=_pr(BOT),
                       ci_failure=True)
    assert [e["event_type"] for e in events] == ["pr_ci_failure"]
    assert events[0]["author"] == BOT


@pytest.mark.parametrize("bad", ["sometimes", None, True, 1])
def test_inventory_rejects_invalid_auto_review(tmp_path, bad):
    path = tmp_path / "repositories.yaml"
    path.write_text(yaml.safe_dump({"repositories": [{
        "slug": REPO, "root": str(tmp_path / "checkout"), "mode": "rw",
        "origin": f"https://github.com/{REPO}.git", "base_branch": "main",
        "auto_review": bad,
    }]}))
    with pytest.raises(ValueError, match="auto_review must be"):
        RepositoryInventory.load(path)
