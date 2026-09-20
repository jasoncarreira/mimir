from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from typing import Sequence

import pytest

from mimir.worklink import merge_closure as closure
from mimir.worklink.dispatch_failures import dispatch_failure_state_dir, load_failure_state


HISTORICAL_IDS = (1295, 1296, 1297, 1298, 1299, 1300, 1301, 1762, 1780, 1781, 1766)
PR_URL = "https://github.com/example/project/pull/42"
MERGED_AT = "2026-09-20T12:00:00Z"
MERGE_SHA = "abcdef1234567890"


def cp(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def configured_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    home.mkdir(parents=True)
    repo.mkdir()
    (home / "worklink.yaml").write_text("repository: example/project\n", encoding="utf-8")
    (home / "repositories.yaml").write_text(
        "repositories:\n"
        "  - slug: example/project\n"
        f"    root: {repo}\n"
        "    mode: rw\n"
        "    origin: https://github.com/example/project.git\n"
        "    base_branch: main\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("WORKLINK_REPO", str(repo))
    monkeypatch.delenv("MIMIR_WORKLINK_REPO", raising=False)
    return home, repo


def git_runner(repo: Path):
    def run(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if list(args)[-2:] == ["rev-parse", "--show-toplevel"]:
            return cp(stdout=f"{repo}\n")
        if list(args)[-3:] == ["remote", "get-url", "origin"]:
            return cp(stdout="https://github.com/example/project.git\n")
        return cp(1, stderr="unexpected git command")

    return run


class Tracker:
    def __init__(
        self,
        issue_id: int,
        *,
        labels: set[str] | None = None,
        comments: list[str] | None = None,
        comment_visible: bool = True,
        close_visible: bool = True,
    ) -> None:
        self.issue_id = issue_id
        self.status = "open"
        self.labels = labels or {"worklink:review"}
        self.comments = comments or []
        self.comment_visible = comment_visible
        self.close_visible = close_visible
        self.calls: list[list[str]] = []

    def snapshot(self) -> dict[str, object]:
        return {
            "id": self.issue_id,
            "number": self.issue_id,
            "status": self.status,
            "closed": self.status == "closed",
            "labels": sorted(self.labels),
            "comments": list(self.comments),
            "parent_id": None,
        }

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args = list(args)
        self.calls.append(args)
        command = args[1:]
        if command[:2] == ["issue", "list"]:
            rows = [{"id": self.issue_id}] if self.status == "open" else []
            return cp(stdout=json.dumps(rows))
        if command[:2] == ["issue", "show"]:
            return cp(stdout=json.dumps(self.snapshot()))
        if command[:2] == ["issue", "comment"]:
            if self.comment_visible:
                self.comments.append(command[3])
            return cp()
        if command[:2] == ["issue", "close"]:
            if self.close_visible:
                self.status = "closed"
            return cp()
        if command[:2] == ["issue", "unlabel"]:
            self.labels.discard(command[3])
            return cp()
        return cp(1, stderr="unexpected tracker command")


def forge(issue_id: int, *, body: str | None = None, state: str = "closed", merged: bool = True,
          base: str = "main", slug: str = "example/project"):
    payload = {
        "number": 42,
        "html_url": PR_URL,
        "body": body if body is not None else f"Closes chainlink #{issue_id}.\n\nWorklink evidence.",
        "state": state,
        "merged": merged,
        "merged_at": MERGED_AT if merged else None,
        "merge_commit_sha": MERGE_SHA if merged else None,
        "base": {"repo": {"full_name": slug}, "ref": base},
    }

    def run(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        assert list(args) == ["gh", "api", "repos/example/project/pulls/42"]
        return cp(stdout=json.dumps(payload))

    return run


def evidence(home: Path, issue_id: int, *, url: str = PR_URL, attempt: int = 1,
             status: str = "completed", base_ref: str = "main") -> Path:
    directory = home / "state" / "worklink" / "evidence"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{issue_id}-{attempt}.json"
    path.write_text(json.dumps({
        "issue": issue_id,
        "attempt": attempt,
        "status": status,
        "pr_url": url,
        "base_ref": base_ref,
    }), encoding="utf-8")
    return path


@pytest.mark.parametrize("issue_id", HISTORICAL_IDS)
def test_canonical_historical_leaf_closes_audit_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, issue_id: int,
) -> None:
    """Historical IDs are synthetic positive fixtures, not payload replays."""
    home, repo = configured_home(tmp_path, monkeypatch)
    evidence(home, issue_id)
    tracker = Tracker(issue_id)

    outcomes = closure.reconcile_merged_leaves(
        home,
        chainlink_runner=tracker,
        gh_runner=forge(issue_id),
        git_runner=git_runner(repo),
    )

    assert [outcome.issue_id for outcome in outcomes] == [issue_id]
    mutations = [call[2] for call in tracker.calls if call[1] == "issue" and call[2] in {"comment", "close", "unlabel"}]
    assert mutations == ["comment", "close", "unlabel"]
    audit = tracker.comments[-1]
    assert audit.startswith("WORKLINK_CLOSED v1 ")
    assert f"Chainlink #{issue_id} complete via PR {PR_URL}." in audit
    assert f"Merged at {MERGED_AT}; merge commit {MERGE_SHA}; completion base main." in audit
    assert closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker, gh_runner=forge(issue_id), git_runner=git_runner(repo),
    ) == []
    assert mutations == ["comment", "close", "unlabel"]


@pytest.mark.parametrize("body", [
    "Closes chainlink #1295.\n\nCloses chainlink #1295.",
    "Closes chainlink #1295.\n\nThis is partial.",
    "Closes chainlink #1295.\n\nDo not close yet.",
    "> Closes chainlink #1295.",
    "- Closes chainlink #1295.",
    "```\nCloses chainlink #1295.\n```",
    "Closes chainlink #1295.\n\nFixes #7",
    "Closes chainlink #1295.\n\nRefs follow-up",
    "Closes chainlink #1295.\x00",
    "Closes chainlink #1295.\u200b",
])
def test_completion_grammar_negative_variants(body: str) -> None:
    assert not closure.parse_completion_reference(body, expected_issue_id=1295).qualifies


@pytest.mark.parametrize("issue_id", HISTORICAL_IDS)
def test_historical_ids_have_separate_noncompleting_variants(issue_id: int) -> None:
    decision = closure.parse_completion_reference(
        f"Refs chainlink #{issue_id}; remaining work follows.", expected_issue_id=issue_id,
    )
    assert not decision.qualifies


def test_evidence_association_inventory_refuses_conflict(tmp_path: Path) -> None:
    issue = closure.IssueSnapshot(7, "open", frozenset({"worklink:review"}), (), None)
    evidence(tmp_path, 7, url=PR_URL, attempt=1)
    evidence(tmp_path, 7, url="https://github.com/example/project/pull/43", attempt=2)
    association = closure.discover_associations(tmp_path, issue)
    assert association.reason == "conflicting_pr_associations"
    assert association.pr_url is None


def test_comment_associations_are_discovery_not_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    tracker = Tracker(9, comments=[f"WORKLINK_EVIDENCE issue=9 attempt=1 pr_url={PR_URL}"])
    outcomes = closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker,
        gh_runner=forge(9, body="Ordinary mention of chainlink #9."),
        git_runner=git_runner(repo),
    )
    assert outcomes == []
    assert tracker.status == "open"
    notices = load_failure_state(dispatch_failure_state_dir(home))["merge_reconciliations"]["notices"]
    assert any("missing_exact_completion_declaration" in entry["detail"] for entry in notices.values())


def test_unrelated_open_issue_is_silently_excluded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    tracker = Tracker(8, labels={"triage"})
    called = False

    def unexpected_forge(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        return cp(1)

    assert closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker, gh_runner=unexpected_forge,
        git_runner=git_runner(repo),
    ) == []
    assert not called
    state = load_failure_state(dispatch_failure_state_dir(home))
    assert state["merge_reconciliations"]["notices"] == {}


@pytest.mark.parametrize("body", [
    "Completes #10.",
    "PR #10 is done.",
    "Closes chainlink #11.",
])
def test_number_collision_and_inferred_sources(body: str) -> None:
    assert not closure.parse_completion_reference(body, expected_issue_id=10).qualifies


def test_open_pr_is_pending_without_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    evidence(home, 12)
    tracker = Tracker(12)
    assert closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker,
        gh_runner=forge(12, state="open", merged=False), git_runner=git_runner(repo),
    ) == []
    state = load_failure_state(dispatch_failure_state_dir(home))
    assert not state.get("merge_reconciliations", {}).get("notices")


def test_closed_unmerged_archives_only_completed_unique_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    path = evidence(home, 13)
    tracker = Tracker(13)
    assert closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker,
        gh_runner=forge(13, state="closed", merged=False), git_runner=git_runner(repo),
    ) == []
    assert not path.exists()
    assert path.with_suffix(".json.closed-unmerged").is_file()


@pytest.mark.parametrize("defect", ["number", "url", "merged_at", "sha", "base"])
def test_invalid_pr_reads_refuse(defect: str) -> None:
    payload = {
        "number": 42, "html_url": PR_URL, "body": "Closes chainlink #14.",
        "state": "closed", "merged": True, "merged_at": MERGED_AT,
        "merge_commit_sha": MERGE_SHA,
        "base": {"repo": {"full_name": "example/project"}, "ref": "main"},
    }
    field, value = {
        "number": ("number", 43),
        "url": ("html_url", PR_URL + "?x=1"),
        "merged_at": ("merged_at", "yesterday"),
        "sha": ("merge_commit_sha", "nope"),
        "base": ("base", None),
    }[defect]
    payload[field] = value
    with pytest.raises(closure.ClosureReadError):
        closure.read_pr_snapshot(PR_URL, gh_bin="gh", runner=lambda args: cp(stdout=json.dumps(payload)))


def test_completion_base_not_evidence_base_and_repository_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    evidence(home, 15, base_ref="release")
    tracker = Tracker(15)
    assert closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker, gh_runner=forge(15, base="release"),
        git_runner=git_runner(repo),
    ) == []
    assert tracker.status == "open"
    monkeypatch.setenv("WORKLINK_REPO", str(tmp_path / "other"))
    assert closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker, gh_runner=forge(15), git_runner=git_runner(repo),
    ) == []


def test_parented_leaf_qualifies_and_epic_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    evidence(home, 16)
    tracker = Tracker(16)
    original = tracker.snapshot
    tracker.snapshot = lambda: {**original(), "parent_id": 2}  # type: ignore[method-assign]
    assert [item.issue_id for item in closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker, gh_runner=forge(16), git_runner=git_runner(repo),
    )] == [16]

    home2, repo2 = configured_home(tmp_path / "second", monkeypatch)
    evidence(home2, 17)
    epic = Tracker(17, labels={"worklink:review", "worklink:epic"})
    assert closure.reconcile_merged_leaves(
        home2, chainlink_runner=epic, gh_runner=forge(17), git_runner=git_runner(repo2),
    ) == []


def test_dry_run_is_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    evidence(home, 18)
    tracker = Tracker(18)
    assert len(closure.reconcile_merged_leaves(
        home, dry_run=True, chainlink_runner=tracker,
        gh_runner=forge(18), git_runner=git_runner(repo),
    )) == 1
    assert tracker.status == "open"
    assert not (home / "state" / "pollers").exists()
    assert not (home / "state" / "worklink" / "merge-closure.lock").exists()


def test_audit_uncertainty_never_reposts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    evidence(home, 19)
    tracker = Tracker(19, comment_visible=False)
    for _ in range(2):
        assert closure.reconcile_merged_leaves(
            home, chainlink_runner=tracker, gh_runner=forge(19), git_runner=git_runner(repo),
        ) == []
    comments = [call for call in tracker.calls if call[1:3] == ["issue", "comment"]]
    closes = [call for call in tracker.calls if call[1:3] == ["issue", "close"]]
    assert len(comments) == 1
    assert closes == []


def test_close_uncertainty_never_replays_open_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    evidence(home, 20)
    tracker = Tracker(20, close_visible=False)
    for _ in range(2):
        assert closure.reconcile_merged_leaves(
            home, chainlink_runner=tracker, gh_runner=forge(20), git_runner=git_runner(repo),
        ) == []
    closes = [call for call in tracker.calls if call[1:3] == ["issue", "close"]]
    assert len(closes) == 1


def test_pending_recovery_with_empty_open_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    evidence(home, 21)
    tracker = Tracker(21, close_visible=False)
    closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker, gh_runner=forge(21), git_runner=git_runner(repo),
    )
    tracker.status = "closed"
    assert [item.issue_id for item in closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker, gh_runner=forge(21), git_runner=git_runner(repo),
    )] == [21]


def test_strict_tracker_snapshots() -> None:
    base = {
        "id": 22, "status": "open", "closed": False,
        "labels": ["worklink:review"], "comments": [], "parent_id": None,
    }
    assert closure.parse_issue_snapshot(base, expected_issue_id=22).issue_id == 22
    for field in ("status", "labels", "comments", "parent_id"):
        malformed = dict(base)
        malformed.pop(field)
        with pytest.raises(closure.ClosureReadError):
            closure.parse_issue_snapshot(malformed, expected_issue_id=22)
