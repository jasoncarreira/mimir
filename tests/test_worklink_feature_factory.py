"""Tests for feature-factory backend (chainlink #833)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mimir.worklink.backends.feature_factory import (
    FactoryRunState,
    FeatureFactoryBackend,
    gate_answer_path,
    has_concurrent_factory_session,
    read_factory_run_state,
    read_gate_answer,
    write_gate_answer,
    SCHEMA_VERSION,
)


def test_factory_run_state_schema_version() -> None:
    assert SCHEMA_VERSION == 1


def test_factory_run_state_is_terminal() -> None:
    assert FactoryRunState(
        schema_version=1,
        heartbeat_at=datetime.now(UTC).isoformat(),
        status="completed",
    ).is_terminal

    assert FactoryRunState(
        schema_version=1,
        heartbeat_at=datetime.now(UTC).isoformat(),
        status="failed",
    ).is_terminal

    assert not FactoryRunState(
        schema_version=1,
        heartbeat_at=datetime.now(UTC).isoformat(),
        status="in_progress",
    ).is_terminal


def test_factory_run_state_is_stale() -> None:
    now = datetime.now(UTC)

    assert FactoryRunState(
        schema_version=1,
        heartbeat_at=(now - timedelta(seconds=400)).isoformat(),
        status="in_progress",
    ).is_stale

    assert not FactoryRunState(
        schema_version=1,
        heartbeat_at=now.isoformat(),
        status="in_progress",
    ).is_stale


def test_read_factory_run_state_missing_dir(tmp_path: Path) -> None:
    state = read_factory_run_state(tmp_path)
    assert state is None


def test_read_factory_run_state_valid(tmp_path: Path) -> None:
    factory_dir = tmp_path / ".opencode" / "factory"
    factory_dir.mkdir(parents=True)
    run_json = factory_dir / "run.json"
    now = datetime.now(UTC).isoformat()
    run_json.write_text(
        json.dumps({
            "schema_version": SCHEMA_VERSION,
            "heartbeat_at": now,
            "status": "in_progress",
            "pr_url": None,
            "gates_needed": ["test"],
            "gates_complete": [],
            "error": None,
        }),
        encoding="utf-8",
    )

    state = read_factory_run_state(tmp_path)
    assert state is not None
    assert state.schema_version == SCHEMA_VERSION
    assert state.status == "in_progress"
    assert state.gates_needed == ("test",)


def test_read_factory_run_state_invalid_schema(tmp_path: Path) -> None:
    factory_dir = tmp_path / ".opencode" / "factory"
    factory_dir.mkdir(parents=True)
    run_json = factory_dir / "run.json"
    run_json.write_text(
        json.dumps({
            "schema_version": 999,
            "heartbeat_at": datetime.now(UTC).isoformat(),
            "status": "in_progress",
        }),
        encoding="utf-8",
    )

    state = read_factory_run_state(tmp_path)
    assert state is None


def test_read_factory_run_state_missing_heartbeat(tmp_path: Path) -> None:
    factory_dir = tmp_path / ".opencode" / "factory"
    factory_dir.mkdir(parents=True)
    run_json = factory_dir / "run.json"
    run_json.write_text(
        json.dumps({
            "schema_version": SCHEMA_VERSION,
            "status": "in_progress",
        }),
        encoding="utf-8",
    )

    state = read_factory_run_state(tmp_path)
    assert state is None


def test_has_concurrent_factory_session_no_session(tmp_path: Path) -> None:
    assert not has_concurrent_factory_session(tmp_path)


def test_has_concurrent_factory_session_terminal(tmp_path: Path) -> None:
    factory_dir = tmp_path / ".opencode" / "factory"
    factory_dir.mkdir(parents=True)
    run_json = factory_dir / "run.json"
    run_json.write_text(
        json.dumps({
            "schema_version": SCHEMA_VERSION,
            "heartbeat_at": datetime.now(UTC).isoformat(),
            "status": "completed",
        }),
        encoding="utf-8",
    )

    assert not has_concurrent_factory_session(tmp_path)


def test_has_concurrent_factory_session_stale(tmp_path: Path) -> None:
    factory_dir = tmp_path / ".opencode" / "factory"
    factory_dir.mkdir(parents=True)
    run_json = factory_dir / "run.json"
    run_json.write_text(
        json.dumps({
            "schema_version": SCHEMA_VERSION,
            "heartbeat_at": (datetime.now(UTC) - timedelta(seconds=400)).isoformat(),
            "status": "in_progress",
        }),
        encoding="utf-8",
    )

    assert not has_concurrent_factory_session(tmp_path)


def test_has_concurrent_factory_session_active(tmp_path: Path) -> None:
    factory_dir = tmp_path / ".opencode" / "factory"
    factory_dir.mkdir(parents=True)
    run_json = factory_dir / "run.json"
    run_json.write_text(
        json.dumps({
            "schema_version": SCHEMA_VERSION,
            "heartbeat_at": datetime.now(UTC).isoformat(),
            "status": "in_progress",
        }),
        encoding="utf-8",
    )

    assert has_concurrent_factory_session(tmp_path)


def test_gate_answer_path(tmp_path: Path) -> None:
    path = gate_answer_path(tmp_path, "test-gate")
    assert path == tmp_path / ".opencode" / "factory" / "gates" / "test-gate.answer"


def test_read_gate_answer_missing(tmp_path: Path) -> None:
    answer = read_gate_answer(tmp_path, "nonexistent")
    assert answer is None


def test_write_and_read_gate_answer(tmp_path: Path) -> None:
    write_gate_answer(tmp_path, "test-gate", "approved")
    answer = read_gate_answer(tmp_path, "test-gate")
    assert answer == "approved"


def test_feature_factory_backend_capabilities() -> None:
    backend = FeatureFactoryBackend()
    caps = backend.capabilities()
    assert caps.tool_category == "feature-factory"
    assert caps.persistent_sessions is True
    assert caps.worktree_safe is False
