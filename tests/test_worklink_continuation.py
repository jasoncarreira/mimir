from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
import subprocess
import sys
from typing import Sequence

import pytest

from mimir import event_logger
from mimir.identities import IdentityResolver
from mimir.models import AgentEvent, TurnContext, TurnRecord
from mimir.worklink.continuation import (
    CONTINUATION_PREFIX,
    HTTP_EVENT_INGRESS_EXTRA_KEY,
    HTTP_EVENT_INGRESS_EXTRA_VALUE,
    _default_runner,
    consume_worklink_budget_continuations,
    maybe_create_worklink_budget_continuation,
)
from mimir.worklink.orchestrator import _runner_for_home
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


class ContinuationConsumerRunner(SpyRunner):
    def __init__(self, *, status: str = "open", labels: list[str] | None = None) -> None:
        super().__init__()
        self.status = status
        self.labels = labels or ["worklink:ready"]
        self.comments: list[str] = []

    def __call__(
        self,
        args: Sequence[str],
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        argv = list(args)
        self.calls.append((argv, cwd))
        if argv[:3] == ["chainlink", "issue", "show"]:
            payload = {
                "id": int(argv[3]),
                "status": self.status,
                "labels": self.labels,
                "comments": self.comments,
            }
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")
        if argv[:3] == ["chainlink", "issue", "comment"]:
            self.comments.append(argv[-1])
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)


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


def _write_admin_identities(home: Path) -> IdentityResolver:
    identities = home / "state" / "identities.yaml"
    identities.parent.mkdir(parents=True, exist_ok=True)
    identities.write_text(
        """
people:
  - canonical: root
    aliases: [slack-UADMIN]
    access:
      roles: [user, admin]
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
    assert payload["dedupe_scope"] == "source_id"
    assert payload["priority"] == "high"
    assert payload["association"]["branch"] == "feature/worklink-recovery"
    assert payload["external_comment"]["posted"] is False
    assert payload["external_comment"]["skipped_reason"] == "no_validated_target"
    assert any(
        "preserve worklink:review" in item
        for item in payload["next"]["labels_or_status_changes_needed"]
    )
    assert runner.issue_comments == []


def test_canonical_run_id_resolves_validated_issue(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = _init_repo(tmp_path, branch="feature/worklink-recovery")
    _save_run_state(home, issue_id=700, branch="feature/worklink-recovery", test_command="pytest")
    event = _make_event(content="generic worklink follow-up")

    result = maybe_create_worklink_budget_continuation(
        home=home,
        event=event,
        ctx=_make_ctx(event),
        record=_make_record(event, input_text="generic worklink follow-up"),
        repo=repo,
        current_worktree=repo,
        run_id="chainlink-700",
        runner=SpyRunner(),
    )

    assert result is not None
    assert result.payload["association"]["issue_id"] == 700
    assert result.payload["association"]["run_state_path"] == str(
        home / "state" / "worklink" / "runs" / "700.json"
    )
    assert result.payload["source_event"]["run_id_hint"] == "chainlink-700"


def test_unknown_production_continuations_use_work_identity_not_server_checkout(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    server_checkout = _init_repo(tmp_path, branch="feature/worklink-recovery")

    results = []
    for source_id, turn_id, content in (
        ("work-a", "turn-a", "generic worklink follow-up A"),
        ("work-b", "turn-b", "generic worklink follow-up B"),
    ):
        event = _make_event(content=content, source_id=source_id)
        results.append(
            maybe_create_worklink_budget_continuation(
                home=home,
                event=event,
                ctx=_make_ctx(event, turn_id=turn_id),
                record=_make_record(event, turn_id=turn_id, input_text=content),
                repo=server_checkout,
                current_worktree=server_checkout,
                runner=SpyRunner(),
            )
        )

    assert all(result is not None for result in results)
    assert results[0].sidecar_path != results[1].sidecar_path
    sidecars = sorted((home / "state" / "worklink" / "continuations").glob("*.json"))
    assert len(sidecars) == 2
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in sidecars]
    assert {payload["dedupe_scope"] for payload in payloads} == {"source_id"}
    assert {payload["source_event"]["source_id"] for payload in payloads} == {
        "work-a",
        "work-b",
    }


def test_unknown_same_source_work_merges_across_turns(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    server_checkout = _init_repo(tmp_path, branch="feature/worklink-recovery")
    results = []

    for turn_id in ("turn-a", "turn-b"):
        event = _make_event(content="generic worklink follow-up", source_id="same-work")
        results.append(
            maybe_create_worklink_budget_continuation(
                home=home,
                event=event,
                ctx=_make_ctx(event, turn_id=turn_id),
                record=_make_record(event, turn_id=turn_id, input_text=event.content),
                repo=server_checkout,
                current_worktree=server_checkout,
                runner=SpyRunner(),
            )
        )

    assert all(result is not None for result in results)
    assert results[0].sidecar_path == results[1].sidecar_path
    payload = json.loads(results[1].sidecar_path.read_text(encoding="utf-8"))
    assert payload["occurrences"] == 2
    assert {turn["turn_id"] for turn in payload["turns"]} == {"turn-a", "turn-b"}


def test_unknown_work_without_source_id_uses_turn_id(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    server_checkout = _init_repo(tmp_path, branch="feature/worklink-recovery")
    event = _make_event(content="generic worklink follow-up", source_id=None)

    result = maybe_create_worklink_budget_continuation(
        home=home,
        event=event,
        ctx=_make_ctx(event, turn_id="fallback-turn"),
        record=_make_record(event, turn_id="fallback-turn", input_text=event.content),
        repo=server_checkout,
        current_worktree=server_checkout,
        runner=SpyRunner(),
    )

    assert result is not None
    assert result.payload["dedupe_scope"] == "turn_id"
    assert result.payload["dedupe_material"] == "fallback-turn"


def test_existing_sidecar_identity_mismatch_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    server_checkout = _init_repo(tmp_path, branch="feature/worklink-recovery")
    event = _make_event(content="generic worklink follow-up", source_id="stable-work")
    kwargs = {
        "home": home,
        "event": event,
        "ctx": _make_ctx(event, turn_id="turn-a"),
        "record": _make_record(event, turn_id="turn-a", input_text=event.content),
        "repo": server_checkout,
        "current_worktree": server_checkout,
        "runner": SpyRunner(),
    }
    first = maybe_create_worklink_budget_continuation(**kwargs)
    assert first is not None
    collided = dict(first.payload)
    collided["dedupe_material"] = "different-work"
    collided["source_event"] = {"source_id": "different-work"}
    first.sidecar_path.write_text(json.dumps(collided), encoding="utf-8")

    with pytest.raises(RuntimeError, match="identity collision"):
        maybe_create_worklink_budget_continuation(**kwargs)

    retained = json.loads(first.sidecar_path.read_text(encoding="utf-8"))
    assert retained["source_event"] == {"source_id": "different-work"}


def test_generic_continuation_never_reads_legacy_factory_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = _init_repo(tmp_path, branch="feature/worklink-recovery")
    legacy = repo / ".opencode" / "factory" / "chainlink-740"
    legacy.mkdir(parents=True)
    (legacy / "run.json").write_text('{"issue_id": 999}', encoding="utf-8")
    original = Path.read_text

    def guarded(path: Path, *args: object, **kwargs: object) -> str:
        if ".opencode/factory" in path.as_posix():
            raise AssertionError(f"legacy factory state read: {path}")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)
    event = _make_event(content="generic worklink follow-up", source_id="legacy-guard")
    result = maybe_create_worklink_budget_continuation(
        home=home,
        event=event,
        ctx=_make_ctx(event, turn_id="legacy-guard"),
        record=_make_record(
            event,
            turn_id="legacy-guard",
            input_text="generic worklink follow-up",
        ),
        repo=repo,
        current_worktree=repo,
        current_labels=["worklink:review"],
        runner=SpyRunner(),
    )

    assert result is not None
    assert result.payload["association"]["issue_id"] is None
    assert result.payload["priority"] == "high"


def test_idempotent_same_work_item_updates_existing_artifact(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = _init_repo(tmp_path, branch="chainlink-740-budget-continuation--be-continuation-core")
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


def test_chainlink_comment_uses_home_not_worktree(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = _init_repo(tmp_path, branch="chainlink-740-cwd")
    _save_run_state(home, issue_id=740, branch="chainlink-740-cwd", test_command="pytest")
    event = _make_event(extra={"schedule_name": "worklink-continuation"})
    runner = SpyRunner()

    result = maybe_create_worklink_budget_continuation(
        home=home,
        event=event,
        ctx=_make_ctx(event),
        record=_make_record(event),
        repo=repo,
        current_worktree=repo,
        runner=runner,
    )

    assert result is not None
    [comment_call] = [call for call in runner.calls if call[0][:3] == ["chainlink", "issue", "comment"]]
    assert comment_call[1] == home


def test_runner_for_home_overrides_explicit_cwd_only_for_chainlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    worktree = tmp_path / "worktree"
    calls: list[tuple[list[str], Path | None]] = []

    def fake_run(args, **kwargs):
        calls.append((list(args), kwargs.get("cwd")))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("mimir.worklink.orchestrator.subprocess.run", fake_run)
    runner = _runner_for_home(home, "chainlink")

    runner(["chainlink", "issue", "show", "740"], worktree)
    runner(["git", "status", "--short"], worktree)

    assert calls == [
        (["chainlink", "issue", "show", "740"], home),
        (["git", "status", "--short"], worktree),
    ]


def test_consumer_posts_once_and_marks_sidecar_actioned_after_delivery(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = _init_repo(tmp_path, branch="chainlink-740-consume")
    _save_run_state(home, issue_id=740, branch="chainlink-740-consume", test_command="pytest")
    event = _make_event(trigger="user_message", source="slack")
    producer = SpyRunner()
    created = maybe_create_worklink_budget_continuation(
        home=home,
        event=event,
        ctx=_make_ctx(event),
        record=_make_record(event),
        repo=repo,
        current_worktree=repo,
        runner=producer,
    )
    assert created is not None
    consumer = ContinuationConsumerRunner()

    before = created.sidecar_path.read_bytes()
    first = consume_worklink_budget_continuations(home, runner=consumer)
    after_offer = created.sidecar_path.read_bytes()
    retried = consume_worklink_budget_continuations(home, runner=consumer)
    delivered = consume_worklink_budget_continuations(
        home,
        runner=consumer,
        delivery_receipt_exists=lambda key: key == first[0].delivery_key,
    )

    assert len(first) == 1
    assert [action.delivery_key for action in retried] == [first[0].delivery_key]
    assert delivered == []
    assert after_offer == before
    assert len(consumer.comments) == 1
    payload = json.loads(created.sidecar_path.read_text(encoding="utf-8"))
    assert payload["actioned_at"]
    assert payload["external_comment"]["posted"] is True
    assert all(
        cwd == home
        for argv, cwd in consumer.calls
        if argv and argv[0] == "chainlink"
    )


def test_no_target_continuation_routes_once_without_churn_then_expires(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = _init_repo(tmp_path, branch="feature/worklink-recovery")
    event = _make_event(content="generic worklink follow-up", source_id="no-target")
    created = maybe_create_worklink_budget_continuation(
        home=home,
        event=event,
        ctx=_make_ctx(event, turn_id="no-target"),
        record=_make_record(event, turn_id="no-target", input_text=event.content),
        repo=repo,
        current_worktree=repo,
        runner=SpyRunner(),
    )
    assert created is not None
    routed_at = datetime(2026, 8, 1, tzinfo=UTC)

    before_offer = created.sidecar_path.read_bytes()
    offered = consume_worklink_budget_continuations(home, runner=SpyRunner(), now=routed_at)
    assert len(offered) == 1
    assert created.sidecar_path.read_bytes() == before_offer

    assert consume_worklink_budget_continuations(
        home,
        runner=SpyRunner(),
        now=routed_at,
        delivery_receipt_exists=lambda key: key == offered[0].delivery_key,
    ) == []
    payload = json.loads(created.sidecar_path.read_text(encoding="utf-8"))
    assert payload["actioned_at"] == routed_at.isoformat()
    assert payload["resolved_at"] == routed_at.isoformat()
    assert payload["resolved_reason"] == "no_validated_target"

    with monkeypatch.context() as patch:
        patch.setattr(
            "mimir.worklink.continuation.atomic_write_json",
            lambda *_args, **_kwargs: pytest.fail("resolved sidecar was rewritten"),
        )
        assert consume_worklink_budget_continuations(
            home,
            runner=SpyRunner(),
            now=routed_at + timedelta(days=1),
        ) == []

    assert consume_worklink_budget_continuations(
        home,
        runner=SpyRunner(),
        now=routed_at + timedelta(days=30),
    ) == []
    assert not created.sidecar_path.exists()


def test_consumer_resolves_closed_issue_without_reaction(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = _init_repo(tmp_path, branch="chainlink-740-closed")
    _save_run_state(home, issue_id=740, branch="chainlink-740-closed", test_command="pytest")
    event = _make_event(trigger="user_message", source="slack")
    created = maybe_create_worklink_budget_continuation(
        home=home,
        event=event,
        ctx=_make_ctx(event),
        record=_make_record(event),
        repo=repo,
        current_worktree=repo,
        runner=SpyRunner(),
    )
    assert created is not None
    consumer = ContinuationConsumerRunner(status="closed", labels=[])

    assert consume_worklink_budget_continuations(
        home, runner=consumer, delivery_receipt_exists=lambda _key: True,
    ) == []

    payload = json.loads(created.sidecar_path.read_text(encoding="utf-8"))
    assert payload["resolved_at"]
    assert payload["resolved_reason"] == "issue_closed"
    assert consumer.comments == []


def test_consumer_treats_completed_evidence_as_terminal(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = _init_repo(tmp_path, branch="chainlink-740-completed")
    _save_run_state(home, issue_id=740, branch="chainlink-740-completed", test_command="pytest")
    event = _make_event(trigger="user_message", source="slack")
    created = maybe_create_worklink_budget_continuation(
        home=home,
        event=event,
        ctx=_make_ctx(event),
        record=_make_record(event),
        repo=repo,
        current_worktree=repo,
        runner=SpyRunner(),
    )
    assert created is not None
    evidence = home / "state" / "worklink" / "evidence" / "740-1.json"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(
        json.dumps({"status": "completed", "pr_url": "https://github.com/acme/demo/pull/1"}),
        encoding="utf-8",
    )
    consumer = ContinuationConsumerRunner()

    assert consume_worklink_budget_continuations(
        home, runner=consumer, delivery_receipt_exists=lambda _key: True,
    ) == []

    payload = json.loads(created.sidecar_path.read_text(encoding="utf-8"))
    assert payload["resolved_reason"] == "completed_evidence"
    assert not any(argv[:3] == ["chainlink", "issue", "show"] for argv, _ in consumer.calls)


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


def test_evidence_checkout_reader_accepts_legacy_key_and_prefers_new_key(tmp_path: Path) -> None:
    from mimir.worklink.continuation import _load_evidence_records

    home = tmp_path / "home"
    evidence_dir = home / "state" / "worklink" / "evidence"
    evidence_dir.mkdir(parents=True)
    legacy_checkout = tmp_path / "legacy"
    current_checkout = tmp_path / "current"
    legacy_checkout.mkdir()
    current_checkout.mkdir()

    (evidence_dir / "740-1.json").write_text(
        json.dumps({"worktree": str(legacy_checkout)}), encoding="utf-8"
    )
    (evidence_dir / "740-2.json").write_text(
        json.dumps({"checkout": str(current_checkout), "worktree": str(legacy_checkout)}),
        encoding="utf-8",
    )

    records = _load_evidence_records(home, 740)

    assert [record.checkout for record in records] == [
        legacy_checkout.resolve(),
        current_checkout.resolve(),
    ]


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


def test_default_runner_bounds_hung_external_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(_default_runner.__globals__, "_EXTERNAL_COMMAND_TIMEOUT_SECONDS", 0.1)

    result = _default_runner(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
        ],
    )

    assert result.returncode == 124
    assert "command timed out after 0.1s" in result.stderr


def test_http_event_server_stamp_still_cannot_post_external_comment(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = _init_repo(tmp_path, branch="chainlink-740-http-event-server-stamp")
    _save_run_state(
        home,
        issue_id=740,
        branch="chainlink-740-http-event-server-stamp",
        test_command="uv run pytest -q tests/test_worklink_continuation.py",
    )
    event = _make_event(
        trigger="poller",
        source="api",
        content="resume chainlink #740",
        extra={
            HTTP_EVENT_INGRESS_EXTRA_KEY: HTTP_EVENT_INGRESS_EXTRA_VALUE,
            "poller_name": "worklink-ready-queue",
        },
    )
    ctx = _make_ctx(event, access_control_enforced=False, channel_source="api")
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
    assert payload["external_comment"]["skipped_reason"] == "http_event_author_untrusted"
    assert runner.issue_comments == []



def test_http_event_forged_admin_author_cannot_post_external_comment(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = _init_repo(tmp_path, branch="chainlink-740-http-event-forged-admin")
    _save_run_state(
        home,
        issue_id=740,
        branch="chainlink-740-http-event-forged-admin",
        test_command="uv run pytest -q tests/test_worklink_continuation.py",
    )
    resolver = _write_admin_identities(home)
    event = _make_event(
        trigger="user_message",
        source="api",
        author="slack-UADMIN",
        content="resume chainlink #740",
        extra={
            HTTP_EVENT_INGRESS_EXTRA_KEY: HTTP_EVENT_INGRESS_EXTRA_VALUE,
            "keep": "me",
        },
    )
    ctx = _make_ctx(
        event,
        access_control_enforced=True,
        author="slack-UADMIN",
        resolver=resolver,
        channel_source="api",
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
    assert payload["external_comment"]["skipped_reason"] == "http_event_author_untrusted"
    assert payload["external_comment"]["target"] == "issue"
    assert runner.issue_comments == []


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
