from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from mimir.worklink.backends import (
    BackendRegistry,
    Caps,
    ClaudeCliBackend,
    CodexBackend,
    ComputeResult,
    DockerSiblingComputeBackend,
    EcsRunTaskComputeBackend,
    EcsRunTaskConfig,
    LocalSubprocessComputeBackend,
    RawResult,
    ToolPin,
    WorkOrder,
    WorklinkConfig,
)
from mimir.worklink.backends.base import blocked_reason_from_output
from mimir.worklink.compute import ComputeCaps, ComputeLaunchError, LaunchHandle, WorkSpec
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

def test_worklink_config_builds_docker_sibling_compute_backend(tmp_path: Path) -> None:
    config_path = tmp_path / "worklink.yaml"
    config_path.write_text(
        """
defaults:
  backend: codex
  compute_backend: docker-sibling
routes:
  - label: local-ok
    backend: codex
    compute_backend: local_subprocess
compute_backends:
  docker-sibling:
    broker_url: "unix:///run/worklink-broker.sock"
    image: mimirbot-mimirbot
    policy:
      network: none
""".strip()
    )

    config = WorklinkConfig.load(config_path)
    registry = BackendRegistry(config)

    assert config.defaults.compute_backend == "docker_sibling"
    assert config.compute_backend_settings["docker_sibling"] == {
        "broker_url": "unix:///run/worklink-broker.sock",
        "image": "mimirbot-mimirbot",
        "policy": {"network": "none"},
    }
    backend = registry.select_compute(labels={"worklink"})
    assert isinstance(backend, DockerSiblingComputeBackend)
    assert backend.name == "docker_sibling"
    assert backend.broker_url == "unix:///run/worklink-broker.sock"
    assert backend.image == "mimirbot-mimirbot"
    assert backend.policy == {"network": "none"}
    assert backend.capabilities() == ComputeCaps(
        shared_filesystem=False,
        network_isolated=True,
        handle_cancel=True,
        persistent_after_disconnect=True,
    )
    assert isinstance(registry.select_compute(labels={"local-ok"}), LocalSubprocessComputeBackend)


class FakeDockerSiblingTransport:
    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []
        self.waits: list[tuple[str, int]] = []
        self.logs_requested: list[str] = []
        self.cancelled: list[str] = []
        self.cleaned: list[str] = []
        self.wait_response: dict[str, Any] = {
            "status": "completed",
            "exit_code": 0,
            "stdout": "done",
            "stderr": "",
        }

    async def submit_job(self, payload: dict[str, Any], *, timeout_s: int) -> dict[str, Any]:
        self.submitted.append({"payload": payload, "timeout_s": timeout_s})
        return {"job_id": "job-123"}

    async def wait_job(self, job_id: str, *, timeout_s: int) -> dict[str, Any]:
        self.waits.append((job_id, timeout_s))
        return self.wait_response

    async def job_logs(self, job_id: str) -> str:
        self.logs_requested.append(job_id)
        return "broker logs"

    async def cancel_job(self, job_id: str) -> None:
        self.cancelled.append(job_id)

    async def cleanup_job(self, job_id: str) -> None:
        self.cleaned.append(job_id)


def _portable_spec() -> WorkSpec:
    return WorkSpec(
        issue_id=472,
        attempt=2,
        repo_url="git@github.com:jasoncarreira/mimir.git",
        base_ref="main",
        branch="issue/472-a2",
        prompt="prompt",
        rules=None,
        test_command="echo ok",
        backend="codex",
        timeout_s=5,
        env={"MIMIR_HOME": "/home/mimir"},
        backend_config={"bin": "codex", "args": ["exec", "--json"]},
    )


@pytest.mark.asyncio
async def test_docker_sibling_compute_backend_uses_broker_contract_not_docker() -> None:
    transport = FakeDockerSiblingTransport()
    backend = DockerSiblingComputeBackend(
        broker_url="unix:///run/worklink-broker.sock",
        image="mimirbot-mimirbot",
        policy={"network": "none"},
        transport=transport,
    )

    handle = await backend.launch(_portable_spec())
    result = await backend.wait(handle, timeout_s=7)
    logs = await backend.logs(handle)
    await backend.cancel(handle)
    await backend.cleanup(handle)

    assert handle.substrate == "docker_sibling"
    assert handle.identifier == "job-123"
    assert result == ComputeResult(
        exit_code=0,
        stdout="done",
        stderr="",
        handle=handle,
        command=("worklink-broker", "unix:///run/worklink-broker.sock", "wait", "job-123"),
    )
    assert logs == "broker logs"
    assert transport.waits == [("job-123", 7)]
    assert transport.logs_requested == ["job-123"]
    assert transport.cancelled == ["job-123"]
    assert transport.cleaned == ["job-123"]
    submitted = transport.submitted[0]
    assert submitted["timeout_s"] == 5
    assert submitted["payload"]["image"] == "mimirbot-mimirbot"
    assert submitted["payload"]["policy"] == {"network": "none"}
    worker_payload = submitted["payload"]["worker_payload"]
    assert worker_payload["repo_dir"] == "/work/repo"
    assert worker_payload["evidence_path"] == "/work/evidence/evidence.json"
    assert worker_payload["transcript_root"] == "/work/transcripts"
    assert worker_payload["spec"]["branch"] == "issue/472-a2"
    assert worker_payload["spec"]["backend_config"] == {"bin": "codex", "args": ["exec", "--json"]}


@pytest.mark.asyncio
async def test_docker_sibling_compute_backend_maps_transport_failures_to_results() -> None:
    class FailingWaitTransport(FakeDockerSiblingTransport):
        async def wait_job(self, job_id: str, *, timeout_s: int) -> dict[str, Any]:
            raise RuntimeError("broker unavailable")

    backend = DockerSiblingComputeBackend(
        broker_url="https://broker.example.invalid",
        image="mimirbot-mimirbot",
        transport=FailingWaitTransport(),
    )

    handle = await backend.launch(_portable_spec())
    result = await backend.wait(handle, timeout_s=7)

    assert result.exit_code == -1
    assert result.launch_error == "docker-sibling broker wait failed: broker unavailable"
    assert result.handle == handle


@pytest.mark.asyncio
async def test_docker_sibling_compute_backend_cleanup_is_best_effort() -> None:
    class FailingCleanupTransport(FakeDockerSiblingTransport):
        async def cleanup_job(self, job_id: str) -> None:
            raise RuntimeError("cleanup failed")

    backend = DockerSiblingComputeBackend(
        broker_url="https://broker.example.invalid",
        image="mimirbot-mimirbot",
        transport=FailingCleanupTransport(),
    )

    handle = await backend.launch(_portable_spec())
    await backend.cleanup(handle)


@pytest.mark.asyncio
async def test_docker_sibling_compute_backend_requires_broker_job_id() -> None:
    class MissingJobIdTransport(FakeDockerSiblingTransport):
        async def submit_job(self, payload: dict[str, Any], *, timeout_s: int) -> dict[str, Any]:
            return {}

    backend = DockerSiblingComputeBackend(
        broker_url="https://broker.example.invalid",
        image="mimirbot-mimirbot",
        transport=MissingJobIdTransport(),
    )

    with pytest.raises(ComputeLaunchError, match="missing job_id"):
        await backend.launch(_portable_spec())


def test_worklink_config_accepts_legacy_compute_alias_for_docker_sibling(tmp_path: Path) -> None:
    config_path = tmp_path / "worklink.yaml"
    config_path.write_text(
        """
defaults:
  backend: codex
  compute: docker-sibling
compute_backends:
  docker_sibling:
    broker_endpoint: "https://broker.example.invalid"
    image: mimirbot-mimirbot
""".strip()
    )

    registry = BackendRegistry(WorklinkConfig.load(config_path))

    backend = registry.select_compute()
    assert isinstance(backend, DockerSiblingComputeBackend)
    assert backend.broker_url == "https://broker.example.invalid"


def test_worklink_config_rejects_malformed_docker_sibling_compute_backend(tmp_path: Path) -> None:
    config_path = tmp_path / "worklink.yaml"
    config_path.write_text(
        """
defaults:
  compute_backend: docker-sibling
compute_backends:
  docker-sibling:
    broker_url: "file:///tmp/socket"
    image: mimirbot-mimirbot
""".strip()
    )
    with pytest.raises(ValueError, match="broker_url must use"):
        BackendRegistry(WorklinkConfig.load(config_path))

    config_path.write_text(
        """
defaults:
  compute_backend: docker-sibling
compute_backends:
  docker-sibling:
    broker_url: "unix:///run/worklink-broker.sock"
    image: mimirbot-mimirbot
    docker_socket: /var/run/docker.sock
""".strip()
    )
    with pytest.raises(ValueError, match="unknown setting"):
        BackendRegistry(WorklinkConfig.load(config_path))

    config_path.write_text("compute_backends:\n  docker-sibling: []\n")
    with pytest.raises(
        ValueError, match="worklink compute_backends.docker-sibling must be a mapping"
    ):
        WorklinkConfig.load(config_path)

    config_path.write_text("defaults:\n  compute_backend: docker-sibling\n")
    with pytest.raises(ValueError, match="requires compute_backends.docker-sibling config"):
        BackendRegistry(WorklinkConfig.load(config_path)).select_compute()


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



def test_ecs_runtask_backend_builds_value_blind_runtask_request() -> None:
    config = EcsRunTaskConfig(
        cluster="worklink",
        task_definition="worklink-worker:7",
        container_name="worker",
        subnets=("subnet-a", "subnet-b"),
        security_groups=("sg-worklink",),
        platform_version="1.4.0",
        task_role_arn="arn:aws:iam::123:role/worklink-task",
        execution_role_arn="arn:aws:iam::123:role/worklink-exec",
        safe_env={"MIMIR_HOME": "/worklink/home"},
        tags={"component": "worklink"},
    )
    spec = WorkSpec(
        issue_id=459,
        attempt=3,
        repo_url="https://github.com/jasoncarreira/mimir.git",
        base_ref="main",
        branch="issue/459-a3",
        prompt="Implement ECS launcher",
        rules=None,
        test_command="uv run pytest -q tests/test_worklink_backends.py",
        backend="codex",
        timeout_s=900,
        creds_ref={"github": "task-definition-env:GITHUB_TOKEN"},
        env={"WORKLINK_MODE": "test"},
        backend_config={"bin": "codex", "args": ["exec", "--json"]},
    )

    request = EcsRunTaskComputeBackend(config).build_request(spec)

    assert request.params["cluster"] == "worklink"
    assert request.params["taskDefinition"] == "worklink-worker:7"
    assert request.params["launchType"] == "FARGATE"
    assert request.params["platformVersion"] == "1.4.0"
    assert request.params["networkConfiguration"] == {
        "awsvpcConfiguration": {
            "subnets": ["subnet-a", "subnet-b"],
            "assignPublicIp": "DISABLED",
            "securityGroups": ["sg-worklink"],
        }
    }
    overrides = request.params["overrides"]
    assert overrides["taskRoleArn"] == "arn:aws:iam::123:role/worklink-task"
    assert overrides["executionRoleArn"] == "arn:aws:iam::123:role/worklink-exec"
    container = overrides["containerOverrides"][0]
    assert container["name"] == "worker"
    assert container["command"][:3] == ["mimir", "worklink", "worker"]
    assert "secrets" not in container
    rendered = json.dumps(request.params, sort_keys=True)
    assert "ghp_" not in rendered
    assert "github-token" not in rendered
    assert "ssm:" not in rendered
    payload = json.loads(container["command"][4])
    assert payload == request.payload
    assert payload["spec"]["branch"] == "issue/459-a3"
    assert payload["spec"]["local_worktree"] is None
    assert payload["spec"]["local_argv"] is None
    env = {item["name"]: item["value"] for item in container["environment"]}
    assert env["MIMIR_HOME"] == "/worklink/home"
    assert env["WORKLINK_MODE"] == "test"
    assert "WORKLINK_PAYLOAD_JSON" not in env


def test_ecs_payload_propagates_test_only_and_nulls_local_fields(tmp_path) -> None:
    # chainlink #538: the ECS payload builder must carry spec.test_only. If it's
    # dropped, the controller's sandboxed test job runs the full implementation
    # worker again and the controller folds THAT exit code as if it were the test
    # command's — silently wrong. (Regression: the old field-by-field rebuild
    # omitted test_only; replace() carries it.)
    config = EcsRunTaskConfig(
        cluster="worklink",
        task_definition="worklink-worker:7",
        container_name="worker",
        subnets=("subnet-a",),
    )
    spec = WorkSpec(
        issue_id=538,
        attempt=1,
        repo_url="https://github.com/jasoncarreira/mimir.git",
        base_ref="main",
        branch="issue/538-a1",
        prompt="run tests only",
        rules=None,
        test_command="uv run pytest -q",
        backend="codex",
        timeout_s=900,
        local_worktree=tmp_path,
        local_argv=("mimir", "worklink", "worker"),
        test_only=True,
    )

    payload = EcsRunTaskComputeBackend(config).build_request(spec).payload

    assert payload["spec"]["test_only"] is True
    # local-substrate-only fields are nulled for the remote worker
    assert payload["spec"]["local_worktree"] is None
    assert payload["spec"]["local_argv"] is None
    # round-trips back to a test_only spec the worker will route to _run_test_only
    assert payload_from_json(payload).spec.test_only is True
    # a normal implementation spec stays test_only=False
    normal_payload = EcsRunTaskComputeBackend(config).build_request(
        replace(spec, test_only=False)
    ).payload
    assert normal_payload["spec"]["test_only"] is False


def test_ecs_runtask_request_validates_against_botocore_param_model() -> None:
    botocore_session = pytest.importorskip("botocore.session")
    botocore_validate = pytest.importorskip("botocore.validate")
    config = EcsRunTaskConfig(
        cluster="worklink",
        task_definition="worklink-worker:7",
        container_name="worker",
        subnets=("subnet-a",),
        security_groups=("sg-worklink",),
        task_role_arn="arn:aws:iam::123:role/worklink-task",
        execution_role_arn="arn:aws:iam::123:role/worklink-exec",
        safe_env={"MIMIR_HOME": "/worklink/home"},
    )
    spec = WorkSpec(
        issue_id=459,
        attempt=3,
        repo_url="https://github.com/jasoncarreira/mimir.git",
        base_ref="main",
        branch="issue/459-a3",
        prompt="Implement ECS launcher",
        rules=None,
        test_command="uv run pytest -q tests/test_worklink_backends.py",
        backend="codex",
        timeout_s=900,
        creds_ref={"github": "task-definition-env:GITHUB_TOKEN"},
        env={"WORKLINK_MODE": "test"},
        backend_config={"bin": "codex", "args": ["exec", "--json"]},
    )
    request = EcsRunTaskComputeBackend(config).build_request(spec)

    service_model = botocore_session.get_session().get_service_model("ecs")
    run_task_model = service_model.operation_model("RunTask")
    validation = botocore_validate.ParamValidator().validate(
        dict(request.params), run_task_model.input_shape
    )

    assert not validation.has_errors(), validation.generate_report()


@pytest.mark.asyncio
async def test_ecs_runtask_backend_launch_wait_and_cancel_with_fake_client() -> None:
    class FakeEcsClient:
        def __init__(self) -> None:
            self.run_kwargs: dict[str, Any] | None = None
            self.stopped = False

        def run_task(self, **kwargs: Any) -> dict[str, Any]:
            self.run_kwargs = kwargs
            return {"tasks": [{"taskArn": "arn:aws:ecs:task/worklink/abc"}]}

        def describe_tasks(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "tasks": [
                    {
                        "taskArn": kwargs["tasks"][0],
                        "lastStatus": "STOPPED",
                        "stoppedReason": "Essential container exited",
                        "containers": [{"name": "worker", "exitCode": 0}],
                    }
                ]
            }

        def stop_task(self, **kwargs: Any) -> dict[str, Any]:
            self.stopped = True
            return {"task": {"taskArn": kwargs["task"]}}

    client = FakeEcsClient()
    backend = EcsRunTaskComputeBackend(
        EcsRunTaskConfig(
            cluster="worklink",
            task_definition="worklink-worker",
            container_name="worker",
            subnets=("subnet-a",),
        ),
        client=client,
    )
    spec = WorkSpec(
        issue_id=459,
        attempt=1,
        repo_url="repo",
        base_ref="main",
        branch="issue/459-a1",
        prompt="x",
        rules=None,
        test_command="echo ok",
        backend="codex",
        timeout_s=30,
    )

    handle = await backend.launch(spec)
    result = await backend.wait(handle, timeout_s=30)
    await backend.cancel(handle)

    assert handle == LaunchHandle("ecs_runtask", "arn:aws:ecs:task/worklink/abc")
    assert result.exit_code == 0
    assert result.stderr == "Essential container exited"
    assert client.run_kwargs is not None
    assert client.stopped is True


def test_worklink_config_rejects_ecs_runtask_runtime_secrets(tmp_path: Path) -> None:
    config_path = tmp_path / "worklink.yaml"
    config_path.write_text(
        """
compute_backends:
  ecs-runtask:
    cluster: worklink
    task_definition: worklink-worker
    container_name: worker
    subnets: [subnet-a]
    secrets:
      - name: GITHUB_TOKEN
        value_from: arn:aws:secretsmanager:us-east-1:123:secret:github
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown setting.*secrets"):
        BackendRegistry(WorklinkConfig.load(config_path))


def test_worklink_config_registers_ecs_runtask_compute_backend(tmp_path: Path) -> None:
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
    security_groups: [sg-worklink]
    safe_env:
      MIMIR_HOME: /worklink/home
""".strip(),
        encoding="utf-8",
    )

    registry = BackendRegistry(WorklinkConfig.load(config_path))
    backend = registry.select_compute()

    assert isinstance(backend, EcsRunTaskComputeBackend)
    assert backend.config.cluster == "worklink"
    assert backend.config.subnets == ("subnet-a",)
    assert backend.config.security_groups == ("sg-worklink",)
    assert backend.config.safe_env == {"MIMIR_HOME": "/worklink/home"}
