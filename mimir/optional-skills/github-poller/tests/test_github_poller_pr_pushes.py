"""Tests for ``_check_pr_pushes`` — the pr_synchronize handler.

Mocks ``_gh_api`` to return canned PR-list responses and captures
``_emit`` calls via a monkeypatched stdout writer. Each test asserts
both the emit count and the returned ``new_pr_heads`` dict, since the
caller relies on the dict-replacement cleanup contract (closed/merged
PRs drop out because the new dict is rebuilt from the current
``state=open`` snapshot).
"""
from __future__ import annotations

import json

import pytest

import poller


def _pr(number: int, sha: str, login: str = "alice",
        title: str = "Some PR", url: str | None = None,
        requested_reviewers: list[str] | None = None) -> dict:
    return {
        "number": number,
        "title": title,
        "html_url": url or f"https://github.com/o/r/pull/{number}",
        "user": {"login": login},
        "head": {"sha": sha},
        "requested_reviewers": [
            {"login": r} for r in (requested_reviewers or [])
        ],
    }


@pytest.fixture
def captured_emits(monkeypatch):
    """Capture every ``_emit`` call as a list of parsed-JSON dicts."""
    events: list[dict] = []

    def fake_emit(prompt, **extras):
        events.append({"prompt": prompt, **extras})

    monkeypatch.setattr(poller, "_emit", fake_emit)
    return events


def _patch_api(monkeypatch, response, compare_response=None):
    """Patch ``_gh_api`` to return ``response`` for PR-list calls and
    ``compare_response`` (default None) for compare/ endpoint calls.

    Most existing tests don't care about commit enrichment, so they pass
    no ``compare_response`` and the compare call degrades gracefully to
    ``"(commit details unavailable)"`` in the emitted prompt.
    """
    def fake_api(endpoint: str, token: str):
        if "compare/" in endpoint:
            return compare_response
        return response
    monkeypatch.setattr(poller, "_gh_api", fake_api)


def test_first_sighting_does_not_emit(monkeypatch, captured_emits):
    """First poll after this feature ships sees existing open PRs;
    record their heads but suppress the bulk-fire — pr_opened
    already covered the originally-new ones."""
    _patch_api(monkeypatch, [
        _pr(101, "sha_a"),
        _pr(102, "sha_b"),
    ])
    count, new_heads, _ = poller._check_pr_pushes(
        "o/r", token="t", me="", pr_heads={},
    )
    assert count == 0
    assert captured_emits == []
    assert new_heads == {"101": "sha_a", "102": "sha_b"}


def test_head_advances_emits_pr_synchronize(monkeypatch, captured_emits):
    _patch_api(monkeypatch, [_pr(103, "new_sha", title="Tweak")])
    count, new_heads, _ = poller._check_pr_pushes(
        "o/r", token="t", me="",
        pr_heads={"103": "old_sha"},
    )
    assert count == 1
    assert len(captured_emits) == 1
    ev = captured_emits[0]
    assert ev["event_type"] == "pr_synchronize"
    assert ev["repo"] == "o/r"
    assert ev["number"] == 103
    assert ev["previous_head"] == "old_sha"
    assert ev["new_head"] == "new_sha"
    assert "PR #103 updated on o/r" in ev["prompt"]
    assert new_heads == {"103": "new_sha"}


def test_no_change_does_not_emit(monkeypatch, captured_emits):
    _patch_api(monkeypatch, [_pr(103, "sha_x")])
    count, new_heads, _ = poller._check_pr_pushes(
        "o/r", token="t", me="",
        pr_heads={"103": "sha_x"},
    )
    assert count == 0
    assert captured_emits == []
    # Sha preserved so next poll still has the baseline.
    assert new_heads == {"103": "sha_x"}


def test_closed_pr_drops_out(monkeypatch, captured_emits):
    """API only returns state=open, so a closed/merged PR (#104) is
    absent from the response and therefore absent from the new
    dict — that's the cleanup mechanism."""
    _patch_api(monkeypatch, [_pr(103, "sha_p")])
    count, new_heads, _ = poller._check_pr_pushes(
        "o/r", token="t", me="",
        pr_heads={"103": "sha_p", "104": "sha_q"},
    )
    assert count == 0
    assert captured_emits == []
    assert new_heads == {"103": "sha_p"}
    assert "104" not in new_heads


def test_self_author_filtered_out(monkeypatch, captured_emits):
    """PRs authored by the configured ``me`` login are skipped — they
    don't emit AND they don't end up in the new dict (so if mimir
    later force-pushes its own PR, we don't fire on it)."""
    _patch_api(monkeypatch, [
        _pr(201, "sha_self", login="mimirbot"),
        _pr(202, "sha_other", login="alice"),
    ])
    count, new_heads, _ = poller._check_pr_pushes(
        "o/r", token="t", me="mimirbot", pr_heads={},
    )
    assert count == 0
    assert captured_emits == []
    assert new_heads == {"202": "sha_other"}
    assert "201" not in new_heads


def test_force_push_with_unchanged_diff_still_emits(
    monkeypatch, captured_emits,
):
    """Known false-positive: a rebase that doesn't change the diff vs.
    base still advances ``head.sha``, so we fire on it. The
    alternative (diffing every PR every poll) is too expensive."""
    _patch_api(monkeypatch, [_pr(103, "rebased_sha")])
    count, new_heads, _ = poller._check_pr_pushes(
        "o/r", token="t", me="",
        pr_heads={"103": "old_sha"},
    )
    assert count == 1
    assert captured_emits[0]["event_type"] == "pr_synchronize"
    assert new_heads == {"103": "rebased_sha"}


def test_api_failure_preserves_prior_heads(monkeypatch, captured_emits):
    """If ``_gh_api`` returns None (transient failure), don't drop
    the cached heads — that would cause every PR to look "new" on
    the next successful poll and suppress legitimate pushes."""
    _patch_api(monkeypatch, None)
    count, new_heads, _ = poller._check_pr_pushes(
        "o/r", token="t", me="",
        pr_heads={"103": "sha_x", "104": "sha_y"},
    )
    assert count == 0
    assert captured_emits == []
    assert new_heads == {"103": "sha_x", "104": "sha_y"}


def test_emit_payload_is_jsonable(monkeypatch):
    """Sanity: the real ``_emit`` writes JSONL; make sure our event
    extras serialize cleanly (no datetimes, no sets).

    NOTE: monkeypatching ``poller.print`` works because Python resolves
    bare ``print(...)`` calls inside ``poller`` against the module
    globals first, then falls through to builtins. A future refactor
    that switches to ``from builtins import print`` or
    ``sys.stdout.write(...)`` would silently break this capture (the
    assertions below would fail because nothing got written). If that
    happens, switch to capsys or monkeypatch ``sys.stdout.write``.
    """
    captured_lines: list[str] = []

    import sys as _sys

    def fake_print(*args, file=_sys.stdout, **kwargs):
        # Only stdout-bound JSONL lines are events. ``_eprint`` diagnostics
        # (e.g. the review-skill-preload warning when
        # MIMIR_GITHUB_PRELOAD_REVIEW_SKILL=1 but the skill is absent) go to
        # stderr and must NOT be parsed as JSON — without this filter the
        # warning string reached json.loads() and the test failed in the
        # container (chainlink #299 review).
        if file is not _sys.stdout:
            return
        captured_lines.append(args[0])

    monkeypatch.setattr(poller, "print", fake_print, raising=False)
    monkeypatch.setattr(
        poller, "_gh_api",
        lambda endpoint, token: [_pr(103, "new_sha")],
    )
    count, _, _ = poller._check_pr_pushes(
        "o/r", token="t", me="", pr_heads={"103": "old_sha"},
    )
    assert count == 1
    # One JSONL line emitted (the print monkeypatch above swallows the
    # informational stderr prints because they hit the same builtin).
    payloads = [json.loads(line) for line in captured_lines]
    sync_events = [p for p in payloads if p.get("event_type") == "pr_synchronize"]
    assert len(sync_events) == 1
    assert sync_events[0]["number"] == 103


# ─── Review-needed events: submission rule + expected-tool-call marker ─


def _capture_emits(monkeypatch) -> list[dict]:
    """Replace ``poller.print`` with a capturer. Skill's ``_emit``
    calls ``print(json.dumps(event))`` to stdout (the JSONL contract);
    skill's ``_eprint`` calls ``print(..., file=sys.stderr)`` for
    diagnostics. Filter on ``file=`` so the capturer only collects
    stdout-bound JSON lines — diagnostic stderr stays out.
    """
    import sys as _sys
    captured: list[dict] = []

    def fake_print(*args, file=_sys.stdout, **kw):
        # _eprint passes file=sys.stderr; skip those.
        if file is not _sys.stdout:
            return
        captured.append(json.loads(args[0]))

    monkeypatch.setattr(poller, "print", fake_print, raising=False)
    return captured


def test_review_needed_event_carries_submission_rule_and_marker(monkeypatch):
    """``pr_opened`` and ``pr_synchronize`` events carry the
    submission-rule suffix in the prompt AND the
    ``expected_tool_call`` marker in extras. The marker is the
    framework-side hook (Mimir's PR #234/#235 decoupling) that lets
    agent.py emit ``poller_review_missed_submission`` when the turn
    didn't actually submit.
    """
    captured = _capture_emits(monkeypatch)
    monkeypatch.delenv("MIMIR_GITHUB_PRELOAD_REVIEW_SKILL", raising=False)

    poller._emit(
        "New PR on o/r: #234 Add config.json (by @alice)",
        event_type="pr_opened",
        repo="o/r", number=234,
    )
    ev = captured[0]
    assert ev["event_type"] == "pr_opened"
    assert "REVIEW SUBMISSION RULE" in ev["prompt"]
    assert "Add config.json" in ev["prompt"]  # base prompt preserved
    marker = ev.get("expected_tool_call")
    assert isinstance(marker, dict)
    assert marker["signal_on_missing"] == "poller_review_missed_submission"
    assert "pull_request_review_write" in marker["tool_names"]
    # chainlink #308: the marker is now PR-specific so the framework can
    # attribute WHICH review wasn't submitted (a duplicate review of one PR
    # no longer masks an unreviewed sibling). The bash substring carries the
    # PR number — ``gh pr review 234`` still discriminates a real submission
    # from ``gh pr review-comment`` (which has no ``review 234`` substring) —
    # and ``ref`` identifies the item in the emitted signal.
    assert marker["bash_substrings"] == [
        "gh pr review 234",
        "gh pr review --repo o/r 234",
        "gh pr review -R o/r 234",
    ]
    assert marker["ref"] == "#234"
    assert marker["repo"] == "o/r"
    assert marker["number"] == 234


def test_non_review_events_carry_no_marker(monkeypatch):
    """``issue_opened`` / ``issue_comment`` / ``pr_review_comment`` /
    ``pr_review`` must NOT carry the submission rule or marker —
    they're not review-needed."""
    captured = _capture_emits(monkeypatch)
    for non_review_type in (
        "issue_opened", "issue_comment", "pr_review_comment", "pr_review",
    ):
        captured.clear()
        poller._emit("body", event_type=non_review_type)
        ev = captured[0]
        assert "REVIEW SUBMISSION RULE" not in ev["prompt"]
        assert "expected_tool_call" not in ev


def test_review_skill_preload_inlines_full_body_when_env_set(monkeypatch, tmp_path):
    """``MIMIR_GITHUB_PRELOAD_REVIEW_SKILL=1`` inlines the
    ``<MIMIR_HOME>/skills/review/SKILL.md`` body alongside the
    rule. This is the workaround for the reasoning-before-Skill-loads
    issue — full rule set in context before the model commits its
    output structure."""
    mimir_home = tmp_path / "home"
    skill_path = mimir_home / "skills" / "review" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        "---\nname: review\n---\n"
        "# PR Review\n"
        "After drafting, **submit via `gh pr review`**.",
        encoding="utf-8",
    )

    captured = _capture_emits(monkeypatch)
    monkeypatch.setenv("MIMIR_HOME", str(mimir_home))
    monkeypatch.setenv("MIMIR_GITHUB_PRELOAD_REVIEW_SKILL", "1")

    poller._emit("base", event_type="pr_opened")
    ev = captured[0]
    assert "REVIEW SUBMISSION RULE" in ev["prompt"]
    assert "/review SKILL.md (pre-loaded)" in ev["prompt"]
    assert "After drafting, **submit via `gh pr review`**" in ev["prompt"]


def test_review_skill_preload_finds_bundled_builtin_skill(monkeypatch, tmp_path):
    """``review`` is a BUNDLED skill, so on a real install it lives at
    ``<home>/.mimir_builtin_skills/review/`` — NOT the operator
    ``<home>/skills/`` dir. The preload must resolve it there too;
    checking only ``skills/`` is why it silently no-op'd on mimirbot
    (chainlink #299 follow-up — the review skill WAS installed, just in
    the bundled location)."""
    mimir_home = tmp_path / "home"
    skill_path = mimir_home / ".mimir_builtin_skills" / "review" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        "---\nname: review\n---\n# PR Review\nBUNDLED REVIEW BODY.",
        encoding="utf-8",
    )

    captured = _capture_emits(monkeypatch)
    monkeypatch.setenv("MIMIR_HOME", str(mimir_home))
    monkeypatch.setenv("MIMIR_GITHUB_PRELOAD_REVIEW_SKILL", "1")

    poller._emit("base", event_type="pr_opened")
    ev = captured[0]
    assert "/review SKILL.md (pre-loaded)" in ev["prompt"]
    assert "BUNDLED REVIEW BODY." in ev["prompt"]


def test_review_skill_preload_operator_skill_wins_over_builtin(monkeypatch, tmp_path):
    """When the review skill exists in BOTH the operator dir and the
    bundled dir, the operator copy wins — matches mimir's dual-location
    last-source-wins resolution (operator overrides bundled)."""
    mimir_home = tmp_path / "home"
    op = mimir_home / "skills" / "review" / "SKILL.md"
    op.parent.mkdir(parents=True)
    op.write_text("---\nname: review\n---\nOPERATOR REVIEW BODY.", encoding="utf-8")
    bundled = mimir_home / ".mimir_builtin_skills" / "review" / "SKILL.md"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("---\nname: review\n---\nBUNDLED REVIEW BODY.", encoding="utf-8")

    captured = _capture_emits(monkeypatch)
    monkeypatch.setenv("MIMIR_HOME", str(mimir_home))
    monkeypatch.setenv("MIMIR_GITHUB_PRELOAD_REVIEW_SKILL", "1")

    poller._emit("base", event_type="pr_opened")
    ev = captured[0]
    assert "OPERATOR REVIEW BODY." in ev["prompt"]
    assert "BUNDLED REVIEW BODY." not in ev["prompt"]


def test_review_skill_preload_off_by_default(monkeypatch, tmp_path):
    """No env var → rule only, no inline body. Default-off keeps the
    per-event token cost bounded."""
    mimir_home = tmp_path / "home"
    skill_path = mimir_home / "skills" / "review" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("SHOULD NOT APPEAR", encoding="utf-8")

    captured = _capture_emits(monkeypatch)
    monkeypatch.setenv("MIMIR_HOME", str(mimir_home))
    monkeypatch.delenv("MIMIR_GITHUB_PRELOAD_REVIEW_SKILL", raising=False)

    poller._emit("base", event_type="pr_synchronize")
    ev = captured[0]
    assert "REVIEW SUBMISSION RULE" in ev["prompt"]
    assert "SHOULD NOT APPEAR" not in ev["prompt"]


def test_review_skill_preload_missing_file_falls_back_to_rule_only(
    monkeypatch, tmp_path,
):
    """Preload requested but skill file missing → rule still appended;
    no crash, no inline body. Operator gets a stderr warning."""
    captured = _capture_emits(monkeypatch)
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setenv("MIMIR_GITHUB_PRELOAD_REVIEW_SKILL", "1")

    poller._emit("base", event_type="pr_opened")
    ev = captured[0]
    assert "REVIEW SUBMISSION RULE" in ev["prompt"]
    assert "/review SKILL.md (pre-loaded)" not in ev["prompt"]


def test_review_skill_preload_explicit_path_override(monkeypatch, tmp_path):
    """``MIMIR_GITHUB_REVIEW_SKILL_PATH`` overrides the default location
    when the operator stages a custom review skill outside the standard
    tree."""
    custom = tmp_path / "elsewhere" / "my-review.md"
    custom.parent.mkdir(parents=True)
    custom.write_text("CUSTOM REVIEW SKILL BODY", encoding="utf-8")

    captured = _capture_emits(monkeypatch)
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setenv("MIMIR_GITHUB_PRELOAD_REVIEW_SKILL", "1")
    monkeypatch.setenv("MIMIR_GITHUB_REVIEW_SKILL_PATH", str(custom))

    poller._emit("base", event_type="pr_opened")
    ev = captured[0]
    assert "CUSTOM REVIEW SKILL BODY" in ev["prompt"]


# ─── pr_review_requested detection (reviewer added to a PR) ─────────


def _patch_api_with_reviews(monkeypatch, response, reviews_response):
    def fake_api(endpoint: str, token: str):
        if endpoint.endswith("/reviews"):
            return reviews_response
        if "compare/" in endpoint:
            return None
        return response

    monkeypatch.setattr(poller, "_gh_api", fake_api)


def _review(login: str, commit_id: str, state: str = "APPROVED") -> dict:
    return {
        "user": {"login": login},
        "commit_id": commit_id,
        "state": state,
        "submitted_at": "2026-06-24T02:10:00Z",
    }


def test_review_requested_first_sighting_emits(captured_emits, monkeypatch):
    """Empty cursor + a PR where ``mimir-carreira`` is in
    ``requested_reviewers`` → emit ``pr_review_requested`` (attempt 1)."""
    monkeypatch.setattr(
        poller, "_gh_api",
        lambda endpoint, token: [
            _pr(42, "sha", login="alice",
                requested_reviewers=["mimir-carreira"]),
        ],
    )
    count, _, new_rr = poller._check_pr_pushes(
        "o/r", token="t", me="mimir-carreira",
        pr_heads={"42": "sha"},  # already seen, no push fires
        pr_review_requests={},
    )
    assert count == 1
    rr_events = [e for e in captured_emits if e.get("event_type") == "pr_review_requested"]
    assert len(rr_events) == 1
    assert rr_events[0]["number"] == 42
    assert rr_events[0]["requested_reviewer"] == "mimir-carreira"
    assert rr_events[0]["attempt"] == 1
    assert rr_events[0]["max_attempts"] == poller.REVIEW_REQUEST_MAX_ATTEMPTS
    assert "Review requested" in rr_events[0]["prompt"]
    # Cursor records attempt 1; re-emits next poll if ``me`` stays
    # requested (chainlink #299 — recovers a review dropped by a dead turn).
    assert new_rr == {"42": 1}


def test_review_requested_re_emits_while_still_requested(
    captured_emits, monkeypatch,
):
    """PR already had one recorded attempt AND ``me`` is still requested
    → RE-EMIT (attempt 2), not silence. The #299 fix: a still-requested
    PR means the prior review never landed, so retry until the cap.
    (Old behavior was emit-once-never-retry — the drop bug.)"""
    monkeypatch.setattr(
        poller, "_gh_api",
        lambda endpoint, token: [
            _pr(42, "sha", login="alice",
                requested_reviewers=["mimir-carreira"]),
        ],
    )
    count, _, new_rr = poller._check_pr_pushes(
        "o/r", token="t", me="mimir-carreira",
        pr_heads={"42": "sha"},
        pr_review_requests={"42": 1},
    )
    assert count == 1
    rr_events = [e for e in captured_emits if e.get("event_type") == "pr_review_requested"]
    assert len(rr_events) == 1
    assert rr_events[0]["attempt"] == 2
    # Retry prompts flag the re-request so the agent knows a prior
    # attempt produced no submitted review.
    assert "STILL on the reviewers list" in rr_events[0]["prompt"]
    assert new_rr == {"42": 2}


def test_review_request_removed_drops_from_cursor(captured_emits, monkeypatch):
    """Cursor had PR #42 with a recorded attempt but the latest poll
    shows ``me`` is no longer in ``requested_reviewers`` (review
    submitted, request removed) → PR drops from the cursor so a later
    re-request fires fresh at attempt 1. No event emitted on drop."""
    monkeypatch.setattr(
        poller, "_gh_api",
        lambda endpoint, token: [
            _pr(42, "sha", login="alice", requested_reviewers=[]),
        ],
    )
    count, _, new_rr = poller._check_pr_pushes(
        "o/r", token="t", me="mimir-carreira",
        pr_heads={"42": "sha"},
        pr_review_requests={"42": 1},
    )
    rr_events = [e for e in captured_emits if e.get("event_type") == "pr_review_requested"]
    assert rr_events == []
    assert new_rr == {}


def test_review_requested_re_fires_after_being_removed(
    captured_emits, monkeypatch,
):
    """Operator un-requests, then re-requests → second add fires
    again at attempt 1. Tests the drop-and-rejoin cycle (empty entry)."""
    monkeypatch.setattr(
        poller, "_gh_api",
        lambda endpoint, token: [
            _pr(42, "sha", login="alice",
                requested_reviewers=["mimir-carreira"]),
        ],
    )
    # Prior poll: was requested, then removed (cursor cleared for #42).
    count, _, new_rr = poller._check_pr_pushes(
        "o/r", token="t", me="mimir-carreira",
        pr_heads={"42": "sha"},
        pr_review_requests={},  # empty — last poll saw removal
    )
    rr_events = [e for e in captured_emits if e.get("event_type") == "pr_review_requested"]
    assert len(rr_events) == 1
    assert rr_events[0]["attempt"] == 1
    assert new_rr == {"42": 1}


def test_review_requested_current_head_review_suppresses_rerequest(
    captured_emits, monkeypatch,
):
    """Operator re-request after a current-head self review is satisfied."""
    _patch_api_with_reviews(
        monkeypatch,
        [_pr(42, "sha", login="alice", requested_reviewers=["mimir-carreira"])],
        [_review("mimir-carreira", "sha", state="APPROVED")],
    )
    count, _, new_rr = poller._check_pr_pushes(
        "o/r", token="t", me="mimir-carreira",
        pr_heads={"42": "sha"},
        pr_review_requests={},
    )
    assert count == 0
    assert [e for e in captured_emits if e.get("event_type") == "pr_review_requested"] == []
    assert new_rr == {}


def test_review_requested_current_head_review_suppresses_give_up(
    monkeypatch,
):
    """Do not emit give-up after a substantive current-head self review."""
    captured = _capture_emits(monkeypatch)
    _patch_api_with_reviews(
        monkeypatch,
        [_pr(42, "sha", login="alice", requested_reviewers=["mimir-carreira"])],
        [_review("mimir-carreira", "sha", state="CHANGES_REQUESTED")],
    )
    cap = poller.REVIEW_REQUEST_MAX_ATTEMPTS
    count, _, new_rr = poller._check_pr_pushes(
        "o/r", token="t", me="mimir-carreira",
        pr_heads={"42": "sha"},
        pr_review_requests={"42": cap},
    )
    assert count == 0
    assert [e for e in captured if e.get("event_type") == "pr_review_requested"] == []
    assert [e for e in captured if e.get("signal") == "pr_review_request_gave_up"] == []
    assert new_rr == {}


def test_review_requested_old_head_review_still_retries(
    captured_emits, monkeypatch,
):
    """A stale self review does not satisfy a current review request."""
    _patch_api_with_reviews(
        monkeypatch,
        [_pr(42, "new-sha", login="alice", requested_reviewers=["mimir-carreira"])],
        [_review("mimir-carreira", "old-sha", state="APPROVED")],
    )
    count, _, new_rr = poller._check_pr_pushes(
        "o/r", token="t", me="mimir-carreira",
        pr_heads={"42": "new-sha"},
        pr_review_requests={},
    )
    assert count == 1
    rr_events = [e for e in captured_emits if e.get("event_type") == "pr_review_requested"]
    assert len(rr_events) == 1
    assert rr_events[0]["attempt"] == 1
    assert new_rr == {"42": 1}


def test_review_requested_other_reviewer_current_head_review_still_retries(
    captured_emits, monkeypatch,
):
    """Another reviewer's current-head review does not satisfy our request."""
    _patch_api_with_reviews(
        monkeypatch,
        [_pr(42, "sha", login="alice", requested_reviewers=["mimir-carreira"])],
        [_review("someone-else", "sha", state="APPROVED")],
    )
    count, _, new_rr = poller._check_pr_pushes(
        "o/r", token="t", me="mimir-carreira",
        pr_heads={"42": "sha"},
        pr_review_requests={},
    )
    assert count == 1
    rr_events = [e for e in captured_emits if e.get("event_type") == "pr_review_requested"]
    assert len(rr_events) == 1
    assert new_rr == {"42": 1}


def test_review_requested_non_substantive_self_review_still_retries(
    captured_emits, monkeypatch,
):
    """Pending/dismissed review states are not treated as submitted reviews."""
    _patch_api_with_reviews(
        monkeypatch,
        [_pr(42, "sha", login="alice", requested_reviewers=["mimir-carreira"])],
        [_review("mimir-carreira", "sha", state="PENDING")],
    )
    count, _, new_rr = poller._check_pr_pushes(
        "o/r", token="t", me="mimir-carreira",
        pr_heads={"42": "sha"},
        pr_review_requests={},
    )
    assert count == 1
    rr_events = [e for e in captured_emits if e.get("event_type") == "pr_review_requested"]
    assert len(rr_events) == 1
    assert new_rr == {"42": 1}


def test_review_requested_reviews_api_failure_preserves_retry(
    captured_emits, monkeypatch,
):
    """Reviews API failures fall back to the existing retry behavior."""
    _patch_api_with_reviews(
        monkeypatch,
        [_pr(42, "sha", login="alice", requested_reviewers=["mimir-carreira"])],
        None,
    )
    count, _, new_rr = poller._check_pr_pushes(
        "o/r", token="t", me="mimir-carreira",
        pr_heads={"42": "sha"},
        pr_review_requests={},
    )
    assert count == 1
    rr_events = [e for e in captured_emits if e.get("event_type") == "pr_review_requested"]
    assert len(rr_events) == 1
    assert new_rr == {"42": 1}

def test_review_requested_other_reviewer_does_not_trigger(
    captured_emits, monkeypatch,
):
    """A PR requesting a DIFFERENT login than ``me`` must not fire
    a pr_review_requested event for us."""
    monkeypatch.setattr(
        poller, "_gh_api",
        lambda endpoint, token: [
            _pr(42, "sha", login="alice",
                requested_reviewers=["someone-else"]),
        ],
    )
    count, _, new_rr = poller._check_pr_pushes(
        "o/r", token="t", me="mimir-carreira",
        pr_heads={"42": "sha"},
        pr_review_requests={},
    )
    rr_events = [e for e in captured_emits if e.get("event_type") == "pr_review_requested"]
    assert rr_events == []
    assert new_rr == {}


def test_review_requested_empty_me_skips_detection(captured_emits, monkeypatch):
    """No ``me`` configured → review-request detection silently
    skipped. Push detection still runs."""
    monkeypatch.setattr(
        poller, "_gh_api",
        lambda endpoint, token: [
            _pr(42, "sha", login="alice",
                requested_reviewers=["mimir-carreira"]),
        ],
    )
    count, _, new_rr = poller._check_pr_pushes(
        "o/r", token="t", me="",  # empty
        pr_heads={"42": "sha"},
        pr_review_requests={},
    )
    rr_events = [e for e in captured_emits if e.get("event_type") == "pr_review_requested"]
    assert rr_events == []
    assert new_rr == {}


def test_review_requested_event_carries_submission_marker(monkeypatch):
    """A pr_review_requested event must carry the same submission
    rule + ``expected_tool_call`` marker as pr_opened/pr_synchronize
    — the agent must submit the review on this trigger too."""
    captured: list[dict] = []

    import sys as _sys
    def fake_print(*a, file=_sys.stdout, **kw):
        if file is _sys.stdout:
            captured.append(json.loads(a[0]))
    monkeypatch.setattr(poller, "print", fake_print, raising=False)

    poller._emit(
        "Review requested on o/r PR #42: ...",
        event_type="pr_review_requested",
        repo="o/r", number=42, url="...",
        requested_reviewer="mimir-carreira",
    )
    ev = captured[0]
    assert "REVIEW SUBMISSION RULE" in ev["prompt"]
    marker = ev.get("expected_tool_call")
    assert isinstance(marker, dict)
    assert marker["signal_on_missing"] == "poller_review_missed_submission"
    assert marker["repo"] == "o/r"
    assert marker["number"] == 42
    assert marker["reviewer"] == "mimir-carreira"


# ─── re-emit / wedge-guard / give-up (chainlink #299) ───────────────


def test_review_request_re_emits_until_cap_then_gives_up(monkeypatch):
    """Full lifecycle: while ``me`` stays requested, each poll re-emits
    pr_review_requested up to the cap; the next poll emits a one-shot
    pr_review_request_gave_up SIGNAL; subsequent polls are dormant.
    Drives the same PR through CAP+2 polls (print-capture so both the
    prompt re-emits AND the signal are visible)."""
    captured = _capture_emits(monkeypatch)
    monkeypatch.setattr(
        poller, "_gh_api",
        lambda endpoint, token: [
            _pr(42, "sha", login="alice",
                requested_reviewers=["mimir-carreira"]),
        ],
    )
    cap = poller.REVIEW_REQUEST_MAX_ATTEMPTS
    rr: dict = {}
    req_per_poll, gaveup_per_poll = [], []
    for _ in range(cap + 2):
        captured.clear()
        _, _, rr = poller._check_pr_pushes(
            "o/r", token="t", me="mimir-carreira",
            pr_heads={"42": "sha"}, pr_review_requests=rr,
        )
        req_per_poll.append(
            sum(1 for e in captured if e.get("event_type") == "pr_review_requested")
        )
        gaveup_per_poll.append(
            sum(1 for e in captured if e.get("signal") == "pr_review_request_gave_up")
        )
    # Polls 1..cap: one review-request each. Poll cap+1: the give-up
    # signal. Poll cap+2: dormant (nothing emitted).
    assert req_per_poll == [1] * cap + [0, 0]
    assert gaveup_per_poll == [0] * cap + [1, 0]
    # Parked at the dormant sentinel (cap + 1).
    assert rr == {"42": cap + 1}


def test_review_request_gave_up_signal_shape(monkeypatch):
    """At the cap (and ``me`` still requested) the poller emits a SIGNAL
    record — ``signal`` not ``prompt`` (no turn) — that ``feedback.classify``
    maps to a negative ``gave_up`` algedonic signal. Asserts the record
    shape the renderer consumes (repo / number / url / attempts)."""
    captured = _capture_emits(monkeypatch)
    monkeypatch.setattr(
        poller, "_gh_api",
        lambda endpoint, token: [
            _pr(42, "sha", login="alice", url="https://gh/o/r/pull/42",
                requested_reviewers=["mimir-carreira"]),
        ],
    )
    cap = poller.REVIEW_REQUEST_MAX_ATTEMPTS
    count, _, new_rr = poller._check_pr_pushes(
        "o/r", token="t", me="mimir-carreira",
        pr_heads={"42": "sha"},
        pr_review_requests={"42": cap},  # already at the cap
    )
    assert count == 1
    assert [e for e in captured if e.get("event_type") == "pr_review_requested"] == []
    gaveup = [e for e in captured if e.get("signal") == "pr_review_request_gave_up"]
    assert len(gaveup) == 1
    ev = gaveup[0]
    assert "prompt" not in ev          # signal-only → no turn spawned
    assert ev["number"] == 42
    assert ev["repo"] == "o/r"
    assert ev["url"] == "https://gh/o/r/pull/42"
    assert ev["attempts"] == cap
    assert new_rr == {"42": cap + 1}   # dormant sentinel


def test_review_request_dormant_after_give_up(monkeypatch):
    """Once parked at the dormant sentinel (cap+1) with ``me`` still
    requested, the poller emits nothing — neither a retry nor another
    give-up — and carries the sentinel forward."""
    captured = _capture_emits(monkeypatch)
    monkeypatch.setattr(
        poller, "_gh_api",
        lambda endpoint, token: [
            _pr(42, "sha", login="alice",
                requested_reviewers=["mimir-carreira"]),
        ],
    )
    cap = poller.REVIEW_REQUEST_MAX_ATTEMPTS
    count, _, new_rr = poller._check_pr_pushes(
        "o/r", token="t", me="mimir-carreira",
        pr_heads={"42": "sha"},
        pr_review_requests={"42": cap + 1},  # dormant
    )
    assert count == 0
    assert captured == []
    assert new_rr == {"42": cap + 1}


def test_review_request_give_up_resets_when_removed(monkeypatch):
    """A given-up PR (dormant sentinel) whose ``me`` is later removed
    drops from the cursor, so a future re-request starts fresh at
    attempt 1 — the give-up doesn't permanently blacklist the PR."""
    captured = _capture_emits(monkeypatch)
    monkeypatch.setattr(
        poller, "_gh_api",
        lambda endpoint, token: [
            _pr(42, "sha", login="alice", requested_reviewers=[]),
        ],
    )
    cap = poller.REVIEW_REQUEST_MAX_ATTEMPTS
    count, _, new_rr = poller._check_pr_pushes(
        "o/r", token="t", me="mimir-carreira",
        pr_heads={"42": "sha"},
        pr_review_requests={"42": cap + 1},  # dormant, gave up earlier
    )
    assert count == 0
    assert new_rr == {}


def test_coerce_review_requests_migrates_and_validates():
    """``_coerce_review_requests`` migrates the pre-#299 bare-list cursor
    to ``{key: attempts}`` and defends against a corrupt/hand-edited
    entry."""
    # Pre-#299 list format → one recorded attempt each (eligible for retry).
    assert poller._coerce_review_requests(["7", "9"]) == {"7": 1, "9": 1}
    assert poller._coerce_review_requests([7, 9]) == {"7": 1, "9": 1}
    # New dict format passes through.
    assert poller._coerce_review_requests({"7": 2}) == {"7": 2}
    # Defensive: bool (an int subclass), negatives, junk → dropped/empty.
    assert poller._coerce_review_requests({"7": True}) == {}
    assert poller._coerce_review_requests({"7": -1}) == {}
    assert poller._coerce_review_requests(None) == {}
    assert poller._coerce_review_requests("garbage") == {}


# ── commit-enrichment tests ───────────────────────────────────────────────────


def _make_commit(message: str) -> dict:
    return {"commit": {"message": message}, "sha": "aabbccdd"}


def test_commit_subjects_included_in_prompt(monkeypatch, captured_emits):
    """When the compare endpoint returns commits, their first-line subjects
    appear in the prompt so the agent can act on individual changes."""
    compare = {
        "ahead_by": 2,
        "commits": [
            _make_commit("Add feature X\n\nDetails here"),
            _make_commit("Fix edge case Y"),
        ],
    }
    _patch_api(monkeypatch, [_pr(110, "new_sha")], compare_response=compare)
    _, _, _ = poller._check_pr_pushes(
        "o/r", token="t", me="",
        pr_heads={"110": "old_sha"},
    )
    assert len(captured_emits) == 1
    prompt = captured_emits[0]["prompt"]
    assert "2 commit(s)" in prompt
    assert "Add feature X" in prompt
    assert "Fix edge case Y" in prompt
    assert "Details here" not in prompt          # only first line of message
    assert "old_sha" in prompt                   # sha delta preserved
    assert "new_sha" in prompt


def test_commit_subjects_truncated_at_three(monkeypatch, captured_emits):
    """Only the first 3 commit subjects are shown inline; remainder shown
    as '… (N more)' so the prompt doesn't balloon on large force-pushes."""
    compare = {
        "ahead_by": 5,
        "commits": [_make_commit(f"Commit {i}") for i in range(5)],
    }
    _patch_api(monkeypatch, [_pr(111, "sha_b")], compare_response=compare)
    _, _, _ = poller._check_pr_pushes(
        "o/r", token="t", me="",
        pr_heads={"111": "sha_a"},
    )
    prompt = captured_emits[0]["prompt"]
    assert "Commit 0" in prompt
    assert "Commit 1" in prompt
    assert "Commit 2" in prompt
    assert "Commit 3" not in prompt
    assert "… (2 more)" in prompt


def test_compare_api_failure_degrades_gracefully(monkeypatch, captured_emits):
    """When the compare endpoint returns None (API error), the prompt falls
    back to '(commit details unavailable)' — the event still fires."""
    _patch_api(monkeypatch, [_pr(112, "sha_new")], compare_response=None)
    count, _, _ = poller._check_pr_pushes(
        "o/r", token="t", me="",
        pr_heads={"112": "sha_old"},
    )
    assert count == 1
    prompt = captured_emits[0]["prompt"]
    assert "(commit details unavailable)" in prompt
    assert "PR #112 updated on o/r" in prompt


def test_seeds_state_gitignore(tmp_path, monkeypatch):
    """Poller seeds a write-if-missing .gitignore ignoring its transient cursor."""
    monkeypatch.setattr(poller, "STATE_DIR", tmp_path)
    poller._seed_state_gitignore()
    gi = tmp_path / ".gitignore"
    assert gi.exists()
    assert "cursor.json" in gi.read_text()
    gi.write_text("operator-custom\n")
    poller._seed_state_gitignore()  # write-if-missing → not clobbered
    assert gi.read_text() == "operator-custom\n"


def test_scratch_cleanup_rule_on_repo_work_events(monkeypatch):
    """Poller-driven repo work must clean up its scratch clone — the
    per-event leftovers reached 140 GB on a live home. The rule rides
    every repo-work event type; informational events stay clean."""
    captured = _capture_emits(monkeypatch)
    monkeypatch.delenv("MIMIR_GITHUB_PRELOAD_REVIEW_SKILL", raising=False)

    poller._emit(
        "New PR on o/r: #1 t (by @a)",
        event_type="pr_opened", repo="o/r", number=1,
    )
    poller._emit(
        "Review posted on your PR #2",
        event_type="pr_review", repo="o/r", number=2,
    )
    poller._emit(
        "New issue on o/r: #3",
        event_type="issue_opened", repo="o/r", number=3,
    )

    pr_opened, pr_review, issue_opened = captured
    assert "SCRATCH CLEANUP RULE" in pr_opened["prompt"]
    assert "REVIEW SUBMISSION RULE" in pr_opened["prompt"]
    # Cleanup rule precedes the submission rule so it survives any
    # downstream tail truncation.
    assert (
        pr_opened["prompt"].index("SCRATCH CLEANUP RULE")
        < pr_opened["prompt"].index("REVIEW SUBMISSION RULE")
    )
    # Own-PR revision work clones too — cleanup rule, no submission rule.
    assert "SCRATCH CLEANUP RULE" in pr_review["prompt"]
    assert "REVIEW SUBMISSION RULE" not in pr_review["prompt"]
    # Informational events don't get the rule.
    assert "SCRATCH CLEANUP RULE" not in issue_opened["prompt"]
