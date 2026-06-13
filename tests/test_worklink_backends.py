from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from mimir.worklink.backends import (
    BackendRegistry,
    Caps,
    CodexBackend,
    ComputeResult,
    LocalSubprocessComputeBackend,
    RawResult,
    WorkOrder,
    WorklinkConfig,
)
from mimir.worklink.compute import ComputeCaps, WorkSpec
import mimir.worklink.backends.codex as codex_module
import mimir.worklink.compute as compute_module


class FakeProcess:
    def __init__(self, *, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


@pytest.mark.asyncio
async def test_codex_backend_invokes_exec_json_with_worktree_and_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_exec(*args: str, **kwargs: Any) -> FakeProcess:
        calls.append({"args": args, "kwargs": kwargs})
        return FakeProcess(returncode=0, stdout=b'{"event":"done"}\n', stderr=b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    backend = CodexBackend()
    transcript_root = tmp_path / "state" / "worklink" / "transcripts"
    order = WorkOrder(
        issue_id=440,
        worktree=tmp_path / "worktree",
        prompt="Do slice 1b",
        rules=None,
        timeout_s=30,
        env={"PATH": "/bin"},
        transcript_root=transcript_root,
    )

    spec = backend.work_spec(
        order,
        attempt=1,
        repo_url="git@github.com:jasoncarreira/mimir.git",
        base_ref="main",
        branch="issue/440-a1",
        test_command="echo ok",
    )
    compute = LocalSubprocessComputeBackend()
    handle = await compute.launch(spec)
    compute_result = await compute.wait(handle, spec.timeout_s)
    await compute.cleanup(handle)
    result = await backend.interpret(order, compute_result)

    assert result == RawResult(
        exit_code=0,
        transcript_path=result.transcript_path,
        backend_status="success",
        error=None,
    )
    assert calls == [
        {
            "args": ("codex", "exec", "--cd", str(order.worktree), "--json", "Do slice 1b"),
            "kwargs": {
                "stdout": asyncio.subprocess.PIPE,
                "stderr": asyncio.subprocess.PIPE,
                "cwd": str(order.worktree),
                "env": {"PATH": "/bin"},
                "start_new_session": True,
            },
        }
    ]
    assert result.transcript_path is not None
    assert result.transcript_path.parent == transcript_root
    assert not result.transcript_path.is_relative_to(order.worktree)
    transcript = json.loads(result.transcript_path.read_text())
    assert transcript["backend"] == "codex"
    assert transcript["stdout"] == '{"event":"done"}\n'


@pytest.mark.asyncio
async def test_codex_backend_maps_quota_and_auth_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fake_exec(*args: str, **kwargs: Any) -> FakeProcess:
        return FakeProcess(returncode=1, stdout=b"", stderr=b"HTTP 429 quota exhausted")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    order = WorkOrder(
        issue_id=440,
        worktree=tmp_path,
        prompt="x",
        rules=None,
        timeout_s=30,
        transcript_root=tmp_path / "transcripts",
    )
    spec = CodexBackend().work_spec(
        order,
        attempt=1,
        repo_url="repo",
        base_ref="main",
        branch="issue/440-a1",
        test_command="echo ok",
    )
    compute = LocalSubprocessComputeBackend()
    handle = await compute.launch(spec)
    compute_result = await compute.wait(handle, spec.timeout_s)
    await compute.cleanup(handle)
    result = await CodexBackend().interpret(order, compute_result)

    assert result.exit_code == 1
    assert result.backend_status == "quota_exhausted"
    assert result.error == "HTTP 429 quota exhausted"


@pytest.mark.asyncio
async def test_codex_backend_enforces_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = FakeProcess(stdout=b"partial", stderr=b"")
    killed: list[FakeProcess] = []

    async def fake_exec(*args: str, **kwargs: Any) -> FakeProcess:
        return process

    async def fake_wait_for(awaitable: Any, *, timeout: float) -> Any:
        awaitable.close()
        raise TimeoutError

    async def fake_kill_process_group(proc: FakeProcess) -> None:
        killed.append(proc)
        proc.kill()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    monkeypatch.setattr(compute_module, "_kill_process_group", fake_kill_process_group)
    order = WorkOrder(
        issue_id=440,
        worktree=tmp_path,
        prompt="x",
        rules=None,
        timeout_s=1,
        transcript_root=tmp_path / "transcripts",
    )
    spec = CodexBackend().work_spec(
        order,
        attempt=1,
        repo_url="repo",
        base_ref="main",
        branch="issue/440-a1",
        test_command="echo ok",
    )
    compute = LocalSubprocessComputeBackend()
    handle = await compute.launch(spec)
    compute_result = await compute.wait(handle, spec.timeout_s)
    await compute.cleanup(handle)
    result = await CodexBackend().interpret(order, compute_result)

    assert killed == [process]
    assert process.killed is True
    assert result.backend_status == "timeout"
    assert result.exit_code == -9
    assert result.error == "codex execution timed out: partial"
    assert result.transcript_path is not None
    assert json.loads(result.transcript_path.read_text())["timed_out"] is True


def test_codex_capabilities_declare_quota_pool_and_worktree_safety() -> None:
    assert CodexBackend().capabilities() == Caps(
        tool_category="coding-cli",
        persistent_sessions=False,
        json_output=True,
        native_pr_creation=False,
        worktree_safe=True,
        quota_pool="codex-subscription",
    )


def test_worklink_config_routes_first_match_and_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "worklink.yaml"
    config_path.write_text(
        """
defaults:
  backend: codex
  compute_backend: local_subprocess
  timeout_s: 45
  backend_by_category:
    renderer: mermaid
routes:
  - label: render
    backend: mermaid
    compute_backend: local_subprocess
  - repo: jasoncarreira/mimir
    backend: codex
backends:
  codex:
    bin: /opt/bin/codex
    args: [exec, --json, --sandbox, workspace-write]
""".strip()
    )

    config = WorklinkConfig.load(config_path)

    assert config.defaults.timeout_s == 45
    assert config.select_backend_name(labels={"render"}, repo="jasoncarreira/mimir") == "mermaid"
    assert config.select_backend_name(labels={"worklink"}, repo="jasoncarreira/mimir") == "codex"
    assert config.select_backend_name(labels={"worklink"}, repo="elsewhere/repo", tool_category="renderer") == "mermaid"
    assert config.select_backend_name(labels={"worklink"}, repo="elsewhere/repo") == "codex"
    registry = BackendRegistry(config)
    codex = registry.get("codex")
    assert isinstance(codex, CodexBackend)
    assert codex.bin == "/opt/bin/codex"
    assert codex.extra_args == ("exec", "--json", "--sandbox", "workspace-write")
    assert config.defaults.compute_backend == "local_subprocess"
    assert isinstance(registry.select_compute(), LocalSubprocessComputeBackend)


def test_codex_work_spec_includes_rules_in_local_argv(tmp_path: Path) -> None:
    backend = CodexBackend()
    spec = backend.work_spec(
        WorkOrder(issue_id=440, worktree=tmp_path, prompt="Do work", rules="Follow policy", timeout_s=30),
        attempt=1,
        repo_url="repo",
        base_ref="main",
        branch="issue/440-a1",
        test_command="echo ok",
    )

    assert spec.local_argv == (
        "codex",
        "exec",
        "--cd",
        str(tmp_path),
        "--json",
        "Follow policy\n\nDo work",
    )


def test_registry_selection_has_no_codex_specific_orchestrator_branch() -> None:
    config = WorklinkConfig(routes=())
    registry = BackendRegistry(config)

    assert registry.select(labels={"worklink"}, repo="jasoncarreira/mimir").name == "codex"


def test_codex_backend_builds_portable_git_handoff_work_spec(tmp_path: Path) -> None:
    order = WorkOrder(
        issue_id=455,
        worktree=tmp_path / "worktree",
        prompt="Do work",
        rules=None,
        timeout_s=17,
        env={"MIMIR_HOME": "/tmp/home"},
        transcript_root=tmp_path / "transcripts",
    )

    spec = CodexBackend().work_spec(
        order,
        attempt=2,
        repo_url="git@github.com:jasoncarreira/mimir.git",
        base_ref="origin/main",
        branch="issue/455-a2",
        test_command="echo ok",
    )

    assert spec == WorkSpec(
        issue_id=455,
        attempt=2,
        repo_url="git@github.com:jasoncarreira/mimir.git",
        base_ref="origin/main",
        branch="issue/455-a2",
        prompt="Do work",
        rules=None,
        test_command="echo ok",
        backend="codex",
        timeout_s=17,
        env={"MIMIR_HOME": "/tmp/home"},
        backend_config={"bin": "codex", "args": ["exec", "--json"]},
        local_worktree=order.worktree,
        local_argv=("codex", "exec", "--cd", str(order.worktree), "--json", "Do work"),
    )


@pytest.mark.asyncio
async def test_local_subprocess_compute_backend_preserves_subprocess_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_exec(*args: str, **kwargs: Any) -> FakeProcess:
        calls.append({"args": args, "kwargs": kwargs})
        return FakeProcess(returncode=0, stdout=b"ok", stderr=b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    backend = LocalSubprocessComputeBackend()

    spec = WorkSpec(
        issue_id=1,
        attempt=1,
        repo_url="repo",
        base_ref="main",
        branch="issue/1-a1",
        prompt="prompt",
        rules=None,
        test_command="echo ok",
        backend="other_tool",
        timeout_s=5,
        env={"PATH": "/custom/bin", "X": "1"},
        backend_config={"bin": "other", "args": ["ignored"]},
        local_worktree=tmp_path,
        local_argv=("tool", "arg", "--cd", str(tmp_path), "prompt"),
    )
    handle = await backend.launch(spec)
    result = await backend.wait(handle, 5)
    await backend.cleanup(handle)

    assert backend.capabilities() == ComputeCaps(
        shared_filesystem=True,
        network_isolated=False,
        handle_cancel=True,
        persistent_after_disconnect=False,
    )
    assert result == ComputeResult(
        exit_code=0,
        stdout="ok",
        stderr="",
        handle=result.handle,
        command=("tool", "arg", "--cd", str(tmp_path), "prompt"),
    )
    assert calls == [
        {
            "args": ("tool", "arg", "--cd", str(tmp_path), "prompt"),
            "kwargs": {
                "stdout": asyncio.subprocess.PIPE,
                "stderr": asyncio.subprocess.PIPE,
                "cwd": str(tmp_path),
                "env": {"PATH": "/custom/bin", "X": "1"},
                "start_new_session": True,
            },
        }
    ]


def test_codex_status_auth_detection_does_not_match_author_text() -> None:
    assert codex_module._status_from_output(1, "author: test@example.com", "") == "failed"
    assert codex_module._status_from_output(1, "", "authentication required") == "auth_error"
