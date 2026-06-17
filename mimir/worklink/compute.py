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
from dataclasses import dataclass, field, replace
import http.client
import json
import os
from pathlib import Path
import signal
import socket
from typing import Any, Mapping, Protocol, Sequence
from urllib import request as urlrequest
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
    # chainlink #538: test-only run. The worker clones + checks out the pushed
    # ``branch`` and runs ``test_command``, exiting with the test's exit code (no
    # backend, no push). Lets the controller re-derive REMOTE test evidence in a
    # fresh sandboxed compute job — controller-orchestrated, exit-code as the
    # trust channel — instead of fail-closing on unverified tests.
    test_only: bool = False


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


class DockerSiblingBrokerTransport(Protocol):
    """Narrow broker API used by the Docker-sibling compute client.

    The broker, not the agent container, owns docker.sock/container policy.  The
    agent-side client can only submit a serialized worker payload, wait for a
    broker job, fetch bounded logs, cancel, and cleanup by broker job id.
    """

    async def submit_job(self, payload: Mapping[str, Any], *, timeout_s: int) -> Mapping[str, Any]: ...

    async def wait_job(self, job_id: str, *, timeout_s: int) -> Mapping[str, Any]: ...

    async def job_logs(self, job_id: str) -> str: ...

    async def cancel_job(self, job_id: str) -> None: ...

    async def cleanup_job(self, job_id: str) -> None: ...


@dataclass(frozen=True)
class HttpDockerSiblingBrokerTransport:
    """HTTP implementation of the Docker-sibling broker contract.

    Endpoint contract:
    - ``POST /jobs`` with ``{"image", "policy", "worker_payload"}`` ->
      ``{"job_id": "..."}``
    - ``POST /jobs/<job_id>/wait`` with ``{"timeout_s": N}`` -> compute result
      fields (``exit_code``, ``stdout``, ``stderr``, ``timed_out``, optional
      ``launch_error``)
    - ``GET /jobs/<job_id>/logs`` -> either text/plain logs or ``{"logs": "..."}``
    - ``POST /jobs/<job_id>/cancel`` and ``DELETE /jobs/<job_id>`` for cleanup.
    """

    broker_url: str

    async def submit_job(self, payload: Mapping[str, Any], *, timeout_s: int) -> Mapping[str, Any]:
        return await asyncio.to_thread(self._json_request, "POST", "/jobs", payload, timeout_s)

    async def wait_job(self, job_id: str, *, timeout_s: int) -> Mapping[str, Any]:
        return await asyncio.to_thread(
            self._json_request,
            "POST",
            f"/jobs/{job_id}/wait",
            {"timeout_s": timeout_s},
            timeout_s,
        )

    async def job_logs(self, job_id: str) -> str:
        result = await asyncio.to_thread(self._request, "GET", f"/jobs/{job_id}/logs", None, 30)
        content_type = result[0]
        body = result[1]
        if "json" in content_type:
            try:
                data = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                return _bounded_text(body.decode("utf-8", errors="replace"))
            return _bounded_text(str(data.get("logs", ""))) if isinstance(data, Mapping) else ""
        return _bounded_text(body.decode("utf-8", errors="replace"))

    async def cancel_job(self, job_id: str) -> None:
        await asyncio.to_thread(self._json_request, "POST", f"/jobs/{job_id}/cancel", {}, 30)

    async def cleanup_job(self, job_id: str) -> None:
        await asyncio.to_thread(self._request, "DELETE", f"/jobs/{job_id}", None, 30)

    def _json_request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any],
        timeout_s: int,
    ) -> Mapping[str, Any]:
        content_type, body = self._request(method, path, payload, timeout_s)
        text = body.decode("utf-8", errors="replace")
        if not text.strip():
            return {}
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ComputeLaunchError(f"broker returned non-JSON response: {exc}") from exc
        if not isinstance(data, Mapping):
            raise ComputeLaunchError("broker returned JSON that is not an object")
        return data

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None,
        timeout_s: int,
    ) -> tuple[str, bytes]:
        parsed = urlparse(self.broker_url)
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if parsed.scheme == "unix":
            return _unix_http_request(
                socket_path=parsed.path,
                method=method,
                path=path,
                body=data,
                headers=headers,
                timeout_s=timeout_s,
            )
        if parsed.scheme not in {"http", "https"}:
            raise ComputeLaunchError(
                "default docker-sibling broker transport supports unix://, http://, or https:// URLs"
            )
        url = self.broker_url.rstrip("/") + path
        req = urlrequest.Request(url, data=data, headers=headers, method=method)
        with urlrequest.urlopen(req, timeout=timeout_s) as response:  # nosec B310 - configured broker URL
            return response.headers.get("content-type", ""), response.read()


@dataclass(frozen=True)
class DockerSiblingComputeBackend:
    """Agent-side Docker-sibling compute client.

    The client talks only to a narrow broker endpoint.  It never shells out to
    ``docker`` and never opens docker.sock from the agent container; the broker
    is responsible for creating the worker container and enforcing policy.
    """

    broker_url: str
    image: str
    policy: Mapping[str, Any] = field(default_factory=dict)
    transport: DockerSiblingBrokerTransport | None = None
    worker_repo_dir: Path = Path("/work/repo")
    worker_evidence_path: Path = Path("/work/evidence/evidence.json")
    worker_transcript_root: Path = Path("/work/transcripts")
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
        if self.transport is None:
            object.__setattr__(self, "transport", HttpDockerSiblingBrokerTransport(self.broker_url))

    def capabilities(self) -> ComputeCaps:
        return ComputeCaps(
            shared_filesystem=False,
            network_isolated=True,
            handle_cancel=True,
            persistent_after_disconnect=True,
        )

    async def launch(self, spec: WorkSpec) -> LaunchHandle:
        from .worker import WorkerPayload, payload_to_json

        payload = {
            "image": self.image,
            "policy": dict(self.policy),
            "worker_payload": payload_to_json(
                WorkerPayload(
                    spec=spec,
                    repo_dir=self.worker_repo_dir,
                    evidence_path=self.worker_evidence_path,
                    transcript_root=self.worker_transcript_root,
                    safe_env={},
                )
            ),
        }
        try:
            response = await self._transport.submit_job(payload, timeout_s=spec.timeout_s)
        except ComputeLaunchError:
            raise
        except Exception as exc:  # pragma: no cover - defensive adapter boundary
            raise ComputeLaunchError(f"docker-sibling broker launch failed: {exc}") from exc
        job_id = response.get("job_id", response.get("id"))
        if not job_id:
            raise ComputeLaunchError("docker-sibling broker launch response missing job_id")
        return LaunchHandle(self.name, str(job_id))

    async def wait(self, handle: LaunchHandle, timeout_s: int) -> ComputeResult:
        self._validate_handle(handle)
        try:
            response = await self._transport.wait_job(handle.identifier, timeout_s=timeout_s)
        except Exception as exc:
            error = _bounded_text(f"docker-sibling broker wait failed: {exc}")
            return ComputeResult(
                exit_code=-1,
                stdout="",
                stderr=error,
                timed_out=False,
                launch_error=error,
                handle=handle,
                command=self._command(handle, "wait"),
            )
        return _compute_result_from_broker_response(response, handle, self._command(handle, "wait"))

    async def logs(self, handle: LaunchHandle) -> str:
        self._validate_handle(handle)
        try:
            return _bounded_text(await self._transport.job_logs(handle.identifier))
        except Exception as exc:
            return _bounded_text(f"docker-sibling broker logs failed: {exc}")

    async def cancel(self, handle: LaunchHandle) -> None:
        self._validate_handle(handle)
        await self._transport.cancel_job(handle.identifier)

    async def cleanup(self, handle: LaunchHandle) -> None:
        self._validate_handle(handle)
        try:
            await self._transport.cleanup_job(handle.identifier)
        except Exception:
            # Cleanup is best-effort and must not mask the observed compute result
            # after a remote worker has already finished. Operators can still
            # inspect broker-side logs/state by job id if cleanup fails.
            return None

    @property
    def _transport(self) -> DockerSiblingBrokerTransport:
        assert self.transport is not None
        return self.transport

    def _validate_handle(self, handle: LaunchHandle) -> None:
        if handle.substrate != self.name:
            raise KeyError(f"unknown {self.name} handle: {handle.identifier}")

    def _command(self, handle: LaunchHandle, action: str) -> tuple[str, ...]:
        return ("worklink-broker", self.broker_url, action, handle.identifier)


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, *, timeout: int) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


def _unix_http_request(
    *,
    socket_path: str,
    method: str,
    path: str,
    body: bytes | None,
    headers: Mapping[str, str],
    timeout_s: int,
) -> tuple[str, bytes]:
    if not socket_path:
        raise ComputeLaunchError("unix:// broker URL must include a socket path")
    conn = _UnixHTTPConnection(socket_path, timeout=timeout_s)
    try:
        conn.request(method, path, body=body, headers=dict(headers))
        response = conn.getresponse()
        payload = response.read()
        if response.status >= 400:
            raise ComputeLaunchError(
                f"broker HTTP {response.status}: {payload.decode('utf-8', errors='replace')}"
            )
        return response.getheader("content-type", ""), payload
    finally:
        conn.close()


def _compute_result_from_broker_response(
    response: Mapping[str, Any],
    handle: LaunchHandle,
    command: tuple[str, ...],
) -> ComputeResult:
    status = str(response.get("status", "")).lower().strip()
    timed_out = bool(response.get("timed_out", False)) or status in {"timeout", "timed_out"}
    launch_error = (
        response.get("launch_error") or response.get("error")
        if status in {"launch_error", "launch_failed"}
        else response.get("launch_error")
    )
    exit_code_raw = response.get("exit_code")
    if exit_code_raw is None:
        exit_code = (
            -9
            if timed_out
            else (0 if status in {"completed", "success", "succeeded", "ok"} else 1)
        )
    else:
        try:
            exit_code = int(exit_code_raw)
        except (TypeError, ValueError):
            exit_code = 1
    return ComputeResult(
        exit_code=exit_code,
        stdout=_bounded_text(str(response.get("stdout", ""))),
        stderr=_bounded_text(str(response.get("stderr", response.get("error", "")))),
        timed_out=timed_out,
        launch_error=_bounded_text(str(launch_error)) if launch_error else None,
        handle=handle,
        command=command,
    )


def _bounded_text(value: str, *, limit: int = 20_000) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n... [truncated {len(value) - limit} chars]"


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

    This backend is intentionally value-blind for secrets: the generated
    RunTask request never contains raw secret values or secret references. ECS
    secrets must be declared on the task definition container definition (for
    example with SSM/Secrets Manager ``valueFrom`` references) and accessed by
    the task/execution roles. The worker payload is passed as JSON because it is
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
            "command": [
                "mimir",
                "worklink",
                "worker",
                "--payload-json",
                json.dumps(payload, separators=(",", ":")),
            ],
            "environment": [{"name": key, "value": value} for key, value in sorted(env.items())],
        }
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
                for key, value in sorted(
                    {
                        **dict(self.config.tags),
                        "worklink:issue": str(spec.issue_id),
                        "worklink:attempt": str(spec.attempt),
                    }.items()
                )
            ],
        }
        if self.config.security_groups:
            params["networkConfiguration"]["awsvpcConfiguration"]["securityGroups"] = list(
                self.config.security_groups
            )
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
        return None

    def _client(self) -> EcsRunTaskClient | Any:
        if self.client is not None:
            return self.client
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on optional AWS install
            raise ComputeLaunchError("ecs_runtask compute backend requires boto3") from exc
        self.client = EcsRunTaskClient(boto3.client("ecs"))
        return self.client


def _ecs_worker_payload(config: EcsRunTaskConfig, spec: WorkSpec) -> dict[str, Any]:
    from .worker import WorkerPayload, payload_to_json

    return payload_to_json(
        WorkerPayload(
                # Null only the local-substrate-only fields; replace() carries every
                # other field (incl. test_only — #538) so a new WorkSpec field can't
                # be silently dropped on the ECS path the way a field-by-field rebuild
                # would.
                spec=replace(spec, local_worktree=None, local_argv=None),
                repo_dir=Path(config.worker_repo_dir),
                evidence_path=Path(config.worker_evidence_path),
                transcript_root=(
                    Path(config.worker_transcript_root)
                    if config.worker_transcript_root is not None
                    else None
                ),
                safe_env=dict(config.safe_env),
            )
        )


def _ecs_environment(config: EcsRunTaskConfig, spec: WorkSpec) -> dict[str, str]:
    env = {str(key): str(value) for key, value in config.safe_env.items()}
    env.update({str(key): str(value) for key, value in spec.env.items()})
    return env


def _ensure_ecs_handle(name: str, handle: LaunchHandle) -> None:
    if handle.substrate != name:
        raise KeyError(f"unknown {name} handle: {handle.identifier}")


def _ecs_exit_code(task: Mapping[str, Any]) -> int:
    containers = task.get("containers") or []
    if containers and isinstance(containers[0], Mapping) and "exitCode" in containers[0]:
        return int(containers[0]["exitCode"])
    return 1


def _safe_json(value: Any) -> str:
    return _bounded_text(json.dumps(value, sort_keys=True, default=str))


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

