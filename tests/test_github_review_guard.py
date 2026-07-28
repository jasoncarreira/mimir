from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from mimir.tools import github_review_guard as guard


def _request(command: str, *, direct_argv: list[str] | None = None) -> SimpleNamespace:
    args: dict[str, object] = {"command": command}
    if direct_argv is not None:
        args["mimir_direct_argv"] = direct_argv
    return SimpleNamespace(tool_call={"name": "shell_exec", "args": args})


class FakeGitHub:
    def __init__(self) -> None:
        self.head = "head-1"
        self.reviewer = "mimir-carreira"
        self.reviews: list[dict[str, object]] = []
        self.review_side_effects = 0
        self.lock = threading.Lock()

    def run(self, spec: guard.ReviewSubmission, arguments: list[str]):
        if arguments[:2] == ["api", "user"]:
            output = self.reviewer
        elif arguments[:2] == ["api", f"repos/o/r/pulls/{spec.number}"]:
            output = self.head
        elif arguments[:2] == ["api", f"repos/o/r/pulls/{spec.number}/reviews"]:
            with self.lock:
                output = json.dumps(self.reviews)
        else:  # pragma: no cover - catches an unexpected API shape clearly
            raise AssertionError(arguments)
        return SimpleNamespace(returncode=0, stdout=output)

    def submit(self, spec: guard.ReviewSubmission) -> None:
        with self.lock:
            self.review_side_effects += 1
            self.reviews.append({
                "user": {"login": self.reviewer},
                "commit_id": self.head,
                "state": spec.state,
            })


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    fake = FakeGitHub()
    monkeypatch.setattr(guard, "_run", fake.run)
    guard._locks.clear()
    return fake


def test_same_head_same_reviewer_same_state_is_suppressed(github: FakeGitHub) -> None:
    github.reviews.append({
        "user": {"login": github.reviewer},
        "commit_id": github.head,
        "state": "APPROVED",
    })
    spec = guard.ReviewSubmission("gh", "o/r", 152, "APPROVED", None)

    claim = guard.claim_review_submission(spec)

    assert claim is not None
    assert claim.duplicate is True
    assert (claim.repo, claim.number, claim.head, claim.reviewer, claim.state) == (
        "o/r", 152, "head-1", "mimir-carreira", "APPROVED",
    )
    claim.release()


@pytest.mark.parametrize(
    ("old_head", "old_state", "old_reviewer", "new_head", "new_state"),
    [
        ("head-1", "APPROVED", "mimir-carreira", "head-2", "APPROVED"),
        (
            "head-1", "CHANGES_REQUESTED", "mimir-carreira",
            "head-1", "APPROVED",
        ),
        ("head-1", "APPROVED", "another-reviewer", "head-1", "APPROVED"),
    ],
)
def test_new_head_different_state_and_different_reviewer_are_allowed(
    github: FakeGitHub,
    old_head: str,
    old_state: str,
    old_reviewer: str,
    new_head: str,
    new_state: str,
) -> None:
    github.reviews.append({
        "user": {"login": old_reviewer},
        "commit_id": old_head,
        "state": old_state,
    })
    github.head = new_head

    claim = guard.claim_review_submission(
        guard.ReviewSubmission("gh", "o/r", 152, new_state, None),
    )

    assert claim is not None
    assert claim.duplicate is False
    claim.release()


def test_recovered_poller_and_manual_turn_race_has_one_review_side_effect(
    github: FakeGitHub,
) -> None:
    manual = guard.review_submission_from_request(
        _request("gh pr review 152 --repo o/r --approve --body ok"),
    )
    poller = guard.review_submission_from_request(
        _request(
            "gh pr review 152 --repo o/r --approve --body ok",
            direct_argv=[
                "/usr/bin/gh", "pr", "review", "152", "--repo", "o/r",
                "--approve", "--body", "ok",
            ],
        ),
    )
    assert manual is not None and poller is not None

    start = threading.Barrier(2)

    def execute(spec: guard.ReviewSubmission) -> bool:
        start.wait()
        claim = guard.claim_review_submission(spec)
        assert claim is not None
        try:
            if claim.duplicate:
                return False
            github.submit(spec)
            return True
        finally:
            claim.release()

    with ThreadPoolExecutor(max_workers=2) as pool:
        submitted = list(pool.map(execute, [manual, poller]))

    assert sorted(submitted) == [False, True]
    assert github.review_side_effects == 1


def test_duplicate_signal_and_tool_result_do_not_include_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.tools import budget_gate

    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        budget_gate, "_emit_event_sync",
        lambda kind, **fields: events.append((kind, fields)),
    )
    request = SimpleNamespace(tool_call={
        "name": "shell_exec", "id": "call-1",
        "args": {"command": "gh pr review 152 --repo o/r --approve --body SECRET"},
    })
    claim = guard.ReviewClaim(
        repo="o/r", number=152, head="head-1", reviewer="mimir-carreira",
        state="APPROVED", duplicate=True,
    )

    result = budget_gate._duplicate_review_result(request, claim)

    assert result.status == "success"
    assert "already satisfies this submission" in str(result.content)
    assert "SECRET" not in str(result.content)
    assert events == [("github_review_duplicate_suppressed", {
        "repo": "o/r", "pr": 152, "head": "head-1",
        "reviewer": "mimir-carreira", "review_state": "APPROVED",
    })]
