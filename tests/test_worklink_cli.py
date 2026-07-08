from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from mimir.cli import main
from mimir.worklink.orchestrator import WorklinkRunResult


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
