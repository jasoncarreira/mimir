from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from mimir.worklink.backends import (
    BackendRegistry,
    Caps,
    ClaudeCliBackend,
    CodexBackend,
    ComputeResult,
    LocalSubprocessComputeBackend,
    RawResult,
    ToolPin,
    WorkOrder,
    WorklinkConfig,
)
from mimir.worklink.backends.base import blocked_reason_from_output
from mimir.worklink.compute import ComputeCaps, WorkSpec
from mimir.worklink.worker import WorkerPayload, payload_from_json, payload_to_json
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



@pytest.mark.asyncio
async def test_claude_cli_backend_invokes_print_json_in_worktree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_exec(*args: str, **kwargs: Any) -> FakeProcess:
        calls.append({"args": args, "kwargs": kwargs})
        return FakeProcess(returncode=0, stdout=b'{"type":"result"}\n', stderr=b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    backend = ClaudeCliBackend()
    transcript_root = tmp_path / "state" / "worklink" / "transcripts"
    order = WorkOrder(
        issue_id=445,
        worktree=tmp_path / "worktree",
        prompt="Do the backend slice",
        rules="Follow Worklink policy",
        timeout_s=30,
        env={"PATH": "/bin"},
        transcript_root=transcript_root,
    )

    spec = backend.work_spec(
        order,
        attempt=1,
        repo_url="git@github.com:jasoncarreira/mimir.git",
        base_ref="main",
        branch="issue/445-a1",
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
            "args": (
                "claude",
                "-p",
                "--output-format",
                "json",
                "Follow Worklink policy\n\nDo the backend slice",
            ),
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
    assert transcript["backend"] == "claude_cli"
    assert transcript["stdout"] == '{"type":"result"}\n'


def test_claude_cli_capabilities_declare_separate_quota_pool() -> None:
    assert ClaudeCliBackend().capabilities() == Caps(
        tool_category="coding-cli",
        persistent_sessions=False,
        json_output=True,
        native_pr_creation=False,
        worktree_safe=True,
        quota_pool="anthropic-max-plan",
    )


def test_claude_cli_work_spec_builds_portable_git_handoff(tmp_path: Path) -> None:
    order = WorkOrder(
        issue_id=445,
        worktree=tmp_path / "worktree",
        prompt="Do work",
        rules=None,
        timeout_s=17,
        env={"MIMIR_HOME": "/tmp/home"},
        transcript_root=tmp_path / "transcripts",
    )

    spec = ClaudeCliBackend().work_spec(
        order,
        attempt=2,
        repo_url="git@github.com:jasoncarreira/mimir.git",
        base_ref="origin/main",
        branch="issue/445-a2",
        test_command="echo ok",
    )

    assert spec == WorkSpec(
        issue_id=445,
        attempt=2,
        repo_url="git@github.com:jasoncarreira/mimir.git",
        base_ref="origin/main",
        branch="issue/445-a2",
        prompt="Do work",
        rules=None,
        test_command="echo ok",
        backend="claude_cli",
        timeout_s=17,
        env={"MIMIR_HOME": "/tmp/home"},
        backend_config={"bin": "claude", "args": ["-p", "--output-format", "json"]},
        local_worktree=order.worktree,
        local_argv=("claude", "-p", "--output-format", "json", "Do work"),
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
  claude_cli:
    bin: /opt/bin/claude
    args: [-p, --output-format, json, --allowedTools, Bash]
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
    claude_cli = registry.get("claude_cli")
    assert isinstance(claude_cli, ClaudeCliBackend)
    assert claude_cli.bin == "/opt/bin/claude"
    assert claude_cli.extra_args == ("-p", "--output-format", "json", "--allowedTools", "Bash")
    assert config.defaults.compute_backend == "local_subprocess"
    assert isinstance(registry.select_compute(), LocalSubprocessComputeBackend)

    assert config.tool_pins == ()


def test_worklink_config_parses_tool_pins(tmp_path: Path) -> None:
    config_path = tmp_path / "worklink.yaml"
    config_path.write_text(
        """
tool_pins:
  - name: codex
    category: coding-cli
    pin: "0.137.0"
    smoke: "codex --version"
    source: npm
    package: "@openai/codex"
    install: "scaffold Dockerfiles"
    risk: "high"
  - name: chainlink
    category: tracker
    pin: "chainlink-1.6.0"
    smoke: "chainlink --help"
    source: github-release
    repo: dollspace-gay/chainlink
""".strip()
    )

    config = WorklinkConfig.load(config_path)

    assert config.tool_pins == (
        ToolPin(
            name="codex",
            category="coding-cli",
            pin="0.137.0",
            smoke="codex --version",
            source="npm",
            package="@openai/codex",
            install="scaffold Dockerfiles",
            risk="high",
        ),
        ToolPin(
            name="chainlink",
            category="tracker",
            pin="chainlink-1.6.0",
            smoke="chainlink --help",
            source="github-release",
            repo="dollspace-gay/chainlink",
        ),
    )


def test_worklink_config_allows_missing_or_empty_tool_pins(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing.yaml"
    missing_path.write_text("defaults:\n  backend: codex\n")
    empty_path = tmp_path / "empty.yaml"
    empty_path.write_text("tool_pins: []\n")
    null_path = tmp_path / "null.yaml"
    null_path.write_text("tool_pins:\n")

    assert WorklinkConfig.load(missing_path).tool_pins == ()
    assert WorklinkConfig.load(empty_path).tool_pins == ()
    assert WorklinkConfig.load(null_path).tool_pins == ()


def test_worklink_config_rejects_invalid_tool_pins(tmp_path: Path) -> None:
    config_path = tmp_path / "worklink.yaml"
    config_path.write_text(
        """
tool_pins:
  - name: codex
    category: coding-cli
    pin: "0.137.0"
""".strip()
    )

    with pytest.raises(ValueError, match=r"worklink tool_pins\[0\] missing required field"):
        WorklinkConfig.load(config_path)

    config_path.write_text("tool_pins: not-a-list\n")
    with pytest.raises(ValueError, match="worklink tool_pins must be a list"):
        WorklinkConfig.load(config_path)



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
    assert registry.get("claude_cli").name == "claude_cli"


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




def test_coding_backends_parse_structured_worklink_blocked_marker(tmp_path: Path) -> None:
    result = ComputeResult(
        exit_code=1,
        stdout="I cannot implement this safely.\nWORKLINK_BLOCKED: design requires raw docker.sock access",
        stderr="",
        command=("codex",),
    )
    order = WorkOrder(
        issue_id=466,
        worktree=tmp_path,
        prompt="do it",
        rules=None,
        timeout_s=30,
        transcript_root=tmp_path / "transcripts",
    )

    codex_raw = asyncio.run(CodexBackend().interpret(order, result))
    claude_raw = asyncio.run(ClaudeCliBackend().interpret(order, result))

    assert codex_raw.backend_status == "blocked"
    assert codex_raw.blocked_reason == "design requires raw docker.sock access"
    assert codex_raw.error == "design requires raw docker.sock access"
    assert claude_raw.backend_status == "blocked"
    assert claude_raw.blocked_reason == "design requires raw docker.sock access"


def test_blocked_reason_from_output_takes_final_marker_not_prompt_echo() -> None:
    # No marker → no block.
    assert blocked_reason_from_output("did the work\n", "") is None
    # A bare marker is parsed.
    assert blocked_reason_from_output("WORKLINK_BLOCKED: real reason", "") == "real reason"
    # Whitespace-only reason is not a signal.
    assert blocked_reason_from_output("WORKLINK_BLOCKED:    \n", "") is None
    # A backend that echoes the prompt's marker line early must not override the
    # real, final decision: the last matching line wins.
    echoed = (
        "WORKLINK_BLOCKED: <one-line reason>\n"  # echoed instruction placeholder
        "...did some analysis...\n"
        "WORKLINK_BLOCKED: acceptance criteria contradict #438\n"
    )
    assert blocked_reason_from_output(echoed, "") == "acceptance criteria contradict #438"


def test_codex_status_auth_detection_does_not_match_author_text() -> None:
    assert codex_module._status_from_output(1, "author: test@example.com", "") == "failed"
    assert codex_module._status_from_output(1, "", "authentication required") == "auth_error"


def test_worker_payload_round_trips_portable_work_spec(tmp_path: Path) -> None:
    spec = WorkSpec(
        issue_id=456,
        attempt=2,
        repo_url="git@github.com:jasoncarreira/mimir.git",
        base_ref="origin/main",
        branch="issue/456-a2",
        prompt="Do the worker slice",
        rules="Follow policy",
        test_command="echo ok",
        backend="codex",
        timeout_s=30,
        creds_ref={"github": "worklink-github"},
        env={"MIMIR_HOME": "/home/worklink"},
        backend_config={"bin": "codex", "args": ["exec", "--json"]},
        local_worktree=tmp_path / "ignored-local",
        local_argv=("codex", "exec", "--cd", str(tmp_path / "ignored-local"), "--json", "Do the worker slice"),
    )
    payload = WorkerPayload(
        spec=spec,
        repo_dir=tmp_path / "repo",
        evidence_path=tmp_path / "evidence.json",
        transcript_root=tmp_path / "transcripts",
        safe_env={"PATH": "/bin"},
    )

    restored = payload_from_json(payload_to_json(payload))

    assert restored.spec == spec
    assert restored.repo_dir == (tmp_path / "repo").resolve()
    assert restored.evidence_path == (tmp_path / "evidence.json").resolve()
    assert restored.transcript_root == (tmp_path / "transcripts").resolve()
    assert restored.safe_env == {"PATH": "/bin"}
