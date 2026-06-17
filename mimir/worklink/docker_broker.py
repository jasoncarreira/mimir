"""Docker-sibling broker for Worklink compute jobs.

The broker is the narrow privileged process that may talk to Docker.  The
agent-side Worklink client submits only a worker payload; the broker turns that
payload into a policy-derived ``docker run`` command.  It deliberately does not
accept arbitrary docker arguments, host mounts, privileged mode, or unbounded
environment/network choices from the agent container.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import signal
import uuid
from typing import Any, Awaitable, Callable, Mapping, MutableMapping, Sequence

from aiohttp import web

from .worker import payload_from_json

ProcessFactory = Callable[..., Awaitable[Any]]


class DockerBrokerPolicyError(ValueError):
    """Raised when a broker request violates the configured container policy."""


@dataclass(frozen=True)
class CredsMount:
    """An operator-declared, read-only host path mounted into worker containers.

    This is how a worker gets *file-based* backend credentials that can't travel
    as an env var — e.g. codex's ``~/.codex/auth.json`` OAuth bundle. It is
    always read-only and declared ONLY in the static, operator-owned policy file:
    the agent cannot request a mount, so this widens what a worker can *read*
    without widening what the agent can *control*. ``source`` is a host path (the
    broker's ``docker run`` creates sibling containers on the host daemon)."""

    source: str
    target: str

    def __post_init__(self) -> None:
        if not (self.source.startswith("/") and self.target.startswith("/")):
            raise DockerBrokerPolicyError(
                "docker broker creds_mount source and target must be absolute paths"
            )


@dataclass(frozen=True)
class DockerBrokerPolicy:
    """Static policy for Docker-sibling Worklink worker containers."""

    allowed_images: tuple[str, ...]
    network: str = "none"
    max_timeout_s: int = 1800
    env_allowlist: tuple[str, ...] = ()
    default_env: Mapping[str, str] = field(default_factory=dict)
    creds_mounts: tuple[CredsMount, ...] = ()
    docker_bin: str = "docker"
    worker_command: tuple[str, ...] = ("mimir", "worklink", "worker", "--payload-json")
    job_prefix: str = "worklink"
    log_limit: int = 100_000

    def __post_init__(self) -> None:
        if not self.allowed_images:
            raise DockerBrokerPolicyError(
                "docker broker policy requires at least one allowed image"
            )
        if self.network not in _ALLOWED_NETWORKS:
            raise DockerBrokerPolicyError(
                "docker broker network must be one of: " + ", ".join(sorted(_ALLOWED_NETWORKS))
            )
        if self.max_timeout_s <= 0:
            raise DockerBrokerPolicyError("docker broker max_timeout_s must be positive")
        bad_env = [key for key in self.env_allowlist if not _safe_env_name(key)]
        if bad_env:
            raise DockerBrokerPolicyError(
                "docker broker env allowlist contains invalid key(s): "
                + ", ".join(bad_env)
            )
        bad_default_env = [key for key in self.default_env if key not in set(self.env_allowlist)]
        if bad_default_env:
            raise DockerBrokerPolicyError(
                "docker broker default_env key(s) must be present in env_allowlist: "
                + ", ".join(sorted(bad_default_env))
            )
        if not self.worker_command:
            raise DockerBrokerPolicyError("docker broker worker_command must not be empty")

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "DockerBrokerPolicy":
        images_raw = data.get("allowed_images", data.get("images", data.get("image")))
        if isinstance(images_raw, str):
            images = (images_raw,)
        elif isinstance(images_raw, Sequence):
            images = tuple(str(item) for item in images_raw)
        else:
            images = ()
        env_raw = data.get("env_allowlist", data.get("allowed_env", ()))
        if isinstance(env_raw, str):
            env_allowlist = (env_raw,)
        elif isinstance(env_raw, Sequence):
            env_allowlist = tuple(str(item) for item in env_raw)
        else:
            env_allowlist = ()
        default_env = data.get("default_env") or {}
        if not isinstance(default_env, Mapping):
            raise DockerBrokerPolicyError("docker broker default_env must be a mapping")
        worker_command_raw = data.get("worker_command") or (
            "mimir",
            "worklink",
            "worker",
            "--payload-json",
        )
        if isinstance(worker_command_raw, str):
            worker_command = tuple(worker_command_raw.split())
        elif isinstance(worker_command_raw, Sequence):
            worker_command = tuple(str(item) for item in worker_command_raw)
        else:
            raise DockerBrokerPolicyError("docker broker worker_command must be a string or list")
        return cls(
            allowed_images=images,
            network=str(data.get("network", "none")),
            max_timeout_s=int(data.get("max_timeout_s", 1800)),
            env_allowlist=env_allowlist,
            default_env={str(key): str(value) for key, value in default_env.items()},
            creds_mounts=_parse_creds_mounts(data.get("creds_mounts")),
            docker_bin=str(data.get("docker_bin", "docker")),
            worker_command=worker_command,
            job_prefix=str(data.get("job_prefix", "worklink")),
            log_limit=int(data.get("log_limit", 100_000)),
        )


@dataclass
class DockerBrokerJob:
    job_id: str
    image: str
    command: tuple[str, ...]
    timeout_s: int
    process: Any
    started_at: datetime
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    launch_error: str | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def complete(self) -> bool:
        return self.done.is_set()


class DockerSiblingBroker:
    """In-process Docker-sibling broker implementation.

    ``process_factory`` defaults to ``asyncio.create_subprocess_exec`` and is
    injected in tests so no test requires a live Docker daemon.
    """

    def __init__(
        self,
        policy: DockerBrokerPolicy,
        *,
        process_factory: ProcessFactory | None = None,
    ) -> None:
        self.policy = policy
        self._process_factory = process_factory or asyncio.create_subprocess_exec
        self._jobs: MutableMapping[str, DockerBrokerJob] = {}

    async def submit_job(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        self._validate_request_shape(request)
        image = self._select_image(request.get("image"))
        worker_payload = request.get("worker_payload")
        if not isinstance(worker_payload, Mapping):
            raise DockerBrokerPolicyError("docker broker request missing worker_payload object")
        parsed_payload = self._validate_worker_payload(worker_payload)
        timeout_s = self._select_timeout(request.get("timeout_s", parsed_payload.spec.timeout_s))
        self._validate_request_policy(request.get("policy") or {})
        payload_json = json.dumps(worker_payload, separators=(",", ":"))
        job_id = self._new_job_id()
        command = self._docker_command(
            job_id=job_id, image=image, payload_json=payload_json, timeout_s=timeout_s
        )
        try:
            process = await self._process_factory(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                env=self._docker_env(),
            )
        except Exception as exc:  # pragma: no cover - defensive process boundary
            raise DockerBrokerPolicyError(
                f"docker broker failed to launch worker container: {exc}"
            ) from exc
        job = DockerBrokerJob(
            job_id=job_id,
            image=image,
            command=command,
            timeout_s=timeout_s,
            process=process,
            started_at=datetime.now(UTC),
        )
        self._jobs[job_id] = job
        asyncio.create_task(self._collect(job))
        return {"job_id": job_id}

    async def wait_job(self, job_id: str, *, timeout_s: int | None = None) -> Mapping[str, Any]:
        job = self._get_job(job_id)
        wait_s = self._select_timeout(timeout_s or job.timeout_s)
        try:
            await asyncio.wait_for(job.done.wait(), timeout=wait_s)
        except TimeoutError:
            return self._job_response(job, force_timed_out=True)
        return self._job_response(job)

    async def job_logs(self, job_id: str) -> str:
        job = self._get_job(job_id)
        logs = (job.stdout or "") + (('\n' + job.stderr) if job.stderr else "")
        return _bounded_text(logs, self.policy.log_limit)

    async def cancel_job(self, job_id: str) -> None:
        job = self._get_job(job_id)
        if job.complete:
            return
        process = job.process
        try:
            pgid = os.getpgid(process.pid)
        except Exception:
            pgid = None
        try:
            if pgid is not None:
                os.killpg(pgid, signal.SIGTERM)
            elif hasattr(process, "terminate"):
                process.terminate()
        except ProcessLookupError:
            return
        except Exception:
            if hasattr(process, "kill"):
                process.kill()

    async def cleanup_job(self, job_id: str) -> None:
        job = self._get_job(job_id)
        if not job.complete:
            await self.cancel_job(job_id)
        await self._docker_rm(job_id)
        self._jobs.pop(job_id, None)

    def app(self) -> web.Application:
        app = web.Application()
        app.add_routes(
            [
                web.post("/jobs", self._http_submit_job),
                web.post(r"/jobs/{job_id}/wait", self._http_wait_job),
                web.get(r"/jobs/{job_id}/logs", self._http_job_logs),
                web.post(r"/jobs/{job_id}/cancel", self._http_cancel_job),
                web.delete(r"/jobs/{job_id}", self._http_cleanup_job),
            ]
        )
        return app

    async def _http_submit_job(self, request: web.Request) -> web.Response:
        return web.json_response(await self.submit_job(await request.json()))

    async def _http_wait_job(self, request: web.Request) -> web.Response:
        payload = await request.json()
        return web.json_response(
            await self.wait_job(
                request.match_info["job_id"],
                timeout_s=int(payload.get("timeout_s") or 30),
            )
        )

    async def _http_job_logs(self, request: web.Request) -> web.Response:
        return web.json_response({"logs": await self.job_logs(request.match_info["job_id"])})

    async def _http_cancel_job(self, request: web.Request) -> web.Response:
        await self.cancel_job(request.match_info["job_id"])
        return web.json_response({"ok": True})

    async def _http_cleanup_job(self, request: web.Request) -> web.Response:
        await self.cleanup_job(request.match_info["job_id"])
        return web.json_response({"ok": True})

    async def _collect(self, job: DockerBrokerJob) -> None:
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                job.process.communicate(), timeout=job.timeout_s
            )
        except TimeoutError:
            job.timed_out = True
            await self.cancel_job(job.job_id)
            stdout_b, stderr_b = await job.process.communicate()
        except Exception as exc:  # pragma: no cover - defensive process boundary
            job.launch_error = str(exc)
            stdout_b, stderr_b = b"", str(exc).encode("utf-8", errors="replace")
        job.stdout = _bounded_text(_decode(stdout_b), self.policy.log_limit)
        job.stderr = _bounded_text(_decode(stderr_b), self.policy.log_limit)
        job.exit_code = int(getattr(job.process, "returncode", -1) or 0)
        job.done.set()

    async def _docker_rm(self, job_id: str) -> None:
        try:
            proc = await self._process_factory(
                self.policy.docker_bin,
                "rm",
                "-f",
                job_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            await proc.communicate()
        except Exception:
            return

    def _validate_request_shape(self, request: Mapping[str, Any]) -> None:
        allowed_keys = {"image", "policy", "worker_payload", "timeout_s"}
        unknown = sorted(set(request) - allowed_keys)
        if unknown:
            raise DockerBrokerPolicyError(
                "docker broker request has unsupported key(s): " + ", ".join(unknown)
            )

    def _select_image(self, requested: Any) -> str:
        image = str(requested or self.policy.allowed_images[0])
        if image not in set(self.policy.allowed_images):
            raise DockerBrokerPolicyError("docker broker image is not allowed by policy")
        return image

    def _select_timeout(self, raw: Any) -> int:
        try:
            timeout_s = self.policy.max_timeout_s if raw is None else int(raw)
        except (TypeError, ValueError) as exc:
            raise DockerBrokerPolicyError("docker broker timeout_s must be an integer") from exc
        if timeout_s <= 0 or timeout_s > self.policy.max_timeout_s:
            raise DockerBrokerPolicyError("docker broker timeout_s exceeds policy bound")
        return timeout_s

    def _validate_request_policy(self, requested: Any) -> None:
        if not isinstance(requested, Mapping):
            raise DockerBrokerPolicyError("docker broker request policy must be a mapping")
        allowed_keys = {"network", "timeout_s"}
        unknown = sorted(set(requested) - allowed_keys)
        if unknown:
            raise DockerBrokerPolicyError(
                "docker broker request policy has unsupported key(s): " + ", ".join(unknown)
            )
        if "network" in requested and str(requested["network"]) != self.policy.network:
            raise DockerBrokerPolicyError("docker broker request network violates policy")
        if "timeout_s" in requested:
            self._select_timeout(requested["timeout_s"])

    def _validate_worker_payload(self, worker_payload: Mapping[str, Any]) -> Any:
        # Parse through the normal worker loader so malformed payloads fail before
        # any container is launched.
        parsed = payload_from_json(worker_payload)
        allowed = set(self.policy.env_allowlist)
        env_keys = set(parsed.safe_env) | set(parsed.spec.env)
        disallowed = sorted(key for key in env_keys if key not in allowed)
        if disallowed:
            raise DockerBrokerPolicyError(
                "docker broker worker env key(s) are not allowed by policy: "
                + ", ".join(disallowed)
            )
        return parsed

    def _docker_command(
        self, *, job_id: str, image: str, payload_json: str, timeout_s: int
    ) -> tuple[str, ...]:
        args: list[str] = [
            self.policy.docker_bin,
            "run",
            "--rm",
            "--name",
            job_id,
            "--network",
            self.policy.network,
            "--label",
            "mimir.worklink=true",
            "--label",
            f"mimir.worklink.timeout_s={timeout_s}",
        ]
        for key in sorted(self.policy.default_env):
            args.extend(["--env", key])
        # Operator-declared read-only credential mounts (e.g. ~/.codex). Always
        # :ro; the policy is static + operator-owned, so the agent can't add or
        # widen these.
        for mount in self.policy.creds_mounts:
            args.extend(["-v", f"{mount.source}:{mount.target}:ro"])
        args.append(image)
        args.extend(self.policy.worker_command)
        args.append(payload_json)
        return tuple(args)

    def _docker_env(self) -> Mapping[str, str]:
        return {**os.environ, **{str(key): str(value) for key, value in self.policy.default_env.items()}}

    def _job_response(
        self, job: DockerBrokerJob, *, force_timed_out: bool = False
    ) -> Mapping[str, Any]:
        timed_out = job.timed_out or force_timed_out
        if job.launch_error:
            status = "launch_error"
        elif timed_out:
            status = "timed_out"
        elif job.exit_code == 0:
            status = "completed"
        elif job.exit_code is None:
            status = "running"
        else:
            status = "failed"
        return {
            "job_id": job.job_id,
            "status": status,
            "exit_code": job.exit_code,
            "stdout": job.stdout,
            "stderr": job.stderr,
            "timed_out": timed_out,
            "launch_error": job.launch_error,
        }

    def _get_job(self, job_id: str) -> DockerBrokerJob:
        try:
            return self._jobs[job_id]
        except KeyError as exc:
            raise DockerBrokerPolicyError(f"unknown docker broker job: {job_id}") from exc

    def _new_job_id(self) -> str:
        prefix = "".join(
            ch for ch in self.policy.job_prefix if ch.isalnum() or ch in {"-", "_"}
        ).strip("-_")
        prefix = prefix or "worklink"
        return f"{prefix}-{uuid.uuid4().hex[:12]}"


async def run_broker(
    *,
    policy: DockerBrokerPolicy,
    host: str | None = None,
    port: int | None = None,
    socket_path: Path | None = None,
) -> None:
    """Run the Docker-sibling broker HTTP service until interrupted."""

    broker = DockerSiblingBroker(policy)
    runner = web.AppRunner(broker.app())
    await runner.setup()
    if socket_path is not None:
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass
        site: web.BaseSite = web.UnixSite(runner, str(socket_path))
    else:
        site = web.TCPSite(runner, host or "127.0.0.1", port or 8765)
    await site.start()
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


_ALLOWED_NETWORKS = {"none", "bridge"}


def _safe_env_name(value: str) -> bool:
    return bool(value) and all(ch.isalnum() or ch == "_" for ch in value) and not value[0].isdigit()


def _parse_creds_mounts(raw: Any) -> tuple[CredsMount, ...]:
    """Parse the policy's optional ``creds_mounts`` list. Each entry needs a
    ``source`` (host path); ``target`` defaults to ``source``. Always read-only."""
    if not raw:
        return ()
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise DockerBrokerPolicyError("docker broker creds_mounts must be a list of {source, target}")
    mounts: list[CredsMount] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise DockerBrokerPolicyError(
                "docker broker creds_mounts entries must be mappings with a source"
            )
        source = item.get("source")
        if not source:
            raise DockerBrokerPolicyError("docker broker creds_mount requires a source path")
        target = item.get("target") or source
        mounts.append(CredsMount(source=str(source), target=str(target)))
    return tuple(mounts)


def _decode(data: bytes | str) -> str:
    if isinstance(data, str):
        return data
    return data.decode("utf-8", errors="replace")


def _bounded_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n... [truncated {len(value) - limit} chars]"
