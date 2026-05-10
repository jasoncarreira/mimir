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
        title: str = "Some PR", url: str | None = None) -> dict:
    return {
        "number": number,
        "title": title,
        "html_url": url or f"https://github.com/o/r/pull/{number}",
        "user": {"login": login},
        "head": {"sha": sha},
    }


@pytest.fixture
def captured_emits(monkeypatch):
    """Capture every ``_emit`` call as a list of parsed-JSON dicts."""
    events: list[dict] = []

    def fake_emit(prompt, **extras):
        events.append({"prompt": prompt, **extras})

    monkeypatch.setattr(poller, "_emit", fake_emit)
    return events


def _patch_api(monkeypatch, response):
    monkeypatch.setattr(
        poller, "_gh_api", lambda endpoint, token: response,
    )


def test_first_sighting_does_not_emit(monkeypatch, captured_emits):
    """First poll after this feature ships sees existing open PRs;
    record their heads but suppress the bulk-fire — pr_opened
    already covered the originally-new ones."""
    _patch_api(monkeypatch, [
        _pr(101, "sha_a"),
        _pr(102, "sha_b"),
    ])
    count, new_heads = poller._check_pr_pushes(
        "o/r", token="t", me="", pr_heads={},
    )
    assert count == 0
    assert captured_emits == []
    assert new_heads == {"101": "sha_a", "102": "sha_b"}


def test_head_advances_emits_pr_synchronize(monkeypatch, captured_emits):
    _patch_api(monkeypatch, [_pr(103, "new_sha", title="Tweak")])
    count, new_heads = poller._check_pr_pushes(
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
    count, new_heads = poller._check_pr_pushes(
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
    count, new_heads = poller._check_pr_pushes(
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
    count, new_heads = poller._check_pr_pushes(
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
    count, new_heads = poller._check_pr_pushes(
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
    count, new_heads = poller._check_pr_pushes(
        "o/r", token="t", me="",
        pr_heads={"103": "sha_x", "104": "sha_y"},
    )
    assert count == 0
    assert captured_emits == []
    assert new_heads == {"103": "sha_x", "104": "sha_y"}


def test_emit_payload_is_jsonable(monkeypatch):
    """Sanity: the real ``_emit`` writes JSONL; make sure our event
    extras serialize cleanly (no datetimes, no sets)."""
    captured_lines: list[str] = []

    def fake_print(*args, **kwargs):
        captured_lines.append(args[0])

    monkeypatch.setattr(poller, "print", fake_print, raising=False)
    monkeypatch.setattr(
        poller, "_gh_api",
        lambda endpoint, token: [_pr(103, "new_sha")],
    )
    count, _ = poller._check_pr_pushes(
        "o/r", token="t", me="", pr_heads={"103": "old_sha"},
    )
    assert count == 1
    # One JSONL line emitted (the print monkeypatch above swallows the
    # informational stderr prints because they hit the same builtin).
    payloads = [json.loads(line) for line in captured_lines]
    sync_events = [p for p in payloads if p.get("event_type") == "pr_synchronize"]
    assert len(sync_events) == 1
    assert sync_events[0]["number"] == 103
