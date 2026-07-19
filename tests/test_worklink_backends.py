from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from mimir.worklink.backends import (
    WORKLINK_MERGED_LABEL,
    BackendRegistry,
    Caps,
    ClaudeCliBackend,
    CodexBackend,
    OpenCodeBackend,
    ComputeResult,
    LocalSubprocessComputeBackend,
    RawResult,
    TieredReviewConfig,
    ToolPin,
    WorkOrder,
    WorklinkConfig,
    WorklinkDefaults,
)
from mimir.worklink.backends.base import blocked_reason_from_output
from mimir.worklink.compute import ComputeCaps, ComputeLaunchError, LaunchHandle, WorkSpec
import mimir.worklink.backends.codex as codex_module
import mimir.worklink.compute as compute_module


class FakeProcess:
    def __init__(self, *, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.killed = False
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0)
        return self.returncode

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
    monkeypatch.setattr("mimir.worklink.compute._local_child_env", dict)
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
            "args": ("codex", "exec", "-C", str(order.worktree), "--json", "Do slice 1b"),
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
    monkeypatch.setattr("mimir.worklink.compute._local_child_env", dict)
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
    process = FakeProcess(returncode=None, stdout=b"partial", stderr=b"")
    killed: list[FakeProcess] = []

    async def fake_exec(*args: str, **kwargs: Any) -> FakeProcess:
        return process

    async def fake_kill_process_group(proc: FakeProcess) -> None:
        killed.append(proc)
        proc.kill()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr("mimir.worklink.compute._local_child_env", dict)
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
    compute_result = await compute.wait(handle, 0.01)
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
    monkeypatch.setattr("mimir.worklink.compute._local_child_env", dict)
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
    assert (
        config.select_backend_name(
            labels={"worklink"}, repo="elsewhere/repo", tool_category="renderer"
        )
        == "mermaid"
    )
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
    # Unset base_branch falls back to the built-in default.
    assert config.defaults.base_branch == "main"

    assert config.tool_pins == ()


def test_worklink_config_malformed_autonomy_ints_fall_back(tmp_path: Path) -> None:
    config_path = tmp_path / "worklink.yaml"
    config_path.write_text(
        """
defaults:
  max_concurrent: definitely-not-an-int
  reaper_ttl_s: -5
""",
        encoding="utf-8",
    )
    defaults = WorklinkConfig.load(config_path).defaults
    assert defaults.max_concurrent == 2
    assert defaults.reaper_ttl_s == 7200


def test_worklink_config_epic_defaults_and_merged_label_constant(tmp_path: Path) -> None:
    old_config = tmp_path / "worklink.yaml"
    old_config.write_text(
        """
defaults:
  backend: claude_cli
  timeout_s: 60
""".strip(),
        encoding="utf-8",
    )

    defaults = WorklinkConfig.load(old_config).defaults

    assert defaults.epic_branch_prefix == "epic/"
    assert defaults.max_review_retries == 3
    assert defaults.max_claim_attempts == 3
    assert defaults.reviewer_backend == "claude_cli"
    assert defaults.tiered_review.multi_vote_reviewer_count == 3
    assert "**/migrations/**" in defaults.tiered_review.high_risk_scope_patterns
    assert "**/*secret*" in defaults.tiered_review.high_risk_scope_patterns
    assert "auth" in defaults.tiered_review.high_risk_labels
    assert "generated-code" in defaults.tiered_review.high_risk_labels
    assert "*.lock" in defaults.tiered_review.high_risk_scope_patterns
    assert all("mimir/" not in pattern for pattern in defaults.tiered_review.high_risk_scope_patterns)
    assert WorklinkDefaults(backend="claude_cli").reviewer_backend == "claude_cli"
    assert WORKLINK_MERGED_LABEL == "worklink:merged"


def test_worklink_config_epic_overrides_and_tiered_review_parse(tmp_path: Path) -> None:
    config_path = tmp_path / "worklink.yaml"
    config_path.write_text(
        """
defaults:
  backend: codex
  epic_branch_prefix: stacked/
  max_review_retries: 5
  max_claim_attempts: 10
  reviewer_backend: claude_cli
  tiered_review:
    high_risk_scope_patterns:
      - "**/security/**"
      - "**/migrations/prod/**"
    high_risk_labels:
      - risk:high
      - production-data
    multi_vote_reviewer_count: 4
""".strip(),
        encoding="utf-8",
    )

    defaults = WorklinkConfig.load(config_path).defaults

    assert defaults.epic_branch_prefix == "stacked/"
    assert defaults.max_review_retries == 5
    assert defaults.max_claim_attempts == 10
    assert defaults.reviewer_backend == "claude_cli"
    assert defaults.tiered_review == TieredReviewConfig(
        high_risk_scope_patterns=("**/security/**", "**/migrations/prod/**"),
        high_risk_labels=("risk:high", "production-data"),
        multi_vote_reviewer_count=4,
    )


def test_worklink_config_builds_local_subprocess_compute_backend(tmp_path: Path) -> None:
    """chainlink #832: local_subprocess is the only built-in compute backend."""
    config_path = tmp_path / "worklink.yaml"
    config_path.write_text(
        """
defaults:
  backend: codex
  compute_backend: local-subprocess
routes:
  - label: docs
    backend: claude_cli
""".strip()
    )

    config = WorklinkConfig.load(config_path)
    registry = BackendRegistry(config)

    assert config.defaults.compute_backend == "local_subprocess"
    backend = registry.select_compute(labels={"worklink"})
    assert isinstance(backend, LocalSubprocessComputeBackend)
    assert backend.name == "local_subprocess"
    assert backend.capabilities() == ComputeCaps(
        shared_filesystem=True,
        network_isolated=False,
        handle_cancel=True,
        persistent_after_disconnect=False,
    )


def test_worklink_config_rejects_retired_docker_sibling_compute_backend(tmp_path: Path) -> None:
    """chainlink #832: docker_sibling was retired. An old config stanza must
    fail clean — registry rejects unknown compute_backend names — instead of
    silently rebuilding it from the docker_sibling/ecs_runtask paths."""
    config_path = tmp_path / "worklink.yaml"
    config_path.write_text(
        """
defaults:
  compute_backend: docker-sibling
compute_backends:
  docker-sibling:
    broker_url: "unix:///run/worklink-broker.sock"
    image: mimirbot-mimirbot
""".strip()
    )
    with pytest.raises(ValueError, match="unknown Worklink compute backend config: docker_sibling"):
        BackendRegistry(WorklinkConfig.load(config_path))

    # select_compute() with no compute_backends block returns the built-in
    # local_subprocess; with an unknown one selected it must fail clean.
    config_path.write_text("defaults:\n  compute_backend: docker_sibling\n")
    with pytest.raises(KeyError, match="unknown Worklink compute backend"):
        BackendRegistry(WorklinkConfig.load(config_path)).select_compute()


def test_worklink_config_rejects_retired_ecs_runtask_compute_backend(tmp_path: Path) -> None:
    """chainlink #832: ecs_runtask was retired alongside docker_sibling."""
    config_path = tmp_path / "worklink.yaml"
    config_path.write_text(
        """
defaults:
  compute_backend: ecs-runtask
compute_backends:
  ecs-runtask:
    cluster: worklink
    task_definition: worklink-worker
    container_name: worker
    subnets: [subnet-a]
""".strip()
    )
    with pytest.raises(ValueError, match="unknown Worklink compute backend config: ecs_runtask"):
        BackendRegistry(WorklinkConfig.load(config_path))

    config_path.write_text("defaults:\n  compute_backend: ecs_runtask\n")
    with pytest.raises(KeyError, match="unknown Worklink compute backend"):
        BackendRegistry(WorklinkConfig.load(config_path)).select_compute()


def test_worklink_config_rejects_local_subprocess_with_settings(tmp_path: Path) -> None:
    """local_subprocess is the only built-in compute backend and it does not
    accept any operator-supplied settings (chainlink #832)."""
    config_path = tmp_path / "worklink.yaml"
    config_path.write_text(
        """
compute_backends:
  local_subprocess:
    something: unexpected
""".strip()
    )
    with pytest.raises(
        ValueError,
        match="worklink local-subprocess compute backend does not accept settings",
    ):
        BackendRegistry(WorklinkConfig.load(config_path))


def test_worklink_config_loads_base_branch(tmp_path: Path) -> None:
    config_path = tmp_path / "worklink.yaml"
    config_path.write_text("defaults:\n  base_branch: integration/worklink\n")

    config = WorklinkConfig.load(config_path)

    assert config.defaults.base_branch == "integration/worklink"

    # Absent file and absent key both default to main.
    assert WorklinkConfig.load(tmp_path / "missing.yaml").defaults.base_branch == "main"
    (tmp_path / "nobase.yaml").write_text("defaults:\n  backend: codex\n")
    assert WorklinkConfig.load(tmp_path / "nobase.yaml").defaults.base_branch == "main"


def test_worklink_config_parses_tool_pins(tmp_path: Path) -> None:
    config_path = tmp_path / "worklink.yaml"
    config_path.write_text(
        """
tool_pins:
  - name: codex
    category: coding-cli
    pin: "0.139.0"
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
            pin="0.139.0",
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
    pin: "0.139.0"
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
        "-C",
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
        local_argv=("codex", "exec", "-C", str(order.worktree), "--json", "Do work"),
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
    monkeypatch.setattr("mimir.worklink.compute._local_child_env", dict)
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


@pytest.mark.asyncio
async def test_local_subprocess_compute_caps_output_and_kills_on_overflow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    process = FakeProcess(returncode=None, stdout=b"abcdefgh", stderr=b"err")

    async def fake_exec(*args: str, **kwargs: Any) -> FakeProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr("mimir.worklink.compute._local_child_env", dict)
    monkeypatch.setenv("MIMIR_WORKLINK_MAX_STDOUT_BYTES", "4")
    backend = LocalSubprocessComputeBackend()
    spec = WorkSpec(
        issue_id=1,
        attempt=1,
        repo_url="repo",
        base_ref="main",
        branch="issue/1-a1",
        prompt="prompt",
        rules=None,
        test_command="true",
        backend="opencode",
        timeout_s=5,
        local_worktree=tmp_path,
        local_argv=("opencode", "run"),
    )

    handle = await backend.launch(spec)
    result = await backend.wait(handle, 5)

    assert process.killed is True
    assert result.stdout == "abcd"
    assert result.stderr == "err"
    assert result.output_overflow is True

    order = WorkOrder(
        issue_id=1,
        worktree=tmp_path,
        prompt="prompt",
        rules=None,
        timeout_s=5,
        transcript_root=tmp_path / "transcripts",
    )
    raw = await OpenCodeBackend().interpret(order, result)
    assert raw.backend_status == "output_overflow"
    assert raw.output_overflow is True
    assert raw.error == "backend output exceeded configured Worklink limit"
    assert raw.transcript_path is not None
    transcript = json.loads(raw.transcript_path.read_text(encoding="utf-8"))
    assert transcript["stdout"] == "abcd"
    assert transcript["output_overflow"] is True


def test_worklink_output_limits_use_safe_defaults_and_env_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MIMIR_WORKLINK_MAX_STDOUT_BYTES", raising=False)
    monkeypatch.delenv("MIMIR_WORKLINK_MAX_STDERR_BYTES", raising=False)
    assert compute_module._worklink_output_limits() == (64 * 1024 * 1024, 16 * 1024 * 1024)

    monkeypatch.setenv("MIMIR_WORKLINK_MAX_STDOUT_BYTES", "123")
    monkeypatch.setenv("MIMIR_WORKLINK_MAX_STDERR_BYTES", "456")
    assert compute_module._worklink_output_limits() == (123, 456)

    monkeypatch.setenv("MIMIR_WORKLINK_MAX_STDOUT_BYTES", "invalid")
    monkeypatch.setenv("MIMIR_WORKLINK_MAX_STDERR_BYTES", "0")
    assert compute_module._worklink_output_limits() == (64 * 1024 * 1024, 16 * 1024 * 1024)


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


def test_blocked_reason_from_output_requires_final_line_marker() -> None:
    # No marker → no block.
    assert blocked_reason_from_output("did the work\n", "") is None
    # Marker as the final non-empty line (trailing blank lines tolerated) → reason.
    assert blocked_reason_from_output("WORKLINK_BLOCKED: real reason\n\n", "") == "real reason"
    # Whitespace-only reason is not a signal.
    assert blocked_reason_from_output("WORKLINK_BLOCKED:    \n", "") is None
    # Marker on stderr's final line is honored too.
    assert blocked_reason_from_output("", "boom\nWORKLINK_BLOCKED: env missing") == "env missing"
    # Regression (#671 review): a backend that echoes the prompt's marker line
    # near the top and then COMPLETES NORMALLY must not be mislabeled blocked —
    # the real final line is its success output, not the echoed marker.
    echo_then_success = (
        "WORKLINK_BLOCKED: <one-line reason>\n"  # echoed instruction placeholder
        "I completed the work successfully\n"
    )
    assert blocked_reason_from_output(echo_then_success, "") is None
    # But an early echo followed by a real FINAL marker is a genuine block.
    echo_then_block = (
        "WORKLINK_BLOCKED: <one-line reason>\n"
        "...did some analysis...\n"
        "WORKLINK_BLOCKED: acceptance criteria contradict #438\n"
    )
    assert blocked_reason_from_output(echo_then_block, "") == "acceptance criteria contradict #438"


def test_codex_status_auth_detection_does_not_match_author_text() -> None:
    assert codex_module._status_from_output(1, "author: test@example.com", "") == "failed"
    assert codex_module._status_from_output(1, "", "authentication required") == "auth_error"


@pytest.mark.asyncio
async def test_opencode_backend_invokes_run_dir_with_prompt_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """chainlink #830: the opencode backend runs `opencode run --dir <worktree>
    -- <prompt>` and interprets output like the codex backend."""
    calls: list[dict[str, Any]] = []

    async def fake_exec(*args: str, **kwargs: Any) -> FakeProcess:
        calls.append({"args": args, "kwargs": kwargs})
        return FakeProcess(returncode=0, stdout=b"done\n", stderr=b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr("mimir.worklink.compute._local_child_env", dict)
    backend = OpenCodeBackend()
    transcript_root = tmp_path / "state" / "worklink" / "transcripts"
    order = WorkOrder(
        issue_id=782,
        worktree=tmp_path / "worktree",
        prompt="-starts with dash",
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
        branch="issue/782-a1",
        test_command="echo ok",
    )
    compute = LocalSubprocessComputeBackend()
    handle = await compute.launch(spec)
    compute_result = await compute.wait(handle, spec.timeout_s)
    await compute.cleanup(handle)
    result = await backend.interpret(order, compute_result)

    assert result.backend_status == "success"
    assert calls[0]["args"] == (
        "opencode", "run", "--dir", str(order.worktree), "--", "-starts with dash"
    )
    permission = json.loads(calls[0]["kwargs"]["env"]["OPENCODE_PERMISSION"])
    assert permission == {
        "external_directory": {"/**": "deny"},
        "bash": {"*": "deny", "git *": "allow", "uv *": "allow", "env *": "allow"},
    }
    assert result.transcript_path is not None
    assert result.transcript_path.parent == transcript_root
    transcript = json.loads(result.transcript_path.read_text())
    assert transcript["backend"] == "opencode"


@pytest.mark.asyncio
async def test_opencode_backend_maps_blocked_auth_and_quota(tmp_path: Path) -> None:
    order = WorkOrder(
        issue_id=782,
        worktree=tmp_path,
        prompt="p",
        rules=None,
        timeout_s=30,
        env={},
        transcript_root=tmp_path / "t",
    )
    backend = OpenCodeBackend()

    blocked = await backend.interpret(
        order, ComputeResult(0, "some work\nWORKLINK_BLOCKED: needs a decision", "")
    )
    assert blocked.backend_status == "blocked"
    assert blocked.blocked_reason == "needs a decision"

    auth = await backend.interpret(order, ComputeResult(1, "", "provider: unauthorized token"))
    assert auth.backend_status == "auth_error"

    quota = await backend.interpret(order, ComputeResult(1, "rate limit exceeded", ""))
    assert quota.backend_status == "quota_exhausted"

    plain = await backend.interpret(order, ComputeResult(3, "", "boom"))
    assert plain.backend_status == "failed"
    assert plain.error == "boom"


@pytest.mark.asyncio
async def test_opencode_backend_transcript_filename_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """chainlink #831: regression test that opencode transcript filenames use
    the 'opencode-' prefix (not 'codex-') and embed the issue id, so mixed
    deployments can tell runs apart."""
    calls: list[dict[str, Any]] = []

    async def fake_exec(*args: str, **kwargs: Any) -> FakeProcess:
        calls.append({"args": args, "kwargs": kwargs})
        return FakeProcess(returncode=0, stdout=b'done\n', stderr=b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr("mimir.worklink.compute._local_child_env", dict)
    backend = OpenCodeBackend()
    transcript_root = tmp_path / "state" / "worklink" / "transcripts"
    issue_id = 831
    order = WorkOrder(
        issue_id=issue_id,
        worktree=tmp_path / "worktree",
        prompt="Do the work",
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
        branch="issue/831-a1",
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
    assert result.transcript_path is not None
    filename = result.transcript_path.name
    assert filename.startswith("opencode-"), f"expected 'opencode-' prefix, got {filename}"
    assert str(issue_id) in filename, f"expected issue id {issue_id} in filename, got {filename}"
    transcript = json.loads(result.transcript_path.read_text())
    assert transcript["backend"] == "opencode"


def test_registry_builds_opencode_backend_with_settings() -> None:
    config = WorklinkConfig(backend_settings={"opencode": {
        "bin": "/usr/local/bin/opencode",
        "args": ["--model", "openai/gpt-5.5"],
        "bash_allowlist": ["git *", "npm *"],
    }})
    backend = BackendRegistry(config).get("opencode")
    assert backend.bin == "/usr/local/bin/opencode"
    assert backend.extra_args == ("--model", "openai/gpt-5.5")
    assert backend.bash_allowlist == ("git *", "npm *")


@pytest.mark.parametrize("allowlist", ["git *", [""], ["*"]])
def test_registry_rejects_invalid_opencode_bash_allowlist(allowlist: object) -> None:
    config = WorklinkConfig(backend_settings={"opencode": {"bash_allowlist": allowlist}})
    with pytest.raises(ValueError, match="bash_allowlist"):
        BackendRegistry(config)


@pytest.mark.asyncio
async def test_local_subprocess_env_allowlist_passes_creds_not_bridge_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """chainlink #830: autonomous local_subprocess must hand the coding CLI its
    provider creds + HOME (so opencode finds config/plugins/auth) while never
    leaking bridge/operator secrets. Inert until the docker->worktree pivot."""
    captured: dict[str, Any] = {}

    async def fake_exec(*args: str, **kwargs: Any) -> FakeProcess:
        captured["env"] = kwargs.get("env", {})
        return FakeProcess(returncode=0, stdout=b"ok\n", stderr=b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setenv("HOME", "/home/mimir")
    monkeypatch.setenv("MINIMAX_API_KEY", "mm-key")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "bridge-secret")
    monkeypatch.setenv("MIMIR_API_KEY", "operator-secret")

    from mimir.worklink.compute import LocalSubprocessComputeBackend
    from mimir.worklink.compute import WorkSpec

    wt = tmp_path / "wt"
    wt.mkdir()
    spec = WorkSpec(
        issue_id=782, attempt=1, repo_url="u", base_ref="main", branch="issue/782-a1",
        prompt="p", rules=None, test_command="true", backend="opencode", timeout_s=30,
        env={"MIMIR_HOME": "/mimir-home"},
        local_worktree=wt, local_argv=("opencode", "run", "--dir", str(wt), "--", "p"),
    )
    compute = LocalSubprocessComputeBackend()
    handle = await compute.launch(spec)
    await compute.wait(handle, 30)
    await compute.cleanup(handle)

    env = captured["env"]
    assert env["HOME"] == "/home/mimir"
    assert env["MINIMAX_API_KEY"] == "mm-key"
    assert env["OPENAI_API_KEY"] == "sk-oai"
    assert env["MIMIR_HOME"] == "/mimir-home"  # spec.env still applied
    assert "DISCORD_BOT_TOKEN" not in env
    assert "MIMIR_API_KEY" not in env
