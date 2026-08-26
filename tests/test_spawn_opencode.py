from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import shutil
import os
import pwd
import uuid
import subprocess
import sys
from types import SimpleNamespace

import pytest

from mimir.contained_execution import CollectedExecutionResult
from mimir.tools.registry import (
    _SPAWN_DEPTH_ENV,
    _spawn_open_code_impl,
    _spawn_reset_for_tests,
    set_spawn_config,
)


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def spawn_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    _spawn_reset_for_tests()
    home = tmp_path / "home"
    seed = tmp_path / "repos" / "seed"
    home.mkdir()
    seed.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MIMIR_MODEL_SPEC", "codex-plus:test-model")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv(_SPAWN_DEPTH_ENV, raising=False)
    auth = home / ".local/share/opencode/auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(json.dumps({"openai": {"type": "oauth", "refresh": "refresh-secret"}}))
    _git(seed, "init")
    _git(seed, "config", "user.name", "Test")
    _git(seed, "config", "user.email", "test@example.invalid")
    (seed / "README.md").write_text("seed\n")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "seed")
    set_spawn_config({"default_cwd": tmp_path / "repos", "artifact_root": home / "artifacts"})
    return home, seed


class FakeCheckout:
    def __init__(self, seed: Path, destination: Path) -> None:
        subprocess.run(["git", "clone", "--quiet", str(seed), str(destination)], check=True)
        self.path = destination
        self.capability = SimpleNamespace(path=destination)
        self.base_tree = _git(destination, "rev-parse", "HEAD^{tree}")
        self.closed = False

    def close(self) -> None:
        self.closed = True


def factory_for(tmp_path: Path, record: dict):
    def factory(seed, *, default_cwd, known_sensitive=()):
        record["factory_seed"] = Path(seed)
        record["sensitive"] = tuple(known_sensitive)
        index = record.get("factory_calls", 0) + 1
        record["factory_calls"] = index
        checkout = FakeCheckout(Path(seed), tmp_path / f"issued-{index}")
        record["checkout"] = checkout
        return checkout
    return factory


def runner_for(record: dict, *, exit_code: int = 0, stdout: bytes = b"done", stderr: bytes = b""):
    async def runner(argv, directory, worker_env, projections=(), **kwargs):
        record["argv"] = list(argv)
        record["directory"] = directory
        record["env"] = dict(worker_env)
        record["projections"] = tuple(projections)
        record["kwargs"] = kwargs
        if exit_code == 0:
            (directory.path / "generated.txt").write_text("generated\n")
        return CollectedExecutionResult(exit_code, stdout, stderr, False, False, 0, 0)
    return runner


async def invoke(seed: Path, tmp_path: Path, record: dict, **kwargs):
    return json.loads(await _spawn_open_code_impl(
        kwargs.pop("prompt", "do the thing"),
        str(seed),
        kwargs.pop("timeout_s", 30),
        kwargs.pop("name", None),
        kwargs.pop("model", None),
        kwargs.pop("agent", None),
        kwargs.pop("artifact_root", None),
        contained_runner=kwargs.pop("runner", runner_for(record)),
        checkout_factory=kwargs.pop("factory", factory_for(tmp_path, record)),
    ))


@pytest.mark.asyncio
async def test_run_uses_fresh_checkout_worker_env_and_oauth_projection(spawn_tree, tmp_path):
    home, seed = spawn_tree
    record = {}
    payload = await invoke(seed, tmp_path, record, model="openai/gpt-5", agent="build")
    assert payload["status"] == "succeeded"
    assert record["factory_seed"] == seed
    assert record["directory"].path != seed
    assert record["argv"][:4] == ["opencode", "run", "--dir", "."]
    assert record["argv"][-2:] == ["--", "do the thing"]
    assert str(seed) not in record["argv"]
    assert "HOME" not in record["env"]
    assert record["env"][_SPAWN_DEPTH_ENV] == "1"
    assert str(home) not in json.dumps(record["env"])
    assert len(record["projections"]) == 2
    assert b"refresh-secret" in record["projections"][1].document
    assert record["checkout"].closed


@pytest.mark.asyncio
async def test_success_returns_lossless_proposal_and_relative_artifact(spawn_tree, tmp_path):
    _home, seed = spawn_tree
    record = {}
    payload = await invoke(seed, tmp_path, record)
    proposal = payload["proposal"]
    assert proposal["kind"] == "git_binary_patch"
    assert base64.b64decode(proposal["patch"], validate=True).startswith(b"diff --git")
    assert payload["artifact_dir"].startswith("opencode-")
    assert "/" not in payload["artifact_dir"]
    assert (tmp_path / "home/artifacts" / payload["artifact_dir"] / "proposal.json").is_file()
    assert not (seed / "generated.txt").exists()


@pytest.mark.asyncio
async def test_artifact_root_outside_home_is_refused_before_contained_launch(
    spawn_tree, tmp_path
):
    _home, seed = spawn_tree
    outside = tmp_path / "outside"
    outside.mkdir()
    record = {}
    payload = await invoke(seed, tmp_path, record, artifact_root=str(outside))
    assert payload["status"] == "artifact_unavailable"
    assert payload["artifact_dir"] is None
    assert "argv" not in record
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_artifact_root_symlink_escape_is_refused_before_contained_launch(
    spawn_tree, tmp_path
):
    home, seed = spawn_tree
    outside = tmp_path / "outside"
    outside.mkdir()
    escape = home / "escape"
    escape.symlink_to(outside, target_is_directory=True)
    record = {}
    payload = await invoke(seed, tmp_path, record, artifact_root=str(escape))
    assert payload["status"] == "artifact_unavailable"
    assert payload["artifact_dir"] is None
    assert "argv" not in record
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_artifact_write_refuses_run_directory_symlink_swap(
    spawn_tree, tmp_path
):
    home, seed = spawn_tree
    outside = tmp_path / "outside"
    outside.mkdir()
    record = {}

    async def swapping_runner(
        argv, directory, worker_env, projections=(), **kwargs
    ):
        record["argv"] = list(argv)
        run_directory = home / "artifacts" / kwargs["identifier"]
        assert run_directory.is_dir()
        shutil.rmtree(run_directory)
        run_directory.symlink_to(outside, target_is_directory=True)
        (directory.path / "generated.txt").write_text("generated\n")
        return CollectedExecutionResult(0, b"done", b"", False, False, 0, 0)

    payload = await invoke(seed, tmp_path, record, runner=swapping_runner)
    assert record["argv"][-1] == "do the thing"
    assert list(outside.iterdir()) == []
    assert payload["status"] == "artifact_unavailable"
    assert payload["artifact_dir"] is None


@pytest.mark.asyncio
async def test_prompt_agent_home_path_is_refused_without_launch(spawn_tree, tmp_path):
    home, seed = spawn_tree
    record = {}
    payload = await invoke(seed, tmp_path, record, prompt=f"read {home}/secret")
    assert payload["status"] == "prompt_refused"
    assert payload["proposal"] is None
    assert "argv" not in record


@pytest.mark.asyncio
@pytest.mark.parametrize("sensitive_kind", ("home", "auth"))
async def test_spawn_args_sensitive_path_is_configuration_refused(
    spawn_tree, tmp_path, monkeypatch, sensitive_kind
):
    home, seed = spawn_tree
    record = {}
    sensitive = (
        home / "secret"
        if sensitive_kind == "home"
        else home / ".local/share/opencode/auth.json"
    )
    monkeypatch.setenv("MIMIR_OPENCODE_SPAWN_ARGS", f"--label {sensitive}")
    payload = await invoke(seed, tmp_path, record)
    assert payload["status"] == "configuration_refused"
    assert "argv" not in record
    assert "factory_seed" not in record


@pytest.mark.asyncio
@pytest.mark.parametrize("sensitive_kind", ("home", "auth"))
async def test_model_sensitive_path_is_configuration_refused(
    spawn_tree, tmp_path, sensitive_kind
):
    home, seed = spawn_tree
    record = {}
    sensitive = (
        home / "model"
        if sensitive_kind == "home"
        else home / ".local/share/opencode/auth.json"
    )
    payload = await invoke(seed, tmp_path, record, model=f"openai/{sensitive}")
    assert payload["status"] == "configuration_refused"
    assert "argv" not in record
    assert "factory_seed" not in record


@pytest.mark.asyncio
@pytest.mark.parametrize("sensitive_kind", ("home", "auth"))
async def test_agent_sensitive_path_is_prompt_refused(
    spawn_tree, tmp_path, sensitive_kind
):
    home, seed = spawn_tree
    record = {}
    sensitive = (
        home / "agent"
        if sensitive_kind == "home"
        else home / ".local/share/opencode/auth.json"
    )
    payload = await invoke(seed, tmp_path, record, agent=str(sensitive))
    assert payload["status"] == "prompt_refused"
    assert "argv" not in record
    assert "factory_seed" not in record


@pytest.mark.asyncio
@pytest.mark.parametrize("sensitive_kind", ("home", "auth"))
async def test_worker_environment_sensitive_path_is_configuration_refused(
    spawn_tree, tmp_path, monkeypatch, sensitive_kind
):
    from mimir import contained_execution

    home, seed = spawn_tree
    record = {}
    original = contained_execution.opencode_worker_environment
    sensitive = (
        home / "secret"
        if sensitive_kind == "home"
        else home / ".local/share/opencode/auth.json"
    )

    def unsafe_environment(base, invocation):
        environment = original(base, invocation)
        environment["VALIDATOR_SELECTOR"] = str(sensitive)
        return environment

    monkeypatch.setattr(
        contained_execution, "opencode_worker_environment", unsafe_environment
    )
    payload = await invoke(seed, tmp_path, record)
    assert payload["status"] == "configuration_refused"
    assert "argv" not in record
    assert "factory_seed" not in record


@pytest.mark.asyncio
@pytest.mark.parametrize("ingress", ("spawn_args", "model", "agent", "environment"))
async def test_benign_complete_argv_and_environment_values_launch(
    spawn_tree, tmp_path, monkeypatch, ingress
):
    from mimir import contained_execution

    _home, seed = spawn_tree
    record = {}
    kwargs = {}
    if ingress == "spawn_args":
        monkeypatch.setenv("MIMIR_OPENCODE_SPAWN_ARGS", "--label benign")
    elif ingress == "model":
        kwargs["model"] = "openai/gpt-5"
    elif ingress == "agent":
        kwargs["agent"] = "build"
    else:
        original = contained_execution.opencode_worker_environment

        def benign_environment(base, invocation):
            environment = original(base, invocation)
            environment["VALIDATOR_SELECTOR"] = "/opt/operator/reference"
            return environment

        monkeypatch.setattr(
            contained_execution, "opencode_worker_environment", benign_environment
        )
    payload = await invoke(seed, tmp_path, record, **kwargs)
    assert payload["status"] == "succeeded"
    assert "argv" in record


@pytest.mark.asyncio
async def test_unrelated_path_in_prompt_is_verbatim(spawn_tree, tmp_path):
    _home, seed = spawn_tree
    record = {}
    prompt = "compare /opt/operator/reference exactly"
    await invoke(seed, tmp_path, record, prompt=prompt)
    assert record["argv"][-1] == prompt


@pytest.mark.asyncio
async def test_containment_failure_is_fail_closed(spawn_tree, tmp_path):
    _home, seed = spawn_tree
    record = {}
    async def unavailable(*args, **kwargs):
        raise OSError("socket unavailable")
    payload = await invoke(seed, tmp_path, record, runner=unavailable)
    assert payload["status"] == "containment_unavailable"
    assert payload["proposal"] is None
    assert not (seed / "generated.txt").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ("capability admission rejected", "request too large"))
async def test_contained_client_value_error_is_fail_closed(
    spawn_tree, tmp_path, monkeypatch, message
):
    from mimir import event_logger

    _home, seed = spawn_tree
    record = {}
    events = []

    async def invalid_request(*args, **kwargs):
        raise ValueError(message)

    async def capture(event, **fields):
        events.append((event, fields))

    monkeypatch.setattr(event_logger, "safe_log_event", capture)
    payload = await invoke(seed, tmp_path, record, runner=invalid_request)
    assert payload["status"] == "containment_unavailable"
    assert payload["proposal"] is None
    assert events == [("spawn_open_code_containment_refused", {
        "run_id": payload["run_id"],
        "status": "containment_unavailable",
        "reason_code": "containment_unavailable",
    })]


@pytest.mark.asyncio
async def test_unrelated_runner_programmer_error_is_not_swallowed(spawn_tree, tmp_path):
    _home, seed = spawn_tree
    record = {}

    async def programmer_error(*args, **kwargs):
        raise TypeError("programmer error")

    with pytest.raises(TypeError, match="programmer error"):
        await invoke(seed, tmp_path, record, runner=programmer_error)


@pytest.mark.asyncio
async def test_nonzero_worker_is_classified(spawn_tree, tmp_path):
    _home, seed = spawn_tree
    record = {}
    payload = await invoke(
        seed, tmp_path, record,
        runner=runner_for(record, exit_code=1, stderr=b"401 unauthorized"),
    )
    assert payload["status"] == "authentication_required"
    assert payload["exit_code"] == 1


@pytest.mark.asyncio
async def test_sensitive_output_is_scrubbed_everywhere(spawn_tree, tmp_path):
    home, seed = spawn_tree
    record = {}
    payload = await invoke(
        seed, tmp_path, record,
        runner=runner_for(record, stdout=b"refresh-secret", stderr=str(home).encode()),
    )
    encoded = json.dumps(payload)
    assert "refresh-secret" not in encoded
    assert str(home) not in encoded
    artifact = tmp_path / "home/artifacts" / payload["artifact_dir"]
    for path in artifact.iterdir():
        assert b"refresh-secret" not in path.read_bytes()
        assert str(home).encode() not in path.read_bytes()


@pytest.mark.asyncio
async def test_sensitive_material_absent_from_real_event_logger_and_caplog(
    spawn_tree, tmp_path, caplog
):
    from mimir import event_logger

    home, seed = spawn_tree
    auth_path = home / ".local/share/opencode/auth.json"
    config_path = home / ".config/opencode/opencode.jsonc"
    credential = b"opaque projected credential 47!"
    auth_path.write_bytes(json.dumps({
        "openai": {"type": "oauth", "refresh": credential.decode()}
    }).encode())
    event_path = tmp_path / "events.jsonl"
    event_logger._reset_logger_for_tests()
    event_logger.init_logger(event_path, "spawn-sensitive-test")
    record = {}
    emitted = b" | ".join((credential, os.fsencode(auth_path), os.fsencode(config_path)))
    caplog.clear()
    try:
        payload = await invoke(
            seed,
            tmp_path,
            record,
            runner=runner_for(record, stdout=emitted, stderr=emitted),
        )
        event_bytes = event_path.read_bytes()
        log_bytes = "\n".join(record.getMessage() for record in caplog.records).encode()
        artifact = home / "artifacts" / payload["artifact_dir"]
        observed_sinks = [
            json.dumps(payload, sort_keys=True).encode(),
            event_bytes,
            log_bytes,
            *(path.read_bytes() for path in artifact.iterdir()),
        ]
        assert b"spawn_open_code_completed" in event_bytes
        for sensitive in (credential, os.fsencode(auth_path), os.fsencode(config_path)):
            assert all(sensitive not in sink for sink in observed_sinks)
    finally:
        event_logger._reset_logger_for_tests()


@pytest.mark.asyncio
async def test_artifact_run_directory_failure_precedes_execution(
    spawn_tree, tmp_path, monkeypatch
):
    from mimir import event_logger, opencode_config
    from mimir.opencode_config import OpenCodeConfigError
    _home, seed = spawn_tree
    record = {}
    events = []
    async def capture(event, **fields):
        events.append((event, fields))
    monkeypatch.setattr(event_logger, "safe_log_event", capture)
    monkeypatch.setattr(
        opencode_config, "resolve_opencode_invocation",
        lambda *a, **k: (_ for _ in ()).throw(OpenCodeConfigError("config_malformed")),
    )
    artifact_file = tmp_path / "home" / "not-a-directory"
    artifact_file.write_text("occupied")
    payload = await invoke(
        seed, tmp_path, record, artifact_root=str(artifact_file)
    )
    assert payload["status"] == "artifact_unavailable"
    assert payload["artifact_dir"] is None
    assert "argv" not in record
    assert events == [("spawn_open_code_provisioning_refused", {
        "run_id": payload["run_id"],
        "status": "artifact_unavailable",
        "reason_code": "artifact_unavailable",
    })]


@pytest.mark.asyncio
async def test_generated_payload_containment_and_unsafe_negative_control(
    spawn_tree, tmp_path
):
    from mimir.contained_execution import execute_contained

    _home, seed = spawn_tree
    controller_home = tmp_path / "controller-home"
    controller_home.mkdir()
    canary_name = f".mimir-spawn-canary-{uuid.uuid4().hex}"
    canary = controller_home / canary_name
    worker_home = tmp_path / "worker-home"
    worker_home.mkdir()
    worker_canary = worker_home / canary_name
    generated_input = "generate and execute the account-home probe"

    def materialize(prompt: str, checkout: Path) -> Path:
        assert prompt == generated_input
        attack = checkout / "generated_attack.py"
        attack.write_text(
            "import os,pwd\n"
            "from pathlib import Path\n"
            f"Path(pwd.getpwuid(os.getuid()).pw_dir, {canary_name!r}).write_text('breach')\n"
            "Path('payload-executed').write_text('executed')\n"
        )
        return attack

    class Process:
        def __init__(self, completed: subprocess.CompletedProcess[bytes]) -> None:
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_data(completed.stdout)
            self.stdout.feed_eof()
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_data(completed.stderr)
            self.stderr.feed_eof()
            self.returncode = completed.returncode

        async def wait(self) -> int:
            return self.returncode

    class ContainedPayloadClient:
        async def launch(self, **kwargs):
            checkout = Path(kwargs["local_checkout"])
            prompt = kwargs["argv"][-1]
            attack = materialize(prompt, checkout)
            wrapper = (
                "import pwd,runpy,sys,types;"
                f"pwd.getpwuid=lambda uid:types.SimpleNamespace(pw_dir={str(worker_home)!r});"
                "runpy.run_path(sys.argv[1],run_name='__main__')"
            )
            completed = subprocess.run(
                [sys.executable, "-c", wrapper, str(attack)],
                cwd=checkout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            return Process(completed)

        async def cancel(self, identifier: str) -> None:
            raise AssertionError(f"unexpected cancellation: {identifier}")

    positive = {}

    def contained_factory(*args, **kwargs):
        checkout = factory_for(tmp_path / "positive", positive)(*args, **kwargs)
        checkout.capability._contained_worker_client = ContainedPayloadClient()
        return checkout

    async def unsafe_runner(argv, directory, worker_env, projections=(), **kwargs):
        attack = materialize(argv[-1], directory.path)
        wrapper = (
            "import pwd,runpy,sys,types;"
            f"pwd.getpwuid=lambda uid:types.SimpleNamespace(pw_dir={str(controller_home)!r});"
            "runpy.run_path(sys.argv[1],run_name='__main__')"
        )
        completed = subprocess.run(
            [sys.executable, "-c", wrapper, str(attack)],
            cwd=directory.path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        return CollectedExecutionResult(
            completed.returncode,
            completed.stdout,
            completed.stderr,
            False,
            False,
            0,
            0,
        )

    try:
        result = await invoke(
            seed,
            tmp_path,
            positive,
            prompt=generated_input,
            runner=execute_contained,
            factory=contained_factory,
        )
        assert result["status"] == "succeeded"
        assert (positive["checkout"].path / "payload-executed").read_text() == "executed"
        assert worker_canary.read_text() == "breach"
        assert not canary.exists()

        negative = {}
        result = await invoke(
            seed,
            tmp_path,
            negative,
            prompt=generated_input,
            runner=unsafe_runner,
            factory=factory_for(tmp_path / "negative", negative),
        )
        assert result["status"] == "succeeded"
        assert (negative["checkout"].path / "payload-executed").read_text() == "executed"
        assert canary.read_text() == "breach"
    finally:
        canary.unlink(missing_ok=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "status", "exit_code", "reason", "event_type", "proposal_file"),
    (
        ("config", "configuration_refused", None, "config_malformed", "spawn_open_code_configuration_refused", False),
        ("auth", "authentication_refused", None, "auth_invalid", "spawn_open_code_authentication_refused", False),
        ("prompt_auth", "prompt_refused", None, "config_auth_source_path", "spawn_open_code_prompt_refused", False),
        ("credentials", "seed_credentials_refused", None, "seed_credentials", "spawn_open_code_provisioning_refused", False),
        ("invalid_seed", "seed_refused", None, "invalid_seed", "spawn_open_code_provisioning_refused", False),
        ("unsafe_seed", "seed_refused", None, "unsafe_seed_entry", "spawn_open_code_provisioning_refused", False),
        ("seed_changed", "seed_refused", None, "seed_changed", "spawn_open_code_provisioning_refused", False),
        ("provisioning", "provisioning_unavailable", None, "provisioning_unavailable", "spawn_open_code_provisioning_refused", False),
        ("containment", "containment_unavailable", None, "containment_unavailable", "spawn_open_code_containment_refused", False),
        ("timeout", "timeout", None, "timeout", "spawn_open_code_completed", False),
        ("overflow", "output_overflow", 0, "output_overflow", "spawn_open_code_completed", False),
        ("auth_worker", "authentication_required", 1, "worker_authentication_required", "spawn_open_code_completed", False),
        ("failed", "failed", 2, "worker_nonzero", "spawn_open_code_completed", False),
        ("no_changes", "succeeded", 0, "no_changes", "spawn_open_code_completed", False),
        ("path_count", "proposal_overflow", 0, "path_count", "spawn_open_code_completed", False),
        ("path_bytes", "proposal_overflow", 0, "path_bytes", "spawn_open_code_completed", False),
        ("patch_bytes", "proposal_overflow", 0, "patch_bytes", "spawn_open_code_completed", False),
        ("sensitive", "proposal_sensitive_content", 0, "proposal_sensitive_content", "spawn_open_code_completed", False),
        ("proposal_unavailable", "proposal_unavailable", 0, "proposal_unavailable", "spawn_open_code_completed", False),
        ("proposal", "succeeded", 0, "proposal_created", "spawn_open_code_completed", True),
    ),
)
async def test_terminal_state_contract(
    spawn_tree, tmp_path, monkeypatch, case, status, exit_code, reason, event_type,
    proposal_file,
):
    from mimir import event_logger, opencode_config, opencode_proposal
    from mimir.contained_snapshot import (
        SnapshotCredentialsRefused, SnapshotSourceChanged, SnapshotUnsafeEntry,
    )
    from mimir.opencode_config import OpenCodeAuthError, OpenCodeConfigError
    from mimir.opencode_proposal import OpenCodeProposal, ProposalBuildResult

    home, seed = spawn_tree
    record = {}
    events = []

    async def capture(event, **fields):
        events.append((event, fields))
    monkeypatch.setattr(event_logger, "safe_log_event", capture)

    factory = factory_for(tmp_path, record)
    runner = runner_for(record, stdout=b"refresh-secret", stderr=b"refresh-secret")
    prompt = "task refresh-secret"
    if case == "config":
        prompt = f"task {home}"
        monkeypatch.setattr(
            opencode_config, "resolve_opencode_invocation",
            lambda *a, **k: (_ for _ in ()).throw(OpenCodeConfigError("config_malformed")),
        )
    elif case == "auth":
        prompt = f"task {home}"
        monkeypatch.setattr(
            opencode_config, "resolve_opencode_invocation",
            lambda *a, **k: (_ for _ in ()).throw(OpenCodeAuthError("auth_invalid")),
        )
    elif case == "prompt_auth":
        config_source = tmp_path / "repos" / "opencode.json"
        config_source.write_text(json.dumps({"model": "openai/test-model"}))
        set_spawn_config({
            "default_cwd": tmp_path / "repos",
            "artifact_root": tmp_path / "home/artifacts",
            "opencode_config_path": config_source,
        })
        prompt = str(config_source)
    elif case in {"credentials", "invalid_seed", "unsafe_seed", "seed_changed", "provisioning"}:
        error = {
            "credentials": SnapshotCredentialsRefused(),
            "invalid_seed": ValueError("bad seed"),
            "unsafe_seed": SnapshotUnsafeEntry("unsafe"),
            "seed_changed": SnapshotSourceChanged("changed"),
            "provisioning": OSError("disk"),
        }[case]
        def factory(*args, **kwargs):
            raise error
    elif case == "containment":
        async def runner(*args, **kwargs):
            raise OSError("socket")
    elif case in {"timeout", "overflow", "auth_worker", "failed"}:
        values = {
            "timeout": (None, False, True, b""),
            "overflow": (0, True, False, b""),
            "auth_worker": (1, False, False, b"401 unauthorized"),
            "failed": (2, False, False, b"compile failed"),
        }[case]
        async def runner(argv, directory, worker_env, projections=(), **kwargs):
            code, overflow, timed_out, err = values
            return CollectedExecutionResult(
                code,
                b"refresh-secret",
                err + b" refresh-secret",
                timed_out,
                overflow,
                0,
                0,
            )
    elif case == "no_changes":
        runner = runner_for(record, exit_code=0)
        async def runner(argv, directory, worker_env, projections=(), **kwargs):
            return CollectedExecutionResult(
                0, b"refresh-secret", b"refresh-secret", False, False, 0, 0
            )
    elif case in {"path_count", "path_bytes", "patch_bytes", "sensitive", "proposal_unavailable", "proposal"}:
        proposal_reason = {
            "sensitive": "proposal_sensitive_content",
            "proposal_unavailable": "proposal_unavailable",
        }.get(case, case if case != "proposal" else "proposal_created")
        proposal = None
        if case == "proposal":
            proposal = OpenCodeProposal(1, "git_binary_patch", "a" * 40, "base64", 0, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "")
        monkeypatch.setattr(
            opencode_proposal, "build_opencode_proposal",
            lambda *a, **k: ProposalBuildResult(proposal, proposal_reason),
        )

    payload = await invoke(
        seed, tmp_path, record, prompt=prompt, runner=runner, factory=factory
    )
    assert payload["status"] == status
    assert payload["exit_code"] == exit_code
    assert (payload["proposal"] is not None) is proposal_file
    assert events == [(event_type, {
        "run_id": payload["run_id"],
        "status": status,
        "reason_code": reason,
    })]
    artifact = tmp_path / "home/artifacts" / payload["artifact_dir"]
    expected_artifacts = {"prompt.md", "stdout.txt", "stderr.txt", "manifest.json"}
    if proposal_file:
        expected_artifacts.add("proposal.json")
    assert {path.name for path in artifact.iterdir()} == expected_artifacts
    manifest = json.loads((artifact / "manifest.json").read_text())
    assert manifest["status"] == status
    assert manifest["reason_code"] == reason
    for artifact_path in artifact.iterdir():
        artifact_bytes = artifact_path.read_bytes()
        assert b"refresh-secret" not in artifact_bytes
        assert str(home).encode() not in artifact_bytes
    assert "<redacted>" in (artifact / "prompt.md").read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected_status", "expected_reason"),
    (
        ("configuration_before_prompt", "configuration_refused", "config_malformed"),
        ("prompt_before_provisioning", "prompt_refused", "agent_home_path"),
        ("provisioning_before_containment", "seed_credentials_refused", "seed_credentials"),
        ("timeout_before_overflow", "timeout", "timeout"),
        ("overflow_before_nonzero", "output_overflow", "output_overflow"),
    ),
)
async def test_terminal_state_first_match_precedence(
    spawn_tree, tmp_path, monkeypatch, case, expected_status, expected_reason
):
    from mimir import opencode_config
    from mimir.contained_snapshot import SnapshotCredentialsRefused
    from mimir.opencode_config import OpenCodeConfigError

    home, seed = spawn_tree
    record = {}
    prompt = "task"
    factory = factory_for(tmp_path, record)
    runner = runner_for(record)

    if case == "configuration_before_prompt":
        prompt = f"inspect {home}/secret"
        monkeypatch.setattr(
            opencode_config,
            "resolve_opencode_invocation",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                OpenCodeConfigError("config_malformed")
            ),
        )

        def factory(*args, **kwargs):
            raise AssertionError("provisioning must not run")
    elif case == "prompt_before_provisioning":
        prompt = f"inspect {home}/secret"

        def factory(*args, **kwargs):
            raise AssertionError("provisioning must not run")
    elif case == "provisioning_before_containment":
        def factory(*args, **kwargs):
            raise SnapshotCredentialsRefused()

        async def runner(*args, **kwargs):
            raise AssertionError("containment must not run")
    elif case == "timeout_before_overflow":
        async def runner(*args, **kwargs):
            return CollectedExecutionResult(1, b"", b"401", True, True, 0, 0)
    elif case == "overflow_before_nonzero":
        async def runner(*args, **kwargs):
            return CollectedExecutionResult(1, b"", b"401", False, True, 0, 0)

    payload = await invoke(
        seed,
        tmp_path,
        record,
        prompt=prompt,
        runner=runner,
        factory=factory,
    )
    assert payload["status"] == expected_status
    artifact = tmp_path / "home/artifacts" / payload["artifact_dir"]
    manifest = json.loads((artifact / "manifest.json").read_text())
    assert manifest["reason_code"] == expected_reason


@pytest.mark.asyncio
async def test_post_execution_artifact_write_failure_is_artifact_unavailable(
    spawn_tree, tmp_path, monkeypatch
):
    from mimir import event_logger

    _home, seed = spawn_tree
    record = {}
    events = []

    async def capture(event, **fields):
        events.append((event, fields))

    async def runner(argv, directory, worker_env, projections=(), **kwargs):
        record["argv"] = list(argv)
        (directory.path / "generated.txt").write_text("generated\n")
        artifact_directory = tmp_path / "home/artifacts" / kwargs["identifier"]
        stdout_path = artifact_directory / "stdout.txt"
        stdout_path.unlink()
        stdout_path.mkdir()
        return CollectedExecutionResult(0, b"done", b"", False, False, 0, 0)

    monkeypatch.setattr(event_logger, "safe_log_event", capture)
    payload = await invoke(seed, tmp_path, record, runner=runner)

    assert record["argv"][-1] == "do the thing"
    assert payload["status"] == "artifact_unavailable"
    assert payload["artifact_dir"] is None
    artifact = tmp_path / "home/artifacts" / payload["run_id"]
    assert not (artifact / "proposal.json").exists()
    assert events == [("spawn_open_code_provisioning_refused", {
        "run_id": payload["run_id"],
        "status": "artifact_unavailable",
        "reason_code": "artifact_unavailable",
    })]


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_mutate_terminal_result(
    spawn_tree, tmp_path, monkeypatch
):
    from mimir import event_logger
    _home, seed = spawn_tree
    record = {}
    events = []
    async def capture(event, **fields):
        events.append((event, fields))
    monkeypatch.setattr(event_logger, "safe_log_event", capture)
    factory = factory_for(tmp_path, record)
    def failing_factory(*args, **kwargs):
        checkout = factory(*args, **kwargs)
        def close():
            raise OSError("cleanup")
        checkout.close = close
        return checkout
    payload = await invoke(seed, tmp_path, record, factory=failing_factory)
    assert payload["status"] == "succeeded"
    assert events[0][0] == "spawn_open_code_cleanup_failed"
    assert events[-1][0] == "spawn_open_code_completed"
