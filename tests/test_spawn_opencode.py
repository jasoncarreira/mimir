from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import shutil
import subprocess
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
        checkout = FakeCheckout(Path(seed), tmp_path / "issued")
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
async def test_prompt_agent_home_path_is_refused_without_launch(spawn_tree, tmp_path):
    home, seed = spawn_tree
    record = {}
    payload = await invoke(seed, tmp_path, record, prompt=f"read {home}/secret")
    assert payload["status"] == "prompt_refused"
    assert payload["proposal"] is None
    assert "argv" not in record


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
