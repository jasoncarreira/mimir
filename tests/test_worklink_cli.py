from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from mimir.cli import main
from mimir.worklink.orchestrator import WorklinkRunResult
from mimir.worklink.control import reconcile_run_states, stop_worklink, worklink_status
from mimir.worklink.backends.feature_factory import parse_factory_status
from mimir.worklink.compute import LaunchHandle
from mimir.worklink.factory_state import FactoryRunRecord, load_factory_record, save_factory_record
from mimir.worklink.run_state import WorklinkRunState, load_run_state, process_start_ticks, save_run_state


def test_worklink_run_cli_dispatches_operator_vertical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[dict[str, object]] = []

    def fake_run_worklink(**kwargs: object) -> WorklinkRunResult:
        calls.append(kwargs)
        return WorklinkRunResult(
            issue_id=441,
            attempt=1,
            status="completed",
            review_ready=True,
            pr_url="https://github.com/jasoncarreira/mimir/pull/999",
            evidence_path=tmp_path / "state" / "worklink" / "evidence" / "441-1.json",
        )

    import mimir.commands.worklink as worklink_cmd

    monkeypatch.setattr(worklink_cmd, "run_worklink", fake_run_worklink)

    with pytest.raises(SystemExit) as exc:
        main([
            "worklink",
            "run",
            "441",
            "--home",
            str(tmp_path / "home"),
            "--repo",
            str(tmp_path / "repo"),
            "--backend",
            "fake",
        ])

    assert exc.value.code == 0
    assert calls == [
        {
            "home": (tmp_path / "home").resolve(),
            "repo": (tmp_path / "repo").resolve(),
            "issue_id": 441,
            "backend": "fake",
            "dry_run": False,
            "test_command": None,
            "base_branch": None,
            "autonomous": False,
        }
    ]
    assert "worklink #441 attempt 1: completed review-ready" in capsys.readouterr().out


def test_worklink_run_cli_forwards_base_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []

    def fake_run_worklink(**kwargs: object) -> WorklinkRunResult:
        calls.append(kwargs)
        return WorklinkRunResult(issue_id=441, attempt=1, status="completed")

    import mimir.commands.worklink as worklink_cmd

    monkeypatch.setattr(worklink_cmd, "run_worklink", fake_run_worklink)

    with pytest.raises(SystemExit) as exc:
        main([
            "worklink",
            "run",
            "441",
            "--home",
            str(tmp_path / "home"),
            "--repo",
            str(tmp_path / "repo"),
            "--base",
            "integration/worklink",
        ])

    assert exc.value.code == 0
    assert calls and calls[0]["base_branch"] == "integration/worklink"


def test_worklink_run_cli_forwards_autonomous_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []

    def fake_run_worklink(**kwargs: object) -> WorklinkRunResult:
        calls.append(kwargs)
        return WorklinkRunResult(issue_id=441, attempt=1, status="completed")

    import mimir.commands.worklink as worklink_cmd

    monkeypatch.setattr(worklink_cmd, "run_worklink", fake_run_worklink)

    with pytest.raises(SystemExit) as exc:
        main([
            "worklink",
            "run",
            "441",
            "--home",
            str(tmp_path / "home"),
            "--repo",
            str(tmp_path / "repo"),
            "--autonomous",
        ])

    assert exc.value.code == 0
    assert calls and calls[0]["autonomous"] is True


def test_worklink_run_cli_autonomous_refused_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_run_worklink(**kwargs: object) -> WorklinkRunResult:
        return WorklinkRunResult(issue_id=441, attempt=None, status="refused", reason="unsafe compute")

    import mimir.commands.worklink as worklink_cmd

    monkeypatch.setattr(worklink_cmd, "run_worklink", fake_run_worklink)

    with pytest.raises(SystemExit) as exc:
        main([
            "worklink",
            "run",
            "441",
            "--home",
            str(tmp_path / "home"),
            "--repo",
            str(tmp_path / "repo"),
            "--autonomous",
        ])

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "refused" in captured.err and "unsafe compute" in captured.err


@pytest.mark.parametrize("autonomous", [False, True])
def test_worklink_run_epic_cli_routes_manual_and_autonomous_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    autonomous: bool,
) -> None:
    import mimir.commands.worklink as worklink_cmd

    calls: list[dict[str, object]] = []

    def run_epic(**kwargs: object) -> WorklinkRunResult:
        calls.append(kwargs)
        return WorklinkRunResult(700, 1, "needs-human")

    monkeypatch.setattr(worklink_cmd, "run_worklink_epic", run_epic)
    argv = [
        "worklink",
        "run-epic",
        "700",
        "--home",
        str(tmp_path / "home"),
        "--repo",
        str(tmp_path / "repo"),
    ]
    if autonomous:
        argv.append("--autonomous")

    with pytest.raises(SystemExit) as exc:
        main(argv)

    assert exc.value.code == 1
    assert calls[0]["autonomous"] is autonomous


def test_worklink_cli_has_no_factory_cancel_transition() -> None:
    import mimir.commands.worklink as worklink_cmd

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    worklink_cmd.add_argparse(sub)

    with pytest.raises(SystemExit):
        parser.parse_args(["worklink", "factory-cancel", "700"])


def test_worklink_cli_rejects_unknown_subcommand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """chainlink #832: docker-broker and worker subcommands were retired. The
    parser surface only registers ``run`` + ``run-epic``; argparse itself
    rejects anything else (e.g. ``mimir worklink worker …``), so the
    dispatcher's catch-all ``return 1`` for unknown actions never has to fire
    in production. This test pins that contract."""
    import mimir.commands.worklink as worklink_cmd

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    worklink_cmd.add_argparse(sub)

    with pytest.raises(SystemExit):
        parser.parse_args(["worklink", "worker", "/tmp/payload.json"])

    with pytest.raises(SystemExit):
        parser.parse_args(["worklink", "docker-broker", "--policy", "/tmp/p.yaml"])


def test_worklink_run_failed_without_attempt_prints_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from mimir.worklink.orchestrator import WorklinkRunResult

    import mimir.commands.worklink as worklink_cmd

    def fake_run_worklink(**kwargs: object) -> WorklinkRunResult:
        return WorklinkRunResult(529, None, "failed", reason="claim_failed: lock held")

    monkeypatch.setattr(worklink_cmd, "run_worklink", fake_run_worklink)

    with pytest.raises(SystemExit) as exc:
        main(["worklink", "run", "529", "--home", str(tmp_path), "--repo", str(tmp_path)])

    assert exc.value.code == 1
    assert "worklink #529 attempt None: failed — claim_failed: lock held" in capsys.readouterr().out


def _state(
    home: Path,
    issue_id: int,
    pid: int,
    *,
    ticks: int | None,
    started_at: datetime,
) -> WorklinkRunState:
    state = WorklinkRunState(
        issue_id=issue_id,
        attempt=2,
        backend="opencode",
        compute_name="local_subprocess",
        handle_substrate="local_subprocess",
        handle_identifier=str(pid),
        branch=f"issue/{issue_id}-a2",
        base_ref="main",
        local_base="main",
        repo="/repo",
        repo_url="",
        test_command="pytest",
        started_at=started_at.isoformat(),
        checkout=f"/repo/.worklink/{issue_id}-2",
        process_start_ticks=ticks,
    )
    save_run_state(home, state)
    return state


def test_status_classifies_all_states_and_disagreements(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    _state(
        tmp_path,
        1,
        os.getpid(),
        ticks=process_start_ticks(os.getpid()),
        started_at=now - timedelta(minutes=2),
    )
    _state(tmp_path, 2, 999_999_999, ticks=1, started_at=now - timedelta(minutes=3))

    calls: list[list[str]] = []
    issue_details = {
        1: {"id": 1, "labels": ["worklink:in-progress"]},
        2: {"id": 2, "labels": ["worklink:ready"]},
        3: {"id": 3, "labels": ["worklink:in-progress"]},
        4: {"id": 4, "labels": ["worklink:ready"]},
        5: {"id": 5, "labels": ["bug"]},
    }

    def runner(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        if args[2] == "list":
            payload = [{"id": issue_id, "status": "open"} for issue_id in range(1, 6)]
        else:
            payload = issue_details[int(args[3])]
        return subprocess.CompletedProcess(
            args, 0, stdout=json.dumps(payload), stderr=""
        )

    rows = worklink_status(tmp_path, runner=runner, now=now)
    assert {row.issue_id: row.classification for row in rows} == {
        1: "running",
        2: "orphaned",
        3: "unrecorded",
        4: "clean",
    }
    assert rows[0].elapsed_s == 120
    assert rows[0].disagreement is None
    assert rows[1].disagreement == "run state present but worklink:in-progress label absent"
    assert rows[2].disagreement == "worklink:in-progress label present but run state absent"
    assert any(row.label_in_progress for row in rows)
    assert calls == [
        ["chainlink", "issue", "list", "--status", "open", "--json"],
        *[
            ["chainlink", "issue", "show", str(issue_id), "--json"]
            for issue_id in range(1, 6)
        ],
    ]


def test_status_explicit_ids_skip_open_issue_discovery(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    _state(
        tmp_path,
        3,
        os.getpid(),
        ticks=process_start_ticks(os.getpid()),
        started_at=datetime.now(UTC),
    )

    def runner(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps({"id": int(args[3]), "labels": ["worklink:ready"]}),
            stderr="",
        )

    rows = worklink_status(tmp_path, issue_ids=[4], runner=runner)

    assert [row.issue_id for row in rows] == [4]
    assert calls == [["chainlink", "issue", "show", "4", "--json"]]


def test_status_rejects_non_collection_list_payload(tmp_path: Path) -> None:
    def runner(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout="null", stderr="")

    with pytest.raises(RuntimeError, match="invalid JSON payload"):
        worklink_status(tmp_path, runner=runner)


def test_status_reports_labels_unavailable_instead_of_absent(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    _state(
        tmp_path,
        5,
        os.getpid(),
        ticks=process_start_ticks(os.getpid()),
        started_at=now,
    )

    def runner(args: list[str]) -> subprocess.CompletedProcess[str]:
        # This is the real `issue list --json` item shape: it has neither labels
        # nor blocked_by, and is invalid as an `issue show` label response.
        payload = {
            "closed_at": None,
            "created_at": "2026-07-31T00:00:00Z",
            "description": "",
            "id": int(args[3]),
            "parent_id": None,
            "priority": 0,
            "status": "open",
            "title": "Issue",
            "updated_at": "2026-07-31T00:00:00Z",
        }
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")

    rows = worklink_status(tmp_path, issue_ids=[5, 6], runner=runner, now=now)

    assert [row.classification for row in rows] == ["running", "unknown"]
    assert all(row.label_in_progress is None for row in rows)
    assert [row.disagreement for row in rows] == [
        "labels unavailable: chainlink issue show 5 omitted labels",
        "labels unavailable: chainlink issue show 6 omitted labels",
    ]


def test_status_bounds_chainlink_subprocesses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import mimir.worklink.control as control

    calls: list[tuple[list[str], int | None]] = []

    def fake_run(
        args: list[str],
        *,
        cwd: Path,
        capture_output: bool,
        text: bool,
        check: bool,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((args, timeout))
        if args[2] == "list":
            return subprocess.CompletedProcess(args, 0, stdout='[{"id": 7}]', stderr="")
        raise subprocess.TimeoutExpired(args, timeout or 0, stderr="wedged")

    monkeypatch.setattr(control.subprocess, "run", fake_run)

    rows = worklink_status(tmp_path)

    assert [row.classification for row in rows] == ["unknown"]
    assert rows[0].label_in_progress is None
    assert rows[0].disagreement == (
        "labels unavailable: wedged\nchainlink timed out after 10s"
    )
    assert calls == [
        (["chainlink", "issue", "list", "--status", "open", "--json"], 10),
        (["chainlink", "issue", "show", "7", "--json"], 10),
    ]


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="requires procfs")
def test_stop_clears_state_claim_and_label_and_missing_is_noop(tmp_path: Path) -> None:
    # A verified stop requires Linux's stable process birth marker from /proc.
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    calls: list[list[str]] = []

    def runner(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    try:
        ticks = process_start_ticks(proc.pid)
        assert ticks is not None
        _state(tmp_path, 7, proc.pid, ticks=ticks, started_at=datetime.now(UTC))
        result = stop_worklink(tmp_path, 7, runner=runner)
        assert result.stopped
        assert result.state_cleared and result.claim_released and result.label_cleared
        assert load_run_state(tmp_path, 7) is None
        assert ["chainlink", "locks", "release", "7"] in calls
        assert ["chainlink", "issue", "unlabel", "7", "worklink:in-progress"] in calls

        calls.clear()
        missing = stop_worklink(tmp_path, 8, runner=runner)
        assert not missing.stopped and missing.reason == "no live run"
        assert calls == []
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="requires procfs")
def test_stop_clears_state_for_reused_pid_without_signalling(tmp_path: Path) -> None:
    # PID-reuse refusal depends on comparing Linux /proc process birth markers.
    calls: list[list[str]] = []
    _state(
        tmp_path,
        9,
        os.getpid(),
        ticks=(process_start_ticks(os.getpid()) or 0) + 1,
        started_at=datetime.now(UTC),
    )

    result = stop_worklink(
        tmp_path,
        9,
        runner=lambda args: (
            calls.append(list(args))
            or subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        ),
    )

    assert not result.stopped
    assert result.reason == "no live run"
    assert result.state_cleared
    assert calls == []
    assert load_run_state(tmp_path, 9) is None


def test_reconcile_reaps_orphan_and_leaves_live_state(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    _state(
        tmp_path,
        10,
        os.getpid(),
        ticks=process_start_ticks(os.getpid()),
        started_at=now,
    )
    _state(tmp_path, 11, 999_999_999, ticks=1, started_at=now - timedelta(seconds=90))
    shim_state = WorklinkRunState(
        **{
            **_state(
                tmp_path,
                12,
                999_999_998,
                ticks=1,
                started_at=now - timedelta(seconds=60),
            ).to_json(),
            "handle_identifier": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "shim_pid": 999_999_998,
        }
    )
    save_run_state(tmp_path, shim_state)
    events: list[tuple[str, dict[str, object]]] = []

    alive = reconcile_run_states(
        tmp_path,
        event_logger=lambda event, **payload: events.append((event, payload)),
        now=now,
    )

    assert [state.issue_id for state in alive] == [10]
    assert load_run_state(tmp_path, 10) is not None
    assert load_run_state(tmp_path, 11) is None
    assert load_run_state(tmp_path, 12) is None
    assert events == [
        (
            "worklink_run_orphaned",
            {"issue_id": 11, "attempt": 2, "elapsed_s": 90.0, "reaped": True},
        ),
        (
            "worklink_run_orphaned",
            {"issue_id": 12, "attempt": 2, "elapsed_s": 60.0, "reaped": True},
        ),
    ]


def test_factory_stop_cancels_verified_handle_without_factory_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mimir.worklink.control as control

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    status = parse_factory_status(
        {
            "run_id": "700",
            "issue_key": "700",
            "valid": True,
            "sandbox_path": str(sandbox),
            "status": "running",
            "mode": "autonomous",
            "branch": "epic/700",
            "pr_base": "main",
            "pr_draft": False,
            "lock": "fresh",
            "dead_lock": False,
            "lock_session": "session-1",
            "gates": {},
            "steps": ["implementation"],
            "slices": ["factory-070-migration"],
            "validator": None,
            "pr_url": None,
            "terminal_result": None,
            "next": "implementation",
        }
    )
    handle = LaunchHandle("local_subprocess", "4321", 99)
    save_factory_record(
        tmp_path,
        FactoryRunRecord(
            run_id="700",
            issue_id=700,
            attempt=1,
            repository="owner/repo",
            base_ref="main",
            branch="epic/700",
            launcher="/opt/factory/bin/factory.js",
            sandbox=str(sandbox),
            session="session-1",
            handle=handle,
            status=status,
            observed_at="2026-08-18T12:00:00+00:00",
            controller_phase="running",
        ),
    )
    cancelled: list[LaunchHandle] = []
    commands: list[list[str]] = []

    async def cancel(self: object, selected: LaunchHandle) -> None:
        cancelled.append(selected)

    monkeypatch.setattr(control, "factory_process_is_alive", lambda record: True)
    monkeypatch.setattr(control.LocalSubprocessComputeBackend, "cancel", cancel)
    result = stop_worklink(
        tmp_path,
        700,
        runner=lambda args: commands.append(list(args))
        or subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
    )

    assert result.stopped
    assert cancelled == [handle]
    assert load_factory_record(tmp_path, "700").controller_phase == "stopped"
    assert all("factory" not in command for command in commands)


def test_factory_stop_refuses_unverified_or_reused_process_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mimir.worklink.control as control

    monkeypatch.setattr(control, "load_run_state", lambda home, issue_id: None)
    monkeypatch.setattr(control, "load_factory_record", lambda home, run_id: object())
    monkeypatch.setattr(control, "factory_process_is_alive", lambda record: False)
    monkeypatch.setattr(
        control.LocalSubprocessComputeBackend,
        "cancel",
        lambda *args: (_ for _ in ()).throw(AssertionError("unverified process signalled")),
    )

    result = stop_worklink(tmp_path, 700, runner=lambda args: subprocess.CompletedProcess(args, 0))

    assert not result.stopped
    assert result.reason == "no live run"


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="requires procfs")
def test_factory_stop_cancels_verified_process_group(tmp_path: Path) -> None:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        ticks = process_start_ticks(process.pid)
        assert ticks is not None
        status = parse_factory_status(
            {
                "run_id": "700",
                "issue_key": "700",
                "valid": True,
                "sandbox_path": str(sandbox),
                "status": "running",
                "mode": "autonomous",
                "branch": "epic/700",
                "pr_base": "main",
                "pr_draft": False,
                "lock": "fresh",
                "dead_lock": False,
                "lock_session": "session-1",
                "gates": {},
                "steps": ["implementation"],
                "slices": ["factory-070-migration"],
                "validator": None,
                "pr_url": None,
                "terminal_result": None,
                "next": "implementation",
            }
        )
        save_factory_record(
            tmp_path,
            FactoryRunRecord(
                run_id="700",
                issue_id=700,
                attempt=1,
                repository="owner/repo",
                base_ref="main",
                branch="epic/700",
                launcher="/opt/factory/bin/factory.js",
                sandbox=str(sandbox),
                session="session-1",
                handle=LaunchHandle("local_subprocess", str(process.pid), ticks),
                status=status,
                observed_at="2026-08-18T12:00:00+00:00",
                controller_phase="running",
            ),
        )

        result = stop_worklink(
            tmp_path,
            700,
            runner=lambda args: subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
        )

        assert result.stopped
        assert process.wait(timeout=5) != 0
        assert load_factory_record(tmp_path, "700").controller_phase == "stopped"
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
