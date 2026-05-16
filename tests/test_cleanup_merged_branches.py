"""Tests for ``scripts/cleanup_merged_branches.py`` (chainlink #136).

Covers the classification + bucketing logic. The git/gh subprocess
boundary is mocked via injected callables — the script exposes
``pr_lister`` and ``git_runner`` parameters specifically so tests
don't need to shell out.

End-to-end smoke testing happened during PR development: the script
was run with ``--dry-run`` against the live /workspace/mimir worktree
(60+ branches, 49 squash-merged candidates correctly identified).
That run is documented in the PR body rather than reproduced here
because (a) it depends on live GitHub state, and (b) the unit
coverage below pins the failure-prone classification logic, not the
trivial subprocess wrapping.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# scripts/ isn't a Python package; import the module via its path.
_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

import cleanup_merged_branches as cmb  # noqa: E402


def test_classify_branch_no_prs_returns_no_pr() -> None:
    """Local-only experiment branches have no associated PRs."""
    status, pr_num = cmb.classify_branch([])
    assert status == cmb.NO_PR
    assert pr_num is None


def test_classify_branch_single_merged_pr_returns_merged() -> None:
    status, pr_num = cmb.classify_branch(
        [{"number": 42, "state": "MERGED", "mergedAt": "2026-05-12T14:33:10Z"}]
    )
    assert status == cmb.MERGED
    assert pr_num == 42


def test_classify_branch_single_open_pr_returns_open() -> None:
    status, pr_num = cmb.classify_branch(
        [{"number": 171, "state": "OPEN", "mergedAt": None}]
    )
    assert status == cmb.OPEN
    assert pr_num == 171


def test_classify_branch_single_closed_unmerged_pr_returns_closed() -> None:
    status, pr_num = cmb.classify_branch(
        [{"number": 99, "state": "CLOSED", "mergedAt": None}]
    )
    assert status == cmb.CLOSED_UNMERGED
    assert pr_num == 99


def test_classify_branch_merged_wins_over_open() -> None:
    """When a branch is reused, MERGED takes precedence over later OPEN.

    Rationale: 'the work is in main' is the safety-determining fact.
    A later reused-branch PR being open doesn't change that the
    branch's content is already in main (as a squash commit).
    """
    prs = [
        {"number": 50, "state": "OPEN", "mergedAt": None},
        {"number": 30, "state": "MERGED", "mergedAt": "2026-05-01T00:00:00Z"},
    ]
    status, pr_num = cmb.classify_branch(prs)
    assert status == cmb.MERGED
    assert pr_num == 30


def test_classify_branch_open_wins_over_closed_unmerged() -> None:
    prs = [
        {"number": 99, "state": "CLOSED", "mergedAt": None},
        {"number": 100, "state": "OPEN", "mergedAt": None},
    ]
    status, pr_num = cmb.classify_branch(prs)
    assert status == cmb.OPEN
    assert pr_num == 100


def test_collect_branch_statuses_excludes_current_and_main() -> None:
    """Current branch and main are never bucketed (can't delete).

    Regression guard for the obvious foot-gun: a future change to the
    classifier could let main slip into the merged bucket if
    ``gh pr list --head main`` returns anything (e.g. someone opened
    a PR with main as the head ref by mistake). The collector layer
    is the safety net.
    """
    seen_branches: list[str] = []

    def fake_pr_lister(branch: str) -> list[dict]:
        seen_branches.append(branch)
        return [{"number": 1, "state": "MERGED", "mergedAt": "2026-01-01T00:00:00Z"}]

    buckets = cmb.collect_branch_statuses(
        ["main", "current-feature", "old-feature", "another-merged"],
        current_branch="current-feature",
        pr_lister=fake_pr_lister,
    )

    # main + current-feature must be skipped before pr_lister is called.
    assert "main" not in seen_branches
    assert "current-feature" not in seen_branches
    assert sorted(seen_branches) == ["another-merged", "old-feature"]
    assert sorted(b for b, _ in buckets[cmb.MERGED]) == [
        "another-merged",
        "old-feature",
    ]
    assert buckets[cmb.OPEN] == []
    assert buckets[cmb.CLOSED_UNMERGED] == []
    assert buckets[cmb.NO_PR] == []


def test_collect_branch_statuses_buckets_each_category() -> None:
    """Mixed-state corpus distributes correctly across all four buckets."""
    pr_data = {
        "feature-merged":   [{"number": 10, "state": "MERGED", "mergedAt": "2026-01-01"}],
        "feature-open":     [{"number": 20, "state": "OPEN",   "mergedAt": None}],
        "feature-closed":   [{"number": 30, "state": "CLOSED", "mergedAt": None}],
        "feature-local":    [],
    }

    def fake_pr_lister(branch: str) -> list[dict]:
        return pr_data[branch]

    buckets = cmb.collect_branch_statuses(
        list(pr_data.keys()),
        current_branch="some-other-checkout",
        pr_lister=fake_pr_lister,
    )

    assert buckets[cmb.MERGED] == [("feature-merged", 10)]
    assert buckets[cmb.OPEN] == [("feature-open", 20)]
    assert buckets[cmb.CLOSED_UNMERGED] == [("feature-closed", 30)]
    assert buckets[cmb.NO_PR] == [("feature-local", None)]


def test_collect_branch_statuses_respects_extra_skip_branches() -> None:
    """``skip_branches`` arg lets a caller protect named branches.

    Used in main() to skip main itself; the test pins that a caller
    can extend this (e.g. to protect a 'release' branch).
    """
    calls: list[str] = []

    def fake_pr_lister(branch: str) -> list[dict]:
        calls.append(branch)
        return [{"number": 1, "state": "MERGED", "mergedAt": "2026-01-01"}]

    buckets = cmb.collect_branch_statuses(
        ["main", "release", "old-feature"],
        current_branch="",
        pr_lister=fake_pr_lister,
        skip_branches=("main", "release"),
    )

    assert "main" not in calls
    assert "release" not in calls
    assert calls == ["old-feature"]
    assert [b for b, _ in buckets[cmb.MERGED]] == ["old-feature"]


def test_collect_branch_statuses_ignores_empty_strings() -> None:
    """Empty branch names (which ``git branch --format`` should never emit
    but a defensive script handles anyway) are skipped silently."""
    def fake_pr_lister(branch: str) -> list[dict]:
        raise AssertionError(f"pr_lister should not be called for empty branch, got {branch!r}")

    buckets = cmb.collect_branch_statuses(
        ["", "real-branch"],
        current_branch="real-branch",  # skips the only non-empty entry
        pr_lister=fake_pr_lister,
    )

    # No buckets populated; lister never called.
    assert buckets == {
        cmb.MERGED: [],
        cmb.OPEN: [],
        cmb.CLOSED_UNMERGED: [],
        cmb.NO_PR: [],
    }


def test_classify_branch_unknown_state_falls_through_to_closed_unmerged() -> None:
    """Defensive: unknown PR states (e.g. future gh additions) classify as closed-unmerged.

    GitHub's PR states are MERGED/OPEN/CLOSED today. If a future
    schema adds DRAFT or similar and gh starts returning it through
    this code path, the safer bet is treating it as "don't delete"
    (CLOSED_UNMERGED bucket) rather than crashing or — worse —
    accidentally classifying as MERGED.
    """
    prs = [{"number": 200, "state": "DRAFT", "mergedAt": None}]
    status, pr_num = cmb.classify_branch(prs)
    assert status == cmb.CLOSED_UNMERGED
    assert pr_num == 200
