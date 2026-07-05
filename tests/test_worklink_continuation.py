from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Sequence

import pytest

from mimir import event_logger
from mimir.identities import IdentityResolver
from mimir.models import AgentEvent, TurnContext, TurnRecord
from mimir.worklink.continuation import (
    CONTINUATION_PREFIX,
    maybe_create_worklink_budget_continuation,
)
from mimir.worklink.run_state import WorklinkRunState, save_run_state


@pytest.fixture(autouse=True)
def _reset_event_logger() -> None:
    event_logger._reset_logger_for_tests()
    yield
    event_logger._reset_logger_for_tests()


class SpyRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path | None]] = []

    def __call__(
        self,
        args: Sequence[str],
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        argv = list(args)
        self.calls.append((argv, cwd))
        if argv[:3] == ["chainlink", "issue", "comment"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[:3] == ["gh", "pr", "comment"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)

    @property
    def issue_comments(self) -> list[str]:
        return [argv[-1] for argv, _cwd in self.calls if argv[:3] == ["chainlink", "issue", "comment"]]


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _init_repo(
    tmp_path: Path,
    *,
    branch: str,
    remote_url: str = "https://github.com/acme/demo.git",
) -> Path:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "checkout", "-q", "-b", "main")
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "remote", "add", "origin", remote_url)
    if branch != "main":
        _git(repo, "checkout", "-q", "-b", branch)
    return repo


def _write_factory_run(
    repo: Path,
    *,
    run_id: str,
    issue_id: int,
    branch: str,
    worktree: Path,
    pr_url: str | None = None,
) -> None:
    run_dir = repo / ".opencode" / "factory" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "external_ref": f"chainlink #{issue_id}",
        "branch": branch,
        "worktree": str(worktree),
        "pr_url": pr_url,
    }
    (run_dir / "run.json").write_text(json.dumps(payload), encoding="utf-8")


def _save_run_state(home: Path, *, issue_id: int, branch: str, test_command: str) -> None:
    save_run_state(
        home,
        WorklinkRunState(
            issue_id=issue_id,
            attempt=1,
            backend="codex",
            compute_name="local_subprocess",
            handle_substrate="local",
            handle_identifier="run-1",
            branch=branch,
            base_ref="main",
            local_base="main",
            repo="acme/demo",
            repo_url="https://github.com/acme/demo.git",
            test_command=test_command,
            started_at="2026-07-05T03:00:00+00:00",
        ),
    )


def _write_identities(home: Path) -> IdentityResolver:
    identities = home / "state" / "identities.yaml"
    identities.parent.mkdir(parents=True, exist_ok=True)
    identities.write_text(
        """
people:
  - canonical: alice
    aliases: [slack-U1]
    access:
      roles: [user]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    resolver = IdentityResolver(home)
    resolver.reload()
    return resolver


def _make_event(
    *,
    trigger: str = "scheduled_tick",
    source: str = "api",
    source_id: str = "src-1",
    content: str = "resume worklink chainlink #740",
    author: str | None = None,
    extra: dict | None = None,
) -> AgentEvent:
    return AgentEvent(
        trigger=trigger,
        channel_id="ops",
        content=content,
        author=author,
        source=source,
        source_id=source_id,
        extra=extra or {},
    )


def _make_ctx(
    event: AgentEvent,
    *,
    turn_id: str = "turn-1",
    access_control_enforced: bool = False,
    author: str | None = None,
    resolver: IdentityResolver | None = None,
    channel_source: str | None = None,
) -> TurnContext:
    return TurnContext(
        turn_id=turn_id,
        session_id=event.channel_id,
        trigger=event.trigger,
        channel_id=event.channel_id,
        started_at=0.0,
        tool_call_count=7,
        tool_call_budget=7,
        tool_call_budget_exhausted=True,
        tool_call_budget_denied_count=1,
        tool_call_budget_denied_tools=["Bash"],
        tool_call_budget_first_denied_at_count=7,
        access_control_enforced=access_control_enforced,
        author=author,
        identity_resolver=resolver,
        channel_source=channel_source or event.source,
    )


def _make_record(
    event: AgentEvent,
    *,
    turn_id: str = "turn-1",
    input_text: str = "resume chainlink #740",
    events: list[dict] | None = None,
) -> TurnRecord:
    return TurnRecord(
        ts="2026-07-05T03:00:00+00:00",
        turn_id=turn_id,
        session_id=event.channel_id,
        saga_session_id=None,
        trigger=event.trigger,
        channel_id=event.channel_id,
        input=input_text,
        events=events or [],
        output="",
    )


def test_continuation_payload_captures_required_context(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = _init_repo(tmp_path, branch="chainlink-740-budget-continuation--be-continuation-core")
    _write_factory_run(
        repo,
        run_id="chainlink-740",
        issue_id=740,
        branch="chainlink-740-budget-continuation",
        worktree=repo,
    )
    _save_run_state(
        home,
        issue_id=740,
        branch="chainlink-740-budget-continuation--be-continuation-core",
        test_command="uv run pytest -q tests/test_worklink_continuation.py",
    )
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (repo / "new.txt").write_text("new\n", encoding="utf-8")
    events_path = home / "logs" / "events.jsonl"
    event_logger.init_logger(events_path, session_id="test")
    event = _make_event(
        trigger="poller",
        source="poller",
        extra={"poller_name": "chainlink-orchestrator"},
    )
    ctx = _make_ctx(event)
    record = _make_record(event)
    runner = SpyRunner()

    result = maybe_create_worklink_budget_continuation(
        home=home,
        event=event,
        ctx=ctx,
        record=record,
        repo=repo,
        current_worktree=repo,
        current_labels=["worklink:in-progress"],
        runner=runner,
    )

    assert result is not None
    payload = json.loads(result.sidecar_path.read_text(encoding="utf-8"))
    assert payload["kind"] == "worklink_tool_budget_continuation"
    assert payload["version"] == 1
    assert payload["priority"] == "high"
    assert payload["association"]["issue_id"] == 740
    assert payload["association"]["repo"] == "acme/demo"
    assert payload["association"]["worktree"] == str(repo.resolve())
    assert payload["association"]["branch"] == "chainlink-740-budget-continuation--be-continuation-core"
    assert payload["association"]["run_state_path"] == str(home / "state" / "worklink" / "runs" / "740.json")
    assert payload["source_event"]["poller_name"] == "chainlink-orchestrator"
    assert payload["partial_work_state"]["state"] == "dirty"
    assert payload["partial_work_state"]["changed_path_count"] >= 2
    assert "tracked.txt" in payload["partial_work_state"]["changed_paths"]
    assert "new.txt" in payload["partial_work_state"]["changed_paths"]
    assert payload["validation"]["state"] == "unrun"
    assert payload["validation"]["commands"] == ["uv run pytest -q tests/test_worklink_continuation.py"]
    assert any("--reattach" in command for command in payload["next"]["commands"])
    assert any(
        "reattach existing worklink run" in item
        for item in payload["next"]["labels_or_status_changes_needed"]
    )
    assert payload["label_status_mutated"] is False
    assert payload["external_comment"]["posted"] is True
    assert len(runner.issue_comments) == 1
    emitted = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert any(rec.get("type") == "worklink_continuation_created" for rec in emitted)


def test_generic_high_priority_fallback_when_issue_pr_unknown(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = _init_repo(tmp_path, branch="feature/worklink-recovery")
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    event = _make_event(content="generic worklink follow-up", source_id="src-generic")
    ctx = _make_ctx(event, turn_id="turn-generic")
    record = _make_record(event, turn_id="turn-generic", input_text="generic worklink follow-up")
    runner = SpyRunner()

    result = maybe_create_worklink_budget_continuation(
        home=home,
        event=event,
        ctx=ctx,
        record=record,
        repo=repo,
        current_worktree=repo,
        current_labels=["worklink:review"],
        runner=runner,
    )

    assert result is not None
    payload = json.loads(result.sidecar_path.read_text(encoding="utf-8"))
    assert payload["association"]["issue_id"] is None
    assert payload["association"]["pr_url"] is None
    assert payload["dedupe_scope"] == "worktree_branch"
    assert payload["priority"] == "high"
    assert payload["association"]["branch"] == "feature/worklink-recovery"
    assert payload["external_comment"]["posted"] is False
    assert payload["external_comment"]["skipped_reason"] == "no_validated_target"
    assert any(
        "preserve worklink:review" in item
        for item in payload["next"]["labels_or_status_changes_needed"]
    )
    assert runner.issue_comments == []


def test_idempotent_same_work_item_updates_existing_artifact(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = _init_repo(tmp_path, branch="chainlink-740-budget-continuation--be-continuation-core")
    _write_factory_run(
        repo,
        run_id="chainlink-740",
        issue_id=740,
        branch="chainlink-740-budget-continuation",
        worktree=repo,
    )
    _save_run_state(
        home,
        issue_id=740,
        branch="chainlink-740-budget-continuation--be-continuation-core",
        test_command="uv run pytest -q tests/test_worklink_continuation.py",
    )
    runner = SpyRunner()

    event1 = _make_event(
        source_id="src-1",
        extra={"schedule_name": "worklink-continuation"},
    )
    first = maybe_create_worklink_budget_continuation(
        home=home,
        event=event1,
        ctx=_make_ctx(event1, turn_id="turn-1"),
        record=_make_record(event1, turn_id="turn-1"),
        repo=repo,
        current_worktree=repo,
        runner=runner,
    )
    event2 = _make_event(
        source_id="src-2",
        extra={"schedule_name": "worklink-continuation"},
    )
    second = maybe_create_worklink_budget_continuation(
        home=home,
        event=event2,
        ctx=_make_ctx(event2, turn_id="turn-2"),
        record=_make_record(event2, turn_id="turn-2"),
        repo=repo,
        current_worktree=repo,
        runner=runner,
    )

    assert first is not None and second is not None
    assert first.sidecar_path == second.sidecar_path
    payload = json.loads(second.sidecar_path.read_text(encoding="utf-8"))
    assert payload["occurrences"] == 2
    assert payload["created_at"] == first.payload["created_at"]
    assert {item["turn_id"] for item in payload["turns"]} == {"turn-1", "turn-2"}
    assert len(runner.issue_comments) == 1
    assert payload["external_comment"]["posted"] is True
    assert payload["external_comment"]["skipped_reason"] == "already_posted"


def test_untrusted_hints_do_not_drive_comment_or_path_inspection(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = _init_repo(tmp_path, branch="feature/worklink-escape")
    escape = tmp_path / "escape"
    event = _make_event(
        content=f"worklink recovery from {escape}",
        extra={
            "issue_id": 999,
            "pr_url": "https://github.com/evil/repo/pull/9",
            "worktree": str(escape),
        },
    )
    ctx = _make_ctx(event)
    record = _make_record(
        event,
        input_text=f"chainlink #999 inspect {escape}",
        events=[{"tool": "Bash", "result": str(escape)}],
    )
    runner = SpyRunner()

    result = maybe_create_worklink_budget_continuation(
        home=home,
        event=event,
        ctx=ctx,
        record=record,
        repo=repo,
        current_worktree=repo,
        current_labels=["worklink:ready"],
        runner=runner,
    )

    assert result is not None
    payload = json.loads(result.sidecar_path.read_text(encoding="utf-8"))
    assert payload["association"]["issue_id"] is None
    assert payload["association"]["pr_url"] is None
    assert payload["association"]["worktree"] == str(repo.resolve())
    assert payload["external_comment"]["posted"] is False
    assert runner.issue_comments == []
    assert all(str(escape) not in " ".join(argv) for argv, _cwd in runner.calls)


def test_external_comment_schema_is_allowlisted_and_redacted(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = _init_repo(tmp_path, branch="chainlink-740-budget-continuation--be-continuation-core")
    _save_run_state(
        home,
        issue_id=740,
        branch="chainlink-740-budget-continuation--be-continuation-core",
        test_command=f"uv run pytest -q {repo / 'tests' / 'test_worklink_continuation.py'}",
    )
    (repo / "secret.txt").write_text("top secret\n", encoding="utf-8")
    event = _make_event(
        content="resume chainlink #740",
        extra={"schedule_name": "worklink-continuation"},
    )
    ctx = _make_ctx(event)
    record = _make_record(event)
    runner = SpyRunner()

    result = maybe_create_worklink_budget_continuation(
        home=home,
        event=event,
        ctx=ctx,
        record=record,
        repo=repo,
        current_worktree=repo,
        runner=runner,
    )

    assert result is not None
    comment = runner.issue_comments[0]
    assert comment.startswith(CONTINUATION_PREFIX)
    rendered = json.loads(comment[len(CONTINUATION_PREFIX) :])
    assert set(rendered) == {
        "association",
        "created_at",
        "idempotency_key",
        "kind",
        "next",
        "occurrences",
        "partial_work_state",
        "priority",
        "reason",
        "schema",
        "sidecar",
        "validation",
    }
    assert set(rendered["association"]) == {"branch", "issue_id", "pr_url", "repo", "worktree_ref"}
    assert set(rendered["partial_work_state"]) == {"changed_path_count", "dirty"}
    assert str(repo.resolve()) not in comment
    assert "secret.txt" not in comment
    assert any("<worktree:repo>" in command for command in rendered["validation"]["commands"])
    assert any("<worktree:repo>" in command for command in rendered["next"]["commands"])


def test_comment_posting_is_admin_gated(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = _init_repo(tmp_path, branch="chainlink-740-user-turn")
    _save_run_state(
        home,
        issue_id=740,
        branch="chainlink-740-user-turn",
        test_command="uv run pytest -q tests/test_worklink_continuation.py",
    )
    resolver = _write_identities(home)
    event = _make_event(
        trigger="user_message",
        source="slack",
        author="slack-U1",
        content="resume chainlink #740",
    )
    ctx = _make_ctx(
        event,
        access_control_enforced=True,
        author="slack-U1",
        resolver=resolver,
        channel_source="slack",
    )
    record = _make_record(event)
    runner = SpyRunner()

    result = maybe_create_worklink_budget_continuation(
        home=home,
        event=event,
        ctx=ctx,
        record=record,
        repo=repo,
        current_worktree=repo,
        current_labels=["worklink:ready"],
        runner=runner,
    )

    assert result is not None
    payload = json.loads(result.sidecar_path.read_text(encoding="utf-8"))
    assert payload["external_comment"]["posted"] is False
    assert payload["external_comment"]["skipped_reason"] == "admin_required"
    assert runner.issue_comments == []


def test_user_message_default_access_control_cannot_post_external_comment(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = _init_repo(tmp_path, branch="chainlink-740-user-turn-default-open")
    _save_run_state(
        home,
        issue_id=740,
        branch="chainlink-740-user-turn-default-open",
        test_command="uv run pytest -q tests/test_worklink_continuation.py",
    )
    event = _make_event(
        trigger="user_message",
        source="slack",
        author="slack-U1",
        content="resume chainlink #740",
    )
    ctx = _make_ctx(
        event,
        access_control_enforced=False,
        author="slack-U1",
        channel_source="slack",
    )
    record = _make_record(event)
    runner = SpyRunner()

    result = maybe_create_worklink_budget_continuation(
        home=home,
        event=event,
        ctx=ctx,
        record=record,
        repo=repo,
        current_worktree=repo,
        current_labels=["worklink:ready"],
        runner=runner,
    )

    assert result is not None
    payload = json.loads(result.sidecar_path.read_text(encoding="utf-8"))
    assert payload["association"]["issue_id"] == 740
    assert payload["external_comment"]["posted"] is False
    assert payload["external_comment"]["skipped_reason"] == "admin_access_control_required"
    assert not any(
        argv[:3] == ["chainlink", "issue", "comment"]
        or argv[:3] == ["gh", "pr", "comment"]
        for argv, _cwd in runner.calls
    )


@pytest.mark.parametrize(
    ("trigger", "source"),
    [
        ("scheduled_tick", "api"),
        ("poller", "poller"),
    ],
)
def test_forged_non_user_message_trigger_without_server_stamp_cannot_post_external_comment(
    tmp_path: Path,
    trigger: str,
    source: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = _init_repo(tmp_path, branch=f"chainlink-740-forged-{trigger}")
    _save_run_state(
        home,
        issue_id=740,
        branch=f"chainlink-740-forged-{trigger}",
        test_command="uv run pytest -q tests/test_worklink_continuation.py",
    )
    # Simulates a generic /event client after ingress stripping removed any
    # forged schedule_name / poller_name server stamps.
    event = _make_event(
        trigger=trigger,
        source=source,
        content="resume chainlink #740",
        extra={"keep": "me"},
    )
    ctx = _make_ctx(event, access_control_enforced=False, channel_source=source)
    record = _make_record(event)
    runner = SpyRunner()

    result = maybe_create_worklink_budget_continuation(
        home=home,
        event=event,
        ctx=ctx,
        record=record,
        repo=repo,
        current_worktree=repo,
        current_labels=["worklink:ready"],
        runner=runner,
    )

    assert result is not None
    payload = json.loads(result.sidecar_path.read_text(encoding="utf-8"))
    assert payload["association"]["issue_id"] == 740
    assert payload["external_comment"]["posted"] is False
    assert payload["external_comment"]["skipped_reason"] == "admin_access_control_required"
    assert not any(
        argv[:3] == ["chainlink", "issue", "comment"]
        or argv[:3] == ["gh", "pr", "comment"]
        for argv, _cwd in runner.calls
    )


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        (["worklink:ready"], "preserve worklink:ready"),
        (["worklink:review"], "preserve worklink:review"),
        (["worklink:rework"], "preserve worklink:rework"),
        (["worklink:in-progress"], "reattach existing worklink run"),
    ],
)
def test_worklink_labels_are_recorded_not_mutated(
    tmp_path: Path,
    labels: list[str],
    expected: str,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = _init_repo(tmp_path, branch="chainlink-740-labels")
    _save_run_state(
        home,
        issue_id=740,
        branch="chainlink-740-labels",
        test_command="uv run pytest -q tests/test_worklink_continuation.py",
    )
    event = _make_event(content="resume chainlink #740")
    ctx = _make_ctx(event)
    record = _make_record(event)
    runner = SpyRunner()

    result = maybe_create_worklink_budget_continuation(
        home=home,
        event=event,
        ctx=ctx,
        record=record,
        repo=repo,
        current_worktree=repo,
        current_labels=labels,
        runner=runner,
    )

    assert result is not None
    payload = json.loads(result.sidecar_path.read_text(encoding="utf-8"))
    assert any(expected in item for item in payload["next"]["labels_or_status_changes_needed"])
    assert not any(
        argv[:3] in (["chainlink", "issue", "label"], ["chainlink", "issue", "unlabel"])
        or argv[:2] == ["chainlink", "locks"]
        for argv, _cwd in runner.calls
    )
