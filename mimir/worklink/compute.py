"""Compute substrates for Worklink tool backends.

Tool backends decide *what* backend should build a Worklink issue. Compute
backends decide *where* that work unit runs.  The first substrate is local
subprocess execution, preserving Worklink's original operator-invoked behavior;
later substrates can launch the same git-handoff ``WorkSpec`` in a container or
remote task and still expose launch/wait/cancel/cleanup handles to the
orchestrator.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import signal
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlparse


@dataclass(frozen=True)
class ComputeCaps:
    """Capabilities of a Worklink execution substrate."""

    shared_filesystem: bool
    network_isolated: bool
    handle_cancel: bool
    persistent_after_disconnect: bool


@dataclass(frozen=True)
class WorkSpec:
    """Portable work unit handed to a Worklink compute substrate.

    The durable handoff is git-shaped: a worker can clone ``repo_url``, check out
    ``base_ref``/``branch``, run ``backend`` with ``prompt``/``rules``, execute
    ``test_command``, and push evidence. ``local_worktree`` and
    ``local_argv`` are compatibility pointers used only by the
    ``local_subprocess`` fallback for today's manual in-container runs; remote
    substrates ignore them and use the git fields.
    """

    issue_id: int
    attempt: int
    repo_url: str
    base_ref: str
    branch: str
    prompt: str
    rules: str | None
    test_command: str
    backend: str
    timeout_s: int
    creds_ref: Mapping[str, str] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=dict)
    backend_config: Mapping[str, Any] = field(default_factory=dict)
    local_worktree: Path | None = None
    local_argv: Sequence[str] | None = None


@dataclass(frozen=True)
class LaunchHandle:
    """Opaque handle for a launched compute job."""

    substrate: str
    identifier: str


@dataclass(frozen=True)
class ComputeResult:
    """Observed result from a launched compute job."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    launch_error: str | None = None
    handle: LaunchHandle | None = None
    command: tuple[str, ...] = ()


class ComputeLaunchError(RuntimeError):
    """Raised when a compute substrate cannot launch a work unit."""


class ComputeBackend(Protocol):
    name: str

    def capabilities(self) -> ComputeCaps: ...

    async def launch(self, spec: WorkSpec) -> LaunchHandle: ...

    async def wait(self, handle: LaunchHandle, timeout_s: int) -> ComputeResult: ...

    async def logs(self, handle: LaunchHandle) -> str: ...

    async def cancel(self, handle: LaunchHandle) -> None: ...

    async def cleanup(self, handle: LaunchHandle) -> None: ...



@dataclass(frozen=True)
class DockerSiblingComputeBackend:
    """Configured Docker-sibling substrate placeholder.

    This slice only makes Docker-sibling selectable through typed Worklink
    config.  The broker client/process arrives in later slices; launching now
    fails closed instead of silently falling back to local execution.
    """

    broker_url: str
    image: str
    policy: Mapping[str, Any] = field(default_factory=dict)
    name: str = "docker_sibling"

    def __post_init__(self) -> None:
        if not self.broker_url:
            raise ValueError("worklink docker-sibling compute backend requires broker_url")
        parsed = urlparse(self.broker_url)
        if parsed.scheme not in {"unix", "http", "https"}:
            raise ValueError(
                "worklink docker-sibling broker_url must use unix://, http://, or https://"
            )
        if not self.image:
            raise ValueError("worklink docker-sibling compute backend requires image")
        if not isinstance(self.policy, Mapping):
            raise ValueError("worklink docker-sibling policy must be a mapping")

    def capabilities(self) -> ComputeCaps:
        return ComputeCaps(
            shared_filesystem=False,
            network_isolated=True,
            handle_cancel=True,
            persistent_after_disconnect=True,
        )

    async def launch(self, spec: WorkSpec) -> LaunchHandle:
        raise ComputeLaunchError(
            "docker_sibling compute backend broker client is not implemented yet"
        )

    async def wait(self, handle: LaunchHandle, timeout_s: int) -> ComputeResult:
        raise KeyError(f"unknown {self.name} handle: {handle.identifier}")

    async def logs(self, handle: LaunchHandle) -> str:
        raise KeyError(f"unknown {self.name} handle: {handle.identifier}")

    async def cancel(self, handle: LaunchHandle) -> None:
        raise KeyError(f"unknown {self.name} handle: {handle.identifier}")

    async def cleanup(self, handle: LaunchHandle) -> None:
        return None


@dataclass(frozen=True)
class EcsSecretReference:
    """Value-blind secret reference passed to an ECS task container."""

    name: str
    value_from: str


@dataclass(frozen=True)
class EcsRunTaskConfig:
    """Static ECS RunTask configuration for Worklink worker launches."""

    cluster: str
    task_definition: str
    container_name: str
    subnets: tuple[str, ...]
    security_groups: tuple[str, ...] = ()
    assign_public_ip: bool = False
    launch_type: str = "FARGATE"
    platform_version: str | None = None
    task_role_arn: str | None = None
    execution_role_arn: str | None = None
    worker_repo_dir: str = "/worklink/repo"
    worker_evidence_path: str = "/worklink/evidence/evidence.json"
    worker_transcript_root: str | None = "/worklink/transcripts"
    safe_env: Mapping[str, str] = field(default_factory=dict)
    secrets: tuple[EcsSecretReference, ...] = ()
    tags: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EcsRunTaskRequest:
    """Dry-run-friendly representation of the ECS RunTask request."""

    params: Mapping[str, Any]
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class EcsRunTaskClient:
    """Small protocol-like wrapper used by EcsRunTaskComputeBackend."""

    ecs: Any

    def run_task(self, **kwargs: Any) -> Mapping[str, Any]:
        return self.ecs.run_task(**kwargs)

    def describe_tasks(self, **kwargs: Any) -> Mapping[str, Any]:
        return self.ecs.describe_tasks(**kwargs)

    def stop_task(self, **kwargs: Any) -> Mapping[str, Any]:
        return self.ecs.stop_task(**kwargs)


@dataclass
class EcsRunTaskComputeBackend:
    """Launch a Worklink worker as an AWS ECS RunTask task.

    This backend is intentionally value-blind for secrets: config accepts only
    SSM/Secrets Manager ``valueFrom`` references, and the generated request never
    contains raw secret values. The worker payload is passed as JSON because it is
    work-order metadata, not credential material.
    """

    config: EcsRunTaskConfig
    client: Any | None = None
    name: str = "ecs_runtask"

    def capabilities(self) -> ComputeCaps:
        return ComputeCaps(
            shared_filesystem=False,
            network_isolated=True,
            handle_cancel=True,
            persistent_after_disconnect=True,
        )

    def build_request(self, spec: WorkSpec) -> EcsRunTaskRequest:
        payload = _ecs_worker_payload(self.config, spec)
        env = _ecs_environment(self.config, spec)
        container: dict[str, Any] = {
            "name": self.config.container_name,
            "command": ["mimir", "worklink", "worker", "--payload-json", json.dumps(payload, separators=(",", ":"))],
            "environment": [{"name": key, "value": value} for key, value in sorted(env.items())],
        }
        if self.config.secrets:
            container["secrets"] = [
                {"name": secret.name, "valueFrom": secret.value_from}
                for secret in self.config.secrets
            ]
        params: dict[str, Any] = {
            "cluster": self.config.cluster,
            "taskDefinition": self.config.task_definition,
            "launchType": self.config.launch_type,
            "networkConfiguration": {
                "awsvpcConfiguration": {
                    "subnets": list(self.config.subnets),
                    "assignPublicIp": "ENABLED" if self.config.assign_public_ip else "DISABLED",
                }
            },
            "overrides": {"containerOverrides": [container]},
            "tags": [
                {"key": key, "value": value}
                for key, value in sorted({**dict(self.config.tags), "worklink:issue": str(spec.issue_id), "worklink:attempt": str(spec.attempt)}.items())
            ],
        }
        if self.config.security_groups:
            params["networkConfiguration"]["awsvpcConfiguration"]["securityGroups"] = list(self.config.security_groups)
        if self.config.platform_version:
            params["platformVersion"] = self.config.platform_version
        if self.config.task_role_arn:
            params.setdefault("overrides", {})["taskRoleArn"] = self.config.task_role_arn
        if self.config.execution_role_arn:
            params.setdefault("overrides", {})["executionRoleArn"] = self.config.execution_role_arn
        return EcsRunTaskRequest(params=params, payload=payload)

    async def launch(self, spec: WorkSpec) -> LaunchHandle:
        client = self._client()
        request = self.build_request(spec)
        try:
            response = await asyncio.to_thread(client.run_task, **dict(request.params))
        except Exception as exc:  # pragma: no cover - exact AWS exception type is client-specific
            raise ComputeLaunchError(str(exc)) from exc
        failures = response.get("failures") or [] if isinstance(response, Mapping) else []
        if failures:
            raise ComputeLaunchError(f"ecs run_task failed: {_safe_json(failures)}")
        tasks = response.get("tasks") or [] if isinstance(response, Mapping) else []
        if not tasks or not isinstance(tasks[0], Mapping) or not tasks[0].get("taskArn"):
            raise ComputeLaunchError("ecs run_task returned no taskArn")
        return LaunchHandle(self.name, str(tasks[0]["taskArn"]))

    async def wait(self, handle: LaunchHandle, timeout_s: int) -> ComputeResult:
        _ensure_ecs_handle(self.name, handle)
        try:
            client = self._client()
        except ComputeLaunchError as exc:
            return ComputeResult(-1, "", str(exc), launch_error=str(exc), handle=handle)
        deadline = asyncio.get_running_loop().time() + timeout_s
        last: Mapping[str, Any] | None = None
        while True:
            response = await asyncio.to_thread(
                client.describe_tasks,
                cluster=self.config.cluster,
                tasks=[handle.identifier],
            )
            tasks = response.get("tasks") or [] if isinstance(response, Mapping) else []
            last = tasks[0] if tasks and isinstance(tasks[0], Mapping) else None
            if last and last.get("lastStatus") == "STOPPED":
                exit_code = _ecs_exit_code(last)
                reason = str(last.get("stoppedReason") or "")
                return ComputeResult(exit_code, "", reason, timed_out=False, handle=handle)
            if asyncio.get_running_loop().time() >= deadline:
                await self.cancel(handle)
                return ComputeResult(-1, "", _safe_json(last or {}), timed_out=True, handle=handle)
            await asyncio.sleep(5)

    async def logs(self, handle: LaunchHandle) -> str:
        _ensure_ecs_handle(self.name, handle)
        return ""

    async def cancel(self, handle: LaunchHandle) -> None:
        _ensure_ecs_handle(self.name, handle)
        client = self._client()
        await asyncio.to_thread(
            client.stop_task,
            cluster=self.config.cluster,
            task=handle.identifier,
            reason="worklink cancel",
        )

    async def cleanup(self, handle: LaunchHandle) -> None:
        _ensure_ecs_handle(self.name, handle)

    def _client(self) -> Any:
        if self.client is not None:
            return self.client
        try:
            import boto3  # type: ignore[import-not-found]
        except ModuleNotFoundError as exc:
            raise ComputeLaunchError("ecs_runtask requires an ECS client or boto3") from exc
        self.client = EcsRunTaskClient(boto3.client("ecs"))
        return self.client


def _ecs_worker_payload(config: EcsRunTaskConfig, spec: WorkSpec) -> dict[str, Any]:
    return {
        "spec": _spec_to_json_for_ecs(spec),
        "repo_dir": config.worker_repo_dir,
        "evidence_path": config.worker_evidence_path,
        "transcript_root": config.worker_transcript_root,
        "safe_env": dict(config.safe_env),
    }


def _spec_to_json_for_ecs(spec: WorkSpec) -> dict[str, Any]:
    return {
        "issue_id": spec.issue_id,
        "attempt": spec.attempt,
        "repo_url": spec.repo_url,
        "base_ref": spec.base_ref,
        "branch": spec.branch,
        "prompt": spec.prompt,
        "rules": spec.rules,
        "test_command": spec.test_command,
        "backend": spec.backend,
        "timeout_s": spec.timeout_s,
        "creds_ref": dict(spec.creds_ref),
        "env": dict(spec.env),
        "backend_config": dict(spec.backend_config),
        "local_worktree": None,
        "local_argv": None,
    }


def _ecs_environment(config: EcsRunTaskConfig, spec: WorkSpec) -> dict[str, str]:
    env = {**dict(config.safe_env), **dict(spec.env)}
    return {str(key): str(value) for key, value in env.items()}


def _ecs_exit_code(task: Mapping[str, Any]) -> int:
    containers = task.get("containers") or []
    if containers and isinstance(containers[0], Mapping) and "exitCode" in containers[0]:
        try:
            return int(containers[0]["exitCode"])
        except (TypeError, ValueError):
            return -1
    return -1


def _ensure_ecs_handle(name: str, handle: LaunchHandle) -> None:
    if handle.substrate != name:
        raise KeyError(f"unknown {name} handle: {handle.identifier}")


def _safe_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)[:1000]

@dataclass
class LocalSubprocessComputeBackend:
    """Run a WorkSpec as a local subprocess in the current container."""

    name: str = "local_subprocess"

    def __post_init__(self) -> None:
        self._jobs: dict[str, tuple[object, WorkSpec, tuple[str, ...]]] = {}

    def capabilities(self) -> ComputeCaps:
        return ComputeCaps(
            shared_filesystem=True,
            network_isolated=False,
            handle_cancel=True,
            persistent_after_disconnect=False,
        )

    async def launch(self, spec: WorkSpec) -> LaunchHandle:
        if spec.local_worktree is None:
            raise ComputeLaunchError("local_subprocess requires spec.local_worktree")
        if spec.local_argv is None:
            raise ComputeLaunchError("local_subprocess requires spec.local_argv")
        if isinstance(spec.local_argv, (str, bytes)):
            raise ComputeLaunchError("local_subprocess spec.local_argv must be a sequence")
        command = tuple(str(arg) for arg in spec.local_argv)
        if not command:
            raise ComputeLaunchError("local_subprocess spec.local_argv must not be empty")
        env = {"PATH": os.environ.get("PATH", "")}
        env.update(spec.env)
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(spec.local_worktree),
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            raise ComputeLaunchError(str(exc)) from exc
        handle = LaunchHandle(self.name, str(getattr(proc, "pid", "unknown")))
        self._jobs[handle.identifier] = (proc, spec, command)
        return handle

    async def wait(self, handle: LaunchHandle, timeout_s: int) -> ComputeResult:
        proc, _spec, command = self._job(handle)
        timed_out = False
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            timed_out = True
            await self.cancel(handle)
            stdout_b, stderr_b = await proc.communicate()

        stdout = stdout_b.decode(errors="replace")
        stderr = stderr_b.decode(errors="replace")
        exit_code = getattr(proc, "returncode", None)
        return ComputeResult(
            exit_code=exit_code if exit_code is not None else -1,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            handle=handle,
            command=command,
        )

    async def logs(self, handle: LaunchHandle) -> str:
        self._job(handle)
        return ""

    async def cancel(self, handle: LaunchHandle) -> None:
        proc, _spec, _command = self._job(handle)
        await _kill_process_group(proc)

    async def cleanup(self, handle: LaunchHandle) -> None:
        self._jobs.pop(handle.identifier, None)

    def _job(self, handle: LaunchHandle) -> tuple[object, WorkSpec, tuple[str, ...]]:
        if handle.substrate != self.name or handle.identifier not in self._jobs:
            raise KeyError(f"unknown {self.name} handle: {handle.identifier}")
        return self._jobs[handle.identifier]


async def _kill_process_group(proc: object) -> None:
    pid = getattr(proc, "pid", None)
    if pid is None:
        kill = getattr(proc, "kill", None)
        if kill:
            kill()
        return
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        kill = getattr(proc, "kill", None)
        if kill:
            kill()
        return
    wait = getattr(proc, "wait", None)
    if wait is None:
        return
    try:
        await asyncio.wait_for(wait(), timeout=5)
    except TimeoutError:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return
        await wait()

