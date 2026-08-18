from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from mimir.opencode_config import OpenCodeInvocation
from mimir.worklink.backends import (
    WORKLINK_MERGED_LABEL,
    BackendRegistry,
    FeatureFactoryBackend,
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
from mimir.worklink.backends.registry import SHIPPING_BACKENDS, SHIPPING_COMPUTE_BACKENDS
from mimir.worklink.compute import ComputeCaps, ComputeLaunchError, LaunchHandle, WorkSpec
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


def test_worklink_config_routes_first_match_and_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "worklink.yaml"
    config_path.write_text(
        """
defaults:
  backend: opencode
  compute_backend: local_subprocess
  timeout_s: 45
  backend_by_category:
    renderer: feature_factory
routes:
  - label: render
    backend: feature_factory
    compute_backend: local_subprocess
  - repo: jasoncarreira/mimir
    backend: opencode
backends:
  opencode:
    bin: /opt/bin/opencode
    args: [--verbose]
  feature_factory:
    entrypoint: /opt/mimir-opencode/lib/node_modules/feature-factory/bin/factory.js
""".strip()
    )

    config = WorklinkConfig.load(config_path)

    assert config.defaults.timeout_s == 45
    assert config.select_backend_name(labels={"render"}, repo="jasoncarreira/mimir") == "feature_factory"
    assert config.select_backend_name(labels={"worklink"}, repo="jasoncarreira/mimir") == "opencode"
    assert (
        config.select_backend_name(
            labels={"worklink"}, repo="elsewhere/repo", tool_category="renderer"
        )
        == "feature_factory"
    )
    assert config.select_backend_name(labels={"worklink"}, repo="elsewhere/repo") == "opencode"
    registry = BackendRegistry(config)
    opencode = registry.get("opencode")
    assert isinstance(opencode, OpenCodeBackend)
    assert opencode.bin == "/opt/bin/opencode"
    assert opencode.extra_args == ("--verbose",)
    feature_factory = registry.get("feature_factory")
    assert isinstance(feature_factory, FeatureFactoryBackend)
    assert (
        feature_factory.entrypoint
        == "/opt/mimir-opencode/lib/node_modules/feature-factory/bin/factory.js"
    )
    assert config.defaults.compute_backend == "local_subprocess"
    assert isinstance(registry.select_compute(), LocalSubprocessComputeBackend)
    assert config.defaults.base_branch == "main"
    assert config.tool_pins == ()


@pytest.mark.parametrize("retired", ["bin", "args", "ready", "reviewer"])
def test_feature_factory_rejects_retired_settings(retired: str) -> None:
    value: object = [] if retired == "args" else "legacy"
    config = WorklinkConfig(backend_settings={"feature_factory": {retired: value}})
    with pytest.raises(ValueError, match="retired setting"):
        BackendRegistry(config)


def test_feature_factory_requires_absolute_entrypoint() -> None:
    config = WorklinkConfig(
        backend_settings={"feature_factory": {"entrypoint": "feature-factory/bin/factory.js"}}
    )
    with pytest.raises(ValueError, match="absolute"):
        BackendRegistry(config)


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
  backend: opencode
  timeout_s: 60
""".strip(),
        encoding="utf-8",
    )

    defaults = WorklinkConfig.load(old_config).defaults

    assert defaults.epic_branch_prefix == "epic/"
    assert defaults.max_review_retries == 3
    assert defaults.max_claim_attempts == 3
    assert defaults.reviewer_backend == "opencode"
    assert defaults.tiered_review.multi_vote_reviewer_count == 3
    assert "**/migrations/**" in defaults.tiered_review.high_risk_scope_patterns
    assert "**/*secret*" in defaults.tiered_review.high_risk_scope_patterns
    assert "auth" in defaults.tiered_review.high_risk_labels
    assert "generated-code" in defaults.tiered_review.high_risk_labels
    assert "*.lock" in defaults.tiered_review.high_risk_scope_patterns
    assert all("mimir/" not in pattern for pattern in defaults.tiered_review.high_risk_scope_patterns)
    assert WorklinkDefaults(backend="opencode").reviewer_backend == "opencode"
    assert WORKLINK_MERGED_LABEL == "worklink:merged"


def test_worklink_config_epic_overrides_and_tiered_review_parse(tmp_path: Path) -> None:
    config_path = tmp_path / "worklink.yaml"
    config_path.write_text(
        """
defaults:
  backend: opencode
  epic_branch_prefix: stacked/
  max_review_retries: 5
  max_claim_attempts: 10
  reviewer_backend: feature_factory
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
    assert defaults.reviewer_backend == "feature_factory"
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
  backend: opencode
  compute_backend: local-subprocess
routes:
  - label: docs
    backend: feature_factory
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
    with pytest.raises(ValueError, match="referenced by defaults.compute_backend"):
        WorklinkConfig.load(config_path)

    # A selected unknown name remains fatal without a matching settings block.
    config_path.write_text("defaults:\n  compute_backend: docker_sibling\n")
    with pytest.raises(ValueError, match="referenced by defaults.compute_backend"):
        WorklinkConfig.load(config_path)


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
    with pytest.raises(ValueError, match="referenced by defaults.compute_backend"):
        WorklinkConfig.load(config_path)

    config_path.write_text("defaults:\n  compute_backend: ecs_runtask\n")
    with pytest.raises(ValueError, match="referenced by defaults.compute_backend"):
        WorklinkConfig.load(config_path)


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
    (tmp_path / "nobase.yaml").write_text("defaults:\n  backend: opencode\n")
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
    missing_path.write_text("defaults:\n  backend: opencode\n")
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



def test_registry_resolves_only_shipping_backends() -> None:
    config = WorklinkConfig(routes=())
    registry = BackendRegistry(config)

    assert registry.select(labels={"worklink"}, repo="jasoncarreira/mimir").name == "opencode"
    assert registry.get("opencode").name == "opencode"
    assert registry.get("feature_factory").name == "feature_factory"
    with pytest.raises(KeyError, match="unknown Worklink backend: codex"):
        registry.get("codex")
    with pytest.raises(KeyError, match="unknown Worklink backend: claude_cli"):
        registry.get("claude_cli")


def test_shipping_backend_names_match_constructed_registries() -> None:
    registry = BackendRegistry()

    assert set(registry._backends) == SHIPPING_BACKENDS
    assert set(registry._compute_backends) == SHIPPING_COMPUTE_BACKENDS


@pytest.mark.parametrize("name", ["codex", "claude_cli"])
def test_registry_rejects_retired_backend_config(name: str) -> None:
    config = WorklinkConfig(backend_settings={name: {}})
    with pytest.raises(ValueError, match=f"unknown Worklink backend config: {name}"):
        BackendRegistry(config)


@pytest.mark.parametrize("name", ["codex", "claude_cli"])
def test_registry_rejects_retired_selected_backend(name: str) -> None:
    registry = BackendRegistry(WorklinkConfig(defaults=WorklinkDefaults(backend=name)))
    with pytest.raises(KeyError, match=f"unknown Worklink backend: {name}"):
        registry.select()


def test_config_ignores_unreferenced_stale_backend_settings_with_actionable_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config_path = tmp_path / "worklink.yaml"
    config_path.write_text(
        "backends:\n  codex:\n    bin: codex\n"
        "compute_backends:\n  docker-sibling:\n    image: old\n",
        encoding="utf-8",
    )

    config = WorklinkConfig.load(config_path)

    assert config.backend_settings == {}
    assert config.compute_backend_settings == {}
    assert f"unreferenced unknown Worklink backend config 'codex' in {config_path}" in caplog.text
    assert f"remove backends.codex from {config_path}" in caplog.text
    assert (
        f"unreferenced unknown Worklink compute backend config 'docker-sibling' in {config_path}"
        in caplog.text
    )
    assert f"remove compute_backends.docker-sibling from {config_path}" in caplog.text


@pytest.mark.parametrize(
    ("yaml_text", "reference"),
    [
        ("defaults:\n  backend: codex\n", "defaults.backend"),
        (
            "defaults:\n  backend_by_category:\n    coding-cli: codex\n",
            "defaults.backend_by_category.coding-cli",
        ),
        ("routes:\n  - label: legacy\n    backend: codex\n", "routes[0].backend"),
        (
            "routes:\n  - label: remote\n    backend: opencode\n    compute_backend: docker-sibling\n",
            "routes[0].compute_backend",
        ),
    ],
)
def test_config_rejects_every_referenced_unknown_backend_with_path_and_fix(
    tmp_path: Path, yaml_text: str, reference: str
) -> None:
    config_path = tmp_path / "worklink.yaml"
    config_path.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(ValueError) as raised:
        WorklinkConfig.load(config_path)

    message = str(raised.value)
    assert f"referenced by {reference}" in message
    assert str(config_path) in message
    assert f"change {reference}" in message


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
        local_checkout=tmp_path,
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
                    "stdin": asyncio.subprocess.DEVNULL,
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
        local_checkout=tmp_path,
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
        checkout=tmp_path,
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


def test_opencode_parses_structured_worklink_blocked_marker(tmp_path: Path) -> None:
    result = ComputeResult(
        exit_code=1,
        stdout="I cannot implement this safely.\nWORKLINK_BLOCKED: design requires raw docker.sock access",
        stderr="",
        command=("opencode",),
    )
    order = WorkOrder(
        issue_id=466,
        checkout=tmp_path,
        prompt="do it",
        rules=None,
        timeout_s=30,
        transcript_root=tmp_path / "transcripts",
    )

    raw = asyncio.run(OpenCodeBackend().interpret(order, result))

    assert raw.backend_status == "blocked"
    assert raw.blocked_reason == "design requires raw docker.sock access"
    assert raw.error == "design requires raw docker.sock access"


@pytest.mark.asyncio
async def test_opencode_retries_transient_sqlite_startup_contention() -> None:
    attempts = 0
    sleeps: list[float] = []
    events: list[tuple[str, dict[str, object]]] = []

    async def invoke() -> ComputeResult:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return ComputeResult(1, "", "SqliteError: database is locked")
        return ComputeResult(0, "completed", "")

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    result = await OpenCodeBackend().invoke_with_startup_retry(
        invoke,
        issue_id=1085,
        checkout_snapshot=lambda: ("head", ""),
        event_logger=lambda event, **payload: events.append((event, payload)),
        sleeper=sleep,
    )

    assert result.exit_code == 0
    assert attempts == 3
    assert sleeps == [0.1, 0.2]
    assert [(event, payload["outcome"]) for event, payload in events] == [
        ("worklink_backend_startup_contention", "retrying"),
        ("worklink_backend_startup_contention", "retrying"),
        ("worklink_backend_startup_contention", "succeeded"),
    ]
    assert all(
        payload["resource"] == "opencode_sqlite_session_store"
        for _event, payload in events
    )


@pytest.mark.asyncio
async def test_opencode_persistent_sqlite_contention_exhausts_with_named_reason(
    tmp_path: Path,
) -> None:
    attempts = 0
    sleeps: list[float] = []
    events: list[dict[str, object]] = []

    async def invoke() -> ComputeResult:
        nonlocal attempts
        attempts += 1
        return ComputeResult(1, "", "SqliteError: SQLITE_BUSY: database is locked")

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    backend = OpenCodeBackend()
    result = await backend.invoke_with_startup_retry(
        invoke,
        issue_id=1085,
        checkout_snapshot=lambda: ("head", ""),
        event_logger=lambda _event, **payload: events.append(payload),
        sleeper=sleep,
    )
    order = WorkOrder(1085, tmp_path, "p", None, 30, transcript_root=tmp_path / "t")
    raw = await backend.interpret(order, result)

    assert attempts == 5
    assert sleeps == [0.1, 0.2, 0.4, 0.8]
    assert raw.error == "opencode_startup_sqlite_contention_exhausted"
    assert [event["outcome"] for event in events] == [
        "retrying",
        "retrying",
        "retrying",
        "retrying",
        "exhausted",
    ]
    assert events[-1]["max_attempts"] == 5
    assert raw.transcript_path is not None
    transcript = json.loads(raw.transcript_path.read_text(encoding="utf-8"))
    assert "SQLITE_BUSY" in transcript["stderr"]


@pytest.mark.asyncio
async def test_opencode_non_transient_startup_failure_is_not_retried() -> None:
    attempts = 0
    sleeps: list[float] = []

    async def invoke() -> ComputeResult:
        nonlocal attempts
        attempts += 1
        return ComputeResult(1, "", "configuration file is invalid")

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    result = await OpenCodeBackend().invoke_with_startup_retry(
        invoke,
        issue_id=1085,
        checkout_snapshot=lambda: ("head", ""),
        sleeper=sleep,
    )

    assert result.stderr == "configuration file is invalid"
    assert attempts == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_opencode_late_sqlite_failure_is_not_retried() -> None:
    attempts = 0
    sleeps: list[float] = []
    times = iter((10.0, 16.0))

    async def invoke() -> ComputeResult:
        nonlocal attempts
        attempts += 1
        return ComputeResult(1, "work performed", "SqliteError: database is locked")

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    result = await OpenCodeBackend().invoke_with_startup_retry(
        invoke,
        issue_id=1085,
        checkout_snapshot=lambda: ("head", ""),
        sleeper=sleep,
        clock=lambda: next(times),
    )

    assert result.stderr == "SqliteError: database is locked"
    assert attempts == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_opencode_sqlite_failure_after_checkout_mutation_is_not_retried() -> None:
    attempts = 0
    sleeps: list[float] = []
    snapshots = iter((("head", ""), ("head", " M changed.py")))

    async def invoke() -> ComputeResult:
        nonlocal attempts
        attempts += 1
        return ComputeResult(1, "", "SqliteError: database is locked")

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    result = await OpenCodeBackend().invoke_with_startup_retry(
        invoke,
        issue_id=1085,
        checkout_snapshot=lambda: next(snapshots),
        sleeper=sleep,
    )

    assert result.stderr == "SqliteError: database is locked"
    assert attempts == 1
    assert sleeps == []


@pytest.mark.asyncio
async def test_opencode_stdout_only_sqlite_mention_is_not_retried() -> None:
    attempts = 0
    sleeps: list[float] = []

    async def invoke() -> ComputeResult:
        nonlocal attempts
        attempts += 1
        return ComputeResult(1, "recovered from database is locked", "other failure")

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    result = await OpenCodeBackend().invoke_with_startup_retry(
        invoke,
        issue_id=1085,
        checkout_snapshot=lambda: ("head", ""),
        sleeper=sleep,
    )

    assert result.stdout == "recovered from database is locked"
    assert attempts == 1
    assert sleeps == []


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


@pytest.mark.asyncio
async def test_opencode_backend_invokes_run_dir_with_prompt_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """chainlink #830: the opencode backend runs `opencode run --dir <worktree>
    -- <prompt>` and interprets its output."""
    calls: list[dict[str, Any]] = []

    async def fake_exec(*args: str, **kwargs: Any) -> FakeProcess:
        calls.append({"args": args, "kwargs": kwargs})
        return FakeProcess(returncode=0, stdout=b"done\n", stderr=b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr("mimir.worklink.compute._local_child_env", dict)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MIMIR_MODEL_SPEC", "codex-plus:gpt-5.6-luna")
    auth = tmp_path / ".local" / "share" / "opencode" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(
        json.dumps({"openai": {"type": "oauth", "refresh": "subscription"}}),
        encoding="utf-8",
    )
    backend = OpenCodeBackend()
    transcript_root = tmp_path / "state" / "worklink" / "transcripts"
    order = WorkOrder(
        issue_id=782,
        checkout=tmp_path / "worktree",
        prompt="-starts with dash",
        rules=None,
        timeout_s=30,
        env={
            "PATH": "/bin",
            "MIMIR_MODEL_SPEC": "codex-plus:gpt-5.6-luna",
        },
        transcript_root=transcript_root,
    )

    spec = backend.work_spec(
        order,
        attempt=1,
        repo_url="git@github.com:jasoncarreira/mimir.git",
        base_ref="main",
        branch="issue/782-a1",
        test_command="uv run pytest -q",
    )
    compute = LocalSubprocessComputeBackend()
    handle = await compute.launch(spec)
    compute_result = await compute.wait(handle, spec.timeout_s)
    await compute.cleanup(handle)
    result = await backend.interpret(order, compute_result)

    assert result.backend_status == "success"
    assert calls[0]["args"] == (
        "opencode", "run", "--dir", str(order.checkout),
        "-m", "openai/gpt-5.6-luna", "--", "-starts with dash"
    )
    assert calls[0]["args"].count("-m") == 1
    assert spec.backend_config["model"] == "openai/gpt-5.6-luna"
    assert spec.backend_config["model_diverged"] is False
    assert spec.test_command == "uv run pytest -q"
    assert "MIMIR_MODEL_SPEC" not in calls[0]["kwargs"]["env"]
    permission = json.loads(calls[0]["kwargs"]["env"]["OPENCODE_PERMISSION"])
    assert permission == {
        "external_directory": {"/**": "deny"},
        "bash": {"*": "deny", "git *": "allow", "uv *": "allow"},
    }
    assert result.transcript_path is not None
    assert result.transcript_path.parent == transcript_root
    transcript = json.loads(result.transcript_path.read_text())
    assert transcript["backend"] == "opencode"


def test_opencode_backend_rejects_direct_duplicate_model_flag() -> None:
    with pytest.raises(ValueError, match=r"cannot contain '--model'.*remove it"):
        OpenCodeBackend(extra_args=("--model", "openai/stale-worklink-setting"))


def test_opencode_backend_logs_native_model_divergence_from_home_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / ".env").write_text(
        "MIMIR_MODEL_SPEC=codex-plus:gpt-agent\n", encoding="utf-8"
    )
    config = tmp_path / ".config" / "opencode" / "opencode.jsonc"
    config.parent.mkdir(parents=True)
    config.write_text('{"model":"openai/gpt-native"}', encoding="utf-8")
    auth = tmp_path / ".local" / "share" / "opencode" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(
        json.dumps({"openai": {"type": "oauth", "refresh": "subscription"}}),
        encoding="utf-8",
    )
    monkeypatch.delenv("MIMIR_MODEL_SPEC", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    order = WorkOrder(
        issue_id=1063,
        checkout=tmp_path / "worktree",
        prompt="p",
        rules=None,
        timeout_s=30,
        env={"MIMIR_HOME": str(tmp_path)},
    )

    with caplog.at_level("WARNING", logger="mimir.worklink.backends.opencode"):
        spec = OpenCodeBackend().work_spec(
            order,
            attempt=1,
            repo_url="u",
            base_ref="main",
            branch="issue/1063-a1",
            test_command="true",
        )

    assert spec.backend_config["model"] == "openai/gpt-native"
    assert spec.backend_config["configured_model"] == "openai/gpt-agent"
    assert spec.backend_config["model_diverged"] is True
    assert "differs from configured agent model" in caplog.text


@pytest.mark.asyncio
async def test_opencode_backend_maps_blocked_auth_and_quota(tmp_path: Path) -> None:
    order = WorkOrder(
        issue_id=782,
        checkout=tmp_path,
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
    assert "provider 'unknown'" in (auth.error or "")

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
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    auth = tmp_path / ".local" / "share" / "opencode" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(
        json.dumps({"openai": {"type": "oauth", "refresh": "test-refresh"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MIMIR_MODEL_SPEC", "codex-plus:test-model")
    backend = OpenCodeBackend()
    transcript_root = tmp_path / "state" / "worklink" / "transcripts"
    issue_id = 831
    order = WorkOrder(
        issue_id=issue_id,
        checkout=tmp_path / "worktree",
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
    config = WorklinkConfig(
        defaults=WorklinkDefaults(test_command="npm test"),
        backend_settings={"opencode": {
            "bin": "/usr/local/bin/opencode",
            "args": ["--verbose"],
            "bash_allowlist": ["git *", "npm *"],
        }},
    )
    backend = BackendRegistry(config).get("opencode")
    assert backend.bin == "/usr/local/bin/opencode"
    assert backend.extra_args == ("--verbose",)
    assert backend.bash_allowlist == ("git *", "npm *")


@pytest.mark.parametrize(
    "args, flag",
    [
        (["--model", "openai/gpt-5.5"], "--model"),
        (["-m", "openai/gpt-5.5"], "-m"),
        (["--model=openai/gpt-5.5"], "--model=openai/gpt-5.5"),
        (["--dir", "/tmp/other"], "--dir"),
        (["--"], "--"),
    ],
)
def test_registry_rejects_opencode_injected_flags(args: list[str], flag: str) -> None:
    config = WorklinkConfig(backend_settings={"opencode": {"args": args}})

    with pytest.raises(ValueError, match=r"cannot contain .*remove it") as exc_info:
        BackendRegistry(config)
    assert repr(flag) in str(exc_info.value)


@pytest.mark.parametrize("allowlist", ["git *", [""], ["*"]])
def test_registry_rejects_invalid_opencode_bash_allowlist(allowlist: object) -> None:
    config = WorklinkConfig(backend_settings={"opencode": {"bash_allowlist": allowlist}})
    with pytest.raises(ValueError, match="bash_allowlist"):
        BackendRegistry(config)


def test_registry_keeps_and_logs_python_default_bash_allowlist(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("INFO", logger="mimir.worklink.backends.registry"):
        backend = BackendRegistry().get("opencode")

    assert backend.bash_allowlist == ("git *", "uv *")
    assert "effective bash allowlist from defaults.test_command: ['git *', 'uv *']" in caplog.text


@pytest.mark.parametrize(
    ("test_command", "runner_pattern"),
    [
        ("npm test", "npm *"),
        ("go test ./...", "go *"),
        ("./gradlew test", "./gradlew *"),
    ],
)
def test_registry_derives_only_configured_non_python_runner(
    monkeypatch: pytest.MonkeyPatch, test_command: str, runner_pattern: str
) -> None:
    monkeypatch.setattr(
        "mimir.worklink.backends.opencode.resolve_opencode_invocation",
        lambda **_: OpenCodeInvocation(
            model="openai/test-model",
            provider="openai",
            model_source="test",
            config_path=Path("/nonexistent/opencode.jsonc"),
            auth_path=Path("/nonexistent/auth.json"),
            auth_type=None,
        ),
    )
    config = WorklinkConfig(defaults=WorklinkDefaults(test_command=test_command))

    backend = BackendRegistry(config).get("opencode")
    permission = json.loads(
        backend.work_spec(
            WorkOrder(1, Path("/tmp/worktree"), "p", None, 30, {}),
            attempt=1,
            repo_url="u",
            base_ref="main",
            branch="issue/1-a1",
            test_command=test_command,
        ).env["OPENCODE_PERMISSION"]
    )

    assert backend.bash_allowlist == ("git *", runner_pattern)
    assert permission["bash"] == {"*": "deny", "git *": "allow", runner_pattern: "allow"}
    assert "uv *" not in permission["bash"]
    assert "rm *" not in permission["bash"]


def test_operator_bash_allowlist_beats_derivation() -> None:
    config = WorklinkConfig(
        defaults=WorklinkDefaults(test_command="npm test"),
        backend_settings={"opencode": {"bash_allowlist": ["npm test"]}},
    )

    backend = BackendRegistry(config).get("opencode")

    assert backend.bash_allowlist == ("npm test",)


@pytest.mark.parametrize(
    "test_command",
    [
        "env -u MIMIR_MODEL_SPEC uv run pytest -q",
        "make test",
        "sh -c 'npm test'",
        "bash -c 'npm test'",
        "python -m pytest",
    ],
)
def test_registry_refuses_nonderivable_test_runner(test_command: str) -> None:
    config = WorklinkConfig(defaults=WorklinkDefaults(test_command=test_command))

    with pytest.raises(ValueError, match=r"defaults\.test_command=.*runner.*not a derivable"):
        BackendRegistry(config)


def test_registry_reports_explicit_allowlist_mismatch_with_both_values() -> None:
    config = WorklinkConfig(
        defaults=WorklinkDefaults(test_command="npm test"),
        backend_settings={"opencode": {"bash_allowlist": ["git *", "uv *"]}},
    )

    with pytest.raises(ValueError, match=r"test_command='npm test'.*bash_allowlist=.*uv"):
        BackendRegistry(config)


def test_empty_operator_allowlist_fails_closed() -> None:
    config = WorklinkConfig(
        backend_settings={"opencode": {"bash_allowlist": []}},
    )

    with pytest.raises(ValueError, match=r"test_command=.*bash_allowlist=\[\]"):
        BackendRegistry(config)


@pytest.mark.asyncio
async def test_opencode_permission_refusal_names_effective_allowlist(tmp_path: Path) -> None:
    """The refusal message names the patterns that produced it.

    Fixture unchanged across Chainlink #1152: the refusal is the executor's final
    output line and the exit code is 0, so this also pins the fail-closed case
    that a refusal reported last still fails the run without a nonzero exit.
    """
    backend = OpenCodeBackend(bash_allowlist=("git *", "npm *"))
    order = WorkOrder(1, tmp_path, "p", None, 30, {}, transcript_root=tmp_path / "t")

    result = await backend.interpret(
        order,
        ComputeResult(0, "Error: permission denied for bash command rm -rf build", ""),
    )

    assert result.backend_status == "failed"
    assert result.error == (
        "OpenCode refused an executor shell command because it is not allowed by "
        "backends.opencode.bash_allowlist; effective patterns: ['git *', 'npm *']"
    )


@pytest.mark.asyncio
async def test_opencode_startup_permission_error_surfaces_backend_stderr(
    tmp_path: Path,
) -> None:
    backend = OpenCodeBackend(bash_allowlist=("git *", "uv *"))
    order = WorkOrder(1259, tmp_path, "p", None, 30, {}, transcript_root=tmp_path / "t")
    stderr = (
        "EACCES: permission denied, lstat\n"
        "  '/var/lib/mimir-worklink/checkouts/scope/1259-1/checkout'"
    )

    result = await backend.interpret(order, ComputeResult(1, "", stderr))

    assert result.backend_status == "failed"
    assert result.error == stderr
    assert "bash_allowlist" not in result.error


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
        local_checkout=wt, local_argv=("opencode", "run", "--dir", str(wt), "--", "p"),
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


# The exact literal that discarded three completed builds (chainlink #1152):
# a build editing authorization code writes these words into source and tests.
_AUTHZ_SOURCE_LINE = (
    '                    content=f"Error: permission denied for read on {permission_path}",'
)


@pytest.mark.asyncio
async def test_permission_words_in_executor_source_do_not_fail_a_successful_run(
    tmp_path: Path,
) -> None:
    """A completed run is not failed because its diff contains the words.

    Chainlink #1152: `_permission_refusal_reason` substring-scanned free-form
    output, so a build editing read-policy code incriminated itself. #1123,
    #1149 and #1152's own first two attempts each finished with a passing gate
    and were discarded.
    """
    order = WorkOrder(
        issue_id=1152,
        checkout=tmp_path,
        prompt="p",
        rules=None,
        timeout_s=30,
        env={},
        transcript_root=tmp_path / "t",
    )
    backend = OpenCodeBackend()

    raw = await backend.interpret(
        order,
        ComputeResult(0, f"edited a file\n{_AUTHZ_SOURCE_LINE}\nall todos complete", ""),
    )

    assert raw.backend_status == "success"
    assert raw.error is None


@pytest.mark.asyncio
async def test_permission_refusal_that_stopped_the_executor_still_fails_with_its_reason(
    tmp_path: Path,
) -> None:
    """A refusal that actually stopped the run is reported, and names itself."""
    order = WorkOrder(
        issue_id=1152,
        checkout=tmp_path,
        prompt="p",
        rules=None,
        timeout_s=30,
        env={},
        transcript_root=tmp_path / "t",
    )
    backend = OpenCodeBackend(bash_allowlist=("git *",))

    raw = await backend.interpret(
        order, ComputeResult(1, "", "permission denied: bash command not allowed")
    )

    assert raw.backend_status == "failed"
    assert "OpenCode refused an executor shell command" in (raw.error or "")
    assert "git *" in (raw.error or "")


@pytest.mark.asyncio
async def test_midstream_refusal_that_crashed_the_executor_is_still_reported(
    tmp_path: Path,
) -> None:
    """Position is not the only signal: a nonzero exit reports a refusal anywhere.

    The refusal here sits mid-stream with unrelated output after it, which is
    what a refusal followed by the executor's own teardown looks like. Position
    alone would miss it, so the nonzero exit must independently fail the run —
    otherwise Chainlink #1152's fix would trade one silent misclassification for
    another.
    """
    order = WorkOrder(
        issue_id=1152,
        checkout=tmp_path,
        prompt="p",
        rules=None,
        timeout_s=30,
        env={},
        transcript_root=tmp_path / "t",
    )
    backend = OpenCodeBackend(bash_allowlist=("git *",))

    raw = await backend.interpret(
        order,
        ComputeResult(
            2,
            "",
            "permission denied for bash command\naborting run\nsession closed",
        ),
    )

    assert raw.backend_status == "failed"
    assert "OpenCode refused an executor shell command" in (raw.error or "")


def test_blocked_marker_in_echoed_output_does_not_block_a_completed_run() -> None:
    """The sibling matcher is not exposed to the same defect, by position.

    `blocked_reason_from_output` honors its marker only as the final non-empty
    line, so a run that echoes the instruction and then completes normally is
    not mislabeled. Pinned here because #1152 audited it as a candidate for the
    same bug and it is the precedent the refusal fix follows.
    """
    echoed = "reminder: emit WORKLINK_BLOCKED: <reason> and stop\nwork finished cleanly"

    assert blocked_reason_from_output(echoed, "") is None
    assert blocked_reason_from_output("done\nWORKLINK_BLOCKED: real", "") == "real"


class _CheckoutCapability:
    path = Path("/checkout")

    def verify(self, path: Path | None) -> None:
        pass

    def duplicate_fd(self) -> int:
        return -1


def test_registry_compute_is_unbound_and_authorized_backends_are_fresh() -> None:
    registry = BackendRegistry()
    selected = registry.select_compute()
    authorization = _CheckoutCapability()

    first = selected.for_authorized_checkout(authorization)
    second = selected.for_authorized_checkout(authorization)

    assert isinstance(selected, LocalSubprocessComputeBackend)
    assert selected._authorized_checkout is None
    assert first is not selected
    assert second is not selected
    assert first is not second
    assert first._authorized_checkout is authorization
    assert second._authorized_checkout is authorization
    assert first._jobs is not second._jobs
