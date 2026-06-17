from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import mimir.commands.worklink as worklink_cmd
from mimir.worklink.backends import WorkSpec
from mimir.worklink.docker_broker import (
    DockerBrokerPolicy,
    DockerBrokerPolicyError,
    DockerSiblingBroker,
)
from mimir.worklink.worker import WorkerPayload, payload_to_json


class FakeProcess:
    _next_pid = 9000

    def __init__(self, *, returncode: int = 0, stdout: bytes = b"ok", stderr: bytes = b"") -> None:
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.communicated = False
        self.terminated = False
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        self.communicated = True
        return self._stdout, self._stderr

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class RecordingProcessFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.kwargs: list[dict[str, Any]] = []
        self.processes: list[FakeProcess] = []

    async def __call__(self, *args: str, **kwargs: Any) -> FakeProcess:
        self.calls.append(tuple(args))
        self.kwargs.append(kwargs)
        proc = FakeProcess()
        self.processes.append(proc)
        return proc


def _policy() -> DockerBrokerPolicy:
    return DockerBrokerPolicy(
        allowed_images=("mimir-worklink:latest",),
        network="none",
        max_timeout_s=60,
        env_allowlist=("GITHUB_TOKEN", "MIMIR_HOME"),
        default_env={"GITHUB_TOKEN": "redacted-test-token"},
    )


def _worker_payload(*, env: dict[str, str] | None = None) -> dict[str, Any]:
    return payload_to_json(
        WorkerPayload(
            spec=WorkSpec(
                issue_id=473,
                attempt=1,
                repo_url="git@github.com:jasoncarreira/mimir.git",
                base_ref="main",
                branch="issue/473-a1",
                prompt="do work",
                rules=None,
                test_command="echo ok",
                backend="codex",
                timeout_s=30,
                env=env if env is not None else {"MIMIR_HOME": "/mimir-home"},
                backend_config={"bin": "codex", "args": ["exec", "--json"]},
            ),
            repo_dir=Path("/work/repo"),
            evidence_path=Path("/work/evidence/evidence.json"),
            transcript_root=Path("/work/transcripts"),
            safe_env={},
        )
    )


@pytest.mark.asyncio
async def test_docker_broker_constructs_container_from_policy_not_agent_args() -> None:
    factory = RecordingProcessFactory()
    broker = DockerSiblingBroker(_policy(), process_factory=factory)

    response = await broker.submit_job(
        {
            "image": "mimir-worklink:latest",
            "policy": {"network": "none", "timeout_s": 30},
            "worker_payload": _worker_payload(),
            # Deliberately ignored/rejected API shape: callers do not get a raw
            # docker argument surface.
        }
    )
    result = await broker.wait_job(response["job_id"], timeout_s=5)

    assert result["status"] == "completed"
    assert factory.calls
    command = factory.calls[0]
    assert command[:7] == (
        "docker",
        "run",
        "--rm",
        "--name",
        response["job_id"],
        "--network",
        "none",
    )
    assert "--privileged" not in command
    assert "--volume" not in command
    assert "-v" not in command
    assert "--env" in command
    assert "GITHUB_TOKEN" in command
    assert "GITHUB_TOKEN=redacted-test-token" not in command
    assert factory.kwargs[0]["env"]["GITHUB_TOKEN"] == "redacted-test-token"
    assert f"mimir.worklink.timeout_s=30" in command
    assert command[-5:-1] == ("mimir", "worklink", "worker", "--payload-json")
    worker_payload = json.loads(command[-1])
    assert worker_payload["spec"]["branch"] == "issue/473-a1"
    assert worker_payload["repo_dir"] == "/work/repo"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_request, match",
    [
        ({"image": "evil:latest"}, "image is not allowed"),
        ({"policy": {"privileged": True}}, "unsupported key"),
        ({"policy": {"mounts": ["/:/host"]}}, "unsupported key"),
        ({"policy": {"network": "host"}}, "network violates policy"),
        ({"timeout_s": 61}, "timeout_s exceeds policy"),
        ({"docker_args": ["--privileged"]}, "unsupported key"),
    ],
)
async def test_docker_broker_rejects_escape_policy_requests(
    bad_request: dict[str, Any], match: str
) -> None:
    broker = DockerSiblingBroker(_policy(), process_factory=RecordingProcessFactory())
    request = {
        "image": "mimir-worklink:latest",
        "policy": {"network": "none"},
        "worker_payload": _worker_payload(),
        **bad_request,
    }

    with pytest.raises(DockerBrokerPolicyError, match=match):
        await broker.submit_job(request)


@pytest.mark.asyncio
async def test_docker_broker_rejects_worker_env_outside_allowlist() -> None:
    broker = DockerSiblingBroker(_policy(), process_factory=RecordingProcessFactory())

    with pytest.raises(DockerBrokerPolicyError, match="env key"):
        await broker.submit_job(
            {
                "image": "mimir-worklink:latest",
                "policy": {"network": "none"},
                "worker_payload": _worker_payload(env={"AWS_SECRET_ACCESS_KEY": "nope"}),
            }
        )


def test_docker_broker_policy_rejects_host_network_and_default_env_outside_allowlist() -> None:
    with pytest.raises(DockerBrokerPolicyError, match="network"):
        DockerBrokerPolicy(allowed_images=("img",), network="host")

    with pytest.raises(DockerBrokerPolicyError, match="default_env"):
        DockerBrokerPolicy(allowed_images=("img",), env_allowlist=("A",), default_env={"B": "1"})


@pytest.mark.asyncio
async def test_docker_broker_emits_operator_declared_readonly_creds_mounts() -> None:
    # chainlink #539: file-based backend creds (codex's ~/.codex/auth.json) can't
    # travel as an env var, so the operator policy may declare read-only mounts.
    factory = RecordingProcessFactory()
    policy = DockerBrokerPolicy.from_mapping(
        {
            "allowed_images": ["mimir-worklink:latest"],
            "network": "none",
            "max_timeout_s": 60,
            "env_allowlist": ["GITHUB_TOKEN", "MIMIR_HOME"],
            "default_env": {"GITHUB_TOKEN": "redacted-test-token"},
            "creds_mounts": [
                {"source": "/host/.codex/auth.json", "target": "/home/worker/.codex/auth.json"},
                {"source": "/host/.codex/config.toml"},  # target defaults to source
            ],
        }
    )
    broker = DockerSiblingBroker(policy, process_factory=factory)

    response = await broker.submit_job(
        {
            "image": "mimir-worklink:latest",
            "policy": {"network": "none"},
            "worker_payload": _worker_payload(),
        }
    )
    await broker.wait_job(response["job_id"], timeout_s=5)

    command = factory.calls[0]
    # Mounted read-only, before the image, with the documented :ro suffix.
    assert "-v" in command
    assert "/host/.codex/auth.json:/home/worker/.codex/auth.json:ro" in command
    assert "/host/.codex/config.toml:/host/.codex/config.toml:ro" in command  # target == source
    image_idx = command.index("mimir-worklink:latest")
    for i, tok in enumerate(command):
        if tok == "-v":
            assert i < image_idx  # mounts precede the image, not the worker args
            assert command[i + 1].endswith(":ro")  # never writable


def test_docker_broker_creds_mounts_reject_relative_paths_and_missing_source() -> None:
    with pytest.raises(DockerBrokerPolicyError, match="absolute"):
        DockerBrokerPolicy.from_mapping(
            {"allowed_images": ["img"], "creds_mounts": [{"source": "relative/path"}]}
        )
    with pytest.raises(DockerBrokerPolicyError, match="source"):
        DockerBrokerPolicy.from_mapping(
            {"allowed_images": ["img"], "creds_mounts": [{"target": "/x"}]}
        )
    with pytest.raises(DockerBrokerPolicyError, match="list"):
        DockerBrokerPolicy.from_mapping(
            {"allowed_images": ["img"], "creds_mounts": "/host/.codex:/x"}
        )


def test_docker_broker_default_policy_has_no_creds_mounts() -> None:
    # Opt-in: a policy without creds_mounts mounts nothing (the agent never gets one).
    assert DockerBrokerPolicy.from_mapping({"allowed_images": ["img"]}).creds_mounts == ()


def test_worklink_cli_registers_docker_broker_subcommand(tmp_path: Path) -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    worklink_cmd.add_argparse(sub)

    args = parser.parse_args(
        [
            "worklink",
            "docker-broker",
            "--policy",
            str(tmp_path / "policy.yaml"),
            "--socket",
            str(tmp_path / "broker.sock"),
        ]
    )

    assert args.command == "worklink"
    assert args.worklink_action == "docker-broker"
    assert args.policy == tmp_path / "policy.yaml"
    assert args.socket == tmp_path / "broker.sock"


def test_worklink_cli_rejects_invalid_docker_broker_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text("network: host\nallowed_images: [img]\n", encoding="utf-8")
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    worklink_cmd.add_argparse(sub)
    args = parser.parse_args(["worklink", "docker-broker", "--policy", str(policy_path)])

    assert worklink_cmd.dispatch(args, parser) == 2
    assert "error: docker broker network" in capsys.readouterr().err
