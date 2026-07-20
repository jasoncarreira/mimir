"""Tests for the feature-factory backend + autonomous DETACHED adapter (#833).

The external ``feature-factory`` CLI's **autonomous --detached mode** is
simulated by a stateful fake *compute* backend: ``launch`` writes an INITIAL
running ``run.json`` and BACKGROUNDS a writer that advances the state over
successive ticks (the detached opencode process), ``wait`` returns immediately
(the launcher's exit — NOT the run's), and the orchestrator's POLL LOOP then
drives progression by reading ``run.json`` to a terminal state (with
``terminal_result``). This exercises the thin launch-detached+poll+mirror adapter
(``WorklinkRunner.run_epic`` -> ``_run_detached_epic`` ->
``_poll_factory_to_terminal`` -> ``_finalize_epic``) end-to-end deterministically,
without a real factory. Resume/pre-terminal cases are driven by scripting
``read_factory_run_state`` directly (a prior dispatch's detached run). There is
NO resume/gate-answer step: the factory self-drives every gate and the adapter
only polls, mirrors meaningful transitions to Chainlink, and probes liveness.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

_CHAINLINK_ORCHESTRATOR = Path(__file__).resolve().parent.parent / "mimir" / "optional-skills" / "chainlink-orchestrator"
if str(_CHAINLINK_ORCHESTRATOR) not in sys.path:
    sys.path.insert(0, str(_CHAINLINK_ORCHESTRATOR))

from mimir.worklink.backends import ComputeCaps, ComputeResult
from mimir.worklink.backends import feature_factory as ff
from mimir.worklink.backends.feature_factory import (
    FactoryRunState,
    FactoryTerminalResult,
    FeatureFactoryBackend,
    _parse_run_state,
    epic_run_id,
    factory_run_dir,
    gate_answer_path,
    gate_question_path,
    has_concurrent_factory_session,
    question_mtime,
    read_factory_run_state,
    read_gate_answer,
    write_gate_answer,
)
from mimir.worklink.backends.registry import (
    BackendRegistry,
    WorklinkConfig,
    WorklinkDefaults,
)
from mimir.worklink.orchestrator import (
    WorklinkRunner,
    _cmdline_is_factory_child,
    _detached_factory_alive,
    _epic_stuck_reason,
    _factory_mirror_lines,
    _new_factory_mirror_memo,
    _run_dir_recent_activity_s,
)

ISSUE_ID = 834
# Standalone parse tests namespace run.json under this run-id.
RUN_ID = "chainlink-1"
PR_URL = "https://github.com/o/r/pull/42"


# ── run.json parsing (REAL shape) ───────────────────────────────────────────


def _state(**kw) -> FactoryRunState:
    base = dict(run_id="r", status="running", heartbeat_at=datetime.now(UTC).isoformat())
    base.update(kw)
    return FactoryRunState(**base)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _stale_iso() -> str:
    return (datetime.now(UTC) - timedelta(seconds=3600)).isoformat()


def test_is_terminal_statuses() -> None:
    for terminal in ("completed", "blocked", "partial", "needs-human"):
        assert _state(status=terminal).is_terminal, terminal
    # ``running`` is the only non-terminal status; ``failed``/``cancelled`` never
    # occur in the real contract, so they must NOT be treated as terminal.
    assert not _state(status="running").is_terminal
    assert not _state(status="failed").is_terminal
    assert not _state(status="cancelled").is_terminal


def test_is_stale() -> None:
    assert _state(heartbeat_at=(datetime.now(UTC) - timedelta(seconds=400)).isoformat()).is_stale
    assert not _state(heartbeat_at=_now()).is_stale
    # Missing/unparseable heartbeat is treated as stale, not silently fresh.
    assert _state(heartbeat_at="").is_stale


def test_pending_gate_from_gate_statuses() -> None:
    st = _state(
        gate_statuses=(("story", "approved"), ("brief", "pending"), ("pre_pr", "pending"))
    )
    assert st.pending_gate == "brief"  # first pending in canonical order
    assert (
        _state(
            gate_statuses=(("story", "approved"), ("brief", "approved"), ("pre_pr", "approved"))
        ).pending_gate
        is None
    )
    # A gate the factory adds beyond the known set is still detected.
    assert _state(gate_statuses=(("extra", "pending"),)).pending_gate == "extra"


def _write_run_json(repo: Path, payload: dict, run_id: str = RUN_ID) -> None:
    run_dir = factory_run_dir(repo, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps(payload), encoding="utf-8")


def test_read_factory_run_state_missing_dir(tmp_path: Path) -> None:
    assert read_factory_run_state(tmp_path, RUN_ID) is None


def test_read_factory_run_state_real_shape(tmp_path: Path) -> None:
    _write_run_json(
        tmp_path,
        {
            "run_id": "chainlink-834",
            "status": "running",
            "heartbeat_at": _now(),
            "pr_url": None,
            "gates": {
                "story": {"status": "approved"},
                "brief": {"status": "pending"},
                "pre_pr": {"status": "pending", "override": None},
            },
            "steps": [
                {"agent": "spec-writer", "status": "accepted"},
                {"agent": "work-decomposer", "status": "running"},
            ],
            "slices": [{"id": "s1", "status": "merged"}, {"id": "s2", "status": "building"}],
            "validator": {"verdict": "GO", "report": "artifacts/validation-report.md", "loops": 0},
            "security_review": {"verdict": "PASS", "review_ref": "reviews/sec.json", "loops": 0},
            "cost_attribution": {
                "schema_version": 1,
                "updated_at": "2026-07-09T12:00:00Z",
                "status": "partial",
                "totals": {
                    "status": "partial",
                    "entry_count": 2,
                    "request_count": 2,
                    "total_tokens": 12900,
                    "cost_total": 1.23,
                    "cost_currency": "USD",
                    "mixed_currency": False,
                    "missing": ["output_tokens"],
                },
                "entries": [],
            },
            "debug_snapshot": {
                "created_with": {"collected_at": "2026-07-06T12:00:00Z"},
                "last_resumed_with": {"collected_at": "2026-07-07T12:00:00Z"},
                "resume_count": 1,
            },
        },
    )
    st = read_factory_run_state(tmp_path, RUN_ID)
    assert st is not None
    assert st.run_id == "chainlink-834"
    assert st.status == "running"
    assert st.pending_gate == "brief"
    assert st.validator_verdict == "GO"
    assert st.security_verdict == "PASS"
    assert st.steps == (("spec-writer", "accepted"), ("work-decomposer", "running"))
    assert st.slices == (("s1", "merged"), ("s2", "building"))
    assert st.cost is not None
    assert st.cost.status == "partial"
    assert st.cost.entry_count == 2
    assert st.cost.request_count == 2
    assert st.cost.total_tokens == 12900
    assert st.cost.cost_total == 1.23
    assert st.cost.cost_currency == "USD"
    assert st.cost.missing == ("output_tokens",)
    assert st.debug is not None
    assert st.debug.created_at == "2026-07-06T12:00:00Z"
    assert st.debug.resumed_at == "2026-07-07T12:00:00Z"
    assert st.debug.resume_count == 1
    assert st.terminal_result is None
    assert not st.is_terminal


def test_read_factory_run_state_verdicts_and_pr_absent(tmp_path: Path) -> None:
    _write_run_json(
        tmp_path,
        {"run_id": "r", "status": "running", "heartbeat_at": _now(), "gates": {}},
    )
    st = read_factory_run_state(tmp_path, RUN_ID)
    assert st is not None
    assert st.validator_verdict is None
    assert st.security_verdict is None
    assert st.pr_url is None
    assert st.steps == ()
    assert st.slices == ()
    assert st.cost is None
    assert st.debug is None
    assert st.pending_gate is None


def test_read_factory_run_state_metadata_is_fail_soft(tmp_path: Path) -> None:
    _write_run_json(
        tmp_path,
        {
            "run_id": "r",
            "status": "running",
            "heartbeat_at": _now(),
            "gates": {},
            "steps": "not-a-list",
            "slices": [{"id": "ok", "status": "running"}, "bad"],
            "cost_attribution": {"status": 7, "totals": {"entry_count": True}},
            "debug_snapshot": {"resume_count": -1, "created_with": "bad"},
        },
    )
    st = read_factory_run_state(tmp_path, RUN_ID)
    assert st is not None
    assert st.steps == ()
    assert st.slices == (("ok", "running"),)
    assert st.cost is not None
    assert st.cost.status == "unavailable"
    assert st.cost.entry_count is None
    assert st.debug is not None
    assert st.debug.created_at is None
    assert st.debug.resume_count is None


def test_read_factory_run_state_completed_with_pr(tmp_path: Path) -> None:
    _write_run_json(
        tmp_path,
        {
            "run_id": "r",
            "status": "completed",
            "heartbeat_at": _now(),
            "pr_url": "https://github.com/o/r/pull/7",
            "gates": {"pre_pr": {"status": "approved"}},
        },
    )
    st = read_factory_run_state(tmp_path, RUN_ID)
    assert st is not None
    assert st.is_terminal
    assert st.pr_url == "https://github.com/o/r/pull/7"


def test_read_factory_run_state_blocked_reason(tmp_path: Path) -> None:
    _write_run_json(
        tmp_path,
        {
            "run_id": "r",
            "status": "blocked",
            "heartbeat_at": _now(),
            "gates": {},
            "blocked_reason": "push denied: 403",
        },
    )
    st = read_factory_run_state(tmp_path, RUN_ID)
    assert st is not None and st.is_terminal
    assert st.error == "push denied: 403"


def test_read_factory_run_state_terminal_result(tmp_path: Path) -> None:
    _write_run_json(
        tmp_path,
        {
            "run_id": "chainlink-834",
            "status": "completed",
            "heartbeat_at": _now(),
            "pr_url": "https://github.com/o/r/pull/7",
            "gates": {"pre_pr": {"status": "approved"}},
            "terminal_result": {
                "status": "completed",
                "run_id": "chainlink-834",
                "pr_url": "https://github.com/o/r/pull/7",
                "reason": None,
                "summary": "shipped it",
            },
        },
    )
    st = read_factory_run_state(tmp_path, RUN_ID)
    assert st is not None
    assert isinstance(st.terminal_result, FactoryTerminalResult)
    assert st.terminal_result.status == "completed"
    assert st.terminal_result.pr_url == "https://github.com/o/r/pull/7"
    assert st.terminal_result.reason is None
    assert st.terminal_result.summary == "shipped it"


def test_read_factory_run_state_terminal_result_absent(tmp_path: Path) -> None:
    _write_run_json(
        tmp_path,
        {"run_id": "r", "status": "running", "heartbeat_at": _now(), "gates": {}},
    )
    st = read_factory_run_state(tmp_path, RUN_ID)
    assert st is not None
    assert st.terminal_result is None


# ── gate file protocol + concurrency guard (helpers retained; adapter no longer
#    calls them) ───────────────────────────────────────────────────────────


def test_gate_answer_and_question_paths(tmp_path: Path) -> None:
    assert gate_answer_path(tmp_path, RUN_ID, "pre_pr") == (
        tmp_path / ".opencode" / "factory" / RUN_ID / "gates" / "pre_pr.answer"
    )
    assert gate_question_path(tmp_path, RUN_ID, "pre_pr") == (
        tmp_path / ".opencode" / "factory" / RUN_ID / "gates" / "pre_pr.question.md"
    )


def test_write_and_read_gate_answer(tmp_path: Path) -> None:
    assert read_gate_answer(tmp_path, RUN_ID, "story") is None
    write_gate_answer(tmp_path, RUN_ID, "story", "approve")
    assert read_gate_answer(tmp_path, RUN_ID, "story") == "approve"


def test_question_mtime_distinguishes_reopened_gates(tmp_path: Path) -> None:
    q = gate_question_path(tmp_path, RUN_ID, "pre_pr")
    q.parent.mkdir(parents=True, exist_ok=True)
    assert question_mtime(tmp_path, RUN_ID, "pre_pr") == 0  # absent
    q.write_text("q1", encoding="utf-8")
    os.utime(q, ns=(1_000_000_000, 1_000_000_000))
    first = question_mtime(tmp_path, RUN_ID, "pre_pr")
    os.utime(q, ns=(2_000_000_000, 2_000_000_000))
    assert question_mtime(tmp_path, RUN_ID, "pre_pr") != first


def test_has_concurrent_factory_session(tmp_path: Path) -> None:
    assert not has_concurrent_factory_session(tmp_path)  # no run.json
    _write_run_json(tmp_path, {"run_id": "r", "status": "running", "heartbeat_at": _now(), "gates": {}})
    assert has_concurrent_factory_session(tmp_path)
    _write_run_json(tmp_path, {"run_id": "r", "status": "completed", "heartbeat_at": _now(), "gates": {}})
    assert not has_concurrent_factory_session(tmp_path)  # terminal
    _write_run_json(
        tmp_path,
        {
            "run_id": "r",
            "status": "running",
            "heartbeat_at": (datetime.now(UTC) - timedelta(seconds=400)).isoformat(),
            "gates": {},
        },
    )
    assert not has_concurrent_factory_session(tmp_path)  # stale


def test_has_concurrent_factory_session_scans_attempt_checkouts(tmp_path: Path) -> None:
    # Detached runs live under repo/.worklink/<attempt>/.opencode/factory/, not the
    # repo root, so the guard must scan the attempt checkouts too (else it never
    # sees the sessions the detached adapter actually creates).
    attempt = tmp_path / ".worklink" / "841-1"
    rd = factory_run_dir(attempt, epic_run_id(841))
    rd.mkdir(parents=True)
    rd.joinpath("run.json").write_text(
        json.dumps({"run_id": epic_run_id(841), "status": "running", "heartbeat_at": _now(), "gates": {}}),
        encoding="utf-8",
    )
    assert has_concurrent_factory_session(tmp_path)  # sees the .worklink attempt run
    # A resume/re-dispatch of that same epic must NOT count its own run as concurrent.
    assert not has_concurrent_factory_session(tmp_path, exclude_run_id=epic_run_id(841))
    # A terminal attempt run is not a concurrent session.
    rd.joinpath("run.json").write_text(
        json.dumps({"run_id": epic_run_id(841), "status": "completed", "heartbeat_at": _now(), "gates": {}}),
        encoding="utf-8",
    )
    assert not has_concurrent_factory_session(tmp_path)


# ── _factory_command (autonomous) / work_spec ───────────────────────────────


def test_factory_command_autonomous_multitoken_bin_with_reviewer(tmp_path: Path) -> None:
    backend = FeatureFactoryBackend(
        bin="node /opt/ff/cli.js", ready_for_review=True, reviewer="mimir-carreira"
    )
    wt = tmp_path / "wt"
    cmd = backend._factory_command(wt, "Build chainlink #834: chat skills", 834)
    assert cmd == (
        "node",
        "/opt/ff/cli.js",
        "factory",
        "start",
        "--autonomous",
        "--detached",
        "--repo",
        str(wt),
        "--run-id",
        "chainlink-834",
        "--ready",
        "--reviewer",
        "mimir-carreira",
        "Build chainlink #834: chat skills",
    )
    # Autonomous is a SINGLE detached launch — there is no resume command anymore.
    assert not hasattr(backend, "resume_command")


def test_factory_command_reviewer_absent_omits_flag(tmp_path: Path) -> None:
    backend = FeatureFactoryBackend(bin="feature-factory", ready_for_review=True, reviewer="")
    cmd = backend._factory_command(tmp_path, "prompt", 123)
    assert cmd == (
        "feature-factory",
        "factory",
        "start",
        "--autonomous",
        "--detached",
        "--repo",
        str(tmp_path),
        "--run-id",
        "chainlink-123",
        "--ready",
        "prompt",
    )
    assert "--reviewer" not in cmd


def test_factory_command_extra_args_and_ready_off(tmp_path: Path) -> None:
    backend = FeatureFactoryBackend(extra_args=("--flag",), ready_for_review=False, reviewer="alice")
    cmd = backend._factory_command(tmp_path, "prompt", 456)
    assert cmd == (
        "feature-factory",
        "factory",
        "start",
        "--autonomous",
        "--detached",
        "--repo",
        str(tmp_path),
        "--run-id",
        "chainlink-456",
        "--flag",
        "--reviewer",
        "alice",
        "prompt",
    )
    assert "--ready" not in cmd


def test_factory_command_reviewer_default_from_env(monkeypatch) -> None:
    monkeypatch.setenv("MIMIR_FACTORY_REVIEWER", "env-reviewer")
    backend = FeatureFactoryBackend(bin="feature-factory")
    cmd = backend._factory_command(Path("/wt"), "p", 999)
    assert "--detached" in cmd
    assert "--run-id" in cmd
    assert cmd[cmd.index("--run-id") + 1] == "chainlink-999"
    assert "--reviewer" in cmd
    assert cmd[cmd.index("--reviewer") + 1] == "env-reviewer"


def test_work_spec_carries_autonomous_command_and_worktree(tmp_path: Path) -> None:
    from mimir.worklink.backends.base import WorkOrder

    backend = FeatureFactoryBackend(bin="feature-factory", reviewer="mimir-carreira")
    order = WorkOrder(
        issue_id=ISSUE_ID,
        worktree=tmp_path / "wt",
        prompt="Build chainlink #834: x",
        rules=None,
        timeout_s=1800,
    )
    spec = backend.work_spec(
        order, attempt=1, repo_url="git@github.com:o/r.git", base_ref="main",
        branch="issue/834-a1", test_command="pytest",
    )
    assert spec.local_worktree == order.worktree
    assert tuple(spec.local_argv) == backend._factory_command(order.worktree, order.prompt, order.issue_id)
    assert "--autonomous" in spec.local_argv and "--detached" in spec.local_argv
    assert "--run-id" in spec.local_argv
    assert "--ready" in spec.local_argv
    assert "--headless" not in spec.local_argv


# ── mirror-on-change (pure) ──────────────────────────────────────────────────


def test_factory_mirror_lines_only_on_change() -> None:
    memo = _new_factory_mirror_memo()
    s1 = _state(
        gate_statuses=(("story", "approved"), ("brief", "pending")),
        slices=(("s1", "building"),),
    )
    lines1 = _factory_mirror_lines(s1, memo)
    assert "gate approved: story" in lines1
    assert any("slices: 0/1 merged" in line for line in lines1)
    # Re-polling the SAME state emits nothing (dedup against the memo).
    assert _factory_mirror_lines(s1, memo) == []

    s2 = _state(
        gate_statuses=(
            ("story", "approved"),
            ("brief", "approved"),
            ("pre_pr", "approved"),
        ),
        slices=(("s1", "merged"),),
        validator_verdict="GO",
        security_verdict="PASS",
        pr_url="https://github.com/o/r/pull/9",
    )
    lines2 = _factory_mirror_lines(s2, memo)
    assert "gate approved: brief" in lines2
    assert "gate approved: pre_pr" in lines2
    assert "validator verdict: GO" in lines2
    assert "security verdict: PASS" in lines2
    assert any("slices: 1/1 merged" in line for line in lines2)
    assert "draft PR opened: https://github.com/o/r/pull/9" in lines2


# ── probe-based liveness (pure) ──────────────────────────────────────────────


def test_stuck_reason_fresh_heartbeat_keeps_waiting() -> None:
    assert (
        _epic_stuck_reason(
            state=_state(heartbeat_at=_now()),
            recent_activity_s=None,
            job_alive=None,
            elapsed_s=10_000,
            stale_threshold_s=900,
            probe_window_s=300,
        )
        is None
    )


def test_stuck_reason_stale_but_alive_and_advancing_keeps_waiting() -> None:
    assert (
        _epic_stuck_reason(
            state=_state(heartbeat_at=_stale_iso()),
            recent_activity_s=5.0,  # a run-dir file advanced recently
            job_alive=True,
            elapsed_s=10_000,
            stale_threshold_s=900,
            probe_window_s=300,
        )
        is None
    )


def test_stuck_reason_stale_and_dead_is_stuck() -> None:
    reason = _epic_stuck_reason(
        state=_state(heartbeat_at=_stale_iso()),
        recent_activity_s=5.0,  # advancing, but the process is dead
        job_alive=False,
        elapsed_s=10_000,
        stale_threshold_s=900,
        probe_window_s=300,
    )
    assert reason and "not alive" in reason


def test_stuck_reason_stale_unknown_liveness_and_nothing_advancing_is_stuck() -> None:
    # The file-activity fallback only applies when liveness is UNKNOWN (None) —
    # e.g. a substrate with no process probe.
    st = _state(heartbeat_at=_stale_iso())
    reason = _epic_stuck_reason(
        state=st,
        recent_activity_s=10_000.0,  # newest file is older than the probe window
        job_alive=None,
        elapsed_s=10_000,
        stale_threshold_s=900,
        probe_window_s=300,
    )
    assert reason and "no run-dir file advanced" in reason
    # No run-dir activity at all is also "nothing advanced".
    assert _epic_stuck_reason(
        state=st,
        recent_activity_s=None,
        job_alive=None,
        elapsed_s=10_000,
        stale_threshold_s=900,
        probe_window_s=300,
    )


def test_stuck_reason_stale_alive_but_quiet_keeps_waiting() -> None:
    # Regression for the #840 false-positive: the pre_pr review panel ran with a
    # stale heartbeat AND no run-dir/process file advancing for longer than the
    # probe window — but the detached factory child was alive the whole time.
    # A KNOWN-alive process must keep waiting (only the run timeout bounds it),
    # never be declared stuck on file quiet.
    st = _state(heartbeat_at=_stale_iso())
    assert (
        _epic_stuck_reason(
            state=st,
            recent_activity_s=10_000.0,  # quiet longer than the probe window
            job_alive=True,
            elapsed_s=10_000,
            stale_threshold_s=900,
            probe_window_s=300,
        )
        is None
    )
    assert (
        _epic_stuck_reason(
            state=st,
            recent_activity_s=None,  # no file activity at all
            job_alive=True,
            elapsed_s=10_000,
            stale_threshold_s=900,
            probe_window_s=300,
        )
        is None
    )


def test_stuck_reason_no_runjson_startup_grace_then_stuck() -> None:
    # Within the startup grace window: keep waiting even with no run.json.
    assert (
        _epic_stuck_reason(
            state=None,
            recent_activity_s=None,
            job_alive=None,
            elapsed_s=10,
            stale_threshold_s=900,
            probe_window_s=300,
        )
        is None
    )
    # Past the grace window with no run.json and no activity: stuck (unknown
    # liveness → file-activity fallback).
    reason = _epic_stuck_reason(
        state=None,
        recent_activity_s=None,
        job_alive=None,
        elapsed_s=1000,
        stale_threshold_s=900,
        probe_window_s=300,
    )
    assert reason and "no run.json" in reason
    # But a KNOWN-alive process past the grace window keeps waiting — it may just
    # be slow to write its first run.json.
    assert (
        _epic_stuck_reason(
            state=None,
            recent_activity_s=None,
            job_alive=True,
            elapsed_s=1000,
            stale_threshold_s=900,
            probe_window_s=300,
        )
        is None
    )
    # A known-dead process before writing run.json is stuck immediately.
    assert _epic_stuck_reason(
        state=None,
        recent_activity_s=None,
        job_alive=False,
        elapsed_s=1000,
        stale_threshold_s=900,
        probe_window_s=300,
    )


# ── detached-child liveness probe ────────────────────────────────────────────


def test_cmdline_is_factory_child_matches_opencode_in_worktree() -> None:
    wt = "/workspace/mimir/.worklink/840-1"
    assert _cmdline_is_factory_child(
        f"opencode run --dir {wt} --command feature --agent feature-factory", wt
    )
    # Wrong worktree → not this run's child.
    assert not _cmdline_is_factory_child(
        "opencode run --dir /workspace/mimir/.worklink/999-1 --command feature", wt
    )
    # The adapter's own poller (references the base repo, not the worktree, and
    # is not the opencode runtime) must not match.
    assert not _cmdline_is_factory_child(
        "/workspace/mimir/.venv/bin/mimir worklink run-epic 840 --repo /workspace/mimir",
        wt,
    )
    # A non-opencode process that merely mentions the worktree must not match.
    assert not _cmdline_is_factory_child(f"git -C {wt} status", wt)


def test_detached_factory_alive_from_cmdlines() -> None:
    wt = Path("/workspace/mimir/.worklink/840-1")
    # Alive: an opencode child for this worktree is present.
    assert (
        _detached_factory_alive(
            wt,
            cmdlines=[
                "systemd",
                f"opencode run --dir {wt} --command feature --agent feature-factory",
            ],
        )
        is True
    )
    # No match → UNKNOWN (None), never False: a scan miss must not be read as
    # dead (that is the false-positive this guards against).
    assert (
        _detached_factory_alive(
            wt,
            cmdlines=["systemd", "opencode run --dir /other/tree --command feature"],
        )
        is None
    )
    assert _detached_factory_alive(wt, cmdlines=[]) is None


# ── autonomous adapter drive (fake factory compute) ──────────────────────────


def _issue_json() -> str:
    return json.dumps(
        {
            "id": ISSUE_ID,
            "title": "Epic: chat skills",
            "description": "Build the chat skills feature.",
            "labels": ["worklink:epic", "worklink:ready"],
            "parent_id": None,
            "comments": [],
        }
    )


def _cp(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _write_factory_run(wt: Path, payload: dict) -> None:
    rd = factory_run_dir(wt, epic_run_id(ISSUE_ID))
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "run.json").write_text(json.dumps(payload), encoding="utf-8")


def _process_log_dir(wt: Path) -> Path:
    """The detached factory's process-log dir — a sibling of the per-run dir under
    the factory root: ``<wt>/.opencode/factory/processes/`` (run dir is
    ``.../factory/<run-id>``)."""
    return factory_run_dir(wt, epic_run_id(ISSUE_ID)).parent / "processes"


def _touch_process_log(wt: Path, name: str = "20260705-000000.log") -> Path:
    d = _process_log_dir(wt)
    d.mkdir(parents=True, exist_ok=True)
    log = d / name
    log.write_text("factory process advancing...\n", encoding="utf-8")
    return log


def _payload(
    status: str,
    *,
    gates: dict | None = None,
    pr_url: str | None = None,
    validator: str | None = None,
    security: str | None = None,
    slices: list | None = None,
    terminal: dict | None = None,
    heartbeat: str | None = None,
    blocked_reason: str | None = None,
) -> dict:
    payload: dict = {
        "run_id": "chainlink-834",
        "status": status,
        "heartbeat_at": heartbeat if heartbeat is not None else _now(),
        "gates": gates or {},
    }
    if pr_url:
        payload["pr_url"] = pr_url
    if validator:
        payload["validator"] = {"verdict": validator}
    if security:
        payload["security_review"] = {"verdict": security}
    if slices is not None:
        payload["slices"] = slices
    if terminal is not None:
        payload["terminal_result"] = terminal
    if blocked_reason:
        payload["blocked_reason"] = blocked_reason
    return payload


def _happy_states() -> list[dict]:
    g_story = {"story": {"status": "approved"}}
    g_sb = {"story": {"status": "approved"}, "brief": {"status": "approved"}}
    g_all = {**g_sb, "pre_pr": {"status": "approved"}}
    return [
        _payload("running", gates={"story": {"status": "pending"}}, slices=[{"id": "s1", "status": "building"}]),
        _payload("running", gates=g_story, slices=[{"id": "s1", "status": "building"}]),
        _payload(
            "running",
            gates=g_sb,
            validator="GO",
            slices=[{"id": "s1", "status": "merged"}, {"id": "s2", "status": "building"}],
        ),
        _payload(
            "running",
            gates=g_all,
            validator="GO",
            security="PASS",
            pr_url=PR_URL,
            slices=[{"id": "s1", "status": "merged"}, {"id": "s2", "status": "merged"}],
        ),
        _payload(
            "completed",
            gates=g_all,
            validator="GO",
            security="PASS",
            pr_url=PR_URL,
            slices=[{"id": "s1", "status": "merged"}, {"id": "s2", "status": "merged"}],
            terminal={"status": "completed", "run_id": "chainlink-834", "pr_url": PR_URL, "summary": "shipped"},
        ),
    ]


class FakeFactoryCompute:
    """Autonomous DETACHED feature-factory sim.

    ``--detached`` backgrounds opencode and returns the launcher immediately, so
    this fake models the run in two phases:
    - ``launch`` records the argv, writes the INITIAL running ``run.json``, and
      starts a BACKGROUND writer task (the detached opencode process) that
      advances through ``states[1:]`` over successive ticks — optionally touching a
      process-log file so the run-dir/process-log activity signal advances.
    - ``wait`` returns immediately: the launcher's clean exit, NOT the run's.
    The orchestrator's POLL LOOP then reads the advancing ``run.json`` to terminal.

    ``passive=True`` disables all file writes (``launch`` only records argv): those
    tests script ``read_factory_run_state`` directly (resume / pre-terminal /
    stuck cases, where the compute may not be launched at all). ``launch_result``
    overrides the launcher's exit (to exercise the launch-failure branch).
    """

    name = "fake_compute"

    def __init__(
        self,
        *,
        states: list[dict] | None = None,
        shared_filesystem: bool = True,
        alive: bool = True,
        yields_between: int = 2,
        write_process_log: bool = False,
        passive: bool = False,
        launch_result: ComputeResult | None = None,
    ) -> None:
        self.shared_filesystem = shared_filesystem
        self.states = list(states if states is not None else _happy_states())
        self.alive = alive
        self.yields_between = yields_between
        self.write_process_log = write_process_log
        self.passive = passive
        self.launch_result = launch_result
        self.specs: list = []
        self.cleaned: list = []
        self.cancelled: list = []
        self.launch_argvs: list[list[str]] = []
        self._alive = True
        self._advance_task: "asyncio.Task | None" = None

    def capabilities(self) -> ComputeCaps:
        return ComputeCaps(self.shared_filesystem, False, True, False)

    async def launch(self, spec):
        self.specs.append(spec)
        self.launch_argvs.append([str(a) for a in (spec.local_argv or [])])
        if not self.passive:
            wt = spec.local_worktree
            _write_factory_run(wt, self.states[0])
            if self.write_process_log:
                _touch_process_log(wt)
            self._advance_task = asyncio.create_task(self._advance(wt))
        return spec

    async def _advance(self, wt) -> None:
        # The backgrounded (detached) opencode advancing run.json over time.
        for payload in self.states[1:]:
            for _ in range(self.yields_between):
                await asyncio.sleep(0)
            _write_factory_run(wt, payload)
            if self.write_process_log:
                _touch_process_log(wt)
        self._alive = False

    async def wait(self, handle, timeout_s: int) -> ComputeResult:
        # --detached: the launcher backgrounds opencode and returns immediately.
        if self.launch_result is not None:
            return self.launch_result
        return ComputeResult(exit_code=0, stdout="", stderr="")

    async def logs(self, handle) -> str:
        return ""

    async def cancel(self, handle) -> None:
        self.cancelled.append(handle)
        self._alive = False

    async def cleanup(self, handle) -> None:
        self.cleaned.append(handle)
        # The detached run keeps going during polling; here (post-terminal) just
        # drain the background writer so no task is left dangling.
        if self._advance_task is not None:
            try:
                await self._advance_task
            except asyncio.CancelledError:
                pass

    def job_alive(self, handle) -> bool:
        return self._alive and self.alive


def _install_scripted_reader(monkeypatch, payloads: list[dict | None]) -> dict:
    """Drive the detached poll loop by scripting ``read_factory_run_state``.

    Models a prior dispatch's detached factory whose ``run.json`` this dispatch
    (which may skip the launch entirely) polls: each read returns the next scripted
    payload parsed (``None`` → no run.json), STICKY on the last. Does NOT touch disk
    — so ``has_concurrent_factory_session`` (which globs the repo root) stays clear
    and ``_run_dir_recent_activity_s`` reports no activity (drives the stuck path
    when heartbeats are stale). Returns a mutable ``{"i": n}`` read counter.
    """
    seq = list(payloads)
    idx = {"i": 0}

    def fake_read(repo_path, run_id):
        i = min(idx["i"], len(seq) - 1)
        idx["i"] += 1
        payload = seq[i]
        return None if payload is None else _parse_run_state(payload)

    monkeypatch.setattr(ff, "read_factory_run_state", fake_read)
    return idx


def _drive(
    tmp_path: Path,
    compute,
    *,
    poll_interval_s: float = 0,
    monkeypatch=None,
    scripted: list[dict | None] | None = None,
) -> tuple:
    """Run ``run_epic`` end-to-end with the real backend + a fake factory compute.

    ``scripted`` (with ``monkeypatch``) drives the poll loop via
    ``read_factory_run_state`` instead of the fake's on-disk writes (resume /
    pre-terminal / stuck). Returns (result, gh_calls, worktree, chainlink_comments).
    """
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    worktree = repo / ".worklink" / f"{ISSUE_ID}-1"
    gh_calls: list[list[str]] = []
    comments: list[list[str]] = []

    def runner(args, **_):
        if isinstance(args, str):
            return _cp()
        args = list(args)
        if args[:3] == ["chainlink", "issue", "show"]:
            return _cp(stdout=_issue_json())
        if args[:3] == ["chainlink", "issue", "comment"]:
            comments.append(args)
            return _cp()
        if args[:2] == ["gh", "pr"]:
            gh_calls.append(args)
            return _cp()
        if args[:5] == ["git", "-C", str(repo), "worktree", "add"]:
            worktree.mkdir(parents=True, exist_ok=True)
            return _cp()
        if args[:4] == ["git", "-C", str(repo), "config"]:
            return _cp(stdout="git@github.com:o/r.git\n")
        return _cp()

    if scripted is not None:
        assert monkeypatch is not None, "scripted drive requires monkeypatch"
        _install_scripted_reader(monkeypatch, scripted)

    registry = BackendRegistry(
        WorklinkConfig(defaults=WorklinkDefaults(compute_backend="fake_compute"))
    )
    # poll_interval_s=0 lets the poll loop interleave with the fake's writer.
    registry.register(FeatureFactoryBackend(bin="feature-factory", poll_interval_s=poll_interval_s))
    registry.register_compute(compute)

    result = asyncio.run(
        WorklinkRunner(home=home, repo=repo, runner=runner, registry=registry).run_epic(ISSUE_ID)
    )
    return result, gh_calls, worktree, comments


def _comment_texts(comments: list[list[str]]) -> list[str]:
    return [c[4] for c in comments if len(c) >= 5 and c[:3] == ["chainlink", "issue", "comment"]]


def test_autonomous_happy_path_ships_pr(tmp_path: Path) -> None:
    # launch (detached) → poll advances running→completed+pr → review_ready.
    compute = FakeFactoryCompute()
    result, gh_calls, worktree, comments = _drive(tmp_path, compute)

    assert result.status == "review_ready", (result.status, result.reason)
    assert result.review_ready is True
    assert result.pr_url == PR_URL
    # ONE detached launch — no resume, no gate-answer files.
    assert len(compute.launch_argvs) == 1
    argv = compute.launch_argvs[0]
    assert "--autonomous" in argv and "--detached" in argv
    assert "start" in argv and "factory" in argv
    assert "--headless" not in argv
    assert argv[-1].startswith(f"Build chainlink #{ISSUE_ID}")
    # The factory promoted/requested review via --ready/--reviewer; the adapter
    # must NOT duplicate those gh calls.
    assert not any(c[:2] == ["gh", "pr"] for c in gh_calls)


def test_autonomous_progress_mirrored_to_chainlink(tmp_path: Path) -> None:
    compute = FakeFactoryCompute()
    result, _gh, _wt, comments = _drive(tmp_path, compute)

    assert result.status == "review_ready", (result.status, result.reason)
    joined = "\n".join(_comment_texts(comments))
    assert "gate approved: story" in joined
    assert "gate approved: brief" in joined
    assert "gate approved: pre_pr" in joined
    assert f"draft PR opened: {PR_URL}" in joined
    assert f"WORKLINK_EVIDENCE issue={ISSUE_ID} attempt=1 status=completed review_ready=true pr_url={PR_URL}" in joined
    assert "validator verdict: GO" in joined
    assert "security verdict: PASS" in joined
    assert "merged" in joined  # slice progress mirrored


def test_autonomous_blocked_terminal_mirrors_reason(tmp_path: Path) -> None:
    states = [
        _payload("running", gates={"story": {"status": "pending"}}),
        _payload(
            "blocked",
            gates={"story": {"status": "approved"}},
            terminal={
                "status": "needs-human",
                "run_id": "chainlink-834",
                "reason": "ambiguous brief; human input required",
                "summary": "needs human",
            },
        ),
    ]
    compute = FakeFactoryCompute(states=states)
    result, gh_calls, worktree, comments = _drive(tmp_path, compute)

    assert result.status == "blocked"
    assert "human input required" in (result.reason or "")
    assert result.pr_url is None
    assert not any(c[:2] == ["gh", "pr"] for c in gh_calls)
    # The blocked reason is mirrored to Chainlink (transition_issue → WORKLINK_BLOCKED).
    assert any("human input required" in t for t in _comment_texts(comments))


def test_autonomous_pr_under_non_success_status_not_shipped(tmp_path: Path) -> None:
    states = [
        _payload("running", gates={"story": {"status": "pending"}}),
        _payload(
            "blocked",
            gates={"story": {"status": "approved"}},
            pr_url="https://github.com/o/r/pull/99",
            terminal={
                "status": "blocked",
                "run_id": "chainlink-834",
                "pr_url": "https://github.com/o/r/pull/99",
                "reason": "push finalize failed",
            },
        ),
    ]
    compute = FakeFactoryCompute(states=states)
    result, gh_calls, worktree, comments = _drive(tmp_path, compute)

    assert result.status == "blocked"  # NOT review_ready, despite the pr_url
    assert result.pr_url == "https://github.com/o/r/pull/99"  # kept for visibility
    assert not any(c[:2] == ["gh", "pr"] for c in gh_calls)


def test_autonomous_ships_on_status_fallback_when_terminal_result_absent(tmp_path: Path) -> None:
    # terminal_result is agent-written and may be missing even at a terminal
    # state; the adapter falls back to status/pr_url/gates.
    states = [
        _payload("running", gates={"story": {"status": "pending"}}),
        _payload(
            "completed",
            gates={
                "story": {"status": "approved"},
                "brief": {"status": "approved"},
                "pre_pr": {"status": "approved"},
            },
            pr_url=PR_URL,
        ),  # NO terminal_result
    ]
    compute = FakeFactoryCompute(states=states)
    result, gh_calls, worktree, comments = _drive(tmp_path, compute)

    assert result.status == "review_ready", (result.status, result.reason)
    assert result.pr_url == PR_URL


def test_autonomous_resume_skips_launch_and_polls_to_terminal(
    tmp_path: Path, monkeypatch
) -> None:
    # A prior interrupted dispatch left a detached factory RUNNING for this epic
    # (non-terminal, non-stale run.json). The re-dispatch must NOT relaunch — it
    # resumes polling the existing run to terminal. Scripted run.json: the FIRST
    # read (pre-launch resume check) is a live running run → skip launch.
    scripted = [
        _payload("running", gates={"story": {"status": "approved"}}),
        _payload(
            "running",
            gates={"story": {"status": "approved"}, "brief": {"status": "approved"}},
            validator="GO",
        ),
        _payload(
            "completed",
            gates={
                "story": {"status": "approved"},
                "brief": {"status": "approved"},
                "pre_pr": {"status": "approved"},
            },
            pr_url=PR_URL,
            terminal={"status": "completed", "run_id": "chainlink-834", "pr_url": PR_URL},
        ),
    ]
    compute = FakeFactoryCompute(passive=True)
    result, gh_calls, _wt, comments = _drive(
        tmp_path, compute, monkeypatch=monkeypatch, scripted=scripted
    )

    assert result.status == "review_ready", (result.status, result.reason)
    assert result.pr_url == PR_URL
    # The detached factory was already running — NO relaunch.
    assert compute.launch_argvs == []
    assert compute.specs == []
    # Progress from the resumed run is still mirrored.
    assert any("gate approved: brief" in t for t in _comment_texts(comments))


def test_autonomous_preexisting_terminal_runjson_finalizes_without_launch(
    tmp_path: Path, monkeypatch
) -> None:
    # A prior dispatch's detached run already reached a TERMINAL state before this
    # dispatch even polled: finalize straight away, no launch.
    scripted = [
        _payload(
            "completed",
            gates={
                "story": {"status": "approved"},
                "brief": {"status": "approved"},
                "pre_pr": {"status": "approved"},
            },
            pr_url=PR_URL,
            terminal={"status": "completed", "run_id": "chainlink-834", "pr_url": PR_URL},
        ),
    ]
    compute = FakeFactoryCompute(passive=True)
    result, gh_calls, _wt, _comments = _drive(
        tmp_path, compute, monkeypatch=monkeypatch, scripted=scripted
    )

    assert result.status == "review_ready", (result.status, result.reason)
    assert result.pr_url == PR_URL
    assert compute.launch_argvs == []  # finalized the prior detached run, no relaunch
    assert not any(c[:2] == ["gh", "pr"] for c in gh_calls)


def test_autonomous_missing_run_json_fails(tmp_path: Path, monkeypatch) -> None:
    # The detached launcher exits cleanly but the factory never writes run.json.
    # A 0s startup grace makes the probe fire on the next tick → failed "no run.json".
    monkeypatch.setenv("MIMIR_FACTORY_STALE_HEARTBEAT_S", "0")
    compute = FakeFactoryCompute(passive=True)  # launch writes nothing
    result, gh_calls, _wt, _comments = _drive(tmp_path, compute)

    assert result.status == "failed", (result.status, result.reason)
    assert "no run.json" in (result.reason or "")
    assert compute.launch_argvs, "the factory was launched (detached)"


def test_autonomous_run_timeout_blocks(tmp_path: Path, monkeypatch) -> None:
    # With --detached there is no held compute.wait timeout, so the poll loop's
    # MIMIR_FACTORY_RUN_TIMEOUT_S is the run's hard ceiling. A run whose run.json
    # never reaches terminal within it is failed "run timeout". A 0s ceiling fires
    # deterministically on the first poll tick after launch.
    monkeypatch.setenv("MIMIR_FACTORY_RUN_TIMEOUT_S", "0")
    states = [
        _payload("running", gates={"story": {"status": "approved"}}),
        _payload("running", gates={"story": {"status": "approved"}, "brief": {"status": "approved"}}),
    ]  # never terminal
    compute = FakeFactoryCompute(states=states)
    result, gh_calls, _wt, comments = _drive(tmp_path, compute)

    assert result.status == "failed", (result.status, result.reason)
    assert "run timeout" in (result.reason or "")
    assert len(compute.launch_argvs) == 1  # it DID launch, then timed out polling
    # A poll tick happened before the timeout — the first state was mirrored.
    assert any("gate approved: story" in t for t in _comment_texts(comments))


def test_autonomous_stale_heartbeat_with_activity_keeps_waiting(
    tmp_path: Path, monkeypatch
) -> None:
    # Stale heartbeat is a TRIGGER TO PROBE, not an auto-fail. Here the detached
    # factory keeps advancing run.json + its process log (mtimes advance) → the
    # file-activity signal keeps the probe waiting, and the run ships at terminal.
    monkeypatch.setenv("MIMIR_FACTORY_STALE_HEARTBEAT_S", "1")
    monkeypatch.setenv("MIMIR_FACTORY_PROBE_WINDOW_S", "300")
    stale = _stale_iso()
    states = [
        _payload("running", gates={"story": {"status": "pending"}}, heartbeat=stale),
        _payload("running", gates={"story": {"status": "approved"}}, heartbeat=stale),
        _payload(
            "completed",
            gates={
                "story": {"status": "approved"},
                "brief": {"status": "approved"},
                "pre_pr": {"status": "approved"},
            },
            pr_url=PR_URL,
            heartbeat=stale,
            terminal={"status": "completed", "run_id": "chainlink-834", "pr_url": PR_URL, "summary": "ok"},
        ),
    ]
    compute = FakeFactoryCompute(states=states, yields_between=4, write_process_log=True)
    result, gh_calls, worktree, comments = _drive(tmp_path, compute)

    assert result.status == "review_ready", (result.status, result.reason)
    assert compute.cancelled == []  # the probe never declared it stuck


def test_autonomous_stale_heartbeat_nothing_advancing_is_stuck(
    tmp_path: Path, monkeypatch
) -> None:
    # Stale heartbeat AND no run-dir/process-log file advancing (activity forced to
    # None) with liveness UNKNOWN (the child probe can't find it) → the file-activity
    # fallback declares stuck → failed.
    monkeypatch.setenv("MIMIR_FACTORY_STALE_HEARTBEAT_S", "1")
    monkeypatch.setattr(
        "mimir.worklink.orchestrator._run_dir_recent_activity_s", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "mimir.worklink.orchestrator._detached_factory_alive", lambda *a, **k: None
    )
    scripted = [_payload("running", gates={"story": {"status": "approved"}}, heartbeat=_stale_iso())]
    compute = FakeFactoryCompute(passive=True)
    result, gh_calls, _wt, _comments = _drive(
        tmp_path, compute, monkeypatch=monkeypatch, scripted=scripted
    )

    assert result.status == "failed", (result.status, result.reason)
    assert "no run-dir file advanced" in (result.reason or "")


def test_autonomous_stale_heartbeat_but_child_alive_keeps_waiting(
    tmp_path: Path, monkeypatch
) -> None:
    # Regression for the #840 false-positive: during the pre_pr review panel the
    # factory's heartbeat went stale AND no run-dir/process file advanced within the
    # window — but the detached child was ALIVE the whole time. The probe finds the
    # live child, so the poll loop must keep waiting (not fail-stuck) and proceed to
    # the terminal PR once the panel finishes.
    monkeypatch.setenv("MIMIR_FACTORY_STALE_HEARTBEAT_S", "1")
    monkeypatch.setattr(
        "mimir.worklink.orchestrator._run_dir_recent_activity_s", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "mimir.worklink.orchestrator._detached_factory_alive", lambda *a, **k: True
    )
    scripted = [
        # existing (pre-launch read) → stale, so the adapter launches, then polls:
        _payload("running", gates={"story": {"status": "approved"}}, heartbeat=_stale_iso()),
        # a stale + quiet running tick (the review panel) — must NOT be failed:
        _payload("running", gates={"story": {"status": "approved"}}, heartbeat=_stale_iso()),
        # panel done → terminal PR:
        _payload(
            "completed",
            gates={
                "story": {"status": "approved"},
                "brief": {"status": "approved"},
                "pre_pr": {"status": "approved"},
            },
            pr_url=PR_URL,
        ),
    ]
    compute = FakeFactoryCompute(passive=True)
    result, _gh, _wt, _comments = _drive(
        tmp_path, compute, monkeypatch=monkeypatch, scripted=scripted
    )

    assert result.status == "review_ready", (result.status, result.reason)
    assert result.pr_url == PR_URL


# ── detached liveness: process-log mtimes feed the activity signal ───────────


def test_run_dir_recent_activity_includes_process_log(tmp_path: Path) -> None:
    run_id = epic_run_id(ISSUE_ID)
    # No run dir yet: the detached process log alone is a valid activity signal.
    assert _run_dir_recent_activity_s(tmp_path, run_id) is None
    log = _touch_process_log(tmp_path)
    old = datetime.now(UTC).timestamp() - 10_000
    os.utime(log, (old, old))
    activity = _run_dir_recent_activity_s(tmp_path, run_id)
    assert activity is not None and activity >= 9_000  # reflects the (old) log mtime

    # A freshly-advanced run.json wins (more recent than the old process log).
    _write_factory_run(tmp_path, _payload("running", gates={"story": {"status": "pending"}}))
    assert _run_dir_recent_activity_s(tmp_path, run_id) < 100

    # Conversely, a freshly-advanced process log is picked up even when run.json is old.
    rj = factory_run_dir(tmp_path, run_id) / "run.json"
    os.utime(rj, (old, old))
    _touch_process_log(tmp_path, name="20260705-000001.log")
    assert _run_dir_recent_activity_s(tmp_path, run_id) < 100


def test_run_dir_recent_activity_uses_real_factory_processes_path(tmp_path: Path) -> None:
    # Guard against the impl and test helper drifting to the same wrong path again:
    # write the process log at the LITERAL documented location
    # (<wt>/.opencode/factory/processes/) and confirm it feeds the activity signal,
    # and that the old buggy location (<wt>/.opencode/processes/) is not used.
    run_id = epic_run_id(ISSUE_ID)
    log_dir = tmp_path / ".opencode" / "factory" / "processes"
    log_dir.mkdir(parents=True)
    (log_dir / "ts.log").write_text("advancing\n", encoding="utf-8")
    activity = _run_dir_recent_activity_s(tmp_path, run_id)
    assert activity is not None and activity < 100
    assert not (tmp_path / ".opencode" / "processes").exists()


def test_feature_factory_backend_capabilities() -> None:
    caps = FeatureFactoryBackend().capabilities()
    assert caps.tool_category == "feature-factory"
    assert caps.persistent_sessions is True


def test_is_valid_pr_url() -> None:
    from mimir.worklink.backends.feature_factory import _is_valid_pr_url

    assert _is_valid_pr_url("https://github.com/owner/repo/pull/123")
    assert _is_valid_pr_url("  https://github.com/owner/repo/pull/456  ")
    assert not _is_valid_pr_url(None)
    assert not _is_valid_pr_url("")
    assert not _is_valid_pr_url("  ")
    assert not _is_valid_pr_url("not-a-url")
    assert not _is_valid_pr_url("https://gitlab.com/owner/repo/pull/1")
    assert not _is_valid_pr_url("https://github.com/owner/repo/issues/1")


def test_default_bin_is_feature_factory() -> None:
    backend = FeatureFactoryBackend()
    assert backend.bin == "feature-factory"


def test_interpret_run_id_mismatch_fails_closed(tmp_path: Path) -> None:
    from mimir.worklink.backends.base import WorkOrder
    from mimir.worklink.backends import ComputeResult
    import asyncio

    backend = FeatureFactoryBackend()
    worktree = tmp_path / "wt"
    order = WorkOrder(
        issue_id=834,
        worktree=worktree,
        prompt="prompt",
        rules=None,
        timeout_s=1800,
    )
    _write_run_json(worktree, {"run_id": "chainlink-999", "status": "completed", "heartbeat_at": _now(), "pr_url": "https://github.com/o/r/pull/1"}, "chainlink-834")

    result = asyncio.run(backend.interpret(order, ComputeResult(exit_code=0, stdout="", stderr="")))
    assert result.backend_status == "failed"
    assert "run-id mismatch" in (result.error or "")


def test_interpret_completed_without_pr_url_fails_closed(tmp_path: Path) -> None:
    from mimir.worklink.backends.base import WorkOrder
    from mimir.worklink.backends import ComputeResult
    import asyncio

    backend = FeatureFactoryBackend()
    worktree = tmp_path / "wt"
    order = WorkOrder(
        issue_id=834,
        worktree=worktree,
        prompt="prompt",
        rules=None,
        timeout_s=1800,
    )
    _write_run_json(worktree, {"run_id": "chainlink-834", "status": "completed", "heartbeat_at": _now()}, "chainlink-834")

    result = asyncio.run(backend.interpret(order, ComputeResult(exit_code=0, stdout="", stderr="")))
    assert result.backend_status == "failed"
    assert "PR URL" in (result.error or "")


def test_interpret_invalid_pr_url_fails_closed(tmp_path: Path) -> None:
    from mimir.worklink.backends.base import WorkOrder
    from mimir.worklink.backends import ComputeResult
    import asyncio

    backend = FeatureFactoryBackend()
    worktree = tmp_path / "wt"
    order = WorkOrder(
        issue_id=834,
        worktree=worktree,
        prompt="prompt",
        rules=None,
        timeout_s=1800,
    )
    _write_run_json(worktree, {"run_id": "chainlink-834", "status": "completed", "heartbeat_at": _now(), "pr_url": "not-a-pr-url"}, "chainlink-834")

    result = asyncio.run(backend.interpret(order, ComputeResult(exit_code=0, stdout="", stderr="")))
    assert result.backend_status == "failed"
    assert "PR URL" in (result.error or "")


def test_interpret_malformed_run_json_fails_closed(tmp_path: Path) -> None:
    from mimir.worklink.backends.base import WorkOrder
    from mimir.worklink.backends import ComputeResult
    import asyncio

    backend = FeatureFactoryBackend()
    worktree = tmp_path / "wt"
    order = WorkOrder(
        issue_id=834,
        worktree=worktree,
        prompt="prompt",
        rules=None,
        timeout_s=1800,
    )
    run_dir = factory_run_dir(worktree, "chainlink-834")
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text("not valid json", encoding="utf-8")

    result = asyncio.run(backend.interpret(order, ComputeResult(exit_code=0, stdout="", stderr="")))
    assert result.backend_status == "failed"
    assert "not found" in (result.error or "")


def test_interpret_run_json_as_list_fails_closed(tmp_path: Path) -> None:
    from mimir.worklink.backends.base import WorkOrder
    from mimir.worklink.backends import ComputeResult
    import asyncio

    backend = FeatureFactoryBackend()
    worktree = tmp_path / "wt"
    order = WorkOrder(
        issue_id=834,
        worktree=worktree,
        prompt="prompt",
        rules=None,
        timeout_s=1800,
    )
    _write_run_json(worktree, ["not", "a", "dict"], "chainlink-834")

    result = asyncio.run(backend.interpret(order, ComputeResult(exit_code=0, stdout="", stderr="")))
    assert result.backend_status == "failed"
    assert "not found" in (result.error or "")


def test_lifecycle_cursor_persistence(tmp_path: Path) -> None:
    from poller import (
        LifecycleCursor,
        CursorEntry,
        _load_cursor,
        _save_cursor,
        CURSOR_VERSION,
    )
    cursor = LifecycleCursor()
    assert cursor.version == CURSOR_VERSION
    entry = CursorEntry(
        run_id="chainlink-100",
        issue_id=100,
        attempt=1,
        physical_path="/repo/.worklink/100-1/.opencode/factory/chainlink-100",
        fingerprint="abc123",
        last_observed="2026-01-01T00:00:00Z",
        alerted=True,
        alerted_at="2026-01-01T00:00:00Z",
    )
    cursor.entries["key1"] = entry
    cursor.updated_at = "2026-01-01T00:00:00Z"
    _save_cursor(tmp_path, cursor)
    loaded = _load_cursor(tmp_path)
    assert loaded.version == CURSOR_VERSION
    assert "key1" in loaded.entries
    assert loaded.entries["key1"].run_id == "chainlink-100"
    assert loaded.entries["key1"].issue_id == 100
    assert loaded.entries["key1"].attempt == 1


def test_lifecycle_cursor_missing_file(tmp_path: Path) -> None:
    from poller import (
        LifecycleCursor,
        _load_cursor,
        CURSOR_VERSION,
    )
    loaded = _load_cursor(tmp_path)
    assert loaded.version == CURSOR_VERSION
    assert loaded.entries == {}


def test_lifecycle_cursor_corrupted_file(tmp_path: Path) -> None:
    from poller import (
        LifecycleCursor,
        _load_cursor,
        CURSOR_VERSION,
    )
    (tmp_path / "lifecycle_cursor.json").write_text("not valid json", encoding="utf-8")
    loaded = _load_cursor(tmp_path)
    assert loaded.version == CURSOR_VERSION
    assert loaded.entries == {}


def test_discover_factory_runs_root_only(tmp_path: Path) -> None:
    from poller import (
        _discover_factory_runs,
        FACTORY_DIR,
        RUN_JSON,
    )
    factory_dir = tmp_path / FACTORY_DIR
    run_dir = factory_dir / "chainlink-100"
    run_dir.mkdir(parents=True)
    (run_dir / RUN_JSON).write_text("{}", encoding="utf-8")
    discovered = _discover_factory_runs(tmp_path)
    assert len(discovered) == 1
    assert discovered[0][2] == "chainlink-100"


def test_discover_factory_runs_nested_attempt(tmp_path: Path) -> None:
    from poller import (
        _discover_factory_runs,
        FACTORY_DIR,
        WORKLINK_DIR,
        RUN_JSON,
    )
    worklink_dir = tmp_path / WORKLINK_DIR
    attempt_dir = worklink_dir / "100-1"
    factory_dir = attempt_dir / FACTORY_DIR
    run_dir = factory_dir / "chainlink-100-attempt1"
    run_dir.mkdir(parents=True)
    (run_dir / RUN_JSON).write_text("{}", encoding="utf-8")
    discovered = _discover_factory_runs(tmp_path)
    assert len(discovered) == 1
    assert discovered[0][2] == "chainlink-100-attempt1"


def test_discover_factory_runs_multiple(tmp_path: Path) -> None:
    from poller import (
        _discover_factory_runs,
        FACTORY_DIR,
        WORKLINK_DIR,
        RUN_JSON,
    )
    factory_dir = tmp_path / FACTORY_DIR
    run_dir1 = factory_dir / "chainlink-100"
    run_dir1.mkdir(parents=True)
    (run_dir1 / RUN_JSON).write_text("{}", encoding="utf-8")
    worklink_dir = tmp_path / WORKLINK_DIR
    attempt_dir = worklink_dir / "100-1"
    factory_dir2 = attempt_dir / FACTORY_DIR
    run_dir2 = factory_dir2 / "chainlink-100-attempt1"
    run_dir2.mkdir(parents=True)
    (run_dir2 / RUN_JSON).write_text("{}", encoding="utf-8")
    discovered = _discover_factory_runs(tmp_path)
    assert len(discovered) == 2


def test_observe_factory_run_parsing(tmp_path: Path) -> None:
    from poller import (
        _observe_factory_run,
        factory_run_dir,
        RUN_JSON,
    )
    run_id = "chainlink-100"
    run_dir = factory_run_dir(tmp_path, run_id)
    run_dir.mkdir(parents=True)
    now = datetime.now(UTC).isoformat()
    (run_dir / RUN_JSON).write_text(
        f'{{"run_id": "{run_id}", "status": "running", "heartbeat_at": "{now}", '
        f'"pr_url": "https://github.com/owner/repo/pull/1", '
        f'"gates": {{"story": {{"status": "approved"}}, "brief": {{"status": "pending"}}}}, '
        f'"validator": {{"verdict": "GO"}}, "security_review": {{"verdict": "PASS"}}}}',
        encoding="utf-8"
    )
    obs = _observe_factory_run(tmp_path, run_id)
    assert obs is not None
    assert obs.run_id == run_id
    assert obs.issue_id == 100
    assert obs.attempt is None
    assert obs.status == "running"
    assert obs.pr_url == "https://github.com/owner/repo/pull/1"
    assert obs.pending_gate == "brief"
    assert obs.validator_verdict == "GO"
    assert obs.security_verdict == "PASS"
    assert obs.is_terminal is False
    assert obs.liveness_class == "healthy"


def test_observe_factory_run_stale(tmp_path: Path) -> None:
    from poller import (
        _observe_factory_run,
        factory_run_dir,
        RUN_JSON,
    )
    run_id = "chainlink-100"
    run_dir = factory_run_dir(tmp_path, run_id)
    run_dir.mkdir(parents=True)
    stale = (datetime.now(UTC) - timedelta(seconds=400)).isoformat()
    (run_dir / RUN_JSON).write_text(
        f'{{"run_id": "{run_id}", "status": "running", "heartbeat_at": "{stale}"}}',
        encoding="utf-8"
    )
    obs = _observe_factory_run(tmp_path, run_id)
    assert obs is not None
    assert obs.liveness_class == "stale"


def test_observe_factory_run_terminal_blocked(tmp_path: Path) -> None:
    from poller import (
        _observe_factory_run,
        factory_run_dir,
        RUN_JSON,
    )
    run_id = "chainlink-100"
    run_dir = factory_run_dir(tmp_path, run_id)
    run_dir.mkdir(parents=True)
    (run_dir / RUN_JSON).write_text(
        f'{{"run_id": "{run_id}", "status": "blocked", "heartbeat_at": "2026-01-01T00:00:00Z", '
        f'"blocked_reason": "needs credentials"}}',
        encoding="utf-8"
    )
    obs = _observe_factory_run(tmp_path, run_id)
    assert obs is not None
    assert obs.is_terminal is True
    assert obs.status == "blocked"
    assert obs.reason == "needs credentials"


def test_observe_factory_run_with_terminal_result(tmp_path: Path) -> None:
    from poller import (
        _observe_factory_run,
        factory_run_dir,
        RUN_JSON,
    )
    run_id = "chainlink-100"
    run_dir = factory_run_dir(tmp_path, run_id)
    run_dir.mkdir(parents=True)
    (run_dir / RUN_JSON).write_text(
        f'{{"run_id": "{run_id}", "status": "completed", "heartbeat_at": "2026-01-01T00:00:00Z", '
        f'"terminal_result": {{"status": "completed", "pr_url": "https://github.com/owner/repo/pull/1", '
        f'"reason": "all done", "summary": "completed successfully"}}}}',
        encoding="utf-8"
    )
    obs = _observe_factory_run(tmp_path, run_id)
    assert obs is not None
    assert obs.is_terminal is True
    assert obs.status == "completed"
    assert obs.pr_url == "https://github.com/owner/repo/pull/1"
    assert obs.reason == "all done"
    assert obs.summary == "completed successfully"


def test_observe_factory_run_missing_json(tmp_path: Path) -> None:
    from poller import (
        _observe_factory_run,
        factory_run_dir,
    )
    run_id = "chainlink-100"
    run_dir = factory_run_dir(tmp_path, run_id)
    run_dir.mkdir(parents=True)
    obs = _observe_factory_run(tmp_path, run_id)
    assert obs is None


def test_observe_factory_run_malformed_json(tmp_path: Path) -> None:
    from poller import (
        _observe_factory_run,
        factory_run_dir,
        RUN_JSON,
    )
    run_id = "chainlink-100"
    run_dir = factory_run_dir(tmp_path, run_id)
    run_dir.mkdir(parents=True)
    (run_dir / RUN_JSON).write_text("not valid json", encoding="utf-8")
    obs = _observe_factory_run(tmp_path, run_id)
    assert obs is None


def test_run_id_to_issue_attempt_basic(tmp_path: Path) -> None:
    from poller import (
        _run_id_to_issue_attempt,
    )
    assert _run_id_to_issue_attempt("chainlink-100") == (100, None)
    assert _run_id_to_issue_attempt("chainlink-100-attempt1") == (100, 1)
    assert _run_id_to_issue_attempt("chainlink-100-attempt2") == (100, 2)
    assert _run_id_to_issue_attempt("invalid") == (0, None)


def test_fingerprint_deterministic(tmp_path: Path) -> None:
    from poller import (
        FactoryRunObservation,
    )
    obs1 = FactoryRunObservation(
        run_id="chainlink-100",
        issue_id=100,
        attempt=None,
        physical_path=tmp_path / "path1",
        status="running",
        pr_url=None,
        reason=None,
        summary=None,
        pending_gate=None,
        is_terminal=False,
        is_stale=False,
        validator_verdict=None,
        security_verdict=None,
        terminal_result=None,
    )
    obs2 = FactoryRunObservation(
        run_id="chainlink-100",
        issue_id=100,
        attempt=None,
        physical_path=tmp_path / "path1",
        status="running",
        pr_url=None,
        reason=None,
        summary=None,
        pending_gate=None,
        is_terminal=False,
        is_stale=False,
        validator_verdict=None,
        security_verdict=None,
        terminal_result=None,
    )
    assert obs1.fingerprint == obs2.fingerprint


def test_fingerprint_changes_with_status(tmp_path: Path) -> None:
    from poller import (
        FactoryRunObservation,
    )
    obs1 = FactoryRunObservation(
        run_id="chainlink-100",
        issue_id=100,
        attempt=None,
        physical_path=tmp_path / "path1",
        status="running",
        pr_url=None,
        reason=None,
        summary=None,
        pending_gate=None,
        is_terminal=False,
        is_stale=False,
        validator_verdict=None,
        security_verdict=None,
        terminal_result=None,
    )
    obs2 = FactoryRunObservation(
        run_id="chainlink-100",
        issue_id=100,
        attempt=None,
        physical_path=tmp_path / "path1",
        status="blocked",
        pr_url=None,
        reason=None,
        summary=None,
        pending_gate=None,
        is_terminal=True,
        is_stale=False,
        validator_verdict=None,
        security_verdict=None,
        terminal_result=None,
    )
    assert obs1.fingerprint != obs2.fingerprint


CLEANUP_DIGEST = f"ff-cleanup-v1.{('a' * 64)}.{('b' * 64)}"


def _cleanup_report(
    run_id: str, *, mode: str = "preview", eligible: bool = True
) -> dict:
    return {
        "mode": mode,
        "status": "previewed" if mode == "preview" else "completed",
        "authorization": {
            "digest": CLEANUP_DIGEST,
            "provided_digest": None if mode == "preview" else CLEANUP_DIGEST,
            "matched": None if mode == "preview" else True,
        },
        "candidates": [
            {
                "run_id": run_id,
                "classification": (
                    "eligible" if eligible and mode == "preview"
                    else "deleted" if eligible
                    else "protected"
                ),
            }
        ],
    }


def _lifecycle_alert(run_dir: Path, *, status: str = "completed"):
    from poller import LifecycleAlert

    return LifecycleAlert(
        source_id=f"lifecycle:chainlink-100:{run_dir}",
        signal="worklink_factory_actionable",
        run_id="chainlink-100",
        issue_id=100,
        attempt=None,
        physical_path=str(run_dir),
        prior_fingerprint=None,
        current_fingerprint="fingerprint",
        status=status,
        prior_status=None,
        reason=None,
        pr_url=PR_URL,
        pending_gate=None,
        liveness_class="unknown",
        validity_class="valid",
        validator_verdict="GO",
        security_verdict="PASS",
        cleanup_eligible=status == "completed",
        routing_instructions="safe cleanup only",
    )


def _save_cleanup_cursor(state_dir: Path, run_dir: Path) -> None:
    from poller import CursorEntry, LifecycleCursor, _save_cursor

    key = f"chainlink-100:{run_dir}"
    _save_cursor(
        state_dir,
        LifecycleCursor(
            entries={
                key: CursorEntry(
                    run_id="chainlink-100",
                    issue_id=100,
                    attempt=None,
                    physical_path=str(run_dir),
                    fingerprint="fingerprint",
                    last_observed=_now(),
                    alerted=False,
                    alerted_at=None,
                )
            }
        ),
    )


def test_is_pr_merged_uses_canonical_url_and_fails_closed(
    tmp_path: Path, monkeypatch
) -> None:
    import poller

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="MERGED\n", stderr="")

    monkeypatch.setattr(poller.subprocess, "run", fake_run)
    assert poller._is_pr_merged(PR_URL, tmp_path)
    assert calls == [["gh", "pr", "view", PR_URL, "--json", "state", "--jq", ".state"]]

    calls.clear()
    assert not poller._is_pr_merged("https://example.com/pull/42", tmp_path)
    assert calls == []

    monkeypatch.setattr(
        poller.subprocess,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="unavailable"
        ),
    )
    assert not poller._is_pr_merged(PR_URL, tmp_path)


def test_run_factory_cleanup_binds_execute_to_preview_digest(
    tmp_path: Path, monkeypatch
) -> None:
    import poller

    calls: list[list[str]] = []
    reports = [
        _cleanup_report("chainlink-100"),
        _cleanup_report("chainlink-100", mode="execute"),
    ]

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps(reports.pop(0)), stderr=""
        )

    monkeypatch.setattr(poller.subprocess, "run", fake_run)
    success, _, preview = poller._run_factory_cleanup(tmp_path, dry_run=True)
    assert success
    digest = poller._cleanup_digest_for_run(preview, "chainlink-100")
    assert digest == CLEANUP_DIGEST
    success, _, _ = poller._run_factory_cleanup(
        tmp_path, dry_run=False, digest=digest
    )
    assert success
    assert calls == [
        ["factory", "cleanup", "--all", "--dry-run", "--json"],
        [
            "factory",
            "cleanup",
            "--all",
            "--digest",
            CLEANUP_DIGEST,
            "--json",
        ],
    ]
    assert all("--force" not in call for call in calls)


def test_run_factory_cleanup_refuses_unbound_execute(tmp_path: Path, monkeypatch) -> None:
    import poller

    called = False

    def fake_run(cmd, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("factory must not run without an evidence digest")

    monkeypatch.setattr(poller.subprocess, "run", fake_run)
    success, message, report = poller._run_factory_cleanup(tmp_path, dry_run=False)
    assert not success
    assert "requires a valid preview digest" in message
    assert report is None
    assert not called


def test_attempt_cleanup_dry_runs_then_executes_exact_digest(
    tmp_path: Path, monkeypatch
) -> None:
    import poller

    state_dir = tmp_path / "state"
    run_dir = tmp_path / ".opencode" / "factory" / "chainlink-100"
    run_dir.mkdir(parents=True)
    _save_cleanup_cursor(state_dir, run_dir)
    calls: list[tuple[Path, bool, str | None]] = []

    def fake_cleanup(worktree, dry_run=True, *, digest=None):
        calls.append((worktree, dry_run, digest))
        if dry_run:
            return True, "previewed", _cleanup_report("chainlink-100")
        return True, "executed", _cleanup_report("chainlink-100", mode="execute")

    monkeypatch.setattr(poller, "_run_factory_cleanup", fake_cleanup)
    failed = poller._attempt_cleanup(
        tmp_path, state_dir, [_lifecycle_alert(run_dir)]
    )
    assert failed == []
    assert calls == [
        (tmp_path, True, None),
        (tmp_path, False, CLEANUP_DIGEST),
    ]
    cursor = poller._load_cursor(state_dir)
    entry = cursor.entries[f"chainlink-100:{run_dir}"]
    assert entry.cleaned and entry.tombstone


def test_attempt_cleanup_alerts_on_empty_preview_without_executing(
    tmp_path: Path, monkeypatch
) -> None:
    import poller

    state_dir = tmp_path / "state"
    run_dir = tmp_path / ".opencode" / "factory" / "chainlink-100"
    run_dir.mkdir(parents=True)
    _save_cleanup_cursor(state_dir, run_dir)
    calls: list[tuple[Path, bool, str | None]] = []

    def fake_cleanup(worktree, dry_run=True, *, digest=None):
        calls.append((worktree, dry_run, digest))
        assert dry_run
        return True, "previewed", _cleanup_report("chainlink-100", eligible=False)

    monkeypatch.setattr(poller, "_run_factory_cleanup", fake_cleanup)
    alert = _lifecycle_alert(run_dir)
    assert poller._attempt_cleanup(tmp_path, state_dir, [alert]) == [alert]
    assert calls == [(tmp_path, True, None)]
    entry = poller._load_cursor(state_dir).entries[f"chainlink-100:{run_dir}"]
    assert not entry.cleaned and not entry.tombstone


def test_attempt_cleanup_requires_execute_to_report_run_deleted(
    tmp_path: Path, monkeypatch
) -> None:
    import poller

    state_dir = tmp_path / "state"
    run_dir = tmp_path / ".opencode" / "factory" / "chainlink-100"
    run_dir.mkdir(parents=True)
    _save_cleanup_cursor(state_dir, run_dir)

    def fake_cleanup(worktree, dry_run=True, *, digest=None):
        if dry_run:
            return True, "previewed", _cleanup_report("chainlink-100")
        return True, "executed", _cleanup_report(
            "chainlink-100", mode="execute", eligible=False
        )

    monkeypatch.setattr(poller, "_run_factory_cleanup", fake_cleanup)
    alert = _lifecycle_alert(run_dir)
    assert poller._attempt_cleanup(tmp_path, state_dir, [alert]) == [alert]
    entry = poller._load_cursor(state_dir).entries[f"chainlink-100:{run_dir}"]
    assert not entry.cleaned and not entry.tombstone


def test_reconcile_cleanup_eligible_only_for_completed_merged_run(
    tmp_path: Path, monkeypatch
) -> None:
    import poller

    run_dir = tmp_path / ".opencode" / "factory" / "chainlink-100"
    run_dir.mkdir(parents=True)

    def observation(status: str) -> poller.FactoryRunObservation:
        return poller.FactoryRunObservation(
            run_id="chainlink-100",
            issue_id=100,
            attempt=None,
            physical_path=run_dir,
            status=status,
            pr_url=PR_URL,
            reason=None,
            summary=None,
            pending_gate=None,
            is_terminal=status in {"completed", "blocked", "partial", "needs-human"},
            is_stale=False,
            validator_verdict="GO",
            security_verdict="PASS",
            terminal_result=None,
            validity_class="valid",
        )

    current = observation("completed")
    merged = True
    monkeypatch.setattr(
        poller,
        "_discover_factory_runs",
        lambda repo: [(tmp_path, run_dir, "chainlink-100")],
    )
    monkeypatch.setattr(poller, "_observe_factory_run", lambda root, run_id: current)
    monkeypatch.setattr(poller, "_is_pr_merged", lambda pr_url, repo: merged)

    alerts, cleanup = poller._reconcile_factory_runs(tmp_path, tmp_path / "merged")
    assert alerts == []
    assert [alert.run_id for alert in cleanup] == ["chainlink-100"]

    merged = False
    _, cleanup = poller._reconcile_factory_runs(tmp_path, tmp_path / "open")
    assert cleanup == []

    merged = True
    for status in ("blocked", "partial", "needs-human"):
        current = observation(status)
        _, cleanup = poller._reconcile_factory_runs(
            tmp_path, tmp_path / f"state-{status}"
        )
        assert cleanup == [], status

    current = observation("completed")
    object.__setattr__(current, "validity_class", "invalid")
    _, cleanup = poller._reconcile_factory_runs(tmp_path, tmp_path / "invalid")
    assert cleanup == []

    current = observation("completed")
    object.__setattr__(current, "liveness_class", "stale")
    _, cleanup = poller._reconcile_factory_runs(tmp_path, tmp_path / "stale")
    assert cleanup == []
