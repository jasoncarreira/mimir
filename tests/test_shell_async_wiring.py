"""181-K regression: ``bash_async`` / ``bash_jobs_list`` / ``bash_job_output``.

The SDK build had three async-shell tools backed by ``ShellJobRegistry``
for long-running subprocesses (CI waits, webhook listeners, multi-
hour climbs). The deepagents cutover dropped the @mcp_tool registrations
without re-wiring them as @tool callables — only the synchronous
``shell_exec`` survived, which blocks the dispatcher for the entire
subprocess lifetime.

181-K ports the three back as native langchain ``@tool``s in
``mimir/tools/shell_async.py``, wires them in via
``set_shell_job_registry(...)`` from ``server.py``, and restores
``Agent._handle_shell_job_complete`` / ``_on_shell_job_complete``
so the spawning channel wakes when a job exits.

These tests exercise the surface end-to-end with a real
``ShellJobRegistry`` (no subprocess spawn — we monkey-patch
``ShellJobRegistry.spawn`` to a controllable stand-in) so we cover
the wiring without taking on subprocess flakiness in CI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mimir.shell_jobs import ShellJobRegistry
from mimir.tools import shell_async


class _FakeJob:
    """Stand-in for the ShellJob dataclass — just enough fields for
    the tool surface + completion-bridge tests."""

    def __init__(
        self,
        job_id: str = "job-1",
        pid: int = 12345,
        command: str = "echo hi",
        channel_id: str | None = "ch-1",
        status: str = "running",
        exit_code: int | None = None,
    ) -> None:
        self.job_id = job_id
        self.pid = pid
        self.command = command
        self.channel_id = channel_id
        self.status = status
        self.exit_code = exit_code
        self.elapsed_seconds = 1.5


@pytest.fixture
def fake_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ShellJobRegistry:
    """Wire a real ShellJobRegistry into ``shell_async`` but patch
    ``spawn`` to return a deterministic _FakeJob without touching the OS."""
    reg = ShellJobRegistry(jobs_dir=tmp_path / "jobs")
    spawned: list[dict] = []

    def _fake_spawn(command: str, *, argv: list[str], channel_id: str | None, on_complete=None) -> _FakeJob:
        spawned.append({"command": command, "argv": argv, "channel_id": channel_id})
        return _FakeJob(command=command, channel_id=channel_id)

    monkeypatch.setattr(reg, "spawn", _fake_spawn)
    monkeypatch.setattr(reg, "_spawned_log", spawned, raising=False)
    shell_async.set_shell_job_registry(reg, on_complete=None)
    yield reg
    shell_async.set_shell_job_registry(None, on_complete=None)  # type: ignore[arg-type]


# ─── bash_async ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bash_async_spawns_and_returns_job_id(fake_registry: ShellJobRegistry) -> None:
    out = await shell_async.bash_async.ainvoke({"command": "sleep 5"})
    assert "Spawned job" in out
    assert "job-1" in out
    # Verify the spawn was routed via ``bash -lc``.
    assert fake_registry._spawned_log[0]["argv"] == ["bash", "-lc", "sleep 5"]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_bash_async_rejects_empty_command(fake_registry: ShellJobRegistry) -> None:
    out = await shell_async.bash_async.ainvoke({"command": "  "})
    assert "command is required" in out


@pytest.mark.asyncio
async def test_bash_async_no_registry_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    shell_async.set_shell_job_registry(None, on_complete=None)  # type: ignore[arg-type]
    out = await shell_async.bash_async.ainvoke({"command": "echo hi"})
    assert "no shell-job registry" in out


# ─── bash_jobs_list ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bash_jobs_list_empty_scope(fake_registry: ShellJobRegistry) -> None:
    out = await shell_async.bash_jobs_list.ainvoke({})
    assert "No jobs" in out


@pytest.mark.asyncio
async def test_bash_jobs_list_invalid_scope(fake_registry: ShellJobRegistry) -> None:
    out = await shell_async.bash_jobs_list.ainvoke({"scope": "garbage"})
    assert "bash_jobs_list failed" in out


# ─── bash_job_output ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bash_job_output_requires_job_id(fake_registry: ShellJobRegistry) -> None:
    out = await shell_async.bash_job_output.ainvoke({"job_id": ""})
    assert "job_id is required" in out


@pytest.mark.asyncio
async def test_bash_job_output_unknown_job_propagates_error(
    fake_registry: ShellJobRegistry, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _err_read(job_id: str, *, tail_lines: int, stream: str) -> dict:
        return {"error": f"unknown job {job_id}"}

    monkeypatch.setattr(fake_registry, "read_output", _err_read)
    out = await shell_async.bash_job_output.ainvoke({"job_id": "missing-job"})
    assert "unknown job missing-job" in out


@pytest.mark.asyncio
async def test_bash_job_output_renders_tail(
    fake_registry: ShellJobRegistry, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify both stdout + stderr blocks surface when stream=both."""

    def _ok_read(job_id: str, *, tail_lines: int, stream: str) -> dict:
        return {
            "job_id": job_id,
            "status": "complete",
            "elapsed_seconds": 4.2,
            "pid": 99,
            "exit_code": 0,
            "command": "echo hi",
            "stdout_tail": "line1\nline2",
            "stderr_tail": "warning1",
        }

    monkeypatch.setattr(fake_registry, "read_output", _ok_read)
    out = await shell_async.bash_job_output.ainvoke({"job_id": "job-1"})
    assert "--- stdout tail ---" in out
    assert "line1" in out
    assert "--- stderr tail ---" in out
    assert "warning1" in out
    assert "exit_code=0" in out


# ─── Registry tool list inclusion ─────────────────────────────────


def test_all_mimir_tools_includes_shell_async_trio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The three async-shell tools must be unconditionally present in
    ``all_mimir_tools()`` — they're orthogonal to the provider /
    Tavily / MCP gates that other tools use."""
    from mimir.tools import all_mimir_tools

    monkeypatch.setenv("MIMIR_MODEL_SPEC", "claude-code:foo")
    names = {t.name for t in all_mimir_tools()}
    assert {"bash_async", "bash_jobs_list", "bash_job_output"}.issubset(names)
