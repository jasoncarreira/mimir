from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json

from github_poller_test_support import poller
import pytest


SINCE = "2026-08-16T10:00:00Z"
HEAD = "a" * 40


@pytest.fixture
def captured_emits(monkeypatch: pytest.MonkeyPatch, tmp_path) -> list[dict]:
    events: list[dict] = []
    monkeypatch.setattr(poller, "STATE_DIR", tmp_path)
    monkeypatch.setattr(
        poller, "_emit", lambda _prompt, **extras: events.append(extras),
    )
    return events


def _pr(author: str = "mimir-bot", *, state: str = "open", head: str = HEAD) -> dict:
    return {
        "number": 42,
        "state": state,
        "merged": False,
        "merged_at": None,
        "title": "Fix CI",
        "html_url": "https://github.com/o/r/pull/42",
        "user": {"login": author},
        "head": {"sha": head, "ref": "worklink/42", "repo": {"full_name": "o/r"}},
        "base": {"sha": "b" * 40, "ref": "main"},
    }


def _check(conclusion: str = "failure", *, completed_at: str = "2026-08-16T10:01:00Z") -> dict:
    return {
        "id": 99,
        "name": "tests",
        "status": "completed",
        "conclusion": conclusion,
        "completed_at": completed_at,
        "html_url": "https://github.com/o/r/runs/99",
        "details_url": "https://github.com/o/r/runs/99/logs",
        "external_id": "job-99",
    }


def _api(pr: dict, checks: list[dict], runs: list[dict] | None = None,
         *, reviews: list[dict] | None = None, timeline: list[dict] | None = None):
    def fake(endpoint: str, _token: str):
        if endpoint.startswith("repos/o/r/pulls?state=open"):
            return [pr]
        if endpoint == "repos/o/r/pulls/42":
            return pr
        if endpoint == "repos/o/r/pulls/42/reviews":
            return reviews or []
        if endpoint == "repos/o/r/issues/42/timeline?per_page=100":
            return timeline or []
        if endpoint == f"repos/o/r/commits/{pr['head']['sha']}/check-runs?per_page=100":
            return {"check_runs": checks}
        if endpoint == f"repos/o/r/actions/runs?head_sha={pr['head']['sha']}&per_page=100":
            return {"workflow_runs": runs or [], "total_count": len(runs or [])}
        raise AssertionError(endpoint)

    return fake


def _deferred_review(**changes):
    return {
        "user": {"login": "mimir-bot"}, "commit_id": HEAD,
        "state": "COMMENTED", "submitted_at": SINCE, **changes,
    }


@pytest.mark.parametrize("conclusion", ["success", "failure", "neutral", "skipped", None])
@pytest.mark.parametrize("request_cleared", [False, True])
def test_deferred_review_resumes_once_when_checks_conclude(
    monkeypatch, tmp_path, capsys, conclusion, request_cleared,
):
    monkeypatch.setattr(poller, "STATE_DIR", tmp_path)
    pr = _pr("contributor")
    pr["requested_reviewers"] = [] if request_cleared else [{"login": "mimir-bot"}]
    checks = [dict(_check(), status="in_progress", conclusion=None)]
    timeline = [{"event": "review_requested", "requested_reviewer": {"login": "mimir-bot"},
                 "created_at": "2026-08-16T09:00:00Z"}]
    monkeypatch.setattr(poller, "_gh_api", _api(
        pr, checks, reviews=[_deferred_review()], timeline=timeline,
    ))
    _, cursor = poller._check_pr_ci_failures("o/r", SINCE, "token", "mimir-bot", {})
    assert capsys.readouterr().out == ""
    assert "42:review_ci" not in cursor

    checks[:] = [_check(conclusion)] if conclusion else []
    _, cursor = poller._check_pr_ci_failures("o/r", SINCE, "token", "mimir-bot", cursor)
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    prompts = [e for e in events if e.get("reason") == "checks_concluded"]
    assert len(prompts) == 1
    event = prompts[0]
    assert event["event_type"] == "pr_review_requested"
    assert "prompt" in event and "signal" not in event
    assert "Checks concluded" in event["prompt"]
    assert "complete your deferred COMMENTED review" in event["prompt"]
    assert event["head_sha"] == HEAD
    assert event["requested_reviewer"] == "mimir-bot"
    assert event["dedup_scope"] == "head_sha,reviewer"
    assert cursor["42:review_ci"]["completed_reviews"] == [event["delivery_key"]]
    for hours in (1, 24):
        poller._check_pr_ci_failures(
            "o/r", SINCE, "token", "mimir-bot", json.loads(json.dumps(cursor)),
            now=datetime(2026, 8, 16, 10, 2, tzinfo=timezone.utc) + timedelta(hours=hours),
        )
        assert "checks_concluded" not in capsys.readouterr().out


@pytest.mark.parametrize("state", ["APPROVED", "CHANGES_REQUESTED"])
@pytest.mark.parametrize("running", [False, True])
def test_terminal_review_does_not_resume_for_ci(monkeypatch, captured_emits, state, running):
    pr = _pr("contributor")
    pr["requested_reviewers"] = [{"login": "mimir-bot"}]
    check = dict(_check("success"), status="in_progress") if running else _check("success")
    monkeypatch.setattr(poller, "_gh_api", _api(
        pr, [check], reviews=[_deferred_review(), _deferred_review(
            state=state, submitted_at="2026-08-16T10:01:00Z",
        )],
    ))
    count, _ = poller._check_pr_ci_failures("o/r", SINCE, "token", "mimir-bot", {})
    assert count == 0 and captured_emits == []


def test_completion_dedup_is_per_head_and_reviewer(monkeypatch, captured_emits):
    pr = _pr("contributor")
    reviews = [_deferred_review()]
    api = _api(pr, [_check("success")], reviews=reviews)
    monkeypatch.setattr(poller, "_gh_api", api)
    cursor = {}
    for head, reviewer in [(HEAD, "mimir-bot"), ("c" * 40, "mimir-bot"),
                           ("c" * 40, "another-bot"), (HEAD, "mimir-bot")]:
        pr["head"]["sha"] = head
        pr["requested_reviewers"] = [{"login": reviewer}]
        reviews[:] = [_deferred_review(commit_id=head, user={"login": reviewer})]
        _, cursor = poller._check_pr_ci_failures("o/r", SINCE, "token", reviewer, cursor)
    assert len(captured_emits) == 3
    assert len({e["delivery_key"] for e in captured_emits}) == 3
    assert len(cursor["42:review_ci"]["completed_reviews"]) == 3

    # An incomplete next poll must not forget that these heads were delivered.
    monkeypatch.setattr(poller, "_gh_api", lambda endpoint, token: (
        None if "/check-runs?" in endpoint else api(endpoint, token)
    ))
    _, preserved = poller._check_pr_ci_failures("o/r", SINCE, "token", "mimir-bot", cursor)
    assert preserved["42:review_ci"] == cursor["42:review_ci"]
    monkeypatch.setattr(poller, "_gh_api", api)
    poller._check_pr_ci_failures("o/r", SINCE, "token", "mimir-bot", preserved)
    assert len(captured_emits) == 3


@pytest.mark.parametrize("reason", ["no_request", "wrong_head", "wrong_reviewer", "workflow_running",
                                         "check_unknown", "malformed_check", "incomplete_checks"])
def test_deferred_review_completion_requires_evidence(monkeypatch, captured_emits, reason):
    pr = _pr("contributor")
    pr["requested_reviewers"] = [] if reason == "no_request" else [{"login": "mimir-bot"}]
    review = _deferred_review()
    if reason == "wrong_head":
        review["commit_id"] = "c" * 40
    if reason == "wrong_reviewer":
        review["user"] = {"login": "someone-else"}
    checks = [_check("success")]
    if reason == "check_unknown":
        checks[0]["conclusion"] = None
    if reason == "malformed_check":
        checks = [None]
    runs = [_run(status="in_progress", conclusion=None)] if reason == "workflow_running" else []
    api = _api(pr, checks, runs, reviews=[review])
    monkeypatch.setattr(poller, "_gh_api", lambda endpoint, token: (
        {"check_runs": checks, "total_count": 2}
        if reason == "incomplete_checks" and "/check-runs?" in endpoint else api(endpoint, token)
    ))
    count, _ = poller._check_pr_ci_failures("o/r", SINCE, "token", "mimir-bot", {})
    assert count == 0 and captured_emits == []


def test_owned_failure_emits_bound_remediation(monkeypatch, captured_emits):
    monkeypatch.setattr(poller, "_gh_api", _api(_pr(), [_check()]))

    count, cursor = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", {},
    )

    assert count == 1
    event = captured_emits[0]
    assert event["event_type"] == "pr_ci_failure"
    assert event["repo"] == "o/r"
    assert event["number"] == 42
    assert event["head_sha"] == HEAD
    assert event["author"] == "mimir-bot"
    assert event["failed_checks"] == [{
        "id": 99,
        "name": "tests",
        "conclusion": "failure",
        "url": "https://github.com/o/r/runs/99",
        "details_url": "https://github.com/o/r/runs/99/logs",
        "external_id": "job-99",
    }]
    assert cursor["42"]["delivery_key"] == event["delivery_key"]


def test_external_failure_routes_to_signal_only(monkeypatch, captured_emits):
    signals: list[tuple[str, dict]] = []
    monkeypatch.setattr(poller, "_gh_api", _api(_pr("contributor"), [_check()]))
    monkeypatch.setattr(
        poller, "_emit_signal", lambda signal, **extra: signals.append((signal, extra)),
    )

    count, _cursor = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", {},
    )

    assert count == 1
    assert captured_emits == []
    assert signals[0][0] == "pr_ci_failure_external"
    assert signals[0][1]["author"] == "contributor"


def test_same_failure_is_deduped_during_overlapping_poll(monkeypatch, captured_emits):
    now = datetime(2026, 8, 16, 10, 2, tzinfo=timezone.utc)
    monkeypatch.setattr(poller, "_gh_api", _api(_pr(), [_check()]))
    count, cursor = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", {}, now=now,
    )
    assert count == 1

    count, cursor2 = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", cursor,
        now=now + timedelta(seconds=1),
    )
    assert count == 0
    assert cursor2["42"] == cursor["42"]
    assert len(captured_emits) == 1


def test_unacknowledged_delivery_retries_but_receipt_dedupes(monkeypatch, captured_emits):
    now = datetime(2026, 8, 16, 10, 2, tzinfo=timezone.utc)
    monkeypatch.setattr(poller, "_gh_api", _api(_pr(), [_check()]))
    _, cursor = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", {}, now=now,
    )
    count, retried = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", cursor,
        now=now + poller.CI_DELIVERY_RETRY_INTERVAL,
    )
    assert count == 1

    monkeypatch.setattr(poller, "_delivery_receipt_exists", lambda _key: True)
    count, _ = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", retried,
        now=now + timedelta(hours=1),
    )
    assert count == 0
    assert len(captured_emits) == 2


def test_closed_and_green_races_terminate_and_clear_cursor(monkeypatch, captured_emits):
    prior = {"42": {"head_sha": HEAD, "delivery_key": "old", "emitted_at": SINCE}}
    closed = _pr(state="closed")
    monkeypatch.setattr(poller, "_gh_api", _api(closed, [_check()]))
    count, cursor = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", prior,
    )
    assert count == 0
    assert "42" not in cursor

    monkeypatch.setattr(poller, "_gh_api", _api(_pr(), [_check("success")]))
    count, cursor = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", prior,
    )
    assert count == 0
    assert "42" not in cursor
    assert captured_emits == []


def test_check_api_failure_preserves_cursor(monkeypatch, captured_emits):
    prior = {"42": {"head_sha": HEAD, "delivery_key": "old", "emitted_at": SINCE}}

    def fake(endpoint: str, _token: str):
        if endpoint.startswith("repos/o/r/pulls?state=open"):
            return [_pr()]
        if endpoint == "repos/o/r/pulls/42":
            return _pr()
        return None

    monkeypatch.setattr(poller, "_gh_api", fake)
    count, cursor = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", prior,
    )
    assert count == 0
    assert cursor["42"] == prior["42"]
    assert cursor["_last_checked"] == SINCE
    assert captured_emits == []


def test_check_api_failure_does_not_advance_new_failure_window(
    monkeypatch, captured_emits,
):
    def failed_api(endpoint: str, _token: str):
        if endpoint.startswith("repos/o/r/pulls?state=open"):
            return [_pr()]
        if endpoint == "repos/o/r/pulls/42":
            return _pr()
        return None

    monkeypatch.setattr(poller, "_gh_api", failed_api)
    count, cursor = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", {},
    )
    assert count == 0
    assert cursor == {"_last_checked": SINCE}

    monkeypatch.setattr(poller, "_gh_api", _api(_pr(), [_check()]))
    count, _ = poller._check_pr_ci_failures(
        "o/r", "2026-08-16T10:10:00Z", "token", "mimir-bot", cursor,
    )
    assert count == 1
    assert len(captured_emits) == 1


def test_old_red_head_is_baselined_without_later_retry(monkeypatch, captured_emits):
    old_check = _check(completed_at="2026-08-16T09:00:00Z")
    monkeypatch.setattr(poller, "_gh_api", _api(_pr(), [old_check]))
    first_now = datetime(2026, 8, 16, 10, 2, tzinfo=timezone.utc)

    count, cursor = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", {}, now=first_now,
    )
    assert count == 0
    assert cursor["42"]["baseline"] is True

    count, _ = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", cursor,
        now=first_now + timedelta(hours=1),
    )
    assert count == 0
    assert captured_emits == []


# --- chainlink #1433: per-tick reconciliation bound -------------------------


def _pr_n(number: int, *, author: str = "mimir-bot") -> dict:
    pr = _pr(author=author, head=f"{number:040d}")
    pr["number"] = number
    pr["html_url"] = f"https://github.com/o/r/pull/{number}"
    pr["head"]["ref"] = f"worklink/{number}"
    return pr


def _multi_api(prs: list[dict], checks: list[dict]):
    by_number = {p["number"]: p for p in prs}

    def fake(endpoint: str, _token: str):
        if endpoint.startswith("repos/o/r/pulls?state=open"):
            return prs
        if "/check-runs" in endpoint:
            return {"check_runs": checks}
        if "/actions/runs?head_sha=" in endpoint:
            return {"workflow_runs": [], "total_count": 0}
        if endpoint.startswith("repos/o/r/pulls/"):
            return by_number[int(endpoint.rsplit("/", 1)[1])]
        raise AssertionError(endpoint)

    return fake


def test_truncated_ci_pass_holds_the_window_so_nothing_is_lost(
    monkeypatch, captured_emits,
):
    """Truncation must go through the same no-advance path as an API failure:
    an incomplete collection keeps ``_last_checked`` at ``window_since``, so the
    PRs this tick skipped are re-examined next tick instead of being dropped."""
    numbers = [41, 42, 43, 44, 45]
    monkeypatch.setattr(poller, "_gh_api", _multi_api([_pr_n(n) for n in numbers], [_check()]))

    spent = poller.TickBudget(deadline_seconds=0.0)
    count, cursor = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", {}, tick_budget=spent,
    )
    assert count == poller.PR_RECONCILE_MIN_PER_PASS
    assert spent.truncated == {
        "ci_failures": len(numbers) - poller.PR_RECONCILE_MIN_PER_PASS,
    }
    assert cursor["_last_checked"] == SINCE  # window pinned, not advanced

    # A later tick with budget left picks up exactly the skipped PRs; the two
    # already delivered stay deduped by their cursor entries.
    count2, _ = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", cursor,
        tick_budget=poller.TickBudget(deadline_seconds=600.0),
    )
    assert count2 == len(numbers) - poller.PR_RECONCILE_MIN_PER_PASS
    assert sorted(e["number"] for e in captured_emits) == numbers


def test_unbudgeted_ci_pass_reconciles_every_pr(monkeypatch, captured_emits):
    numbers = [41, 42, 43]
    monkeypatch.setattr(poller, "_gh_api", _multi_api([_pr_n(n) for n in numbers], [_check()]))
    fresh = poller.TickBudget(deadline_seconds=600.0)
    count, cursor = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", {}, tick_budget=fresh,
    )
    assert count == len(numbers)
    assert fresh.truncated == {}
    assert cursor["_last_checked"] != SINCE  # complete collection advances


def _run(run_id=50, *, status="completed", conclusion="cancelled", **extra):
    return {
        "id": run_id, "head_sha": HEAD, "workflow_id": 7,
        "check_suite_id": run_id + 1000,
        "status": status, "conclusion": conclusion,
        "created_at": "2026-08-16T10:00:00Z",
        "updated_at": "2026-08-16T10:01:00Z", **extra,
    }


@pytest.mark.parametrize("author", ["mimir-bot", "contributor"])
@pytest.mark.parametrize("outcome", ["UNKNOWN", "SUPERSEDED", "OVERTAKEN_BY_SUCCESS"])
@pytest.mark.parametrize("binding", ["no_checks", "details_url", "html_url", "check_suite"])
def test_cancelled_run_never_authorizes_remediation(
    monkeypatch, tmp_path, capsys, author, outcome, binding,
):
    cancelled = _run()
    runs = [cancelled]
    if outcome != "UNKNOWN":
        runs.append(_run(
            51, status="in_progress" if outcome == "SUPERSEDED" else "completed",
            conclusion=None if outcome == "SUPERSEDED" else "success",
            workflow_id=7 if outcome == "SUPERSEDED" else 8,
        ))
    checks = []
    if binding != "no_checks":
        check = _check("failure")  # A failed job in a cancelled run is NOT authority.
        if binding == "check_suite":
            check["check_suite"] = {"id": 1050}
        else:
            check[binding] = "https://github.com/o/r/actions/runs/50/job/101"
        checks = [check, dict(check, id=100, conclusion="cancelled")]
    monkeypatch.setattr(poller, "STATE_DIR", tmp_path)
    monkeypatch.setattr(poller, "_gh_api", _api(_pr(author), checks, runs))
    monkeypatch.setattr(poller, "capture_job_log", lambda *a, **kw: pytest.fail("log capture"))
    count, cursor = poller._check_pr_ci_failures("o/r", SINCE, "token", "mimir-bot", {})
    output = capsys.readouterr().out
    if outcome != "UNKNOWN":
        assert count == 0 and output == ""
        assert set(cursor) == {"_last_checked"}
        return
    assert count == 1
    event = json.loads(output)
    # Exact keys make adding any mutation-bearing field a regression.
    assert set(event) == {
        "poller", "source_platform", "prompt", "subject_type", "event_type",
        "repo", "number", "url", "head_sha", "cancelled_run_ids", "delivery_key",
    }
    assert event["event_type"] == "pr_ci_attention"
    assert event["cancelled_run_ids"] == [50]
    assert event["head_sha"] == HEAD
    assert "does not authorize remediation" in event["prompt"]
    assert "https://github.com/o/r/actions/runs/50" in event["prompt"]
    assert "CI log limitation:" in event["prompt"]
    assert cursor["42:attention"]["delivery_key"] == event["delivery_key"]


def test_attention_and_independent_failure_have_separate_deliveries(monkeypatch, captured_emits):
    cancelled_check = dict(
        _check(), details_url="https://github.com/o/r/actions/runs/50/job/101",
    )
    legitimate = dict(_check(), id=102, name="legitimate")
    monkeypatch.setattr(poller, "_gh_api", _api(_pr(), [cancelled_check, legitimate], [_run()]))
    count, cursor = poller._check_pr_ci_failures("o/r", SINCE, "token", "mimir-bot", {})
    assert count == 2
    failure, attention = captured_emits
    assert failure["event_type"] == "pr_ci_failure"
    assert [check["id"] for check in failure["failed_checks"]] == [102]
    assert attention["event_type"] == "pr_ci_attention"
    assert "failed_checks" not in attention and "head_ref" not in attention
    assert cursor["42"]["delivery_key"] != cursor["42:attention"]["delivery_key"]


def test_attention_retries_claims_receipts_and_resolution(monkeypatch, captured_emits, tmp_path):
    now = datetime(2026, 8, 16, 10, 2, tzinfo=timezone.utc)
    runs = [_run()]
    monkeypatch.setattr(poller, "_gh_api", _api(_pr(), [], runs))
    _, cursor = poller._check_pr_ci_failures("o/r", SINCE, "token", "mimir-bot", {}, now=now)
    key = cursor["42:attention"]["delivery_key"]
    # Even an overlapping process with an old cursor respects the atomic claim.
    count, _ = poller._check_pr_ci_failures("o/r", SINCE, "token", "mimir-bot", {}, now=now)
    assert count == 0
    count, _ = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", cursor, now=now + timedelta(minutes=1),
    )
    assert count == 0
    count, cursor = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", cursor,
        now=now + poller.CI_DELIVERY_RETRY_INTERVAL,
    )
    assert count == 1
    assert [e["delivery_key"] for e in captured_emits] == [key, key]
    digest = hashlib.sha256(key.encode()).hexdigest()
    receipt = tmp_path / poller._DELIVERY_RECEIPTS_DIR / digest
    receipt.parent.mkdir()
    receipt.touch()
    count, cursor = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", cursor, now=now + timedelta(hours=1),
    )
    assert count == 0 and receipt.exists()
    runs.append(_run(51, conclusion="success"))
    count, cursor = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", cursor, now=now + timedelta(hours=2),
    )
    assert count == 0 and "42:attention" not in cursor
    assert not receipt.exists()
    assert not (tmp_path / poller._DELIVERY_CLAIMS_DIR / digest).exists()


@pytest.mark.parametrize("listing", [
    None, poller._GH_API_BUDGET_REFUSED, {},
    {"workflow_runs": [_run()], "total_count": 2},
    {"workflow_runs": [None], "total_count": 1},
])
@pytest.mark.parametrize("existing", [False, True])
def test_run_listing_failure_preserves_both_cursors_and_window(
    monkeypatch, captured_emits, listing, existing,
):
    prior = {"_last_checked": SINCE}
    if existing:
        prior.update({"42": {"delivery_key": "failure"}, "42:attention": {"delivery_key": "attention"}})
    api = _api(_pr(), [_check()], [_run()])
    monkeypatch.setattr(poller, "_gh_api", lambda endpoint, token: (
        listing if "/actions/runs?" in endpoint else api(endpoint, token)
    ))
    monkeypatch.setattr(poller, "_remove_delivery_artifacts", lambda *a: pytest.fail("discarded receipt"))
    count, cursor = poller._check_pr_ci_failures(
        "o/r", "2026-08-16T10:10:00Z", "token", "mimir-bot", prior,
    )
    assert count == 0 and cursor == prior and captured_emits == []


@pytest.mark.parametrize("change", [
    {"head_sha": "c" * 40}, {"status": "in_progress"}, {"conclusion": "success"},
])
def test_only_completed_cancelled_current_head_runs_are_classified(monkeypatch, captured_emits, change):
    monkeypatch.setattr(poller, "_gh_api", _api(_pr(), [], [_run(**change)]))
    monkeypatch.setattr(poller, "classify_cancelled_run", lambda *a: pytest.fail("ineligible run"))
    count, _ = poller._check_pr_ci_failures("o/r", SINCE, "token", "mimir-bot", {})
    assert count == 0 and captured_emits == []


def test_run_listing_recovers_without_losing_attention(monkeypatch, captured_emits):
    api = _api(_pr(), [], [_run()])
    monkeypatch.setattr(poller, "_gh_api", lambda endpoint, token: (
        None if "/actions/runs?" in endpoint else api(endpoint, token)
    ))
    count, cursor = poller._check_pr_ci_failures("o/r", SINCE, "token", "mimir-bot", {})
    assert count == 0 and cursor == {"_last_checked": SINCE}
    monkeypatch.setattr(poller, "_gh_api", api)
    count, _ = poller._check_pr_ci_failures(
        "o/r", "2026-08-16T10:10:00Z", "token", "mimir-bot", cursor,
    )
    assert count == 1 and captured_emits[0]["event_type"] == "pr_ci_attention"


def test_old_cancelled_run_is_baselined_without_retry(monkeypatch, captured_emits):
    monkeypatch.setattr(poller, "_gh_api", _api(
        _pr(), [], [_run(updated_at="2026-08-16T09:00:00Z")],
    ))
    now = datetime(2026, 8, 16, 10, 2, tzinfo=timezone.utc)
    count, cursor = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", {}, now=now,
    )
    assert count == 0 and cursor["42:attention"]["baseline"] is True
    count, _ = poller._check_pr_ci_failures(
        "o/r", SINCE, "token", "mimir-bot", cursor, now=now + timedelta(hours=1),
    )
    assert count == 0 and captured_emits == []
