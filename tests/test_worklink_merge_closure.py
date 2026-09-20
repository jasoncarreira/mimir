from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
from typing import Sequence

import pytest

from mimir.worklink import merge_closure as closure
from mimir.worklink import dispatch_failures
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
    rerun_mutations = [
        call[2] for call in tracker.calls
        if call[1] == "issue" and call[2] in {"comment", "close", "unlabel"}
    ]
    assert rerun_mutations == ["comment", "close", "unlabel"]


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


@pytest.mark.parametrize("refusal_word", ["stack", "stacked", "epic"])
def test_stack_and_epic_body_language_refuses_at_reconciler_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusal_word: str,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    evidence(home, 1295)
    tracker = Tracker(1295)
    body = f"Closes chainlink #1295.\n\nThis {refusal_word} is ready."

    assert closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker, gh_runner=forge(1295, body=body),
        git_runner=git_runner(repo),
    ) == []

    assert not [
        call for call in tracker.calls
        if call[1:3] in (["issue", "comment"], ["issue", "close"], ["issue", "unlabel"])
    ]
    notices = load_failure_state(
        dispatch_failure_state_dir(home)
    )["merge_reconciliations"]["notices"]
    assert [entry["reason"] for entry in notices.values() if not entry["resolved"]] == [
        "completion_body_contains_refusal_language"
    ]


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


@pytest.mark.parametrize("defect", [
    "filename", "json", "issue", "attempt", "status", "url", "base",
])
def test_malformed_active_evidence_refuses_without_tracker_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    path = evidence(home, 7)
    if defect == "filename":
        path.rename(path.with_name("7-not-an-attempt.json"))
    elif defect == "json":
        path.write_text("{", encoding="utf-8")
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
        field, value = {
            "issue": ("issue", 8),
            "attempt": ("attempt", 2),
            "status": ("status", "unknown"),
            "url": ("pr_url", "https://github.com/example/project/pull/042"),
            "base": ("base_ref", 42),
        }[defect]
        payload[field] = value
        path.write_text(json.dumps(payload), encoding="utf-8")
    tracker = Tracker(7)
    forge_called = False

    def gh(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        nonlocal forge_called
        forge_called = True
        return cp()

    assert closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker, gh_runner=gh, git_runner=git_runner(repo),
    ) == []
    assert not forge_called
    assert not [
        call for call in tracker.calls
        if call[1:3] in (["issue", "comment"], ["issue", "close"], ["issue", "unlabel"])
    ]
    notices = load_failure_state(
        dispatch_failure_state_dir(home)
    )["merge_reconciliations"]["notices"]
    assert len([entry for entry in notices.values() if not entry["resolved"]]) == 1


def test_duplicate_active_evidence_with_same_url_remains_qualifying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    evidence(home, 8, attempt=1)
    evidence(home, 8, attempt=2)
    tracker = Tracker(8)

    outcomes = closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker, gh_runner=forge(8), git_runner=git_runner(repo),
    )

    assert [outcome.issue_id for outcome in outcomes] == [8]
    assert [
        call[2] for call in tracker.calls
        if call[1] == "issue" and call[2] in {"comment", "close", "unlabel"}
    ] == ["comment", "close", "unlabel"]


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


@pytest.mark.parametrize("foreign_identity", ["pr_repository", "base_repository"])
def test_foreign_pr_or_base_repository_refuses_without_tracker_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, foreign_identity: str,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    issue_id = 45
    pr_slug = "foreign/project" if foreign_identity == "pr_repository" else "example/project"
    base_slug = "foreign/project" if foreign_identity == "base_repository" else "example/project"
    pr_url = f"https://github.com/{pr_slug}/pull/42"
    evidence(home, issue_id, url=pr_url)
    tracker = Tracker(issue_id)
    payload = {
        "number": 42,
        "html_url": pr_url,
        "body": f"Closes chainlink #{issue_id}.",
        "state": "closed",
        "merged": True,
        "merged_at": MERGED_AT,
        "merge_commit_sha": MERGE_SHA,
        "base": {"repo": {"full_name": base_slug}, "ref": "main"},
    }

    def gh(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        assert list(args) == ["gh", "api", f"repos/{pr_slug}/pulls/42"]
        return cp(stdout=json.dumps(payload))

    assert closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker, gh_runner=gh, git_runner=git_runner(repo),
    ) == []
    assert not [
        call for call in tracker.calls
        if call[1:3] in (["issue", "comment"], ["issue", "close"], ["issue", "unlabel"])
    ]
    notices = load_failure_state(
        dispatch_failure_state_dir(home)
    )["merge_reconciliations"]["notices"]
    assert [entry["reason"] for entry in notices.values() if not entry["resolved"]] == [
        "repository_identity_mismatch"
    ]


def test_local_origin_mismatch_records_trust_refusal_before_tracker_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    tracker = Tracker(46)

    def wrong_origin(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if list(args)[-2:] == ["rev-parse", "--show-toplevel"]:
            return cp(stdout=f"{repo}\n")
        if list(args)[-3:] == ["remote", "get-url", "origin"]:
            return cp(stdout="https://github.com/other/project.git\n")
        return cp(1)

    assert closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker, gh_runner=forge(46), git_runner=wrong_origin,
    ) == []
    assert tracker.calls == []
    notices = load_failure_state(
        dispatch_failure_state_dir(home)
    )["merge_reconciliations"]["notices"]
    assert [entry["reason"] for entry in notices.values() if not entry["resolved"]] == [
        "repository_trust_failed"
    ]


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


@pytest.mark.parametrize("label", [
    "worklink:ready", "worklink:in-progress", "worklink:blocked",
])
def test_every_competing_lifecycle_label_refuses_before_forge_or_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    tracker = Tracker(47, labels={"worklink:review", label})
    forge_called = False

    def gh(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        nonlocal forge_called
        forge_called = True
        return cp()

    assert closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker, gh_runner=gh, git_runner=git_runner(repo),
    ) == []
    assert not forge_called
    assert not [
        call for call in tracker.calls
        if call[1:3] in (["issue", "comment"], ["issue", "close"], ["issue", "unlabel"])
    ]
    notices = load_failure_state(
        dispatch_failure_state_dir(home)
    )["merge_reconciliations"]["notices"]
    assert [entry["reason"] for entry in notices.values() if not entry["resolved"]] == [
        f"competing_lifecycle_label:{label}"
    ]


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


def _seed_intent(
    home: Path,
    issue_id: int,
    evidence_path: Path,
    *,
    stage: str,
    uncertainty: str | None = None,
    finalized: bool = False,
) -> tuple[str, str]:
    source_digest = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    identity = json.dumps([1, issue_id, "example/project", 42], separators=(",", ":"))
    key = hashlib.sha256(identity.encode()).hexdigest()
    audit = (
        f"WORKLINK_CLOSED v1 {key}\n"
        f"Chainlink #{issue_id} complete via PR {PR_URL}.\n"
        f"Merged at {MERGED_AT}; merge commit {MERGE_SHA}; completion base main."
    )
    entry = {
        "intent_key": key,
        "issue_id": issue_id,
        "repository": "example/project",
        "pr_number": 42,
        "pr_url": PR_URL,
        "base_ref": "main",
        "merge_commit_sha": MERGE_SHA,
        "merged_at": MERGED_AT,
        "audit_text": audit,
        "source_digests": [source_digest],
        "stage": stage,
        "result_finalized": finalized,
    }
    if uncertainty is not None:
        entry["uncertainty"] = uncertainty
    state_dir = dispatch_failure_state_dir(home)
    with dispatch_failures.merge_reconciliation_transaction(state_dir) as reconciliations:
        reconciliations["intents"][key] = entry
    return key, audit


@pytest.mark.parametrize(
    ("stage", "initial_status", "marker", "uncertainty", "review_label", "actions", "result"),
    [
        ("discovered", "open", False, None, True, ["comment", "close", "unlabel"], True),
        ("audit_started", "open", True, None, True, ["close", "unlabel"], True),
        ("audit_started", "open", False, None, True, [], False),
        ("audit_confirmed", "open", True, None, True, ["close", "unlabel"], True),
        ("close_started", "open", True, None, True, [], False),
        ("close_started", "closed", True, None, True, ["unlabel"], True),
        ("closed_verified", "closed", True, None, True, ["unlabel"], True),
        ("cleanup_pending", "closed", True, None, True, ["unlabel"], True),
        ("cleanup_pending", "closed", True, None, False, [], True),
        ("uncertain", "open", True, "audit_outcome_uncertain", True, ["close", "unlabel"], True),
        ("uncertain", "closed", True, "close_outcome_uncertain", True, ["unlabel"], True),
    ],
)
def test_intent_stage_and_external_call_crash_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    initial_status: str,
    marker: bool,
    uncertainty: str | None,
    review_label: bool,
    actions: list[str],
    result: bool,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    path = evidence(home, 30)
    tracker = Tracker(30, labels={"worklink:review"} if review_label else {"done"})
    _, audit = _seed_intent(
        home, 30, path, stage=stage, uncertainty=uncertainty,
    )
    tracker.status = initial_status
    if marker:
        tracker.comments.append(audit)

    outcomes = closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker, gh_runner=forge(30), git_runner=git_runner(repo),
    )

    actual_actions = [
        call[2] for call in tracker.calls
        if call[1] == "issue" and call[2] in {"comment", "close", "unlabel"}
    ]
    assert actual_actions == actions
    assert bool(outcomes) is result
    ledger = load_failure_state(dispatch_failure_state_dir(home))["merge_reconciliations"]
    persisted = next(iter(ledger["intents"].values()))
    if result:
        assert persisted["stage"] == "finalized"
        assert persisted["result_finalized"] is True
    elif stage in {"audit_started", "close_started"}:
        assert persisted["stage"] == "uncertain"


def test_finalized_tombstone_never_recloses_reopened_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    path = evidence(home, 31)
    tracker = Tracker(31)
    _, audit = _seed_intent(home, 31, path, stage="finalized", finalized=True)
    tracker.comments.append(audit)
    assert closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker, gh_runner=forge(31), git_runner=git_runner(repo),
    ) == []
    assert not [call for call in tracker.calls if call[1:3] in (["issue", "comment"], ["issue", "close"], ["issue", "unlabel"])]


def test_associated_non_review_candidate_is_visible_but_unrelated_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    evidence(home, 32)
    tracker = Tracker(32, labels={"triage"})
    closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker, gh_runner=forge(32), git_runner=git_runner(repo),
    )
    notices = load_failure_state(dispatch_failure_state_dir(home))["merge_reconciliations"]["notices"]
    assert [entry["reason"] for entry in notices.values() if not entry["resolved"]] == [
        "review_lifecycle_required"
    ]


@pytest.mark.parametrize("config", ["repositories", "worklink"])
def test_malformed_yaml_records_trust_refusal_before_tracker_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: str,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    (home / f"{config}.yaml").write_text("broken: [", encoding="utf-8")
    tracker = Tracker(33)
    assert closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker, gh_runner=forge(33), git_runner=git_runner(repo),
    ) == []
    assert tracker.calls == []
    notices = load_failure_state(dispatch_failure_state_dir(home))["merge_reconciliations"]["notices"]
    assert [entry["reason"] for entry in notices.values() if not entry["resolved"]] == [
        "repository_trust_failed"
    ]


def test_non_finite_config_numeric_records_trust_refusal_before_tracker_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    (home / "worklink.yaml").write_text(
        "repository: example/project\ndefaults:\n  timeout_s: .inf\n",
        encoding="utf-8",
    )
    tracker = Tracker(48)
    assert closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker, gh_runner=forge(48), git_runner=git_runner(repo),
    ) == []
    assert tracker.calls == []
    notices = load_failure_state(
        dispatch_failure_state_dir(home)
    )["merge_reconciliations"]["notices"]
    assert [entry["reason"] for entry in notices.values() if not entry["resolved"]] == [
        "repository_trust_failed"
    ]


@pytest.mark.parametrize("failure", [OSError("path unavailable"), RuntimeError("symlink loop")])
def test_environment_repository_path_resolution_failure_is_durable_and_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    broken = tmp_path / "broken-environment-path"
    monkeypatch.setenv("WORKLINK_REPO", str(broken))
    original_resolve = Path.resolve

    def resolve(path: Path, *args, **kwargs):
        if path == broken:
            raise failure
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    tracker = Tracker(49)
    assert closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker, gh_runner=forge(49), git_runner=git_runner(repo),
    ) == []
    assert tracker.calls == []
    notices = load_failure_state(
        dispatch_failure_state_dir(home)
    )["merge_reconciliations"]["notices"]
    assert [entry["reason"] for entry in notices.values() if not entry["resolved"]] == [
        "repository_trust_failed"
    ]


@pytest.mark.parametrize("payload", [
    {"issues": "not-a-list"},
    [{}],
    [{"id": 1, "number": 2}],
    [{"id": 0}],
])
def test_malformed_open_inventory_is_visible_and_stops_before_issue_or_forge_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: object,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    calls = []

    def tracker(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return cp(stdout=json.dumps(payload))

    forge_called = False

    def gh(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        nonlocal forge_called
        forge_called = True
        return cp()

    assert closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker, gh_runner=gh, git_runner=git_runner(repo),
    ) == []
    assert len(calls) == 1
    assert not forge_called
    notices = load_failure_state(dispatch_failure_state_dir(home))["merge_reconciliations"]["notices"]
    assert [entry["reason"] for entry in notices.values() if not entry["resolved"]] == [
        "tracker_inventory_failed"
    ]


def test_non_finite_tracker_numeric_is_visible_and_stops_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    calls = []

    def tracker(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return cp(stdout='[{"id": Infinity}]')

    assert closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker, gh_runner=forge(50), git_runner=git_runner(repo),
    ) == []
    assert len(calls) == 1
    notices = load_failure_state(
        dispatch_failure_state_dir(home)
    )["merge_reconciliations"]["notices"]
    assert [entry["reason"] for entry in notices.values() if not entry["resolved"]] == [
        "tracker_inventory_failed"
    ]


def test_malformed_issue_snapshot_is_visible_and_stops_before_forge_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    evidence(home, 33)
    tracker = Tracker(33)
    original = tracker.__call__

    def malformed(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if list(args)[1:3] == ["issue", "show"]:
            tracker.calls.append(list(args))
            return cp(stdout=json.dumps({"id": 33, "labels": ["worklink:review"]}))
        return original(args)

    forge_called = False

    def gh(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        nonlocal forge_called
        forge_called = True
        return cp()

    assert closure.reconcile_merged_leaves(
        home, chainlink_runner=malformed, gh_runner=gh, git_runner=git_runner(repo),
    ) == []
    assert not forge_called
    assert not [call for call in tracker.calls if call[1:3] in (["issue", "comment"], ["issue", "close"], ["issue", "unlabel"])]
    notices = load_failure_state(dispatch_failure_state_dir(home))["merge_reconciliations"]["notices"]
    assert any(not entry["resolved"] for entry in notices.values())


def test_already_closed_issue_without_intent_is_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    evidence(home, 44)
    tracker = Tracker(44)
    tracker.status = "closed"
    assert closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker, gh_runner=forge(44), git_runner=git_runner(repo),
    ) == []
    assert len(tracker.calls) == 1


@pytest.mark.parametrize("pr_state", ["open", "closed_unmerged"])
def test_open_and_unmerged_reevaluation_resolve_superseded_notices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pr_state: str,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    path = evidence(home, 34)
    state_dir = dispatch_failure_state_dir(home)
    old = dispatch_failures.record_merge_reconciliation_notice(
        state_dir, issue_id=34, repository="example/project", pr_url=PR_URL,
        reason="old_refusal", detail="superseded",
    )
    tracker = Tracker(34)
    gh = (
        forge(34, state="open", merged=False)
        if pr_state == "open" else forge(34, state="closed", merged=False)
    )
    closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker, gh_runner=gh, git_runner=git_runner(repo),
    )
    notice = load_failure_state(state_dir)["merge_reconciliations"]["notices"][old["delivery_key"]]
    assert notice["resolved"] is True
    if pr_state == "closed_unmerged":
        assert path.with_suffix(".json.closed-unmerged").exists()


def test_changed_refusal_resolves_prior_and_keeps_current_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    evidence(home, 35)
    state_dir = dispatch_failure_state_dir(home)
    old = dispatch_failures.record_merge_reconciliation_notice(
        state_dir, issue_id=35, repository="example/project", pr_url=PR_URL,
        reason="old_refusal", detail="superseded",
    )
    tracker = Tracker(35, labels={"worklink:review", "worklink:epic"})
    closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker, gh_runner=forge(35), git_runner=git_runner(repo),
    )
    notices = load_failure_state(state_dir)["merge_reconciliations"]["notices"]
    assert notices[old["delivery_key"]]["resolved"] is True
    current = [entry for entry in notices.values() if not entry["resolved"]]
    assert [entry["reason"] for entry in current] == ["epic_not_leaf"]


def test_merge_between_sweeps_is_reconciled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    evidence(home, 36)
    tracker = Tracker(36)
    assert closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker,
        gh_runner=forge(36, state="open", merged=False), git_runner=git_runner(repo),
    ) == []
    assert [item.issue_id for item in closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker, gh_runner=forge(36), git_runner=git_runner(repo),
    )] == [36]


class FailingTracker(Tracker):
    def __init__(self, *args, fail_action: str, raises: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fail_action = fail_action
        self.raises = raises

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        action = list(args)[2] if len(args) > 2 and list(args)[1] == "issue" else ""
        if action == self.fail_action:
            self.calls.append(list(args))
            if self.raises:
                raise OSError(f"{action} transport failed")
            return cp(1, stderr=f"{action} failed")
        return super().__call__(args)


@pytest.mark.parametrize(("action", "raises"), [
    ("comment", False), ("comment", True), ("close", False), ("close", True),
])
def test_comment_and_close_command_failures_become_uncertain_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str, raises: bool,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    evidence(home, 37)
    tracker = FailingTracker(37, fail_action=action, raises=raises)
    for _ in range(2):
        assert closure.reconcile_merged_leaves(
            home, chainlink_runner=tracker, gh_runner=forge(37), git_runner=git_runner(repo),
        ) == []
    calls = [call for call in tracker.calls if call[1:3] == ["issue", action]]
    assert len(calls) == 1
    entry = next(iter(load_failure_state(
        dispatch_failure_state_dir(home)
    )["merge_reconciliations"]["intents"].values()))
    assert entry["stage"] == "uncertain"
    expected = "audit_outcome_uncertain" if action == "comment" else "close_outcome_uncertain"
    assert entry["uncertainty"] == expected


def test_cleanup_failure_retries_only_while_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    path = evidence(home, 38)
    tracker = FailingTracker(38, fail_action="unlabel")
    _, audit = _seed_intent(home, 38, path, stage="closed_verified")
    tracker.status = "closed"
    tracker.comments.append(audit)
    assert closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker, gh_runner=forge(38), git_runner=git_runner(repo),
    ) == []
    tracker.fail_action = "never"
    assert [item.issue_id for item in closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker, gh_runner=forge(38), git_runner=git_runner(repo),
    )] == [38]
    tracker.status = "open"
    tracker.labels.add("worklink:review")
    assert closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker, gh_runner=forge(38), git_runner=git_runner(repo),
    ) == []
    assert len([call for call in tracker.calls if call[1:3] == ["issue", "unlabel"]]) == 2


def test_forge_failure_is_visible_and_never_reaches_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    evidence(home, 39)
    tracker = Tracker(39)
    assert closure.reconcile_merged_leaves(
        home, chainlink_runner=tracker,
        gh_runner=lambda args: cp(1, stderr="forge unavailable"),
        git_runner=git_runner(repo),
    ) == []
    assert not [call for call in tracker.calls if call[1:3] in (["issue", "comment"], ["issue", "close"], ["issue", "unlabel"])]
    notices = load_failure_state(dispatch_failure_state_dir(home))["merge_reconciliations"]["notices"]
    assert any(not entry["resolved"] for entry in notices.values())


@pytest.mark.parametrize("defect", [
    "issue", "repository", "number", "url", "base", "sha", "time", "digest",
    "audit", "stage", "result", "uncertainty",
])
def test_semantically_corrupt_intent_halts_before_tracker_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    path = evidence(home, 40)
    key, _ = _seed_intent(home, 40, path, stage="discovered")
    state_dir = dispatch_failure_state_dir(home)
    with dispatch_failures.failure_state_transaction(state_dir) as state:
        entry = state["merge_reconciliations"]["intents"][key]
        field, value = {
            "issue": ("issue_id", 0),
            "repository": ("repository", "Example/Project"),
            "number": ("pr_number", 0),
            "url": ("pr_url", PR_URL + "/extra"),
            "base": ("base_ref", "bad base"),
            "sha": ("merge_commit_sha", "not-a-sha"),
            "time": ("merged_at", "2026-09-20T12:00:00"),
            "digest": ("source_digests", ["bad"]),
            "audit": ("audit_text", "wrong"),
            "stage": ("stage", "invented"),
            "result": ("result_finalized", True),
            "uncertainty": ("uncertainty", "close_outcome_uncertain"),
        }[defect]
        entry[field] = value
    tracker = Tracker(40)
    with pytest.raises(OSError, match="merge reconciliation state unavailable"):
        closure.reconcile_merged_leaves(
            home, chainlink_runner=tracker, gh_runner=forge(40), git_runner=git_runner(repo),
        )
    assert not [call for call in tracker.calls if call[1:3] in (["issue", "comment"], ["issue", "close"], ["issue", "unlabel"])]


def test_ambiguous_unresolved_intents_halt_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    path = evidence(home, 41)
    key, _ = _seed_intent(home, 41, path, stage="discovered")
    state_dir = dispatch_failure_state_dir(home)
    with dispatch_failures.failure_state_transaction(state_dir) as state:
        first = state["merge_reconciliations"]["intents"][key]
        second = dict(first, pr_number=43, pr_url="https://github.com/example/project/pull/43")
        identity = json.dumps([1, 41, "example/project", 43], separators=(",", ":"))
        second_key = hashlib.sha256(identity.encode()).hexdigest()
        second["intent_key"] = second_key
        second["audit_text"] = (
            f"WORKLINK_CLOSED v1 {second_key}\n"
            f"Chainlink #41 complete via PR {second['pr_url']}.\n"
            f"Merged at {MERGED_AT}; merge commit {MERGE_SHA}; completion base main."
        )
        state["merge_reconciliations"]["intents"][second_key] = second
    tracker = Tracker(41)
    with pytest.raises(OSError, match="ambiguous unresolved issue intent"):
        closure.reconcile_merged_leaves(
            home, chainlink_runner=tracker, gh_runner=forge(41), git_runner=git_runner(repo),
        )
    assert not [call for call in tracker.calls if call[1:3] in (["issue", "comment"], ["issue", "close"], ["issue", "unlabel"])]


def test_durability_failure_halts_before_external_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    evidence(home, 42)
    state_dir = dispatch_failure_state_dir(home)
    dispatch_failures.pending_merge_reconciliation_notices(state_dir)
    monkeypatch.setattr(
        dispatch_failures, "_fsync_failure_state",
        lambda state_dir: (_ for _ in ()).throw(OSError("fsync failed")),
    )
    tracker = Tracker(42)
    with pytest.raises(OSError, match="fsync failed"):
        closure.reconcile_merged_leaves(
            home, chainlink_runner=tracker, gh_runner=forge(42), git_runner=git_runner(repo),
        )
    assert not [call for call in tracker.calls if call[1:3] in (["issue", "comment"], ["issue", "close"], ["issue", "unlabel"])]


@pytest.mark.parametrize("corruption", ["version", "json", "namespace"])
def test_malformed_durability_halts_before_external_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corruption: str,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    evidence(home, 43)
    state_dir = dispatch_failure_state_dir(home)
    dispatch_failures.pending_merge_reconciliation_notices(state_dir)
    if corruption == "json":
        (state_dir / dispatch_failures.STATE_FILE).write_text("{", encoding="utf-8")
    else:
        with dispatch_failures.failure_state_transaction(state_dir) as state:
            if corruption == "version":
                state["version"] = 2
            else:
                state["merge_reconciliations"] = {"intents": [], "notices": {}}
    tracker = Tracker(43)
    with pytest.raises(OSError):
        closure.reconcile_merged_leaves(
            home, chainlink_runner=tracker, gh_runner=forge(43), git_runner=git_runner(repo),
        )
    assert not [call for call in tracker.calls if call[1:3] in (["issue", "comment"], ["issue", "close"], ["issue", "unlabel"])]


def test_overlapping_process_sweeps_are_serialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, repo = configured_home(tmp_path, monkeypatch)
    journal = tmp_path / "journal"
    script = r'''
import os, subprocess, sys, time
from pathlib import Path
from mimir.worklink.merge_closure import reconcile_merged_leaves
home, repo, journal = map(Path, sys.argv[1:])
def cp(out=""):
    return subprocess.CompletedProcess([], 0, out, "")
def git(args):
    if args[-2:] == ["rev-parse", "--show-toplevel"]:
        with journal.open("a") as handle:
            handle.write(f"start {os.getpid()}\n"); handle.flush(); os.fsync(handle.fileno())
        time.sleep(.25)
        with journal.open("a") as handle:
            handle.write(f"end {os.getpid()}\n"); handle.flush(); os.fsync(handle.fileno())
        return cp(str(repo) + "\n")
    return cp("https://github.com/example/project.git\n")
def chainlink(args):
    return cp("[]")
reconcile_merged_leaves(home, chainlink_runner=chainlink, gh_runner=lambda a: cp("{}"), git_runner=git)
'''
    env = {**os.environ, "WORKLINK_REPO": str(repo)}
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(home), str(repo), str(journal)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for _ in range(2)
    ]
    outputs = [process.communicate(timeout=20) for process in processes]
    assert [(process.returncode, stderr) for process, (_, stderr) in zip(processes, outputs)] == [
        (0, ""), (0, ""),
    ]
    events = journal.read_text(encoding="utf-8").splitlines()
    assert [event.split()[0] for event in events] == ["start", "end", "start", "end"]
    assert events[0].split()[1] == events[1].split()[1]
    assert events[2].split()[1] == events[3].split()[1]
    assert events[0].split()[1] != events[2].split()[1]


# chainlink #1304 integrated-acceptance repair: the two production defects the
# factory's own acceptance review caught against a green suite.


@pytest.mark.parametrize("number_value", [True, 1.0])
def test_pr_identity_rejects_values_that_merely_compare_equal(number_value: object) -> None:
    """True == 1 and 1.0 == 1; an identity check must reject both.

    Equality alone admitted a bool or float where the forge must have returned
    an integer, so a snapshot could satisfy the identity gate without being the
    pull request that was asked for.
    """
    url = "https://github.com/example/project/pull/1"
    payload = {
        "number": number_value, "html_url": url, "body": "Closes chainlink #14.",
        "state": "closed", "merged": True, "merged_at": MERGED_AT,
        "merge_commit_sha": MERGE_SHA,
        "base": {"repo": {"full_name": "example/project"}, "ref": "main"},
    }
    with pytest.raises(closure.ClosureReadError):
        closure.read_pr_snapshot(url, gh_bin="gh", runner=lambda args: cp(stdout=json.dumps(payload)))


def test_pr_identity_still_admits_a_genuine_integer_number() -> None:
    """The guard must reject the type, not the value: a real int still passes."""
    url = "https://github.com/example/project/pull/1"
    payload = {
        "number": 1, "html_url": url, "body": "Closes chainlink #14.",
        "state": "closed", "merged": True, "merged_at": MERGED_AT,
        "merge_commit_sha": MERGE_SHA,
        "base": {"repo": {"full_name": "example/project"}, "ref": "main"},
    }
    snapshot = closure.read_pr_snapshot(
        url, gh_bin="gh", runner=lambda args: cp(stdout=json.dumps(payload)),
    )
    assert snapshot.number == 1


def _seed_pending_intent(home: Path, issue_id: int, *, stage: str) -> str:
    """Persist one unfinalised intent at *stage*, as a prior pass would leave it."""
    state_dir = dispatch_failure_state_dir(home)
    key = hashlib.sha256(
        json.dumps([1, issue_id, "example/project", 42], separators=(",", ":")).encode()
    ).hexdigest()
    closure._persist_intent(state_dir, key, {
        "intent_key": key,
        "issue_id": issue_id,
        "repository": "example/project",
        "pr_number": 42,
        "pr_url": PR_URL,
        "base_ref": "main",
        "merge_commit_sha": MERGE_SHA,
        "merged_at": MERGED_AT,
        "audit_text": (
            f"WORKLINK_CLOSED v1 {key}\n"
            f"Chainlink #{issue_id} complete via PR {PR_URL}.\n"
            f"Merged at {MERGED_AT}; merge commit {MERGE_SHA}; completion base main."
        ),
        "source_digests": [],
    })
    closure._update_intent(state_dir, key, stage=stage)
    return key


def _tracker_failing_only_on_issue_show(issue_id: int):
    """Inventory succeeds; reading the issue fails, as a degraded tracker does.

    Failing every call instead stops the sweep at inventory, which is a
    different defect and would not reach the recovery path under test.
    """
    inventory = Tracker(issue_id)

    def run(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if list(args)[1:3] == ["issue", "show"]:
            return cp(returncode=1, stderr="tracker unavailable")
        return inventory(args)

    return run


@pytest.mark.parametrize("stage", ["close_started", "closed_verified", "cleanup_pending"])
def test_recovery_read_failure_records_a_notice_instead_of_escaping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    """A second read failure in the verified-close recovery path must not escape.

    The recovery branch re-reads the very sources whose failure put it there. An
    unguarded failure propagated out of _process_intent and aborted the entire
    reconciliation pass, leaving no durable record of why and skipping every
    other issue in the batch.
    """
    home, repo = configured_home(tmp_path, monkeypatch)
    issue_id = 1295
    evidence(home, issue_id)
    _seed_pending_intent(home, issue_id, stage=stage)

    outcomes = closure.reconcile_merged_leaves(
        home,
        chainlink_runner=_tracker_failing_only_on_issue_show(issue_id),
        gh_runner=forge(issue_id),
        git_runner=git_runner(repo),
    )

    assert outcomes == []
    notices = load_failure_state(dispatch_failure_state_dir(home))["merge_reconciliations"]["notices"]
    reasons = [entry["reason"] for entry in notices.values() if not entry["resolved"]]
    assert reasons, "a read failure during recovery must leave a durable notice"
    assert all(reason.endswith("_read_failed") or reason == "intent_revalidation_failed" for reason in reasons), reasons


@pytest.mark.parametrize("number_value", [True, 1.0])
def test_reconciler_rejects_non_integer_pr_identity_without_tracker_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    number_value: object,
) -> None:
    """Malformed forge identity is refused at the reconciler boundary.

    This complements the parser-level regression above by proving that a value
    equal to pull request 1 cannot reach any tracker mutation and leaves the
    durable reconciliation surface populated for an operator.
    """
    home, repo = configured_home(tmp_path, monkeypatch)
    issue_id = 1295
    pr_url = "https://github.com/example/project/pull/1"
    evidence(home, issue_id, url=pr_url)
    tracker = Tracker(issue_id)
    payload = {
        "number": number_value,
        "html_url": pr_url,
        "body": f"Closes chainlink #{issue_id}.",
        "state": "closed",
        "merged": True,
        "merged_at": MERGED_AT,
        "merge_commit_sha": MERGE_SHA,
        "base": {"repo": {"full_name": "example/project"}, "ref": "main"},
    }

    def malformed_forge(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        assert list(args) == ["gh", "api", "repos/example/project/pulls/1"]
        return cp(stdout=json.dumps(payload))

    for _ in range(2):
        assert closure.reconcile_merged_leaves(
            home,
            chainlink_runner=tracker,
            gh_runner=malformed_forge,
            git_runner=git_runner(repo),
        ) == []

    assert tracker.status == "open"
    assert not [
        call for call in tracker.calls
        if call[1:3] in (["issue", "comment"], ["issue", "close"], ["issue", "unlabel"])
    ]
    notices = load_failure_state(
        dispatch_failure_state_dir(home)
    )["merge_reconciliations"]["notices"]
    unresolved = [entry for entry in notices.values() if not entry["resolved"]]
    assert len(unresolved) == 1
    assert unresolved[0]["reason"] == "reconciliation_read_failed"
    assert unresolved[0]["detail"] == "PR snapshot identity mismatch"


class _SequencedIssueReadTracker(Tracker):
    """Fail selected production-boundary issue reads while retaining mutations."""

    def __init__(self, issue_id: int, *, fail_show_calls: set[int]) -> None:
        super().__init__(issue_id)
        self.fail_show_calls = fail_show_calls
        self.show_calls = 0

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = list(args)
        if command[1:3] == ["issue", "show"]:
            self.show_calls += 1
            if self.show_calls in self.fail_show_calls:
                self.calls.append(command)
                return cp(returncode=1, stderr="tracker unavailable")
        return super().__call__(command)


def _recovery_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stage: str,
    fail_show_calls: set[int],
) -> tuple[Path, Path, _SequencedIssueReadTracker]:
    home, repo = configured_home(tmp_path, monkeypatch)
    issue_id = 1295
    evidence_path = evidence(home, issue_id)
    tracker = _SequencedIssueReadTracker(issue_id, fail_show_calls=fail_show_calls)
    _, audit = _seed_intent(home, issue_id, evidence_path, stage=stage)
    tracker.status = "closed"
    tracker.comments.append(audit)
    return home, repo, tracker


def test_close_recovery_fallback_read_failure_is_visible_without_replaying_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, repo, tracker = _recovery_fixture(
        tmp_path, monkeypatch, stage="close_started", fail_show_calls={1, 2, 3, 4},
    )

    for _ in range(2):
        assert closure.reconcile_merged_leaves(
            home,
            chainlink_runner=tracker,
            gh_runner=forge(1295),
            git_runner=git_runner(repo),
        ) == []

    assert not [
        call for call in tracker.calls
        if call[1:3] in (["issue", "comment"], ["issue", "close"], ["issue", "unlabel"])
    ]
    notices = load_failure_state(
        dispatch_failure_state_dir(home)
    )["merge_reconciliations"]["notices"]
    unresolved = [entry for entry in notices.values() if not entry["resolved"]]
    assert [entry["reason"] for entry in unresolved] == ["close_recovery_read_failed"]


def test_pre_cleanup_read_failure_is_visible_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, repo, tracker = _recovery_fixture(
        tmp_path, monkeypatch, stage="closed_verified", fail_show_calls={3, 6},
    )

    for _ in range(2):
        assert closure.reconcile_merged_leaves(
            home,
            chainlink_runner=tracker,
            gh_runner=forge(1295),
            git_runner=git_runner(repo),
        ) == []

    assert not [
        call for call in tracker.calls
        if call[1:3] in (["issue", "comment"], ["issue", "close"], ["issue", "unlabel"])
    ]
    state = load_failure_state(dispatch_failure_state_dir(home))["merge_reconciliations"]
    assert next(iter(state["intents"].values()))["stage"] == "closed_verified"
    unresolved = [entry for entry in state["notices"].values() if not entry["resolved"]]
    assert [entry["reason"] for entry in unresolved] == ["cleanup_read_failed"]


def test_post_unlabel_read_failure_is_visible_and_cleanup_is_not_duplicated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, repo, tracker = _recovery_fixture(
        tmp_path, monkeypatch, stage="closed_verified", fail_show_calls={4},
    )

    assert closure.reconcile_merged_leaves(
        home,
        chainlink_runner=tracker,
        gh_runner=forge(1295),
        git_runner=git_runner(repo),
    ) == []
    state = load_failure_state(dispatch_failure_state_dir(home))["merge_reconciliations"]
    assert next(iter(state["intents"].values()))["stage"] == "cleanup_pending"
    unresolved = [entry for entry in state["notices"].values() if not entry["resolved"]]
    assert [entry["reason"] for entry in unresolved] == ["cleanup_read_failed"]
    assert len([call for call in tracker.calls if call[1:3] == ["issue", "unlabel"]]) == 1

    outcomes = closure.reconcile_merged_leaves(
        home,
        chainlink_runner=tracker,
        gh_runner=forge(1295),
        git_runner=git_runner(repo),
    )

    assert [outcome.issue_id for outcome in outcomes] == [1295]
    assert len([call for call in tracker.calls if call[1:3] == ["issue", "unlabel"]]) == 1
