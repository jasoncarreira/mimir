"""Controller source identity in image and editable Cultivate deployments."""

from __future__ import annotations

import logging
import subprocess

import pytest

from mimir_cultivate import server
from mimir_cultivate.worklink import worker_client


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded,expected_status", [
    ("a" * 40, "matched"),
    ("b" * 40, "mismatched"),
    ("invalid-commit", "unknown"),
])
async def test_recorded_controller_commit_overrides_project_head(
    tmp_path, monkeypatch, caplog, recorded, expected_status,
) -> None:
    monkeypatch.setattr(server.sys, "prefix", str(tmp_path))
    monkeypatch.setenv("WORKLINK_REPO", "/workspace/worklink/base/trellis")
    (tmp_path / "controller-source-commit").write_text(recorded + "\n")

    def unexpected_git(*args, **kwargs):
        pytest.fail("WORKLINK_REPO must not be read when the controller marker exists")

    monkeypatch.setattr(server.subprocess, "run", unexpected_git)
    socket = tmp_path / "executor.sock"
    socket.touch()
    monkeypatch.setattr(worker_client, "DEFAULT_EXECUTOR_SOCKET", socket)

    async def identity() -> str:
        return "a" * 40

    monkeypatch.setattr(worker_client, "verify_executor_identity", identity)
    app = {"check_worker_executor_health": True}
    await server.capture_controller_source_commit(app)
    with caplog.at_level(logging.WARNING, logger=server.log.name):
        health = await server.executor_health(app)

    assert app["worklink_controller_source_commit"] == (
        None if expected_status == "unknown" else recorded
    )
    assert health["worklink_controller_source_commit"] == app["worklink_controller_source_commit"]
    assert health["worklink_executor_source_status"] == expected_status
    assert ("Worklink executor source drift" in caplog.text) is (expected_status == "mismatched")


@pytest.mark.asyncio
async def test_absent_controller_marker_uses_repo_head(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(server.sys, "prefix", str(tmp_path))
    monkeypatch.setenv("WORKLINK_REPO", str(tmp_path / "project"))
    head = "c" * 40
    calls = []

    def read_head(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=head + "\n")

    monkeypatch.setattr(server.subprocess, "run", read_head)
    app = {"check_worker_executor_health": True}
    await server.capture_controller_source_commit(app)

    assert calls == [["git", "-C", str(tmp_path / "project"), "rev-parse", "--verify", "HEAD"]]
    assert app["worklink_controller_source_commit"] == head
