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

    def fake_print(*args, **kwargs):
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
    # Trailing space discriminates ``gh pr review <args>`` (real
    # submission) from ``gh pr review-comment`` (standalone comment —
    # NOT a submission). Mimir PR #236 review nit.
    assert "gh pr review " in marker["bash_substrings"]
    assert "gh pr review" not in marker["bash_substrings"]


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
    ``<MIMIR_HOME>/.claude/skills/review/SKILL.md`` body alongside the
    rule. This is the workaround for the reasoning-before-Skill-loads
    issue — full rule set in context before the model commits its
    output structure."""
    mimir_home = tmp_path / "home"
    skill_path = mimir_home / ".claude" / "skills" / "review" / "SKILL.md"
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


def test_review_skill_preload_off_by_default(monkeypatch, tmp_path):
    """No env var → rule only, no inline body. Default-off keeps the
    per-event token cost bounded."""
    mimir_home = tmp_path / "home"
    skill_path = mimir_home / ".claude" / "skills" / "review" / "SKILL.md"
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


def test_review_requested_first_sighting_emits(captured_emits, monkeypatch):
    """Empty cursor + a PR where ``mimir-carreira`` is in
    ``requested_reviewers`` → emit ``pr_review_requested``."""
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
        pr_review_requests=set(),
    )
    assert count == 1
    rr_events = [e for e in captured_emits if e.get("event_type") == "pr_review_requested"]
    assert len(rr_events) == 1
    assert rr_events[0]["number"] == 42
    assert rr_events[0]["requested_reviewer"] == "mimir-carreira"
    assert "Review requested" in rr_events[0]["prompt"]
    # Cursor records the PR so we don't re-emit next poll.
    assert new_rr == {"42"}


def test_review_requested_already_in_cursor_does_not_re_emit(
    captured_emits, monkeypatch,
):
    """PR is in the prior cursor AND still requested → no re-emit.
    The transition has already been surfaced."""
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
        pr_review_requests={"42"},
    )
    rr_events = [e for e in captured_emits if e.get("event_type") == "pr_review_requested"]
    assert rr_events == []
    # Still in the new cursor — the request is still active.
    assert new_rr == {"42"}


def test_review_request_removed_drops_from_cursor(captured_emits, monkeypatch):
    """Cursor had PR #42 flagged but the latest poll shows ``me`` is
    no longer in ``requested_reviewers`` (review submitted, request
    removed, etc.) → PR drops from the cursor so a later re-request
    would fire again. No event emitted on drop."""
    monkeypatch.setattr(
        poller, "_gh_api",
        lambda endpoint, token: [
            _pr(42, "sha", login="alice", requested_reviewers=[]),
        ],
    )
    count, _, new_rr = poller._check_pr_pushes(
        "o/r", token="t", me="mimir-carreira",
        pr_heads={"42": "sha"},
        pr_review_requests={"42"},
    )
    rr_events = [e for e in captured_emits if e.get("event_type") == "pr_review_requested"]
    assert rr_events == []
    assert new_rr == set()


def test_review_requested_re_fires_after_being_removed(
    captured_emits, monkeypatch,
):
    """Operator un-requests, then re-requests → second add fires
    again. Tests the drop-and-rejoin cycle."""
    monkeypatch.setattr(
        poller, "_gh_api",
        lambda endpoint, token: [
            _pr(42, "sha", login="alice",
                requested_reviewers=["mimir-carreira"]),
        ],
    )
    # Prior poll: was requested, then removed (cursor cleared).
    count, _, new_rr = poller._check_pr_pushes(
        "o/r", token="t", me="mimir-carreira",
        pr_heads={"42": "sha"},
        pr_review_requests=set(),  # empty — last poll saw removal
    )
    rr_events = [e for e in captured_emits if e.get("event_type") == "pr_review_requested"]
    assert len(rr_events) == 1
    assert new_rr == {"42"}


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
        pr_review_requests=set(),
    )
    rr_events = [e for e in captured_emits if e.get("event_type") == "pr_review_requested"]
    assert rr_events == []
    assert new_rr == set()


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
        pr_review_requests=set(),
    )
    rr_events = [e for e in captured_emits if e.get("event_type") == "pr_review_requested"]
    assert rr_events == []
    assert new_rr == set()


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
