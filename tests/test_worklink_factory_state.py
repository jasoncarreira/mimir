from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from mimir.worklink.backends.feature_factory import parse_factory_status
from mimir.worklink.compute import LaunchHandle
from mimir.worklink.factory_state import (
    FactoryRecordError,
    FactoryRunRecord,
    clear_factory_record,
    list_factory_records,
    load_factory_record,
    save_factory_record,
)


def record(tmp_path: Path) -> FactoryRunRecord:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir(exist_ok=True)
    status = parse_factory_status(
        {
            "run_id": "1551",
            "issue_key": "1551",
            "valid": True,
            "sandbox_path": str(sandbox),
            "status": "running",
            "mode": "autonomous",
            "branch": "epic/1551",
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
    return FactoryRunRecord(
        run_id="1551",
        issue_id=1551,
        attempt=1,
        repository="owner/repo",
        base_ref="main",
        branch="epic/1551",
        launcher="/opt/mimir-opencode/lib/node_modules/feature-factory/bin/factory.js",
        sandbox=str(sandbox),
        session="session-1",
        handle=LaunchHandle("local_subprocess", "123", 456),
        status=status,
        observed_at="2026-08-18T12:00:00+00:00",
        controller_phase="running",
    )


def test_factory_record_round_trip_is_atomic_and_has_no_cost_fields(tmp_path: Path) -> None:
    expected = record(tmp_path)
    path = save_factory_record(tmp_path, expected)
    assert path == tmp_path / "state" / "worklink" / "factory-runs" / "1551.json"
    assert load_factory_record(tmp_path, "1551") == expected
    assert list_factory_records(tmp_path) == [expected]
    assert "cost" not in path.read_text(encoding="utf-8")
    assert not list(path.parent.glob("*.tmp"))


def test_factory_record_rejects_identity_and_sandbox_mismatch(tmp_path: Path) -> None:
    expected = record(tmp_path)
    with pytest.raises(FactoryRecordError, match="identity"):
        replace(expected, issue_id=1552)
    with pytest.raises(FactoryRecordError, match="sandbox mismatch"):
        replace(expected, sandbox=str(tmp_path / "different"))


def test_factory_record_rejects_symlink_and_non_numeric_names(tmp_path: Path) -> None:
    expected = record(tmp_path)
    path = save_factory_record(tmp_path, expected)
    path.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    path.symlink_to(outside)
    with pytest.raises(FactoryRecordError, match="regular"):
        load_factory_record(tmp_path, "1551")
    with pytest.raises(FactoryRecordError, match="positive decimal"):
        load_factory_record(tmp_path, "chainlink-1551")


def test_clear_factory_record_refuses_symlink(tmp_path: Path) -> None:
    expected = record(tmp_path)
    path = save_factory_record(tmp_path, expected)
    path.unlink()
    target = tmp_path / "target"
    target.write_text("keep", encoding="utf-8")
    path.symlink_to(target)
    with pytest.raises(FactoryRecordError, match="non-regular"):
        clear_factory_record(tmp_path, "1551")
    assert target.read_text(encoding="utf-8") == "keep"
