from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.runtime import Runtime

from mimir.tools import github_review_guard as guard
from mimir.tools import extra
from tests.auth_helpers import middleware_auth_context


def _request(
    command: str,
    *,
    cwd: str | None = None,
    direct_argv: list[str] | None = None,
) -> SimpleNamespace:
    args: dict[str, object] = {"command": command}
    if cwd is not None:
        args["cwd"] = cwd
    if direct_argv is not None:
        args["mimir_direct_argv"] = direct_argv
    return SimpleNamespace(tool_call={"name": "shell_exec", "args": args})


class FakeGitHub:
    def __init__(self) -> None:
        self.head = "head-1"
        self.reviewer = "mimir-carreira"
        self.reviews: list[dict[str, object]] = []
        self.review_side_effects = 0
        self.current_number = 152
        self.repositories_by_cwd: dict[Path, str] = {}
        self.probe_cwds: list[Path] = []
        self.lock = threading.Lock()

    def run(self, spec: guard.ReviewSubmission, arguments: list[str]):
        cwd = Path(spec.cwd).resolve() if spec.cwd is not None else Path.cwd()
        self.probe_cwds.append(cwd)
        if arguments[:2] == ["api", "user"]:
            output = self.reviewer
        elif arguments[:2] == ["repo", "view"]:
            if cwd not in self.repositories_by_cwd:
                return SimpleNamespace(returncode=1, stdout="")
            output = self.repositories_by_cwd[cwd]
        elif arguments[:2] == ["api", f"repos/o/r/pulls/{spec.number}"]:
            output = self.head
        elif arguments[:2] == ["api", f"repos/o/r/pulls/{spec.number}/reviews"]:
            with self.lock:
                output = json.dumps(self.reviews)
        elif arguments[:2] == ["pr", "view"]:
            output = str(self.current_number)
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


def _tool_request(
    command: str,
    *,
    tool_call_id: str,
    direct_argv: list[str] | None = None,
) -> ToolCallRequest:
    args: dict[str, object] = {"command": command}
    if direct_argv is not None:
        args["mimir_direct_argv"] = direct_argv
    return ToolCallRequest(
        tool_call={
            "name": "shell_exec",
            "args": args,
            "id": tool_call_id,
            "type": "tool_call",
        },
        tool=None,
        state=None,
        runtime=Runtime(context=middleware_auth_context()),
    )


@pytest.mark.parametrize(
    "command",
    [
        "cd repo && gh pr review 152 --repo o/r --approve --body ok",
        "printf ready | gh pr review 152 --repo o/r --approve --body ok",
        "true; gh pr review 152 --repo o/r --approve --body ok",
    ],
)
def test_review_submission_is_found_in_compound_command(command: str) -> None:
    spec = guard.review_submission_from_request(_request(command))

    assert spec == guard.ReviewSubmission("gh", "o/r", 152, "APPROVED", None)


def test_direct_argv_remains_authoritative_for_compound_command() -> None:
    direct = [
        "/usr/bin/gh", "pr", "review", "152", "--repo", "o/r",
        "--approve", "--body", "ok",
    ]

    spec = guard.review_submission_from_request(
        _request(
            "gh pr review 999 --repo wrong/repo --request-changes && false",
            direct_argv=direct,
        ),
    )

    assert spec == guard.ReviewSubmission(
        "/usr/bin/gh", "o/r", 152, "APPROVED", None,
    )


def test_current_pull_request_is_inferred_when_number_is_omitted(
    github: FakeGitHub,
) -> None:
    spec = guard.review_submission_from_request(
        _request("gh pr review --repo o/r --approve --body ok"),
    )

    assert spec == guard.ReviewSubmission("gh", "o/r", None, "APPROVED", None)
    claim = guard.claim_review_submission(spec)

    assert claim is not None
    assert claim.number == github.current_number
    claim.release()


def test_omitted_cwd_uses_same_mimir_home_as_shell_exec(
    github: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process_repo = tmp_path / "process-repo"
    shell_repo = tmp_path / "shell-repo"
    process_repo.mkdir()
    shell_repo.mkdir()
    monkeypatch.chdir(process_repo)
    monkeypatch.setenv("MIMIR_HOME", str(shell_repo))
    github.repositories_by_cwd[shell_repo] = "o/r"
    shell_cwds: list[Path | None] = []

    def shell_run(*_args: object, **kwargs: object) -> SimpleNamespace:
        shell_cwds.append(kwargs.get("cwd"))  # type: ignore[arg-type]
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(extra.subprocess, "run", shell_run)
    spec = guard.review_submission_from_request(
        _request("gh pr review 152 --approve --body ok"),
    )
    assert spec is not None

    claim = guard.claim_review_submission(spec)
    extra.shell_exec.invoke({"command": "true"})

    assert claim is not None
    assert claim.repo == "o/r"
    assert github.probe_cwds and set(github.probe_cwds) == {shell_repo}
    assert shell_cwds == [shell_repo]
    claim.release()


def test_explicit_cwd_remains_authoritative(
    github: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    explicit_repo = tmp_path / "explicit-repo"
    explicit_repo.mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path / "other-repo"))
    github.repositories_by_cwd[explicit_repo] = "o/r"

    spec = guard.review_submission_from_request(
        _request("gh pr review 152 --approve", cwd=str(explicit_repo)),
    )
    assert spec is not None
    claim = guard.claim_review_submission(spec)

    assert claim is not None
    assert set(github.probe_cwds) == {explicit_repo}
    claim.release()


def test_compound_cd_resolves_review_repository(
    github: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    repo = home / "repo"
    repo.mkdir(parents=True)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    github.repositories_by_cwd[repo] = "o/r"

    spec = guard.review_submission_from_request(
        _request("cd repo && gh pr review 152 --approve --body ok"),
    )
    assert spec is not None
    claim = guard.claim_review_submission(spec)

    assert claim is not None
    assert set(github.probe_cwds) == {repo}
    claim.release()


def test_unknown_effective_repository_cannot_produce_duplicate(
    github: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MIMIR_HOME", raising=False)
    monkeypatch.setitem(extra._SHELL_STATE, "cwd", None)
    github.repositories_by_cwd[tmp_path] = "o/r"
    github.reviews.append({
        "user": {"login": github.reviewer},
        "commit_id": github.head,
        "state": "APPROVED",
    })

    spec = guard.review_submission_from_request(
        _request("gh pr review 152 --approve --body ok"),
    )

    assert spec is not None
    assert spec.cwd is None
    assert guard.claim_review_submission(spec) is None
    assert github.probe_cwds == []


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


def test_wrap_tool_call_recovered_poller_and_manual_race_has_one_side_effect(
    github: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.tools import budget_gate

    monkeypatch.setattr(budget_gate, "_emit_event_sync", lambda *args, **kwargs: None)
    # The service authorization layer authored this argv before the review guard.
    monkeypatch.setattr(
        budget_gate, "_request_for_authorized_execution", lambda request, *_: request,
    )
    manual = _tool_request(
        "cd repo && gh pr review 152 --repo o/r --approve --body ok",
        tool_call_id="manual",
    )
    poller = _tool_request(
        "gh pr review 152 --repo o/r --approve --body ok",
        tool_call_id="poller",
        direct_argv=[
            "/usr/bin/gh", "pr", "review", "152", "--repo", "o/r",
            "--approve", "--body", "ok",
        ],
    )

    start = threading.Barrier(2)

    def execute(request: ToolCallRequest) -> ToolMessage:
        start.wait()
        def handler(execution_request: ToolCallRequest) -> ToolMessage:
            spec = guard.review_submission_from_request(execution_request)
            assert spec is not None
            github.submit(spec)
            return ToolMessage(content="submitted", tool_call_id=request.tool_call["id"])

        return budget_gate.BudgetGateMiddleware().wrap_tool_call(request, handler)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, [manual, poller]))

    assert github.review_side_effects == 1
    assert sorted(str(result.content) for result in results) == [
        "GitHub review submission was not repeated: the existing APPROVED "
        "review by mimir-carreira on exact head head-1 already satisfies this submission.",
        "submitted",
    ]


def test_wrap_tool_call_releases_review_claim_when_handler_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.tools import budget_gate

    releases: list[None] = []
    claim = guard.ReviewClaim(
        "o/r", 152, "head-1", "mimir-carreira", "APPROVED", False,
    )
    claim.release = lambda: releases.append(None)  # type: ignore[method-assign]
    monkeypatch.setattr(guard, "claim_review_submission", lambda spec: claim)
    monkeypatch.setattr(budget_gate, "_emit_event_sync", lambda *args, **kwargs: None)

    def raises(_request: ToolCallRequest) -> ToolMessage:
        raise RuntimeError("handler failed")

    with pytest.raises(RuntimeError, match="handler failed"):
        budget_gate.BudgetGateMiddleware().wrap_tool_call(
            _tool_request(
                "gh pr review 152 --repo o/r --approve", tool_call_id="exception",
            ),
            raises,
        )

    assert len(releases) == 1


def test_wrap_tool_call_duplicate_release_remains_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.tools import budget_gate

    class CountingLock:
        releases = 0

        def release(self) -> None:
            self.releases += 1

    lock = CountingLock()
    claim = guard.ReviewClaim(
        "o/r", 152, "head-1", "mimir-carreira", "APPROVED", True,
        _lock=lock,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(guard, "claim_review_submission", lambda spec: claim)
    monkeypatch.setattr(budget_gate, "_emit_event_sync", lambda *args, **kwargs: None)
    called = False

    def handler(_request: ToolCallRequest) -> ToolMessage:
        nonlocal called
        called = True
        return ToolMessage(content="submitted", tool_call_id="duplicate")

    result = budget_gate.BudgetGateMiddleware().wrap_tool_call(
        _tool_request(
            "gh pr review 152 --repo o/r --approve", tool_call_id="duplicate",
        ),
        handler,
    )
    claim.release()

    assert called is False
    assert result.status == "success"
    assert lock.releases == 1


def test_wrap_tool_call_serves_service_refusal_before_review_detection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.tools import budget_gate

    refusal = "shell_exec was refused before execution: profile rejected command"
    request = _tool_request(
        "gh pr review 152 --repo o/r --approve", tool_call_id="refused",
    )
    monkeypatch.setattr(
        budget_gate,
        "_request_for_authorized_execution",
        lambda original, *_: original.override(tool_call={
            **original.tool_call,
            "args": {
                **original.tool_call["args"],
                "mimir_shell_refusal": refusal,
            },
        }),
    )
    monkeypatch.setattr(
        guard,
        "review_submission_from_request",
        lambda _request: pytest.fail("review detection ran before service refusal"),
    )
    monkeypatch.setattr(budget_gate, "_emit_event_sync", lambda *args, **kwargs: None)

    result = budget_gate.BudgetGateMiddleware().wrap_tool_call(
        request,
        lambda _request: pytest.fail("refused handler was executed"),
    )

    assert result.status == "error"
    assert result.content == refusal


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
