"""Worklink slice-3 autonomy (chainlink #444).

Covers the two review criteria explicitly:
  * a TIGHT severity window provably stops autonomous worker launches, and
  * no path lets two workers hold the same issue (cap + lock-fail);
plus the concurrency cap, the TTL reaper's stale-claim recovery, and the
ready-queue poller's discovery/dispatch (cap-respecting, detached).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Sequence

import pytest

from mimir.worklink import autonomy
from mimir.worklink.claims import CLAIM_PREFIX, ChainlinkClaims, ClaimRecord


def cp(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class FakeChainlink:
    """Records chainlink invocations and answers from canned tables."""

    def __init__(
        self,
        *,
        in_progress: list[int] | None = None,
        ready: list[int] | None = None,
        comments: dict[int, list[str]] | None = None,
        fail_steal: set[int] | None = None,
        active_locks: list[int] | None = None,
        lock_agents: dict[int, str] | None = None,
        epic_ids: set[int] | None = None,
    ) -> None:
        self.in_progress = in_progress or []
        self.ready = ready or []
        self.comments = comments or {}
        self.fail_steal = fail_steal or set()
        self.active_locks = active_locks if active_locks is not None else list(self.in_progress)
        self.lock_agents = lock_agents or {}
        self.epic_ids = epic_ids or set()
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args = list(args)
        self.calls.append(args)
        # drop the leading binary token
        tail = args[1:]
        if tail[:2] == ["issue", "list"]:
            label = tail[tail.index("--label") + 1] if "--label" in tail else ""
            ids = self.in_progress if label == "worklink:in-progress" else self.ready if label == "worklink:ready" else []
            return cp(stdout=json.dumps([{"id": i} for i in ids]))
        if tail[:2] == ["issue", "show"]:
            issue_id = int(tail[2])
            labels = []
            if issue_id in self.in_progress:
                labels.append("worklink:in-progress")
            if issue_id in self.epic_ids:
                labels.append("worklink:epic")
            payload = {"id": issue_id, "comments": list(self.comments.get(issue_id, [])), "labels": labels}
            return cp(stdout=json.dumps(payload))
        if tail[:2] == ["locks", "list"]:
            locks = {
                str(i): {
                    "issue_id": i,
                    **({"agent_id": self.lock_agents[i]} if i in self.lock_agents else {}),
                }
                for i in self.active_locks
            }
            return cp(stdout=json.dumps({"version": 1, "locks": locks}))
        if tail[:2] == ["locks", "steal"]:
            issue_id = int(tail[2])
            return cp(returncode=1 if issue_id in self.fail_steal else 0)
        # locks release/label/unlabel/comment all succeed
        return cp()

    def names(self) -> list[str]:
        return [" ".join(c[1:4]) for c in self.calls]


def _claim_comment(
    issue_id: int,
    *,
    attempt: int,
    age: timedelta,
    agent: str = "mimir-worklink",
    heartbeat_age: timedelta | None = None,
) -> str:
    now = datetime.now(UTC)
    rec = ClaimRecord(
        issue_id=issue_id,
        attempt=attempt,
        agent_id=agent,
        claimed_at=now - age,
        heartbeat_at=now - heartbeat_age if heartbeat_age is not None else None,
    )
    return rec.to_comment()


# ── claims.py: discovery + cap count ────────────────────────────────


def test_active_worklink_lock_count_reads_locks_json() -> None:
    fake = FakeChainlink(active_locks=[10, 11, 12])
    claims = ChainlinkClaims(agent_id="t", runner=fake)
    assert claims.active_worklink_lock_count() == 3
    assert claims.issue_ids_with_label("worklink:ready") == []


def test_issue_ids_with_label_tolerates_bad_json() -> None:
    # Best-effort DISCOVERY path: a garbled list just means "nothing this cycle".
    claims = ChainlinkClaims(agent_id="t", runner=lambda a: cp(stdout="not json"))
    assert claims.issue_ids_with_label("worklink:ready") == []


def test_active_worklink_lock_count_raises_on_locks_failure() -> None:
    # STRICT cap path: a failed query must NOT read as "0 active" (fail closed).
    claims = ChainlinkClaims(agent_id="t", runner=lambda a: cp(returncode=1, stderr="boom"))
    with pytest.raises(RuntimeError):
        claims.active_worklink_lock_count()


def test_active_worklink_lock_count_raises_on_bad_json() -> None:
    claims = ChainlinkClaims(agent_id="t", runner=lambda a: cp(stdout="not json"))
    with pytest.raises(RuntimeError):
        claims.active_worklink_lock_count()


# ── claims.py: TTL reaper recovery ──────────────────────────────────


def test_reap_home_reads_content_keyed_comment_dicts() -> None:
    comment = {"content": _claim_comment(49, attempt=1, age=timedelta(hours=3))}
    fake = FakeChainlink(in_progress=[49], comments={49: [comment]})  # type: ignore[list-item]
    claims = ChainlinkClaims(agent_id="t", runner=fake)
    reaped = claims.reap_home(ttl=timedelta(hours=2))
    assert [r.issue_id for r in reaped] == [49]


def test_reap_home_fail_soft_when_in_progress_list_errors() -> None:
    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if list(args)[1:3] == ["issue", "list"]:
            return cp(returncode=1, stderr="list failed")
        raise AssertionError(f"unexpected call after list failure: {args}")

    claims = ChainlinkClaims(agent_id="t", runner=runner)
    assert claims.reap_home(ttl=timedelta(hours=2)) == []


def test_reap_home_recovers_stale_claim_to_ready() -> None:
    fake = FakeChainlink(
        in_progress=[50],
        comments={50: [_claim_comment(50, attempt=1, age=timedelta(hours=3))]},
    )
    claims = ChainlinkClaims(agent_id="t", runner=fake, max_attempts=3)
    reaped = claims.reap_home(ttl=timedelta(hours=2))
    assert [r.issue_id for r in reaped] == [50]
    names = fake.names()
    assert "locks steal 50" in names
    assert "locks release 50" in names
    assert "issue label 50" in names  # relabelled
    # back to ready (retries remain), not blocked
    assert any(c[1:] == ["issue", "label", "50", "worklink:ready"] for c in fake.calls)
    assert not any(c[1:] == ["issue", "label", "50", "worklink:blocked"] for c in fake.calls)


def test_reap_home_blocks_when_attempts_exhausted() -> None:
    fake = FakeChainlink(
        in_progress=[51],
        comments={51: [_claim_comment(51, attempt=3, age=timedelta(hours=3))]},
    )
    claims = ChainlinkClaims(agent_id="t", runner=fake, max_attempts=3)
    reaped = claims.reap_home(ttl=timedelta(hours=2))
    assert [r.issue_id for r in reaped] == [51]
    assert any(c[1:] == ["issue", "label", "51", "worklink:blocked"] for c in fake.calls)


def test_reap_home_skips_when_lock_was_already_released() -> None:
    fake = FakeChainlink(
        in_progress=[54],
        active_locks=[],
        comments={54: [_claim_comment(54, attempt=1, age=timedelta(hours=3))]},
    )
    claims = ChainlinkClaims(agent_id="t", runner=fake)
    assert claims.reap_home(ttl=timedelta(hours=2)) == []
    assert "locks steal 54" not in fake.names()


def test_reap_home_skips_when_issue_already_transitioned() -> None:
    fake = FakeChainlink(
        in_progress=[],
        active_locks=[56],
        comments={56: [_claim_comment(56, attempt=1, age=timedelta(hours=3))]},
    )
    claims = ChainlinkClaims(agent_id="t", runner=fake)
    assert claims.reap_stale_claims(
        [ClaimRecord(56, 1, "mimir-worklink", datetime.now(UTC) - timedelta(hours=3))],
        ttl=timedelta(hours=2),
    ) == []
    assert not any(c[1:] == ["issue", "label", "56", "worklink:ready"] for c in fake.calls)


def test_reap_home_skips_when_lock_owner_changed() -> None:
    fake = FakeChainlink(
        in_progress=[55],
        active_locks=[55],
        lock_agents={55: "other-agent"},
        comments={55: [_claim_comment(55, attempt=1, age=timedelta(hours=3), agent="old-agent")]},
    )
    claims = ChainlinkClaims(agent_id="t", runner=fake)
    assert claims.reap_home(ttl=timedelta(hours=2)) == []
    assert "locks steal 55" not in fake.names()


def test_reap_home_leaves_fresh_claim_untouched() -> None:
    fake = FakeChainlink(
        in_progress=[52],
        comments={52: [_claim_comment(52, attempt=1, age=timedelta(minutes=5))]},
    )
    claims = ChainlinkClaims(agent_id="t", runner=fake)
    assert claims.reap_home(ttl=timedelta(hours=2)) == []
    assert "locks steal 52" not in fake.names()


def test_reap_home_leaves_finalizing_claim_with_fresh_heartbeat_untouched() -> None:
    # Regression for chainlink #750: the claim is older than timeout_s and in
    # the finalize window, but a fresh heartbeat proves the orchestrator is
    # still alive while the remote test job runs.
    fake = FakeChainlink(
        in_progress=[57],
        comments={
            57: [
                _claim_comment(
                    57,
                    attempt=1,
                    age=timedelta(seconds=2700),
                    heartbeat_age=timedelta(seconds=30),
                )
            ]
        },
    )
    claims = ChainlinkClaims(agent_id="t", runner=fake)

    assert claims.reap_home(ttl=timedelta(seconds=1860)) == []
    assert "locks steal 57" not in fake.names()


def test_reap_home_uses_latest_record_per_issue() -> None:
    # Two claim comments for the same issue; the latest (attempt 2, fresh) wins,
    # so the stale attempt-1 record must NOT trigger a reap.
    fake = FakeChainlink(
        in_progress=[53],
        comments={53: [
            _claim_comment(53, attempt=1, age=timedelta(hours=5)),
            _claim_comment(53, attempt=2, age=timedelta(minutes=1)),
        ]},
    )
    claims = ChainlinkClaims(agent_id="t", runner=fake)
    assert claims.reap_home(ttl=timedelta(hours=2)) == []


# ── claims.py: per-issue exclusivity (lock fail → not claimed) ──────


def test_claim_issue_refuses_when_lock_unavailable() -> None:
    def runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if list(args)[1:3] == ["locks", "claim"]:
            return cp(returncode=1, stderr="locked by other")
        return cp()

    claims = ChainlinkClaims(agent_id="t", runner=runner)
    result = claims.claim_issue(99, comments=[])
    assert result.claimed is False  # a second worker can never hold the same issue


# ── autonomy.py: concurrency cap + config ───────────────────────────


def _write_worklink_yaml(
    home: Path,
    *,
    max_concurrent: int = 2,
    priority: str = "normal",
    timeout_s: int = 1800,
    reaper_ttl_s: int = 7200,
) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "worklink.yaml").write_text(
        "defaults:\n"
        f"  priority: {priority}\n"
        f"  max_concurrent: {max_concurrent}\n"
        f"  timeout_s: {timeout_s}\n"
        f"  reaper_ttl_s: {reaper_ttl_s}\n",
        encoding="utf-8",
    )


def test_check_concurrency_allows_below_cap(tmp_path: Path) -> None:
    _write_worklink_yaml(tmp_path, max_concurrent=2)
    claims = ChainlinkClaims(agent_id="t", runner=FakeChainlink(active_locks=[1]))
    check = autonomy.check_concurrency(tmp_path, claims=claims)
    assert check.allowed and check.active == 1 and check.cap == 2


def test_check_concurrency_blocks_at_cap(tmp_path: Path) -> None:
    _write_worklink_yaml(tmp_path, max_concurrent=2)
    claims = ChainlinkClaims(agent_id="t", runner=FakeChainlink(active_locks=[1, 2]))
    check = autonomy.check_concurrency(tmp_path, claims=claims)
    assert not check.allowed and check.active == 2 and "cap reached" in check.reason


def test_worklink_repo_requires_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKLINK_REPO", raising=False)
    monkeypatch.delenv("MIMIR_WORKLINK_REPO", raising=False)
    with pytest.raises(RuntimeError, match="WORKLINK_REPO is required"):
        autonomy.worklink_repo()


def test_worklink_repo_accepts_backcompat_alias(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.delenv("WORKLINK_REPO", raising=False)
    monkeypatch.setenv("MIMIR_WORKLINK_REPO", str(tmp_path))
    assert autonomy.worklink_repo() == str(tmp_path)


def test_worklink_priority_from_config(tmp_path: Path) -> None:
    _write_worklink_yaml(tmp_path, priority="high")
    assert autonomy.worklink_priority(tmp_path) == "high"



def test_prune_stale_attempt_worktrees_for_home_uses_worklink_repo_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_worklink_yaml(tmp_path, timeout_s=600, reaper_ttl_s=3600)
    repo = tmp_path / "repo"
    repo.mkdir()
    old = tmp_path / ".worklink" / repo.name / "613-1"
    old.mkdir(parents=True)
    mtime = (datetime.now().timestamp() - 7200)
    import os

    os.utime(old, (mtime, mtime))
    monkeypatch.setenv("WORKLINK_REPO", str(repo))

    pruned = autonomy.prune_stale_attempt_worktrees_for_home(tmp_path)

    assert pruned == [old]
    assert not old.exists()


def test_prune_stale_attempt_worktrees_for_home_silent_without_repo_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_worklink_yaml(tmp_path)
    monkeypatch.delenv("WORKLINK_REPO", raising=False)
    monkeypatch.delenv("MIMIR_WORKLINK_REPO", raising=False)

    assert autonomy.prune_stale_attempt_worktrees_for_home(tmp_path) == []


def test_reap_for_home_uses_config_ttl(tmp_path: Path) -> None:
    # reaper_ttl_s = 1h; a 3h-old claim is stale and gets reaped.
    _write_worklink_yaml(tmp_path, timeout_s=600, reaper_ttl_s=3600)
    fake = FakeChainlink(
        in_progress=[60],
        comments={60: [_claim_comment(60, attempt=1, age=timedelta(hours=3))]},
    )
    claims = ChainlinkClaims(agent_id="t", runner=fake)
    reaped = autonomy.reap_stale_claims_for_home(tmp_path, claims=claims)
    assert [r.issue_id for r in reaped] == [60]


def test_reap_for_home_refuses_ttl_not_greater_than_timeout(tmp_path: Path) -> None:
    _write_worklink_yaml(tmp_path, timeout_s=7200, reaper_ttl_s=7200)
    claims = ChainlinkClaims(agent_id="t", runner=FakeChainlink(in_progress=[60]))
    with pytest.raises(RuntimeError, match="reaper_ttl_s must be greater"):
        autonomy.reap_stale_claims_for_home(tmp_path, claims=claims)


def test_reap_for_home_refuses_ttl_inside_finalize_window(tmp_path: Path) -> None:
    _write_worklink_yaml(tmp_path, timeout_s=1800, reaper_ttl_s=1860)
    claims = ChainlinkClaims(agent_id="t", runner=FakeChainlink(in_progress=[60]))
    with pytest.raises(RuntimeError, match=r"2 \* timeout_s"):
        autonomy.reap_stale_claims_for_home(tmp_path, claims=claims)


# ── worklink_run tool: arbiter shed (TIGHT) + cap + dispatch ────────


class _FakeDecision:
    def __init__(self, fire: bool, severity_name: str = "TIGHT", priority: str = "normal") -> None:
        self.fire = fire
        self.priority = priority
        self.reason = "test"
        self.severity = type("Sev", (), {"name": severity_name})()


class _FakeArbiter:
    def __init__(self, fire: bool) -> None:
        self._fire = fire
        self.calls: list[str] = []

    def should_fire(self, *, priority: str = "normal", **_: object):
        self.calls.append(priority)
        return _FakeDecision(self._fire, priority=priority)


@pytest.fixture
def _tool_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _write_worklink_yaml(tmp_path)
    repo_dir = tmp_path / "src"  # distinct from cwd so we can prove WORKLINK_REPO is honored
    repo_dir.mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setenv("WORKLINK_REPO", str(repo_dir))
    monkeypatch.delenv("MIMIR_WORKLINK_REPO", raising=False)
    from mimir.tools import registry

    dispatched: list[dict] = []

    def fake_run_worklink(*, home, repo, issue_id, backend=None, **kwargs):
        dispatched.append({
            "issue_id": issue_id,
            "repo": str(repo),
            "home": str(home),
            "autonomous": kwargs.get("autonomous"),
        })
        return type("R", (), {
            "issue_id": issue_id, "attempt": 1, "status": "completed",
            "review_ready": True, "pr_url": None, "evidence_path": None, "reason": None,
        })()

    import mimir.worklink.orchestrator as orch
    monkeypatch.setattr(orch, "run_worklink", fake_run_worklink, raising=True)
    # keep concurrency below cap by default (0 active)
    monkeypatch.setattr(autonomy, "check_concurrency", lambda home, **_: autonomy.ConcurrencyCheck(True, 0, 2))
    yield registry, dispatched, repo_dir
    registry.set_arbiter(None)


@pytest.mark.asyncio
async def test_worklink_run_sheds_under_tight(_tool_env) -> None:
    registry, dispatched, _repo = _tool_env
    arbiter = _FakeArbiter(fire=False)  # severity TIGHT → should_fire False
    registry.set_arbiter(arbiter)
    out = await registry.worklink_run.ainvoke({"issue_id": 443})
    assert "shed" in out and "TIGHT" in out
    assert dispatched == []          # provably no launch under TIGHT
    assert arbiter.calls == ["normal"]


@pytest.mark.asyncio
async def test_worklink_run_dispatches_when_clear_using_worklink_repo(_tool_env) -> None:
    registry, dispatched, repo_dir = _tool_env
    registry.set_arbiter(_FakeArbiter(fire=True))
    out = await registry.worklink_run.ainvoke({"issue_id": 443})
    assert "443" in out and "completed" in out
    assert [d["issue_id"] for d in dispatched] == [443]
    assert dispatched[0]["autonomous"] is True
    # the executor runs against WORKLINK_REPO, not the server process cwd
    assert dispatched[0]["repo"] == str(repo_dir)


@pytest.mark.asyncio
async def test_worklink_run_tool_propagates_refused_result(_tool_env, monkeypatch: pytest.MonkeyPatch) -> None:
    registry, dispatched, _repo = _tool_env
    registry.set_arbiter(_FakeArbiter(fire=True))

    def fake_refused(*, home, repo, issue_id, backend=None, **kwargs):
        dispatched.append({"issue_id": issue_id, "autonomous": kwargs.get("autonomous")})
        return type("R", (), {
            "issue_id": issue_id, "attempt": None, "status": "refused",
            "review_ready": False, "pr_url": None, "evidence_path": None,
            "reason": "unsafe compute",
        })()

    import mimir.worklink.orchestrator as orch
    monkeypatch.setattr(orch, "run_worklink", fake_refused, raising=True)
    out = await registry.worklink_run.ainvoke({"issue_id": 443})
    assert "refused" in out and "unsafe compute" in out
    assert dispatched == [{"issue_id": 443, "autonomous": True}]


@pytest.mark.asyncio
async def test_worklink_run_fails_when_worklink_repo_unset(
    _tool_env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, dispatched, _repo = _tool_env
    registry.set_arbiter(_FakeArbiter(fire=True))
    monkeypatch.delenv("WORKLINK_REPO", raising=False)
    monkeypatch.delenv("MIMIR_WORKLINK_REPO", raising=False)
    out = await registry.worklink_run.ainvoke({"issue_id": 443})
    assert "WORKLINK_REPO is required" in out
    assert dispatched == []


@pytest.mark.asyncio
async def test_worklink_run_skips_at_cap(_tool_env, monkeypatch: pytest.MonkeyPatch) -> None:
    registry, dispatched, _repo = _tool_env
    registry.set_arbiter(_FakeArbiter(fire=True))
    monkeypatch.setattr(autonomy, "check_concurrency", lambda home, **_: autonomy.ConcurrencyCheck(False, 2, 2))
    out = await registry.worklink_run.ainvoke({"issue_id": 443})
    assert "skipped" in out
    assert dispatched == []


@pytest.mark.asyncio
async def test_worklink_run_fails_closed_when_cap_unreadable(_tool_env, monkeypatch: pytest.MonkeyPatch) -> None:
    registry, dispatched, _repo = _tool_env
    registry.set_arbiter(_FakeArbiter(fire=True))

    def boom(home, **_):
        raise RuntimeError("chainlink unreachable")

    monkeypatch.setattr(autonomy, "check_concurrency", boom)
    out = await registry.worklink_run.ainvoke({"issue_id": 443})
    assert "concurrency check error" in out  # fail closed: surfaced, not dispatched
    assert dispatched == []


def test_server_build_app_wires_worklink_arbiter_and_reaper() -> None:
    import inspect
    import mimir.server as server

    source = inspect.getsource(server.build_app)
    assert "_agent_tools.set_arbiter(agent._arbiter)" in source
    assert "scheduler.add_worklink_reaper_job(" in source
    assert "MIMIR_WORKLINK_REAPER_CRON" in source


# ── ready-queue poller: discovery + cap-bounded detached dispatch ───


POLLER = Path(__file__).resolve().parent.parent / "mimir" / "optional-skills" / "chainlink-orchestrator" / "poller.py"


def _fake_chainlink_script(
    tmp: Path,
    *,
    ready: list[int],
    epics: list[int] | None = None,
    blocked_epics: list[int] | None = None,
    review_epics: list[int] | None = None,
    parents: dict[int, int] | None = None,
    in_progress: list[int] | None = None,
    active_locks: list[int] | None = None,
    actionable: list[int] | None = None,
) -> Path:
    """A tiny stand-in chainlink for ready-label + actionable queries."""
    script = tmp / "chainlink"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "a = sys.argv[1:]\n"
        f"ready = {ready!r}\n"
        f"epics = {(epics or [])!r}\n"
        f"blocked_epics = {(blocked_epics or [])!r}\n"
        f"review_epics = {(review_epics or [])!r}\n"
        f"parents = {(parents or {})!r}\n"
        f"actionable = {(ready if actionable is None else actionable)!r}\n"
        f"inprog = {(in_progress or [])!r}\n"
        f"locks = {(active_locks if active_locks is not None else (in_progress or []))!r}\n"
        "if a[:2] == ['locks','list']:\n"
        "    print(json.dumps({'version': 1, 'locks': {str(i): {'issue_id': i} for i in locks}}))\n"
        "    sys.exit(0)\n"
        "if a[:2] == ['issue','ready']:\n"
        "    print(json.dumps([{'id': i} for i in actionable]))\n"
        "    sys.exit(0)\n"
        "if a[:2] == ['issue','list']:\n"
        "    label = a[a.index('--label')+1] if '--label' in a else ''\n"
        "    ids = epics if label=='worklink:epic' else blocked_epics if label=='worklink:blocked' else review_epics if label=='worklink:review' else inprog if label=='worklink:in-progress' else ready if label=='worklink:ready' else []\n"
        "    print(json.dumps([{'id': i, 'parent_id': parents.get(i)} for i in ids]))\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_poller_import_path_repair_uses_explicit_source_dir_without_editable_install(tmp_path: Path) -> None:
    """Exercise the actual repair helper without relying on editable-install .pths."""

    skill_dir = tmp_path / "skills" / "chainlink-orchestrator"
    skill_dir.mkdir(parents=True)
    source_dir = tmp_path / "source"
    (source_dir / "mimir").mkdir(parents=True)
    (source_dir / "mimir" / "__init__.py").write_text("", encoding="utf-8")

    # Run only the helper prelude from the real poller under `python -S`, which
    # disables site initialization and therefore the project's editable .pth. If
    # the helper is a no-op, the source dir will not appear on sys.path.
    prelude = POLLER.read_text(encoding="utf-8").split("\n_ensure_mimir_import_path()\n", 1)[0]
    script = prelude + "\n_ensure_mimir_import_path()\nimport sys; print(sys.path[0])\n"
    proc = subprocess.run(
        [sys.executable, "-S", "-c", script],
        cwd=str(skill_dir),
        env={"MIMIR_SOURCE_DIR": str(source_dir), "PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(source_dir)


def test_poller_import_path_repair_adds_source_venv_site_packages(tmp_path: Path) -> None:
    """System python plus source path also needs runtime deps from source .venv."""

    skill_dir = tmp_path / "skills" / "chainlink-orchestrator"
    skill_dir.mkdir(parents=True)
    source_dir = tmp_path / "source"
    (source_dir / "mimir").mkdir(parents=True)
    (source_dir / "mimir" / "__init__.py").write_text("", encoding="utf-8")
    site = source_dir / ".venv" / "lib" / "python3.11" / "site-packages"
    site.mkdir(parents=True)
    (site / "fake_runtime_dep.py").write_text("VALUE = 42\n", encoding="utf-8")

    prelude = POLLER.read_text(encoding="utf-8").split("\n_ensure_mimir_import_path()\n", 1)[0]
    script = (
        prelude
        + "\n_ensure_mimir_import_path()\n"
        + "import fake_runtime_dep; print(fake_runtime_dep.VALUE)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-S", "-c", script],
        cwd=str(skill_dir),
        env={"MIMIR_SOURCE_DIR": str(source_dir), "PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "42"


def _fake_run_bin(tmp: Path) -> Path:
    """A run-bin that records each `worklink run <id>` dispatch to a file."""
    record = tmp / "dispatched.txt"
    pids = tmp / "fake-run-bin-pids.txt"
    log = tmp / "fake-run-bin.log"
    script = tmp / "fakemimir"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        f"log_path = {str(log)!r}\n"
        "devnull = os.open(os.devnull, os.O_RDWR)\n"
        "try:\n"
        "    os.dup2(devnull, 0)\n"
        "    os.dup2(os.open(log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o644), 1)\n"
        "    os.dup2(os.open(log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o644), 2)\n"
        "finally:\n"
        "    os.close(devnull)\n"
        f"open({str(pids)!r}, 'a', encoding='utf-8').write(str(os.getpid()) + '\\n')\n"
        f"open({str(record)!r}, 'a', encoding='utf-8').write(' '.join(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _read_dispatch_lines(tmp: Path) -> list[str]:
    record = tmp / "dispatched.txt"
    if not record.exists():
        return []
    return record.read_text(encoding="utf-8").splitlines()


def _wait_for_dispatch_lines(tmp: Path, expected: list[str], timeout: float = 10.0) -> list[str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        lines = _read_dispatch_lines(tmp)
        if lines == expected:
            return lines
        time.sleep(0.05)
    lines = _read_dispatch_lines(tmp)
    assert lines == expected
    return lines


def _wait_for_fake_run_bin_exit(tmp: Path, expected_count: int, timeout: float = 10.0) -> None:
    pids_path = tmp / "fake-run-bin-pids.txt"
    deadline = time.monotonic() + timeout
    pids: list[int] = []
    while time.monotonic() < deadline:
        if pids_path.exists():
            pids = [
                int(line)
                for line in pids_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        if len(pids) >= expected_count and not _live_pids(pids):
            return
        time.sleep(0.05)

    assert len(pids) >= expected_count
    live = _live_pids(pids)
    for pid in live:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and _live_pids(pids):
        time.sleep(0.05)
    assert not _live_pids(pids)


def _live_pids(pids: list[int]) -> list[int]:
    live: list[int] = []
    for pid in pids:
        proc_stat = Path("/proc") / str(pid) / "stat"
        try:
            if proc_stat.exists() and proc_stat.read_text(encoding="utf-8").split()[2] == "Z":
                continue
        except (OSError, IndexError):
            pass
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            live.append(pid)
        else:
            live.append(pid)
    return live


def _run_poller(tmp: Path, env_extra: dict[str, str]) -> list[dict]:
    env = {k: v for k, v in os.environ.items() if k not in {
        "WORKLINK_REPO", "WORKLINK_RUN_BIN", "WORKLINK_MAX_CONCURRENT", "CHAINLINK_BIN",
    }}
    env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, str(POLLER)], cwd=str(tmp), env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    events = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    expected_fake_runs = sum(1 for event in events if event.get("signal") == "worklink_dispatched")
    _wait_for_fake_run_bin_exit(tmp, expected_fake_runs)
    return events


@pytest.mark.skipif(not POLLER.exists(), reason="poller not present")
def test_poller_reads_cap_from_worklink_yaml(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "worklink.yaml").write_text("defaults:\n  max_concurrent: 3\n", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    state = tmp_path / "state"
    chainlink = _fake_chainlink_script(tmp_path, ready=[10, 11, 12], active_locks=[10])
    run_bin = _fake_run_bin(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(POLLER)],
        env={
            **os.environ,
            "MIMIR_HOME": str(home),
            "WORKLINK_REPO": str(repo),
            "STATE_DIR": str(state),
            "CHAINLINK_BIN": str(chainlink),
            "WORKLINK_RUN_BIN": str(run_bin),
        },
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert proc.returncode == 0
    records = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    _wait_for_fake_run_bin_exit(
        tmp_path,
        sum(1 for record in records if record.get("signal") == "worklink_dispatched"),
    )
    assert [
        r.get("issue_id") for r in records if r.get("signal") == "worklink_dispatched"
    ] == [10, 11]


@pytest.mark.skipif(not POLLER.exists(), reason="poller not present")
def test_poller_reads_flow_style_cap_through_worklink_config(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "worklink.yaml").write_text('defaults: {max_concurrent: "3"}\n', encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    events = _run_poller(tmp_path, {
        "MIMIR_HOME": str(home),
        "CHAINLINK_BIN": str(_fake_chainlink_script(tmp_path, ready=[10, 11, 12], active_locks=[10])),
        "WORKLINK_RUN_BIN": sys.executable + " " + str(_fake_run_bin(tmp_path)),
        "WORKLINK_REPO": str(repo),
        "WORKLINK_MAX_CONCURRENT": "1",
        "STATE_DIR": str(tmp_path / "state"),
    })
    # WorklinkConfig parses flow-style YAML and takes precedence over the legacy
    # env cap: cap 3, 1 active lock → 2 free slots.
    assert [
        r.get("issue_id") for r in events if r.get("signal") == "worklink_dispatched"
    ] == [10, 11]


def test_poller_dispatches_up_to_free_slots(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    chainlink = _fake_chainlink_script(tmp_path, ready=[201, 202, 203], active_locks=[100])
    runbin = _fake_run_bin(tmp_path)
    events = _run_poller(tmp_path, {
        "MIMIR_HOME": str(home),
        "CHAINLINK_BIN": str(chainlink),
        "WORKLINK_RUN_BIN": sys.executable + " " + str(runbin),
        "WORKLINK_REPO": str(home),
        "WORKLINK_MAX_CONCURRENT": "2",
        "STATE_DIR": str(tmp_path / "state"),
    })
    # cap 2, 1 already active → exactly 1 free slot → 1 dispatch
    dispatched = [e for e in events if e.get("signal") == "worklink_dispatched"]
    assert len(dispatched) == 1
    assert dispatched[0]["issue_id"] == 201  # lowest id first
    scan = [e for e in events if e.get("signal") == "worklink_ready_scan"][-1]
    assert scan["ready_count"] == 3 and scan["active"] == 1 and scan["dispatched"] == 1


@pytest.mark.skipif(not POLLER.exists(), reason="poller not present")
def test_poller_does_not_dispatch_worklink_epic_issues(tmp_path: Path) -> None:
    """chainlink #830: the epic runner is removed — an actionable worklink:epic
    issue must NOT be dispatched by the poller (it is built by the opencode
    feature-factory, not as a leaf or via a run-epic controller)."""
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    chainlink = _fake_chainlink_script(
        tmp_path,
        ready=[100],
        epics=[100],
        actionable=[100],
        active_locks=[],
    )
    runbin = _fake_run_bin(tmp_path)

    events = _run_poller(tmp_path, {
        "MIMIR_HOME": str(home),
        "CHAINLINK_BIN": str(chainlink),
        "WORKLINK_RUN_BIN": sys.executable + " " + str(runbin),
        "WORKLINK_REPO": str(repo),
        "WORKLINK_MAX_CONCURRENT": "3",
        "STATE_DIR": str(tmp_path / "state"),
    })

    dispatched = [e for e in events if e.get("signal") == "worklink_dispatched"]
    assert dispatched == []


def test_poller_keeps_bare_ready_leaf_on_per_leaf_run(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    chainlink = _fake_chainlink_script(
        tmp_path,
        ready=[201],
        epics=[],
        actionable=[201],
        active_locks=[],
    )
    runbin = _fake_run_bin(tmp_path)

    events = _run_poller(tmp_path, {
        "MIMIR_HOME": str(home),
        "CHAINLINK_BIN": str(chainlink),
        "WORKLINK_RUN_BIN": sys.executable + " " + str(runbin),
        "WORKLINK_REPO": str(repo),
        "WORKLINK_MAX_CONCURRENT": "1",
        "STATE_DIR": str(tmp_path / "state"),
    })

    dispatched = [e for e in events if e.get("signal") == "worklink_dispatched"]
    assert [(e["mode"], e["issue_id"]) for e in dispatched] == [("leaf", 201)]
    expected_lines = [
        f"worklink run 201 --home {home} --repo {repo} --autonomous"
    ]
    assert _wait_for_dispatch_lines(tmp_path, expected_lines) == expected_lines


@pytest.mark.skipif(not POLLER.exists(), reason="poller not present")
def test_poller_skips_leaf_under_active_epic_lock(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    chainlink = _fake_chainlink_script(
        tmp_path,
        ready=[101, 201],
        epics=[100],
        parents={101: 100},
        actionable=[101, 201],
        active_locks=[100],
    )
    runbin = _fake_run_bin(tmp_path)

    events = _run_poller(tmp_path, {
        "MIMIR_HOME": str(home),
        "CHAINLINK_BIN": str(chainlink),
        "WORKLINK_RUN_BIN": sys.executable + " " + str(runbin),
        "WORKLINK_REPO": str(repo),
        "WORKLINK_MAX_CONCURRENT": "3",
        "STATE_DIR": str(tmp_path / "state"),
    })

    dispatched = [e for e in events if e.get("signal") == "worklink_dispatched"]
    assert [(e["mode"], e["issue_id"]) for e in dispatched] == [("leaf", 201)]


@pytest.mark.skipif(not POLLER.exists(), reason="poller not present")
def test_poller_filters_worklink_ready_through_chainlink_actionable_set(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    chainlink = _fake_chainlink_script(
        tmp_path,
        ready=[201, 202, 203],
        actionable=[101, 201, 203],
        active_locks=[],
    )
    runbin = _fake_run_bin(tmp_path)

    events = _run_poller(tmp_path, {
        "MIMIR_HOME": str(home),
        "CHAINLINK_BIN": str(chainlink),
        "WORKLINK_RUN_BIN": sys.executable + " " + str(runbin),
        "WORKLINK_REPO": str(repo),
        "WORKLINK_MAX_CONCURRENT": "3",
        "STATE_DIR": str(tmp_path / "state"),
    })

    dispatched = [e for e in events if e.get("signal") == "worklink_dispatched"]
    assert [e["issue_id"] for e in dispatched] == [201, 203]
    assert 202 not in [e["issue_id"] for e in dispatched]
    scan = [e for e in events if e.get("signal") == "worklink_ready_scan"][-1]
    assert scan["ready_count"] == 2
    assert scan["labeled_ready_count"] == 3
    assert scan["blocked_ready_count"] == 1


@pytest.mark.skipif(not POLLER.exists(), reason="poller not present")
def test_poller_leaves_blocked_worklink_ready_issues_untouched(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    chainlink = _fake_chainlink_script(
        tmp_path, ready=[301, 302], actionable=[999], active_locks=[]
    )

    events = _run_poller(tmp_path, {
        "MIMIR_HOME": str(home),
        "CHAINLINK_BIN": str(chainlink),
        "WORKLINK_REPO": str(repo),
        "WORKLINK_MAX_CONCURRENT": "2",
        "STATE_DIR": str(tmp_path / "state"),
    })

    assert not [e for e in events if e.get("signal") == "worklink_dispatched"]
    assert not (tmp_path / "dispatched.txt").exists()
    scan = [e for e in events if e.get("signal") == "worklink_ready_scan"][-1]
    assert scan["ready_count"] == 0
    assert scan["labeled_ready_count"] == 2
    assert scan["blocked_ready_count"] == 2


@pytest.mark.skipif(not POLLER.exists(), reason="poller not present")
def test_poller_dispatches_worklink_ready_after_blocker_closes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    # Simulates Chainlink's closed-blocker behavior: the issue remains labeled
    # worklink:ready and appears in `issue ready` once its blocker closes.
    chainlink = _fake_chainlink_script(
        tmp_path, ready=[401], actionable=[401], active_locks=[]
    )
    runbin = _fake_run_bin(tmp_path)

    events = _run_poller(tmp_path, {
        "MIMIR_HOME": str(home),
        "CHAINLINK_BIN": str(chainlink),
        "WORKLINK_RUN_BIN": sys.executable + " " + str(runbin),
        "WORKLINK_REPO": str(repo),
        "WORKLINK_MAX_CONCURRENT": "2",
        "STATE_DIR": str(tmp_path / "state"),
    })

    assert [
        e.get("issue_id") for e in events if e.get("signal") == "worklink_dispatched"
    ] == [401]
    scan = [e for e in events if e.get("signal") == "worklink_ready_scan"][-1]
    assert scan["ready_count"] == 1
    assert scan["labeled_ready_count"] == 1
    assert scan["blocked_ready_count"] == 0


@pytest.mark.skipif(not POLLER.exists(), reason="poller not present")
def test_poller_no_dispatch_when_cap_reached(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    chainlink = _fake_chainlink_script(tmp_path, ready=[201], active_locks=[100, 101])
    events = _run_poller(tmp_path, {
        "MIMIR_HOME": str(home),
        "CHAINLINK_BIN": str(chainlink),
        "WORKLINK_REPO": str(home),
        "WORKLINK_MAX_CONCURRENT": "2",
        "STATE_DIR": str(tmp_path / "state"),
    })
    assert not [e for e in events if e.get("signal") == "worklink_dispatched"]
    scan = [e for e in events if e.get("signal") == "worklink_ready_scan"][-1]
    assert scan["slots"] == 0




@pytest.mark.skipif(not POLLER.exists(), reason="poller not present")
def test_poller_accepts_chainlink_ready_text_when_json_flag_is_ignored(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    script = tmp_path / "chainlink"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "a = sys.argv[1:]\n"
        "if a[:2] == ['locks','list']:\n"
        "    print(json.dumps({'version': 1, 'locks': {}}))\n"
        "    sys.exit(0)\n"
        "if a[:2] == ['issue','ready']:\n"
        "    print('Ready issues (no blockers):')\n"
        "    print('  #201  high     unblocked worklink leaf')\n"
        "    print('  #203  medium   another unblocked leaf')\n"
        "    sys.exit(0)\n"
        "if a[:2] == ['issue','list']:\n"
        "    label = a[a.index('--label')+1] if '--label' in a else ''\n"
        "    ids = [201, 202, 203] if label == 'worklink:ready' else []\n"
        "    print(json.dumps([{'id': i} for i in ids]))\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    runbin = _fake_run_bin(tmp_path)

    events = _run_poller(tmp_path, {
        "MIMIR_HOME": str(home),
        "CHAINLINK_BIN": str(script),
        "WORKLINK_RUN_BIN": sys.executable + " " + str(runbin),
        "WORKLINK_REPO": str(repo),
        "WORKLINK_MAX_CONCURRENT": "3",
        "STATE_DIR": str(tmp_path / "state"),
    })

    dispatched = [e for e in events if e.get("signal") == "worklink_dispatched"]
    assert [e["issue_id"] for e in dispatched] == [201, 203]
    scan = [e for e in events if e.get("signal") == "worklink_ready_scan"][-1]
    assert scan["ready_count"] == 2
    assert scan["labeled_ready_count"] == 3
    assert scan["blocked_ready_count"] == 1


@pytest.mark.skipif(not POLLER.exists(), reason="poller not present")
def test_poller_fails_closed_when_chainlink_errors(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    # a chainlink shim that always errors → poller can't read the queue
    script = tmp_path / "chainlink"
    script.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n", encoding="utf-8")
    script.chmod(0o755)
    events = _run_poller(tmp_path, {
        "MIMIR_HOME": str(home),
        "CHAINLINK_BIN": str(script),
        "WORKLINK_REPO": str(home),
        "WORKLINK_MAX_CONCURRENT": "2",
        "STATE_DIR": str(tmp_path / "state"),
    })
    assert any(e.get("signal") == "worklink_poller_degraded" for e in events)
    assert not [e for e in events if e.get("signal") == "worklink_dispatched"]
