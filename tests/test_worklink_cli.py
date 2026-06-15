from __future__ import annotations

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


def test_worklink_worker_cli_dispatches_payload_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[object] = []
    payload = tmp_path / "payload.json"
    payload.write_text("{}", encoding="utf-8")

    class FakeValidation:
        status = "completed"
        review_ready = True
        reasons = ()

    import mimir.commands.worklink as worklink_cmd

    def fake_payload_from_json(data: object) -> object:
        calls.append(("payload", data))
        return {"payload": data}

    async def fake_run_worker_payload(payload_obj: object) -> FakeValidation:
        calls.append(("run", payload_obj))
        return FakeValidation()

    monkeypatch.setattr(worklink_cmd, "payload_from_json", fake_payload_from_json)
    monkeypatch.setattr(worklink_cmd, "run_worker_payload", fake_run_worker_payload)

    with pytest.raises(SystemExit) as exc:
        main(["worklink", "worker", str(payload)])

    assert exc.value.code == 0
    assert calls == [("payload", {}), ("run", {"payload": {}})]
    assert "worklink worker: completed review-ready" in capsys.readouterr().out



def test_worklink_worker_accepts_inline_payload_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[object] = []

    class FakeValidation:
        status = "blocked"
        review_ready = False
        reasons = ("needs planner",)

    import mimir.commands.worklink as worklink_cmd

    def fake_payload_from_json(data: object) -> object:
        calls.append(("payload", data))
        return {"payload": data}

    async def fake_run_worker_payload(payload_obj: object) -> FakeValidation:
        calls.append(("run", payload_obj))
        return FakeValidation()

    monkeypatch.setattr(worklink_cmd, "payload_from_json", fake_payload_from_json)
    monkeypatch.setattr(worklink_cmd, "run_worker_payload", fake_run_worker_payload)

    with pytest.raises(SystemExit) as exc:
        main(["worklink", "worker", "--payload-json", '{"spec":{"issue_id":459}}'])

    assert exc.value.code == 0
    assert calls == [("payload", {"spec": {"issue_id": 459}}), ("run", {"payload": {"spec": {"issue_id": 459}}})]
    assert "worklink worker: blocked" in capsys.readouterr().out
