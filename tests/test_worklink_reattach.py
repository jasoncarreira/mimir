"""Tests for Worklink controller resume-after-restart (#561)."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Sequence

from mimir.worklink.backends import Caps, ComputeCaps, ComputeResult, RawResult, WorkOrder
from mimir.worklink.backends.registry import BackendRegistry, WorklinkConfig, WorklinkDefaults
from mimir.worklink.compute import LaunchHandle, WorkSpec
from mimir.worklink.orchestrator import WorklinkRunner
from mimir.worklink.run_state import (
    WorklinkContinuationCheckpoint,
    WorklinkRunState,
    clear_run_state,
    list_continuation_checkpoints,
    list_run_states,
    load_continuation_checkpoint,
    load_run_state,
    reattach_dispatch_argv,
    save_continuation_checkpoint,
    save_run_state,
)


def cp(
    args: Sequence[str] | str,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)


ISSUE_JSON_TEMPLATE = '''{{
  "id": {id},
  "title": "worklink slice",
  "description": "Acceptance criteria:\\n- [ ] do it\\n- [ ] echo ok\\n\\nReview criteria:\\n- reviewer checks it\\n\\nWorklink notes:\\n- Scope: test fixture\\n- Out of scope: unrelated work\\n- Suggested test command: echo ok",
  "labels": {labels},
  "parent_id": 380,
  "comments": []
}}'''


def _issue_json(issue_id: int, labels: list[str]) -> str:
    return ISSUE_JSON_TEMPLATE.format(
        id=issue_id, labels=str(labels).replace("'", '"')
    )


class FakeRemoteCompute:
    """A persistent (docker-sibling-shaped) compute: survives a controller
    disconnect, no shared filesystem (so the remote evidence gate applies)."""

    def __init__(
        self,
        *,
        persistent: bool = True,
        wait_result: ComputeResult | None = None,
        on_wait=None,
    ) -> None:
        self.name = "fake_remote"
        self.persistent = persistent
        self.wait_result = wait_result or ComputeResult(exit_code=0, stdout="ok", stderr="")
        self.on_wait = on_wait
        self.launched: list[tuple[WorkSpec, LaunchHandle]] = []
        self.waited: list[LaunchHandle] = []
        self.cleaned: list[LaunchHandle] = []
        self._n = 0

    def capabilities(self) -> ComputeCaps:
        return ComputeCaps(
            shared_filesystem=False,
            network_isolated=True,
            handle_cancel=True,
            persistent_after_disconnect=self.persistent,
        )

    async def launch(self, spec: WorkSpec) -> LaunchHandle:
        self._n += 1
        handle = LaunchHandle(self.name, f"job-{self._n}")
        self.launched.append((spec, handle))
        return handle

    async def wait(self, handle: LaunchHandle, timeout_s: int) -> ComputeResult:
        self.waited.append(handle)
        if self.on_wait is not None:
            self.on_wait()
        return self.wait_result

    async def logs(self, handle: LaunchHandle) -> str:
        return ""

    async def cancel(self, handle: LaunchHandle) -> None:
        return None

    async def cleanup(self, handle: LaunchHandle) -> None:
        self.cleaned.append(handle)


class FakeLocalCompute(FakeRemoteCompute):
    """Shared-filesystem, non-persistent (local_subprocess-shaped): nothing to
    reattach to, so its runs are never persisted."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.name = "fake_local"

    def capabilities(self) -> ComputeCaps:
        return ComputeCaps(
            shared_filesystem=True,
            network_isolated=False,
            handle_cancel=True,
            persistent_after_disconnect=False,
        )


class FakeBackend:
    name = "fake"

    def capabilities(self) -> Caps:
        return Caps("fake", False, False, False, True, None)

    def work_spec(
        self,
        order: WorkOrder,
        *,
        attempt: int,
        repo_url: str,
        base_ref: str,
        branch: str,
        test_command: str,
    ) -> WorkSpec:
        return WorkSpec(
            issue_id=order.issue_id,
            attempt=attempt,
            repo_url=repo_url,
            base_ref=base_ref,
            branch=branch,
            prompt=order.prompt,
            rules=order.rules,
            test_command=test_command,
            backend=self.name,
            timeout_s=order.timeout_s,
            env=order.env,
            local_worktree=order.worktree,
        )

    async def interpret(self, order: WorkOrder, result: object) -> RawResult:
        assert isinstance(result, ComputeResult)
        return RawResult(result.exit_code, order.transcript_root / "fake.json", "success", None)


def _remote_runner(repo: Path, calls: list, *, issue_id: int, labels: list[str]):
    """Fake runner: chainlink JSON + all git/gh routed through here (no real git)."""

    def runner(args: Sequence[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if isinstance(args, str):
            return cp(args)
        if args[:1] == ["chainlink"]:
            if args[1:3] == ["issue", "show"]:
                return cp(args, stdout=_issue_json(issue_id, labels))
            return cp(args)  # locks claim/release, issue label/unlabel/comment
        if args[:1] == ["git"]:
            if "config" in args:
                return cp(args, stdout="git@github.com:jasoncarreira/mimir.git\n")
            if "diff" in args and "--name-only" in args:
                return cp(args, stdout="changed.txt\n")
            if "diff" in args and "--stat" in args:
                return cp(args, stdout=" changed.txt | 1 +\n")
            return cp(args)  # fetch, rev-parse, worktree add/remove, status
        if args[:3] == ["gh", "pr", "create"]:
            return cp(args, stdout="https://github.com/jasoncarreira/mimir/pull/777\n")
        return cp(args)

    return runner


# --------------------------------------------------------------------------
# run_state module
# --------------------------------------------------------------------------


def test_run_state_roundtrip(tmp_path: Path) -> None:
    state = WorklinkRunState(
        issue_id=561,
        attempt=2,
        backend="codex",
        compute_name="docker_sibling",
        handle_substrate="docker_sibling",
        handle_identifier="worklink-abc123",
        branch="issue/561-a2",
        base_ref="main",
        local_base="origin/main",
        repo="/workspace/mimir",
        repo_url="git@github.com:jasoncarreira/mimir.git",
        test_command="echo ok",
        started_at="2026-06-18T16:00:00+00:00",
    )
    path = save_run_state(tmp_path, state)
    assert path.exists()
    assert load_run_state(tmp_path, 561) == state
    assert [s.issue_id for s in list_run_states(tmp_path)] == [561]
    clear_run_state(tmp_path, 561)
    assert load_run_state(tmp_path, 561) is None
    assert list_run_states(tmp_path) == []
    # Clearing a missing state is a no-op, not an error.
    clear_run_state(tmp_path, 561)


def test_list_run_states_skips_unparseable(tmp_path: Path) -> None:
    good = WorklinkRunState(
        issue_id=1, attempt=1, backend="codex", compute_name="docker_sibling",
        handle_substrate="docker_sibling", handle_identifier="j1", branch="issue/1-a1",
        base_ref="main", local_base="main", repo="/r", repo_url="", test_command=None,
        started_at="2026-06-18T16:00:00+00:00",
    )
    save_run_state(tmp_path, good)
    runs = tmp_path / "state" / "worklink" / "runs"
    (runs / "bad.json").write_text("{not json", encoding="utf-8")
    (runs / "incomplete.json").write_text('{"issue_id": 9}', encoding="utf-8")
    assert [s.issue_id for s in list_run_states(tmp_path)] == [1]


def test_reattach_dispatch_argv() -> None:
    argv = reattach_dispatch_argv(["mimir"], Path("/home"), "/repo", 561)
    assert argv == [
        "mimir", "worklink", "run", "561", "--reattach", "--autonomous",
        "--home", "/home", "--repo", "/repo",
    ]


def test_known_issue_continuation_checkpoint_roundtrip(tmp_path: Path) -> None:
    checkpoint = WorklinkContinuationCheckpoint.known_issue(
        issue_id=806,
        related_pr="https://github.com/jasoncarreira/mimir/pull/123",
        work_item_key="chainlink:806",
        exhausted_turn_id="turn-abc",
        repo="/workspace/mimir",
        worktree="/workspace/mimir",
        branch="issue/806-a1",
        completed_edits=["mimir/worklink/run_state.py"],
        unrun_validation=["uv run pytest -q tests/test_worklink_reattach.py"],
        next_commands=["git diff -- mimir/worklink/run_state.py"],
        required_label_status_adjustments=["keep worklink:in-progress"],
        created_at="2026-07-03T12:00:00+00:00",
    )

    path, created = save_continuation_checkpoint(tmp_path, checkpoint)

    assert created is True
    assert path.exists()
    loaded = load_continuation_checkpoint(tmp_path, checkpoint.dedupe_key)
    assert loaded == checkpoint
    assert loaded is not None
    assert loaded.kind == "known_issue"
    assert loaded.related_chainlink_issue_id == 806
    assert loaded.priority == "normal"
    assert [c.dedupe_key for c in list_continuation_checkpoints(tmp_path)] == [
        checkpoint.dedupe_key
    ]


def test_generic_continuation_checkpoint_is_high_priority_when_issue_unknown(
    tmp_path: Path,
) -> None:
    checkpoint = WorklinkContinuationCheckpoint.generic(
        work_item_key="worklink-finalizer:unknown",
        exhausted_turn_id="turn-no-issue",
        repo="/workspace/mimir",
        worktree="/workspace/mimir",
        branch="scratch",
        completed_edits=["inspected run_state.py"],
        unrun_validation=["pytest not started before exhaustion"],
        next_commands=["inspect artifact and infer Chainlink issue"],
        required_label_status_adjustments=["file or label a continuation issue"],
        created_at="2026-07-03T12:05:00+00:00",
    )

    _, created = save_continuation_checkpoint(tmp_path, checkpoint)
    loaded = load_continuation_checkpoint(tmp_path, checkpoint.dedupe_key)

    assert created is True
    assert loaded is not None
    assert loaded.kind == "generic"
    assert loaded.related_chainlink_issue_id is None
    assert loaded.priority == "high"
    assert loaded.next_commands == ["inspect artifact and infer Chainlink issue"]


def test_continuation_checkpoint_duplicate_save_is_suppressed(
    tmp_path: Path,
) -> None:
    first = WorklinkContinuationCheckpoint.known_issue(
        issue_id=806,
        work_item_key="chainlink:806",
        exhausted_turn_id="turn-abc",
        repo="/workspace/mimir",
        worktree="/workspace/mimir",
        branch="issue/806-a1",
        completed_edits=["first snapshot"],
        unrun_validation=["pytest"],
        next_commands=["resume"],
        required_label_status_adjustments=["none"],
        created_at="2026-07-03T12:00:00+00:00",
    )
    rerendered = WorklinkContinuationCheckpoint.known_issue(
        issue_id=806,
        related_pr="https://github.com/jasoncarreira/mimir/pull/123",
        work_item_key="chainlink:806",
        exhausted_turn_id="turn-abc",
        repo="/workspace/mimir",
        worktree="/workspace/mimir",
        branch="issue/806-a1",
        completed_edits=["second snapshot should not replace first"],
        unrun_validation=["pytest -q"],
        next_commands=["resume again"],
        required_label_status_adjustments=["none"],
        created_at="2026-07-03T12:01:00+00:00",
    )

    first_path, first_created = save_continuation_checkpoint(tmp_path, first)
    second_path, second_created = save_continuation_checkpoint(tmp_path, rerendered)

    assert first.dedupe_key == rerendered.dedupe_key
    assert first_path == second_path
    assert first_created is True
    assert second_created is False
    assert list_continuation_checkpoints(tmp_path) == [first]


# --------------------------------------------------------------------------
# orchestrator.reattach
# --------------------------------------------------------------------------


def _save_inflight_state(home: Path, repo: Path, *, issue_id: int, job: str) -> None:
    save_run_state(
        home,
        WorklinkRunState(
            issue_id=issue_id,
            attempt=1,
            backend="fake",
            compute_name="fake_remote",
            handle_substrate="fake_remote",
            handle_identifier=job,
            branch=f"issue/{issue_id}-a1",
            base_ref="main",
            local_base="origin/main",
            repo=str(repo),
            repo_url="git@github.com:jasoncarreira/mimir.git",
            test_command="echo ok",
            started_at="2026-06-18T16:00:00+00:00",
        ),
    )


def test_reattach_waits_on_surviving_worker_and_opens_pr(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    issue_id = 561
    _save_inflight_state(tmp_path, repo, issue_id=issue_id, job="job-orig")
    calls: list = []
    runner = _remote_runner(repo, calls, issue_id=issue_id, labels=["worklink:in-progress"])
    compute = FakeRemoteCompute(wait_result=ComputeResult(exit_code=0, stdout="ok", stderr=""))
    registry = BackendRegistry(WorklinkConfig())
    registry.register(FakeBackend())
    registry.register_compute(compute)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).reattach(issue_id)
    )

    assert result.status == "completed", (result.reason, calls)
    assert result.review_ready is True
    assert result.pr_url == "https://github.com/jasoncarreira/mimir/pull/777"
    # It harvested the SURVIVING worker (reconstructed handle), not a new launch.
    assert compute.waited[0] == LaunchHandle("fake_remote", "job-orig")
    # No re-claim / attempt bump on resume.
    assert not any(isinstance(a, list) and a[:3] == ["chainlink", "locks", "claim"] for a in calls)
    # Transitioned to review + released the lock.
    assert ["chainlink", "issue", "label", str(issue_id), "worklink:review"] in calls
    assert ["chainlink", "locks", "release", str(issue_id)] in calls
    # Run state cleared on terminal completion.
    assert load_run_state(tmp_path, issue_id) is None


def test_reattach_worker_lost_redispatches_to_ready(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    issue_id = 562
    _save_inflight_state(tmp_path, repo, issue_id=issue_id, job="job-gone")
    calls: list = []
    runner = _remote_runner(repo, calls, issue_id=issue_id, labels=["worklink:in-progress"])
    # Broker can no longer produce the result -> launch_error set.
    lost = ComputeResult(
        exit_code=-1, stdout="", stderr="broker gone", launch_error="broker wait failed"
    )
    compute = FakeRemoteCompute(wait_result=lost)
    registry = BackendRegistry(WorklinkConfig())
    registry.register(FakeBackend())
    registry.register_compute(compute)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).reattach(issue_id)
    )

    assert result.status == "failed"
    assert result.reason == "reattach: worker lost"
    assert compute.waited  # it tried to harvest
    # Transitioned back to ready for redispatch (attempt 1 < max), no PR opened.
    assert ["chainlink", "issue", "label", str(issue_id), "worklink:ready"] in calls
    assert not any(isinstance(a, list) and a[:3] == ["gh", "pr", "create"] for a in calls)
    assert load_run_state(tmp_path, issue_id) is None


def test_reattach_skips_when_leaf_no_longer_in_progress(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    issue_id = 563
    _save_inflight_state(tmp_path, repo, issue_id=issue_id, job="job-x")
    calls: list = []
    # Reaper already moved it back to ready (no in-progress label).
    runner = _remote_runner(repo, calls, issue_id=issue_id, labels=["worklink:ready"])
    compute = FakeRemoteCompute()
    registry = BackendRegistry(WorklinkConfig())
    registry.register(FakeBackend())
    registry.register_compute(compute)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).reattach(issue_id)
    )

    assert result.status == "failed"
    assert "no longer in-progress" in (result.reason or "")
    assert compute.waited == []  # never touched the worker
    assert load_run_state(tmp_path, issue_id) is None


def test_reattach_no_state_is_noop(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    registry = BackendRegistry(WorklinkConfig())
    registry.register(FakeBackend())
    registry.register_compute(FakeRemoteCompute())
    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=lambda *a, **k: cp(a), registry=registry).reattach(999)
    )
    assert result.status == "failed"
    assert result.reason == "reattach: no run state"


# --------------------------------------------------------------------------
# run() persistence: persisted for persistent substrates, cleared on completion
# --------------------------------------------------------------------------


def test_run_persists_state_for_persistent_compute_then_clears(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    issue_id = 441
    calls: list = []
    runner = _remote_runner(repo, calls, issue_id=issue_id, labels=["worklink:in-progress"])

    seen: dict[str, object] = {}

    def on_wait() -> None:
        # While the worker runs, the handle is durably persisted so a restart can
        # find it.
        st = load_run_state(tmp_path, issue_id)
        seen["during"] = st

    compute = FakeRemoteCompute(on_wait=on_wait)
    registry = BackendRegistry(
        WorklinkConfig(defaults=WorklinkDefaults(compute_backend="fake_remote"))
    )
    registry.register(FakeBackend())
    registry.register_compute(compute)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=runner, registry=registry).run(
            issue_id, backend_name="fake"
        )
    )

    assert result.status == "completed", (result.reason, calls)
    during = seen["during"]
    assert isinstance(during, WorklinkRunState)
    assert during.compute_name == "fake_remote"
    assert during.handle_identifier == "job-1"
    assert during.branch == "issue/441-a1"
    # Cleared on terminal completion.
    assert load_run_state(tmp_path, issue_id) is None


def test_run_does_not_persist_state_for_local_compute(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    issue_id = 442
    calls: list = []

    seen: dict[str, object] = {}

    def on_wait() -> None:
        seen["during"] = load_run_state(tmp_path, issue_id)

    # Local/shared-filesystem path: build a worktree dir the backend writes into.
    worktree = repo / ".worklink" / f"{issue_id}-1"

    class LocalBackend(FakeBackend):
        async def interpret(self, order: WorkOrder, result: object) -> RawResult:
            order.worktree.mkdir(parents=True, exist_ok=True)
            (order.worktree / "changed.txt").write_text("hi\n", encoding="utf-8")
            return RawResult(0, order.transcript_root / "fake.json", "success", None)

    def local_runner(args, *, cwd=None):
        calls.append(args)
        if isinstance(args, str):
            return cp(args, stdout="ok\n")
        if args[:1] == ["chainlink"]:
            if args[1:3] == ["issue", "show"]:
                return cp(args, stdout=_issue_json(issue_id, ["worklink:in-progress"]))
            return cp(args)
        if args[:1] == ["git"]:
            if "config" in args:
                return cp(args, stdout="git@github.com:jasoncarreira/mimir.git\n")
            if "worktree" in args and "add" in args:
                worktree.mkdir(parents=True, exist_ok=True)
                return cp(args)
            if "diff" in args and "--name-only" in args:
                return cp(args, stdout="changed.txt\n")
            if "diff" in args and "--stat" in args:
                return cp(args, stdout=" changed.txt | 1 +\n")
            if "diff" in args and "--cached" in args and "--quiet" in args:
                return cp(args, returncode=1)
            return cp(args)
        if args[:3] == ["gh", "pr", "create"]:
            return cp(args, stdout="https://github.com/x/y/pull/1\n")
        return cp(args)

    compute = FakeLocalCompute(on_wait=on_wait)
    registry = BackendRegistry(
        WorklinkConfig(defaults=WorklinkDefaults(compute_backend="fake_local"))
    )
    registry.register(LocalBackend())
    registry.register_compute(compute)

    result = asyncio.run(
        WorklinkRunner(home=tmp_path, repo=repo, runner=local_runner, registry=registry).run(
            issue_id, backend_name="fake"
        )
    )

    assert result.status == "completed", (result.reason, calls)
    # Local work dies with the controller -> nothing is ever persisted.
    assert seen["during"] is None
    assert load_run_state(tmp_path, issue_id) is None


# --------------------------------------------------------------------------
# server startup reconcile spawner
# --------------------------------------------------------------------------


def test_reattach_inflight_spawns_detached(tmp_path: Path, monkeypatch) -> None:
    from mimir import server

    _save_inflight_state(tmp_path, tmp_path / "repo", issue_id=601, job="j1")
    _save_inflight_state(tmp_path, tmp_path / "repo", issue_id=602, job="j2")
    monkeypatch.setenv("WORKLINK_REPO", "/workspace/mimir")
    monkeypatch.delenv("WORKLINK_RUN_BIN", raising=False)

    spawned: list[dict] = []

    def fake_popen(argv, **kwargs):
        spawned.append({"argv": argv, "kwargs": kwargs})
        return object()

    dispatched = server.reattach_inflight_worklink_runs(tmp_path, popen=fake_popen)

    assert sorted(dispatched) == [601, 602]
    assert len(spawned) == 2
    argvs = [s["argv"] for s in spawned]
    assert ["mimir", "worklink", "run", "601", "--reattach", "--autonomous",
            "--home", str(tmp_path), "--repo", "/workspace/mimir"] in argvs
    # Detached so a long worker wait never blocks startup.
    assert all(s["kwargs"].get("start_new_session") is True for s in spawned)


def test_reattach_inflight_noop_without_repo(tmp_path: Path, monkeypatch) -> None:
    from mimir import server

    _save_inflight_state(tmp_path, tmp_path / "repo", issue_id=601, job="j1")
    monkeypatch.delenv("WORKLINK_REPO", raising=False)
    spawned: list = []
    dispatched = server.reattach_inflight_worklink_runs(
        tmp_path, popen=lambda *a, **k: spawned.append(a)
    )
    assert dispatched == []
    assert spawned == []
