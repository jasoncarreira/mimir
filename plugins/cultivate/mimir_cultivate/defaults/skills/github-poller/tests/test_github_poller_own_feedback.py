"""Trusted comments on the bot's own PRs start scoped remediation, once per id."""
from __future__ import annotations

import json
import shlex
import sys

from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.runtime import Runtime

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
    assert "First call repo_checkout(" in event["prompt"]
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


@pytest.mark.parametrize("field,event_type", [
    ("author", "pr_opened"), ("pr_author", "pr_synchronize"),
])
def test_noncomment_self_review_is_stopped_before_http(
    monkeypatch, capsys, field, event_type,
):
    def unexpected_http(*args, **kwargs):
        pytest.fail("non-comment self-review must be rejected without lookup")

    monkeypatch.setattr(poller, "_gh_api", unexpected_http)
    assert poller._emit_pr_review_needed(
        "review this", token="token", reviewer=BOT,
        current_head_reviewed=False, event_type=event_type,
        repo=REPO, number=372, **{field: BOT},
    ) is False
    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["issue_comment", "pr_review_comment", "pr_review"])
async def test_emitted_json_through_framework_attestation_and_checkout_grant(
    monkeypatch, tmp_path, capsys, kind,
):
    """Real fire -> HTTP actor attestation -> ingress scope -> hard tool boundary.

    Neither actor/trust helpers nor scope issuance/grant checks are replaced.
    The local repository supplies real Git objects for the checkout lifecycle.
    """
    from mimir import event_logger, pollers as framework
    from mimir.tools.budget_gate import BudgetGateMiddleware
    from mimir_cultivate.repository import create_pr_checkout_lease
    from tests.test_pr_checkout_lease import _repo_and_scope

    monkeypatch.setattr(
        event_logger, "_logger", event_logger.EventLogger(tmp_path / "events.jsonl", "test"),
    )
    source, fixture_scope = _repo_and_scope(tmp_path)
    pr = _pr()
    pr["head"]["ref"] = fixture_scope.destination_ref.removeprefix("refs/heads/")
    pr["head"]["sha"] = fixture_scope.observed_head_sha
    pr["base"]["sha"] = fixture_scope.observed_base_sha
    emitted = _drive(monkeypatch, tmp_path, capsys, [_feedback(kind)], pr=pr)
    [payload] = [item for item in emitted if "prompt" in item]
    assert "First call repo_checkout(" in payload["prompt"]
    assert payload["prompt"].index("repo_checkout(") < payload["prompt"].index("pr_comments")
    # Poller claims are deliberately false: only GitHub's response may restore them.
    payload["actor"] = "spoofed-actor"
    payload["author_is_trusted"] = False

    selected = load_plugins(enable="cultivate").policy
    monkeypatch.setattr(
        access_control, "_canonical_repo_binding_resolution",
        lambda repo: access_control.RepoBindingResolution(
            (str(source), fixture_scope.canonical_origin), (str(source),), 1,
        ),
    )
    lease_root = tmp_path / "leases"
    lease_root.mkdir()
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease_root))
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MIMIR_GITHUB_TRUSTED_BOT_LOGINS", "")
    api_calls = []
    content_endpoint = {
        "issue_comment": f"repos/{REPO}/issues/comments/101",
        "pr_review_comment": f"repos/{REPO}/pulls/comments/101",
        "pr_review": f"repos/{REPO}/pulls/372/reviews/101",
    }[kind]

    def http(endpoint, token, **kwargs):
        api_calls.append(endpoint)
        assert token == "test-server-credential"
        if endpoint == content_endpoint:
            return 200, {"user": {"login": "collaborator"}}
        if endpoint == f"repos/{REPO}/collaborators/collaborator":
            return 204, None
        pytest.fail(f"unexpected GitHub HTTP endpoint: {endpoint}")

    monkeypatch.setattr(framework, "_github_api_attestation", http)
    authority = access_control.build_trigger_service_principal(
        canonical="poller:github-activity", trigger="poller", profile="github",
        tier=access_control.CapabilityTier.CODE_EXECUTION,
        capabilities=("repo_checkout", "pr_comments", "repo_commit", "repo_push"),
        creation_path="test", policy=selected, poller_owner="cultivate",
    )
    script = tmp_path / "emit.py"
    captured = []

    async def enqueue(event, **kwargs):
        captured.append(event)
        return True

    async def fire(item, directory):
        # Replay the skill's actual JSON line through the real subprocess parser.
        script.write_text(f"print({json.dumps(item)!r})\n", encoding="utf-8")
        cfg = framework.PollerConfig(
            name="github-activity", command=f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}",
            cron="* * * * *", skill_dir=tmp_path, persist_dir=directory,
            env={"GITHUB_TOKEN": "test-server-credential"},
            trust_source="github", authority=authority,
        )
        assert await framework.run_poller(cfg, enqueue=enqueue) == 1
        event = captured.pop()
        [attested] = event.extra["items"]
        assert attested["actor"] == "collaborator"
        assert attested["author_is_trusted"] is True
        assert all(source.integrity == "trusted" for source in event.ifc_labels.sources)
        auth = access_control.create_auth_context(
            event, enforce=True, policy=selected, ifc_labels=event.ifc_labels,
        )
        scope = auth.repo_pr_action_scope
        assert scope is not None
        assert scope.provenance == RepoPRScopeProvenance.POLLER_PAYLOAD
        assert {action.value for action in (
            RepoPRAction.WRITE, RepoPRAction.COMMIT, RepoPRAction.PUSH,
        )} <= scope.allowed_operations
        return auth

    with policy_scope(selected):
        auth = await fire(payload, tmp_path / "new-fire")
        args = {"repository": REPO, "pull_request": 372}
        registry = access_control.ToolRegistry()
        boundary = BudgetGateMiddleware(selected)
        executed = []

        def comments(current_auth):
            request = ToolCallRequest(
                tool_call={"name": "pr_comments", "args": args,
                           "id": "comments", "type": "tool_call"},
                tool=selected.tools["pr_comments"].factory(), state=None,
                runtime=Runtime(context=current_auth),
            )
            return boundary.wrap_tool_call(request, lambda request: (
                executed.append("pr_comments")
                or ToolMessage(content="comments admitted", tool_call_id="comments")
            ))

        # The old emitter already supplied the same snapshot. Its first action
        # was a PR read, not checkout. A valid scope alone does not activate it.
        if kind in {"issue_comment", "pr_review_comment"}:
            legacy = dict(payload, prompt="New comment: Fix README 101")
            legacy.pop("actor")
            legacy.pop("author_is_trusted")
            legacy_auth = await fire(legacy, tmp_path / "legacy-fire")
            refusal = registry.authorize_tool(
                "pr_comments", legacy_auth, arguments=args, enforce=True, policy=selected,
            )
            assert not refusal.allowed and refusal.reason == "repo_pr_grant_denied"
            result = comments(legacy_auth)
            assert result.status == "error"
            assert "repo_pr_grant_denied" in str(result.content)
            assert "Call repo_checkout" in str(result.content)
            assert executed == []

        scope = auth.repo_pr_action_scope
        state = auth.repo_review_state
        assert access_control._valid_scoped_pr_grant(
            auth, "cultivate", RepoPRAction.CHECKOUT, selected.scoped_actions["cultivate"],
            scope, state, tool_name="repo_checkout",
        )
        # Do not hide the independent ingress-to-forge gap: the framework's
        # trusted channel source is not currently an allowed forge source.
        # The scoped grant exists, but production checkout still fails IFC.
        checkout = registry.authorize_tool(
            "repo_checkout", auth, arguments=args, enforce=True, policy=selected,
        )
        assert not checkout.allowed and checkout.reason == "ifc_label_blocked:forge"
        # Exercise the real post-checkout grant lifecycle separately. This is
        # not evidence that production repo_checkout was admitted above.
        token = access_control.consume_scoped_pr_checkout(auth, scope, "cultivate")
        assert token is not None
        lease = create_pr_checkout_lease(
            scope, owner=BOT, lease_root=lease_root, review_state=state,
        )
        try:
            assert access_control.observe_scoped_pr_checkout(
                auth, scope, state, token, "cultivate", selected.scoped_actions["cultivate"],
            )
            admitted = registry.authorize_tool(
                "pr_comments", auth, arguments=args, enforce=True, policy=selected,
            )
            assert admitted.allowed, admitted.reason
            result = comments(auth)
            assert result.status != "error", result.content
            assert executed == ["pr_comments"]
        finally:
            lease.revoke()
    expected_calls = [content_endpoint, f"repos/{REPO}/collaborators/collaborator"]
    assert api_calls == expected_calls * (1 if kind == "pr_review" else 2)
