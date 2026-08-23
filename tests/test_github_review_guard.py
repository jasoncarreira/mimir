from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain.agents.middleware import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.runtime import Runtime

from mimir.tools import github_review_guard as guard
from mimir.tools import extra
from mimir.tools.refusals import ToolPolicyRefusal
from tests.auth_helpers import middleware_auth_context


def _request(
    command: str,
    *,
    direct_argv: list[str] | None = None,
    cwd: str | None = None,
) -> SimpleNamespace:
    args: dict[str, object] = {"command": command}
    if direct_argv is not None:
        args["mimir_direct_argv"] = direct_argv
    if cwd is not None:
        args["cwd"] = cwd
    return SimpleNamespace(tool_call={"name": "shell_exec", "args": args})


class FakeGitHub:
    def __init__(self) -> None:
        self.head = "head-1"
        self.reviewer = "mimir-carreira"
        self.reviews: list[dict[str, object]] = []
        self.review_side_effects = 0
        self.current_number = 152
        self.lock = threading.Lock()
        self.repos_by_cwd: dict[str, str] = {}
        self.calls: list[tuple[list[str], str | None]] = []

    def run(self, spec: guard.ReviewSubmission, arguments: list[str]):
        self.calls.append((arguments, spec.cwd))
        returncode = 0
        if arguments[:2] == ["api", "user"]:
            output = self.reviewer
        elif arguments[:2] == ["repo", "view"]:
            cwd = str(Path(spec.cwd or Path.cwd()).resolve())
            output = self.repos_by_cwd.get(cwd, "")
            returncode = 0 if output else 1
        elif (
            len(arguments) >= 2
            and arguments[0] == "api"
            and arguments[1].endswith(f"/pulls/{spec.number}/reviews")
        ):
            with self.lock:
                output = json.dumps(self.reviews)
        elif (
            len(arguments) >= 2
            and arguments[0] == "api"
            and arguments[1].endswith(f"/pulls/{spec.number}")
        ):
            output = self.head
        elif arguments[:2] == ["pr", "view"]:
            output = str(self.current_number)
        else:  # pragma: no cover - catches an unexpected API shape clearly
            raise AssertionError(arguments)
        return SimpleNamespace(returncode=returncode, stdout=output)

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
    monkeypatch.setitem(extra._SHELL_STATE, "cwd", None)
    guard._locks.clear()
    guard._lock_users.clear()
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
    ("command", "expected"),
    [
        (
            "cd repo && gh pr review 152 --repo o/r --approve --body ok",
            guard.ReviewSubmission("gh", "o/r", 152, "APPROVED", None),
        ),
        (
            "printf ready | gh pr review 152 --repo o/r --approve --body ok",
            guard.ReviewSubmission("gh", "o/r", 152, "APPROVED", None),
        ),
        (
            "true; gh pr review 152 --repo o/r --approve --body ok",
            guard.ReviewSubmission("gh", "o/r", 152, "APPROVED", None),
        ),
        (
            "cd repo\ngh pr review 152 --repo o/r --approve --body ok",
            guard.ReviewSubmission("gh", "o/r", 152, "APPROVED", None),
        ),
        (
            "gh pr review 152 -a -b LGTM",
            guard.ReviewSubmission("gh", None, 152, "APPROVED", None),
        ),
        (
            "gh pr review 152 -r --body nope",
            guard.ReviewSubmission("gh", None, 152, "CHANGES_REQUESTED", None),
        ),
        (
            "gh pr review --repo o/r --approve -b LGTM",
            guard.ReviewSubmission("gh", "o/r", None, "APPROVED", None),
        ),
        (
            "gh pr review 152 -c -F review.md",
            guard.ReviewSubmission("gh", None, 152, "COMMENTED", None),
        ),
    ],
)
def test_review_submission_is_found_in_compound_command(
    command: str,
    expected: guard.ReviewSubmission,
) -> None:
    spec = guard.review_submission_from_request(_request(command))

    assert replace(spec, cwd=None) == expected


@pytest.mark.parametrize(
    "command",
    [
        "cd repo\ngh pr review 152 --repo o/r --approve --body ok",
        "gh pr review 152 -a -b LGTM",
        "gh pr review 152 -r -F review.md",
        "gh pr review 152 -c",
    ],
)
@pytest.mark.parametrize("is_async", [False, True])
@pytest.mark.asyncio
async def test_new_review_shapes_are_claimed_by_sync_and_async_middleware(
    command: str,
    is_async: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.tools import budget_gate

    claimed: list[guard.ReviewSubmission] = []
    monkeypatch.setattr(budget_gate, "_emit_event_sync", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        budget_gate, "_request_for_authorized_execution", lambda request, *_: request,
    )
    monkeypatch.setattr(
        guard,
        "claim_review_submission",
        lambda spec: claimed.append(spec) or None,
    )
    request = _tool_request(command, tool_call_id="new-shape")
    middleware = budget_gate.BudgetGateMiddleware()

    if is_async:
        async def async_handler(_request: ToolCallRequest) -> ToolMessage:
            return ToolMessage(content="submitted", tool_call_id="new-shape")

        await middleware.awrap_tool_call(request, async_handler)
    else:
        middleware.wrap_tool_call(
            request,
            lambda _request: ToolMessage(content="submitted", tool_call_id="new-shape"),
        )

    assert len(claimed) == 1


@pytest.mark.parametrize("is_async", [False, True])
@pytest.mark.asyncio
async def test_unparseable_review_is_refused_by_sync_and_async_middleware(
    is_async: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir._context import reset_current_turn, set_current_turn
    from mimir.tools import budget_gate

    tool_calls: list[dict[str, object]] = []
    outcomes: list[tuple[str, str]] = []
    label_merges: list[object] = []
    original_record_outcome = budget_gate._record_tool_outcome
    monkeypatch.setattr(budget_gate, "_emit_event_sync", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        budget_gate,
        "_emit_tool_call_sync",
        lambda _tool, **fields: tool_calls.append(fields),
    )
    monkeypatch.setattr(
        budget_gate,
        "_record_tool_outcome",
        lambda tool, *, refused_reason="": (
            outcomes.append((tool, refused_reason)),
            original_record_outcome(tool, refused_reason=refused_reason),
        )[-1],
    )
    monkeypatch.setattr(
        budget_gate,
        "_merge_result_labels",
        lambda _auth, labels: label_merges.append(labels),
    )
    monkeypatch.setattr(
        budget_gate, "_request_for_authorized_execution", lambda request, *_: request,
    )
    request = _tool_request(
        "gh pr review 152 --approve --unknown value",
        tool_call_id="unparseable",
    )
    turn = SimpleNamespace(
        turn_id=f"unparseable-{'async' if is_async else 'sync'}",
        session_id=None,
        channel_id=None,
        auth_context=request.runtime.context,
        hard_boundary_denials=[],
        remediation_effects=[],
    )
    token = set_current_turn(turn)
    try:
        if is_async:
            async def async_handler(_request: ToolCallRequest) -> ToolMessage:
                pytest.fail("refused handler was executed")

            result = await budget_gate.BudgetGateMiddleware().awrap_tool_call(
                request, async_handler,
            )
        else:
            result = budget_gate.BudgetGateMiddleware().wrap_tool_call(
                request,
                lambda _request: pytest.fail("refused handler was executed"),
            )
    finally:
        reset_current_turn(token)

    assert result.status == "error"
    assert "unrecognised option --unknown" in str(result.content)
    assert outcomes == [("shell_exec", str(result.content))]
    assert len(tool_calls) == 1
    assert tool_calls[0]["denied"] is True
    assert tool_calls[0]["ok"] is False
    assert len(label_merges) == 1
    assert turn.hard_boundary_denials == [{
        "tool": "shell_exec",
        "boundary": "tool_policy",
        "reason": str(result.content),
    }]
    attempt_disposition = (
        "exempt_hard_refusal"
        if turn.hard_boundary_denials and not turn.remediation_effects
        else "charge"
    )
    assert attempt_disposition == "exempt_hard_refusal"


@pytest.mark.parametrize(
    "command",
    [
        "printf 'gh pr review 152 --approve'",
        "echo gh pr review 152 --approve",
        "printf 'gh pr review",
    ],
)
def test_non_review_commands_are_not_refused(command: str) -> None:
    assert guard.review_submission_from_request(_request(command)) is None


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        (
            "gh pr review 152 --approve --unknown value",
            "unrecognised option --unknown",
        ),
        ("gh pr review nope --approve", "pull request number is not an integer"),
        ("gh pr review 152 --approve --body", "option --body requires a value"),
        ("gh pr review 152 --body ok", "exactly one review state flag is required"),
        (
            "gh pr review 152 --approve --body 'unterminated",
            "shell command could not be parsed",
        ),
    ],
)
def test_unparseable_review_reports_reason(command: str, reason: str) -> None:
    with pytest.raises(ToolPolicyRefusal, match=reason):
        guard.review_submission_from_request(_request(command))


def test_body_value_is_not_parsed_as_a_state_flag() -> None:
    spec = guard.review_submission_from_request(
        _request("gh pr review 152 --comment --body --approve"),
    )

    assert spec == guard.ReviewSubmission("gh", None, 152, "COMMENTED", None)


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


def test_guard_probe_uses_server_gh_and_direct_exec_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs: list[tuple[list[str], dict[str, object]]] = []
    env = {"PATH": "/usr/bin", "GH_CONFIG_DIR": "/isolated"}
    monkeypatch.setattr(
        guard,
        "direct_exec_env",
        lambda argv: env if argv == ["gh"] else pytest.fail(str(argv)),
    )
    monkeypatch.setattr(
        guard.subprocess,
        "run",
        lambda argv, **kwargs: (
            runs.append((argv, kwargs))
            or SimpleNamespace(returncode=0, stdout="ok")
        ),
    )
    spec = guard.ReviewSubmission(
        "/tmp/model-controlled/gh", "o/r", 152, "APPROVED", "/repo",
    )

    guard._run(spec, ["api", "user"])

    assert runs == [(["gh", "api", "user"], {
        "capture_output": True,
        "text": True,
        "timeout": 15,
        "cwd": "/repo",
        "env": env,
        "stdin": guard.subprocess.DEVNULL,
    })]


def test_non_review_direct_argv_is_not_refused() -> None:
    spec = guard.review_submission_from_request(
        _request(
            "gh pr review 152 --approve",
            direct_argv=["gh", "pr", "list", "--state", "open"],
        ),
    )

    assert spec is None


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


def test_omitted_cwd_uses_shell_exec_home_not_process_cwd(
    github: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    shell_home = tmp_path / "mimir-home"
    process_repo = tmp_path / "workspace-mimir"
    shell_home.mkdir()
    process_repo.mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(shell_home))
    monkeypatch.chdir(process_repo)
    github.repos_by_cwd = {
        str(shell_home.resolve()): "o/shell-home",
        str(process_repo.resolve()): "o/process-repo",
    }

    spec = guard.review_submission_from_request(
        _request("gh pr review 152 --approve --body ok"),
    )
    assert spec is not None
    claim = guard.claim_review_submission(spec)

    assert spec.cwd == str(extra._effective_shell_cwd()) == str(shell_home.resolve())
    assert claim is not None
    assert claim.repo == "o/shell-home"
    assert {cwd for _, cwd in github.calls} == {str(shell_home.resolve())}
    claim.release()


def test_explicit_cwd_still_overrides_shell_home(
    github: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    shell_home = tmp_path / "mimir-home"
    explicit = tmp_path / "explicit"
    shell_home.mkdir()
    explicit.mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(shell_home))
    github.repos_by_cwd[str(explicit.resolve())] = "o/explicit"

    spec = guard.review_submission_from_request(
        _request("gh pr review 152 --approve", cwd=str(explicit)),
    )
    assert spec is not None
    claim = guard.claim_review_submission(spec)

    assert spec.cwd == str(explicit)
    assert claim is not None
    assert claim.repo == "o/explicit"
    claim.release()


def test_compound_cd_resolves_repository_from_command_directory(
    github: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    shell_home = tmp_path / "mimir-home"
    repo = shell_home / "repo"
    repo.mkdir(parents=True)
    monkeypatch.setenv("MIMIR_HOME", str(shell_home))
    github.repos_by_cwd[str(repo.resolve())] = "o/compound"

    spec = guard.review_submission_from_request(
        _request("cd repo && gh pr review 152 --approve --body ok"),
    )
    assert spec is not None
    claim = guard.claim_review_submission(spec)

    assert spec.cwd == str(repo.resolve())
    assert claim is not None
    assert claim.repo == "o/compound"
    claim.release()


def test_ambiguous_shell_cwd_cannot_return_duplicate(
    github: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    shell_home = tmp_path / "mimir-home"
    shell_home.mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(shell_home))
    github.repos_by_cwd[str(shell_home.resolve())] = "o/wrong-repo"
    github.reviews.append({
        "user": {"login": github.reviewer},
        "commit_id": github.head,
        "state": "APPROVED",
    })

    spec = guard.review_submission_from_request(
        _request('cd "$REVIEW_REPO" && gh pr review 152 --approve'),
    )
    assert spec is not None

    assert spec.repository_context_known is False
    assert guard.claim_review_submission(spec) is None
    assert github.calls == []


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


def test_review_claim_release_prunes_unused_lock(github: FakeGitHub) -> None:
    claim = guard.claim_review_submission(
        guard.ReviewSubmission("gh", "o/r", 152, "APPROVED", None),
    )

    assert claim is not None
    assert len(guard._locks) == 1
    claim.release()

    assert guard._locks == {}
    assert guard._lock_users == {}


def test_review_claim_lock_timeout_refuses_instead_of_proceeding(
    github: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = ("o/r", 152, github.head, github.reviewer.casefold(), "APPROVED")
    held_lock = threading.Lock()
    held_lock.acquire()
    guard._locks[key] = held_lock
    guard._lock_users[key] = 1
    monkeypatch.setattr(guard, "_LOCK_ACQUIRE_TIMEOUT_SECONDS", 0.01)

    try:
        with pytest.raises(ToolPolicyRefusal, match="timed out waiting"):
            guard.claim_review_submission(
                guard.ReviewSubmission("gh", "o/r", 152, "APPROVED", None),
            )
        assert guard._lock_users[key] == 1
    finally:
        held_lock.release()
        guard._release_lock_user(key, held_lock)

    assert key not in guard._locks


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


@pytest.mark.asyncio
async def test_async_wrap_releases_review_claim_when_cancelled_in_prologue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir import access_control
    from mimir.tools import budget_gate

    releases: list[None] = []
    claim = guard.ReviewClaim(
        "o/r", 152, "head-1", "mimir-carreira", "APPROVED", False,
    )
    claim.release = lambda: releases.append(None)  # type: ignore[method-assign]
    monkeypatch.setattr(guard, "claim_review_submission", lambda spec: claim)
    monkeypatch.setattr(budget_gate, "_emit_event_sync", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        access_control,
        "begin_protected_result_capture",
        lambda: (_ for _ in ()).throw(asyncio.CancelledError()),
    )

    async def handler(_request: ToolCallRequest) -> ToolMessage:
        pytest.fail("cancelled prologue reached handler")

    with pytest.raises(asyncio.CancelledError):
        await budget_gate.BudgetGateMiddleware().awrap_tool_call(
            _tool_request(
                "gh pr review 152 --repo o/r --approve", tool_call_id="cancelled",
            ),
            handler,
        )

    assert len(releases) == 1


def test_multi_target_result_labels_use_operative_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.tools import budget_gate

    cwd_authorization = SimpleNamespace(name="cwd")
    artifact_authorization = SimpleNamespace(name="artifact")
    authorizations = iter((cwd_authorization, artifact_authorization))
    consumed: list[object] = []
    monkeypatch.setattr(
        budget_gate,
        "_authorize_tool_call",
        lambda *_args, **_kwargs: (next(authorizations), None),
    )
    monkeypatch.setattr(
        budget_gate,
        "_result_labels_for_call",
        lambda _name, _request, _auth, authorization, **_kwargs: (
            consumed.append(authorization) or None
        ),
    )
    monkeypatch.setattr(budget_gate, "_emit_event_sync", lambda *args, **kwargs: None)
    request = ToolCallRequest(
        tool_call={
            "name": "spawn_open_code",
            "args": {"prompt": "task", "cwd": "/work", "artifact_root": "/artifacts"},
            "id": "multi-target",
            "type": "tool_call",
        },
        tool=None,
        state=None,
        runtime=Runtime(context=middleware_auth_context()),
    )

    result = budget_gate.BudgetGateMiddleware().wrap_tool_call(
        request,
        lambda _request: ToolMessage(content="spawned", tool_call_id="multi-target"),
    )

    assert result.status == "success"
    assert consumed == [cwd_authorization, cwd_authorization]


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


@pytest.mark.parametrize("is_async", [False, True])
@pytest.mark.asyncio
async def test_duplicate_review_uses_common_audit_and_label_tail(
    is_async: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir.tools import budget_gate

    claim = guard.ReviewClaim(
        "o/r", 152, "head-1", "mimir-carreira", "APPROVED", True,
    )
    tool_calls: list[dict[str, object]] = []
    outcomes: list[str] = []
    label_merges: list[object] = []
    monkeypatch.setattr(guard, "claim_review_submission", lambda spec: claim)
    monkeypatch.setattr(budget_gate, "_emit_event_sync", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        budget_gate,
        "_emit_tool_call_sync",
        lambda _tool, **fields: tool_calls.append(fields),
    )
    monkeypatch.setattr(
        budget_gate,
        "_record_tool_outcome",
        lambda tool, **_kwargs: outcomes.append(tool),
    )
    monkeypatch.setattr(
        budget_gate,
        "_merge_result_labels",
        lambda _auth, labels: label_merges.append(labels),
    )
    request = _tool_request(
        "gh pr review 152 --repo o/r --approve", tool_call_id="duplicate-tail",
    )

    if is_async:
        async def async_handler(_request: ToolCallRequest) -> ToolMessage:
            pytest.fail("duplicate handler was executed")

        result = await budget_gate.BudgetGateMiddleware().awrap_tool_call(
            request, async_handler,
        )
    else:
        result = budget_gate.BudgetGateMiddleware().wrap_tool_call(
            request,
            lambda _request: pytest.fail("duplicate handler was executed"),
        )

    assert result.status == "success"
    assert outcomes == ["shell_exec"]
    assert len(label_merges) == 1
    assert len(tool_calls) == 1
    assert tool_calls[0]["ok"] is True


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
