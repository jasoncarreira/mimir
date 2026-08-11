from __future__ import annotations

import base64
import json
from pathlib import Path
import subprocess

import pytest

from mimir.contained_execution import SensitiveMaterialScrubber
from mimir.opencode_proposal import (
    ProposalBuildResult,
    build_opencode_proposal,
    classify_spawn_terminal_state,
    prompt_contains_sensitive_source_path,
    resolve_artifact_handle,
    write_spawn_artifacts,
)


def _git(path: Path, *args: str) -> None:
    subprocess.run(("git", "-C", str(path), *args), check=True, stdout=subprocess.PIPE)


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "Worker")
    (repo / "file.txt").write_text("before\n")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-qm", "seed")
    return repo


def test_proposal_is_lossless_and_does_not_apply_changes(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    payload = b"after\x00binary\xff\n"
    (repo / "binary.dat").write_bytes(payload)
    result = build_opencode_proposal(repo, scrubber=SensitiveMaterialScrubber(home=tmp_path / "home"))
    assert result.reason_code == "proposal_created"
    assert result.proposal is not None
    patch = base64.b64decode(result.proposal.patch, validate=True)
    assert len(patch) == result.proposal.byte_length
    assert result.proposal.kind == "git_binary_patch"
    assert (repo / "binary.dat").read_bytes() == payload
    clone = tmp_path / "clone"
    subprocess.run(("git", "clone", "-q", str(repo), str(clone)), check=True)
    applied = subprocess.run(("git", "-C", str(clone), "apply", "--binary", "-"), input=patch)
    assert applied.returncode == 0
    assert (clone / "binary.dat").read_bytes() == payload


def test_proposal_refuses_sensitive_patch(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    secret = "credential-value"
    (repo / "file.txt").write_text(secret)
    scrubber = SensitiveMaterialScrubber(home=tmp_path / "home")
    scrubber.add_scalar(secret)
    result = build_opencode_proposal(repo, scrubber=scrubber)
    assert result == ProposalBuildResult(None, "proposal_sensitive_content")


def test_prompt_policy_is_finite_and_preserves_other_text(tmp_path: Path) -> None:
    home = tmp_path / "agent"
    config = tmp_path / "config.json"
    assert prompt_contains_sensitive_source_path(f"read {home / 'canary'}", agent_home=home, source_paths=(config,)) == "agent_home_path"
    assert prompt_contains_sensitive_source_path(config.as_uri(), agent_home=home, source_paths=(config,)) == "config_auth_source_path"
    prompt = f"describe {tmp_path / 'unrelated'} byte-for-character"
    assert prompt_contains_sensitive_source_path(prompt, agent_home=home, source_paths=(config,)) is None
    assert prompt_contains_sensitive_source_path(f"read {config}.backup", agent_home=home, source_paths=(config,)) is None


def test_artifacts_are_scrubbed_and_handle_is_relative(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    secret = "do-not-publish"
    scrubber = SensitiveMaterialScrubber(home=tmp_path / "home")
    scrubber.add_scalar(secret)
    handle = write_spawn_artifacts(
        root,
        "run-1",
        prompt=f"prompt {secret}",
        stdout=secret.encode(),
        stderr=b"safe",
        manifest={"schema_version": 2, "status": "failed", "detail": secret},
        scrubber=scrubber,
    )
    assert handle == "run-1"
    directory = resolve_artifact_handle(root, handle)
    assert sorted(path.name for path in directory.iterdir()) == ["manifest.json", "prompt.md", "stderr.txt", "stdout.txt"]
    assert secret not in "".join(path.read_text() for path in directory.iterdir())
    assert json.loads((directory / "manifest.json").read_text())["detail"] == "<redacted>"
    with pytest.raises(ValueError):
        resolve_artifact_handle(root, "../escape")


def test_terminal_state_uses_first_matching_condition() -> None:
    state = classify_spawn_terminal_state(
        configuration_reason="malformed_config",
        prompt_reason="agent_home_path",
        containment_available=False,
    )
    assert (state.status, state.reason_code, state.event_type) == (
        "configuration_refused",
        "malformed_config",
        "spawn_open_code_configuration_refused",
    )
    success = classify_spawn_terminal_state(
        exit_code=0,
        proposal_result=ProposalBuildResult(None, "no_changes"),
    )
    assert success.status == "succeeded"
    assert success.reason_code == "no_changes"
