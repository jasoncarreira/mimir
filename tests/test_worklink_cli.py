from __future__ import annotations

import argparse
from dataclasses import replace
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
from mimir.worklink import autonomy
from mimir.worklink.autonomy import check_concurrency
from mimir.worklink.backends.feature_factory import parse_factory_status
from mimir.worklink.claims import ChainlinkClaims
from mimir.worklink.compute import LaunchHandle
from mimir.worklink.factory_state import FactoryRunRecord, load_factory_record, save_factory_record
from mimir.worklink.run_state import (
    OrphanBlockRecord,
    WorklinkRunState,
    load_orphan_block_record,
    load_run_state,
    process_start_ticks,
    save_run_state,
)


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


def test_worklink_run_cli_requires_explicit_base_instead_of_using_cwd(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("WORKLINK_REPO", raising=False)
    monkeypatch.delenv("MIMIR_WORKLINK_REPO", raising=False)

    with pytest.raises(SystemExit) as exc:
        main(["worklink", "run", "441"])

    assert exc.value.code == 1
    assert "WORKLINK_REPO is required" in capsys.readouterr().err


def test_worklink_emit_work_item_has_wire_clean_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import mimir.commands.worklink as worklink_cmd

    payload = '{"body":"path\\\\to\\\\file","run_id":"chainlink-441","title":"Title"}'
    monkeypatch.setattr(worklink_cmd, "read_work_item", lambda issue_id: payload)

    with pytest.raises(SystemExit) as exc:
        main(["worklink", "emit-work-item", "441"])

    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert captured.out == payload
    assert captured.err == ""


def test_worklink_emit_work_item_failure_has_no_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import mimir.commands.worklink as worklink_cmd
    from mimir.worklink.orchestrator import WorklinkError

    def missing(_issue_id: int) -> str:
        raise WorklinkError("issue not found")

    monkeypatch.setattr(worklink_cmd, "read_work_item", missing)

    with pytest.raises(SystemExit) as exc:
        main(["worklink", "emit-work-item", "999"])

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: issue not found\n"


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


def test_worklink_run_epic_cli_rejects_base_flag() -> None:
    import mimir.commands.worklink as worklink_cmd

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    worklink_cmd.add_argparse(sub)

    with pytest.raises(SystemExit):
        parser.parse_args(["worklink", "run-epic", "700", "--base", "feature/acp"])


def test_worklink_cli_has_no_factory_cancel_transition() -> None:
    import mimir.commands.worklink as worklink_cmd

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    worklink_cmd.add_argparse(sub)

    with pytest.raises(SystemExit):
        parser.parse_args(["worklink", "factory-cancel", "700"])


def test_worklink_archive_factory_run_cli_archives_canonical_and_legacy_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canonical = FactoryRunRecord(
        run_id="chainlink-700",
        issue_id=700,
        attempt=2,
        repository="owner/repo",
        base_ref="main",
        branch="feature/chainlink-700",
        launcher="/opt/factory/bin/factory.js",
        sandbox=str(tmp_path / "chainlink-700"),
        session="session-2",
        handle=None,
        status=None,
        observed_at=None,
        controller_phase="stopped",
    )
    legacy = replace(
        canonical,
        run_id="700",
        attempt=1,
        branch="epic/700",
        sandbox=str(tmp_path / "legacy"),
        session="session-1",
    )
    save_factory_record(tmp_path, canonical)
    save_factory_record(tmp_path, legacy)
    events: list[tuple[str, dict[str, object]]] = []
    import mimir.commands.worklink as worklink_cmd

    monkeypatch.setattr(
        worklink_cmd,
        "log_durable_event_sync",
        lambda event, **payload: events.append((event, payload)),
    )
    import mimir.event_logger as event_logger

    monkeypatch.setattr(event_logger, "init_logger", lambda *args, **kwargs: None)

    with pytest.raises(SystemExit) as exc:
        main(["worklink", "archive-factory-run", "700", "--home", str(tmp_path)])

    assert exc.value.code == 0
    assert load_factory_record(tmp_path, "chainlink-700") is None
    assert load_factory_record(tmp_path, "700") is None
    archives = sorted((tmp_path / "state/worklink/factory-runs/archive").glob("*.json"))
    assert len(archives) == 2
    assert [(event, payload["source"], payload["reason"]) for event, payload in events] == [
        (
            "worklink_factory_record_archived",
            "operator_command",
            "operator requested archival",
        ),
        (
            "worklink_factory_record_archived",
            "operator_command",
            "operator requested archival",
        ),
    ]
    assert {(payload["run_id"], payload["phase"]) for _, payload in events} == {
        ("chainlink-700", "stopped"),
        ("700", "stopped"),
    }
    assert "archived 2 factory record(s)" in capsys.readouterr().out


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


def test_reconcile_releases_orphan_slot_routes_label_and_leaves_live_lock(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    _state(
        tmp_path,
        10,
        os.getpid(),
        ticks=process_start_ticks(os.getpid()),
        started_at=now,
    )
    dead = _state(
        tmp_path, 11, 999_999_999, ticks=1, started_at=now - timedelta(seconds=90)
    )
    checkout = tmp_path / "checkout-11"
    checkout.mkdir()
    dead = replace(dead, checkout=str(checkout), local_base="base-sha")
    save_run_state(tmp_path, dead)
    events: list[tuple[str, dict[str, object]]] = []
    locks = {10, 11}
    labels = {10: {"worklink:in-progress"}, 11: {"worklink:in-progress"}}
    calls: list[list[str]] = []

    def runner(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        if args[1:3] == ["locks", "release"]:
            locks.discard(int(args[3]))
        elif args[1:3] == ["locks", "list"]:
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps(
                    {"locks": [{"issue_id": issue} for issue in locks]}
                ),
                stderr="",
            )
        elif args[1:3] == ["issue", "unlabel"]:
            labels[int(args[3])].discard(args[4])
        elif args[1:3] == ["issue", "label"]:
            labels[int(args[3])].add(args[4])
        elif args[1:3] == ["issue", "show"]:
            issue_id = int(args[3])
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps(
                    {"id": issue_id, "labels": sorted(labels[issue_id])}
                ),
                stderr="",
            )
        elif args[1:3] == ["issue", "list"]:
            return subprocess.CompletedProcess(args, 0, stdout="[]", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def git_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[3:5] == ["rev-parse", "HEAD"]:
            stdout = "local-head\n"
        elif args[3:5] == ["rev-list", "--count"]:
            stdout = "4\n"
        elif args[3:5] == ["ls-remote", "--heads"]:
            stdout = "remote-head\trefs/heads/issue/11-a2\n"
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    alive = reconcile_run_states(
        tmp_path,
        event_logger=lambda event, **payload: events.append((event, payload)),
        runner=runner,
        git_runner=git_runner,
        now=now,
    )

    assert [state.issue_id for state in alive] == [10]
    assert load_run_state(tmp_path, 10) is not None
    assert load_run_state(tmp_path, 11) is None
    assert locks == {10}
    assert labels[10] == {"worklink:in-progress"}
    assert labels[11] == {"worklink:blocked"}
    assert [call for call in calls if call[1:3] == ["locks", "release"]] == [
        ["chainlink", "locks", "release", "11"]
    ]
    event, payload = events[0]
    assert event == "worklink_run_orphaned"
    assert payload["publication_outcome"] == "determined-unpublished"
    assert payload["unpublished_commits"] is True
    assert payload["resulting_label"] == "worklink:blocked"
    assert payload["lock_released"] is True

    # The authoritative lock count, not merely a mocked call, proves that the
    # dead run returned its slot. Status also has no unrecorded disagreement.
    claims = ChainlinkClaims(agent_id="test", runner=runner)
    assert claims.active_worklink_lock_count() == 1
    concurrency = check_concurrency(tmp_path, claims=claims)
    assert (concurrency.active, concurrency.cap, concurrency.allowed) == (1, 2, True)
    rows = worklink_status(tmp_path, issue_ids=[11], runner=runner)
    assert rows[0].classification == "clean"
    assert rows[0].disagreement is None


def test_reconcile_empty_orphan_returns_ready(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    state = _state(tmp_path, 12, 999_999_998, ticks=1, started_at=now)
    checkout = tmp_path / "checkout-12"
    checkout.mkdir()
    save_run_state(tmp_path, replace(state, checkout=str(checkout), local_base="base-sha"))
    calls: list[list[str]] = []

    reconcile_run_states(
        tmp_path,
        runner=lambda args: calls.append(list(args))
        or subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
        git_runner=lambda args: subprocess.CompletedProcess(
            args,
            0,
            stdout="head\n" if args[3] == "rev-parse" else "0\n",
            stderr="",
        ),
        now=now,
    )

    assert ["chainlink", "issue", "label", "12", "worklink:ready"] in calls
    assert load_run_state(tmp_path, 12) is None


def test_reconcile_undetermined_records_outcome_and_explains_temporary_block(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    state = _state(tmp_path, 14, 999_999_996, ticks=1, started_at=now)
    checkout = tmp_path / "checkout-14"
    checkout.mkdir()
    save_run_state(tmp_path, replace(state, checkout=str(checkout), local_base="base-sha"))
    events: list[tuple[str, dict[str, object]]] = []
    calls: list[list[str]] = []

    reconcile_run_states(
        tmp_path,
        runner=lambda args: calls.append(list(args))
        or subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
        git_runner=lambda args: subprocess.CompletedProcess(
            args,
            1 if args[3] == "rev-list" else 0,
            stdout="head\n" if args[3] == "rev-parse" else "",
            stderr="comparison failed" if args[3] == "rev-list" else "",
        ),
        event_logger=lambda event, **payload: events.append((event, payload)),
        now=now,
    )

    event, payload = events[0]
    assert event == "worklink_run_orphaned"
    assert payload["publication_outcome"] == "undetermined"
    assert payload["unpublished_commits"] is None
    assert payload["resulting_label"] == "worklink:blocked"
    [comment] = [call[-1] for call in calls if call[1:3] == ["issue", "comment"]]
    assert "publication status is undetermined" in comment
    assert "pruner removes this checkout" in comment
    record = load_orphan_block_record(tmp_path, 14)
    assert record is not None and record.checkout == str(checkout)


def test_reconcile_missing_checkout_records_undetermined_and_returns_ready(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    state = _state(tmp_path, 15, 999_999_995, ticks=1, started_at=now)
    missing = tmp_path / "already-pruned-15"
    save_run_state(tmp_path, replace(state, checkout=str(missing), local_base="base-sha"))
    calls: list[list[str]] = []
    events: list[tuple[str, dict[str, object]]] = []

    reconcile_run_states(
        tmp_path,
        runner=lambda args: calls.append(list(args))
        or subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
        event_logger=lambda event, **payload: events.append((event, payload)),
        now=now,
    )

    assert events[0][1]["publication_outcome"] == "undetermined"
    assert events[0][1]["resulting_label"] == "worklink:ready"
    assert ["chainlink", "issue", "label", "15", "worklink:ready"] in calls
    assert load_orphan_block_record(tmp_path, 15) is None


def test_reconcile_then_prune_clears_only_checkout_owned_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    repo = tmp_path / "repo"
    repo.mkdir()
    checkout = tmp_path / ".worklink" / repo.name / "16-2"
    checkout.mkdir(parents=True)
    os.utime(checkout, (0, 0))
    state = _state(tmp_path, 16, 999_999_994, ticks=1, started_at=now)
    save_run_state(tmp_path, replace(state, checkout=str(checkout), local_base="base-sha"))
    labels = {"worklink:in-progress"}
    comments: list[str] = []

    def runner(args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[1:3] == ["issue", "unlabel"]:
            labels.discard(args[4])
        elif args[1:3] == ["issue", "label"]:
            labels.add(args[4])
        elif args[1:3] == ["issue", "comment"]:
            comments.append(args[4])
        elif args[1:3] == ["issue", "show"]:
            return subprocess.CompletedProcess(
                args, 0, stdout=json.dumps({"labels": sorted(labels), "comments": comments}), stderr=""
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    reconcile_run_states(
        tmp_path,
        runner=runner,
        git_runner=lambda args: subprocess.CompletedProcess(
            args,
            1 if args[3] == "rev-list" else 0,
            stdout="head\n" if args[3] == "rev-parse" else "",
            stderr="failed" if args[3] == "rev-list" else "",
        ),
        now=now,
    )
    assert labels == {"worklink:blocked"}
    monkeypatch.setattr(autonomy, "worklink_defaults", lambda home: type("D", (), {"reaper_ttl_s": 0})())

    pruned = autonomy.prune_stale_attempt_checkouts_for_home(tmp_path, repo=repo, runner=runner)

    assert pruned == [checkout]
    assert not checkout.exists()
    assert labels == {"worklink:ready"}
    assert load_orphan_block_record(tmp_path, 16) is None


def test_pruner_preserves_unpublished_checkout_and_different_block_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    repo = tmp_path / "repo"
    repo.mkdir()
    checkout = tmp_path / ".worklink" / repo.name / "17-2"
    checkout.mkdir(parents=True)
    os.utime(checkout, (0, 0))
    state = _state(tmp_path, 17, 999_999_993, ticks=1, started_at=now)
    save_run_state(tmp_path, replace(state, checkout=str(checkout), local_base="base-sha"))
    labels = {"worklink:in-progress"}
    comments: list[str] = []

    def runner(args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[1:3] == ["issue", "unlabel"]:
            labels.discard(args[4])
        elif args[1:3] == ["issue", "label"]:
            labels.add(args[4])
        elif args[1:3] == ["issue", "comment"]:
            comments.append(args[4])
        elif args[1:3] == ["issue", "show"]:
            return subprocess.CompletedProcess(
                args, 0, stdout=json.dumps({"labels": sorted(labels), "comments": comments}), stderr=""
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    reconcile_run_states(
        tmp_path,
        runner=runner,
        git_runner=lambda args: subprocess.CompletedProcess(
            args,
            0,
            stdout=(
                "head\n"
                if args[3] == "rev-parse"
                else "1\n"
                if args[3] == "rev-list"
                else "other\trefs/heads/issue/17-a2\n"
            ),
            stderr="",
        ),
        now=now,
    )
    monkeypatch.setattr(autonomy, "worklink_defaults", lambda home: type("D", (), {"reaper_ttl_s": 0})())
    assert autonomy.prune_stale_attempt_checkouts_for_home(tmp_path, repo=repo, runner=runner) == []
    assert checkout.exists()

    record = load_orphan_block_record(tmp_path, 17)
    assert record is not None
    from mimir.worklink.run_state import save_orphan_block_record

    save_orphan_block_record(tmp_path, replace(record, publication_outcome="undetermined"))
    comments.append(
        "WORKLINK_BLOCKED leaf template validation failed before dispatch; re-plan this issue."
    )
    assert autonomy.prune_stale_attempt_checkouts_for_home(tmp_path, repo=repo, runner=runner) == [checkout]
    assert labels == {"worklink:blocked"}


def test_old_prune_cannot_clear_new_attempt_block(tmp_path: Path) -> None:
    from mimir.worklink.run_state import save_orphan_block_record

    old = OrphanBlockRecord(
        issue_id=18,
        attempt=1,
        checkout=str(tmp_path / "18-1"),
        publication_outcome="undetermined",
        comment="WORKLINK_BLOCKED orphaned run issue=18 attempt=1",
    )
    new = replace(
        old,
        attempt=2,
        checkout=str(tmp_path / "18-2"),
        comment="WORKLINK_BLOCKED orphaned run issue=18 attempt=2",
    )
    save_orphan_block_record(tmp_path, new)
    calls: list[list[str]] = []

    cleared = autonomy._clear_pruned_orphan_block(
        tmp_path,
        old,
        run=lambda args: calls.append(list(args))
        or subprocess.CompletedProcess(args, 0, stdout="{}", stderr=""),
    )

    assert cleared is False
    assert calls == []
    assert load_orphan_block_record(tmp_path, 18) == new


def test_reconcile_lock_release_failure_retains_state_and_emits_actionable_event(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    state = _state(tmp_path, 13, 999_999_997, ticks=1, started_at=now)
    checkout = tmp_path / "checkout-13"
    checkout.mkdir()
    save_run_state(tmp_path, replace(state, checkout=str(checkout), local_base="base-sha"))
    events: list[tuple[str, dict[str, object]]] = []
    calls: list[list[str]] = []

    reconcile_run_states(
        tmp_path,
        runner=lambda args: calls.append(list(args))
        or subprocess.CompletedProcess(args, 1, stdout="", stderr="lock owner mismatch"),
        git_runner=lambda args: subprocess.CompletedProcess(
            args, 0, stdout="head\n" if args[3] == "rev-parse" else "0\n", stderr=""
        ),
        event_logger=lambda event, **payload: events.append((event, payload)),
        now=now,
    )

    assert calls == [["chainlink", "locks", "release", "13"]]
    assert load_run_state(tmp_path, 13) is not None
    assert events == [
        (
            "worklink_run_orphan_reconcile_failed",
            {
                "issue_id": 13,
                "attempt": 2,
                "branch": "issue/13-a2",
                "checkout": str(checkout),
                "reason": "lock_release_failed",
                "error": "lock owner mismatch",
                "state_retained": True,
            },
        )
    ]


def test_factory_stop_finds_production_record_and_cancels_verified_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mimir.worklink.control as control

    sandbox = tmp_path / "chainlink-700"
    sandbox.mkdir()
    status = parse_factory_status(
        {
            "run_id": "chainlink-700",
            "issue_key": "chainlink-700",
            "valid": True,
            "sandbox_path": str(sandbox),
            "status": "running",
            "mode": "autonomous",
            "branch": "feature/chainlink-700",
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
            run_id="chainlink-700",
            issue_id=700,
            attempt=1,
            repository="owner/repo",
            base_ref="main",
            branch="feature/chainlink-700",
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
    stopped = load_factory_record(tmp_path, "chainlink-700")
    assert stopped is not None
    assert stopped.controller_phase == "stopped"
    assert commands == [
        ["chainlink", "locks", "release", "700"],
        ["chainlink", "issue", "unlabel", "700", "worklink:in-progress"],
    ]
    assert all("factory" not in command for command in commands)


def test_factory_stop_refuses_unverified_or_reused_process_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mimir.worklink.control as control

    monkeypatch.setattr(control, "load_run_state", lambda home, issue_id: None)
    monkeypatch.setattr(control, "load_factory_records_for_issue", lambda home, issue_id: [object()])
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
