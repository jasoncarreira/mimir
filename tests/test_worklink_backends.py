from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from mimir.worklink.backends import BackendRegistry, Caps, CodexBackend, RawResult, WorkOrder, WorklinkConfig
import mimir.worklink.backends.codex as codex_module


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
async def test_codex_backend_invokes_exec_json_with_worktree_and_prompt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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

    result = await backend.run(order)

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
async def test_codex_backend_maps_quota_and_auth_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def fake_exec(*args: str, **kwargs: Any) -> FakeProcess:
        return FakeProcess(returncode=1, stdout=b"", stderr=b"HTTP 429 quota exhausted")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await CodexBackend().run(
        WorkOrder(
            issue_id=440,
            worktree=tmp_path,
            prompt="x",
            rules=None,
            timeout_s=30,
            transcript_root=tmp_path / "transcripts",
        )
    )

    assert result.exit_code == 1
    assert result.backend_status == "quota_exhausted"
    assert result.error == "HTTP 429 quota exhausted"


@pytest.mark.asyncio
async def test_codex_backend_enforces_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
    monkeypatch.setattr(codex_module, "_kill_process_group", fake_kill_process_group)

    result = await CodexBackend().run(
        WorkOrder(
            issue_id=440,
            worktree=tmp_path,
            prompt="x",
            rules=None,
            timeout_s=1,
            transcript_root=tmp_path / "transcripts",
        )
    )

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
  timeout_s: 45
  backend_by_category:
    renderer: mermaid
routes:
  - label: render
    backend: mermaid
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


def test_codex_command_includes_rules_in_prompt(tmp_path: Path) -> None:
    backend = CodexBackend()
    command = backend._command(
        WorkOrder(issue_id=440, worktree=tmp_path, prompt="Do work", rules="Follow policy", timeout_s=30)
    )

    assert command == ["codex", "exec", "--cd", str(tmp_path), "--json", "Follow policy\n\nDo work"]


def test_registry_selection_has_no_codex_specific_orchestrator_branch() -> None:
    config = WorklinkConfig(
        routes=(),
    )
    registry = BackendRegistry(config)

    assert registry.select(labels={"worklink"}, repo="jasoncarreira/mimir").name == "codex"


def test_codex_status_auth_detection_does_not_match_author_text() -> None:
    assert codex_module._status_from_output(1, "author: test@example.com", "") == "failed"
    assert codex_module._status_from_output(1, "", "authentication required") == "auth_error"
