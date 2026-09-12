from __future__ import annotations

import base64
import json
from pathlib import Path
import shlex
import subprocess

import pytest

import mimir.opencode_proposal as proposal_module
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
    _git(repo, "add", "-A")
    original_patch = subprocess.run(
        ("git", "-C", str(repo), "diff", "--cached", "--binary", "--full-index", "--no-ext-diff"),
        check=True, stdout=subprocess.PIPE,
    ).stdout
    result = build_opencode_proposal(repo, scrubber=SensitiveMaterialScrubber(home=tmp_path / "home"))
    assert result.reason_code == "proposal_created"
    assert result.proposal is not None
    patch = base64.b64decode(result.proposal.patch, validate=True)
    assert patch == original_patch
    assert len(patch) == result.proposal.byte_length
    assert result.proposal.kind == "git_binary_patch"
    assert (repo / "binary.dat").read_bytes() == payload
    clone = tmp_path / "clone"
    subprocess.run(("git", "clone", "-q", str(repo), str(clone)), check=True)
    applied = subprocess.run(("git", "-C", str(clone), "apply", "--binary", "-"), input=patch)
    assert applied.returncode == 0
    assert (clone / "binary.dat").read_bytes() == payload


@pytest.mark.parametrize("hardened", [True, False], ids=["hardened", "negative-control"])
@pytest.mark.parametrize("driver", ["clean", "process", "included-clean", "fsmonitor", "hooks", "textconv"])
def test_proposal_neutralizes_checkout_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, driver: str, hardened: bool,
) -> None:
    repo = _repository(tmp_path)
    (repo / "file.txt").write_text("after\n")
    (repo / ".gitattributes").write_text("file.txt filter=hostile diff=hostile\n")
    scrubber = SensitiveMaterialScrubber(home=tmp_path / "home")
    expected = build_opencode_proposal(repo, scrubber=scrubber)
    assert expected.reason_code == "proposal_created"
    _git(repo, "reset", "-q", "HEAD")
    marker = tmp_path / "executed"
    script = tmp_path / "hostile"
    script.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\ncat\n")
    script.chmod(0o700)
    command = shlex.quote(str(script))
    if driver == "included-clean":
        included = tmp_path / "included.config"
        included.write_text(f'[filter "hostile"]\nclean = {command}\n')
        _git(repo, "config", "include.path", str(included))
    elif driver in {"clean", "process"}:
        # A process driver need not complete its protocol to demonstrate execution.
        if driver == "process":
            script.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 1\n")
        _git(repo, "config", f"filter.hostile.{driver}", command)
    elif driver == "fsmonitor":
        script.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nprintf 'token\\000'\n")
        _git(repo, "config", "core.fsmonitor", str(script))
    elif driver == "hooks":
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        hook = hooks / "post-index-change"
        hook.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n")
        hook.chmod(0o700)
        _git(repo, "config", "core.hooksPath", str(hooks))
    else:
        _git(repo, "config", "diff.hostile.textconv", command)

    if not hardened:
        def original_run_git(checkout: Path, *args: str, limit: int | None = None) -> bytes:
            # Pre-fix argv/environment, including diff's former textconv default.
            args = tuple(arg for arg in args if arg != "--no-textconv")
            completed = subprocess.run(
                ("git", "-C", str(checkout), *args), check=False,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            if completed.returncode:
                raise OSError("proposal Git operation failed")
            return completed.stdout if limit is None else completed.stdout[:limit]

        monkeypatch.setattr(proposal_module, "_run_git", original_run_git)

    actual = build_opencode_proposal(repo, scrubber=scrubber)
    assert marker.exists() is not hardened
    if hardened:
        assert actual == expected


def test_proposal_git_uses_minimal_environment_for_every_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repository(tmp_path)
    (repo / "file.txt").write_text("after\n")
    for key in (
        "GITHUB_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "MIMIR_API_KEY",
        "SLACK_BOT_TOKEN", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0",
    ):
        monkeypatch.setenv(key, "controller-secret")
    original_popen = subprocess.Popen
    calls = []

    def checked_popen(command, *args, **kwargs):
        env = kwargs.get("env")
        assert env == proposal_module._sanitized_git_env()
        assert "controller-secret" not in env.values()
        calls.append(command)
        return original_popen(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", checked_popen)
    result = build_opencode_proposal(
        repo, scrubber=SensitiveMaterialScrubber(home=tmp_path / "home"),
    )
    assert result.reason_code == "proposal_created"
    assert len(calls) == 8  # Four operations, each with its own filter probe.
    for command in calls:
        assert "core.hooksPath=/dev/null" in command
        assert "core.fsmonitor=" in command


def test_proposal_refuses_failed_filter_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repository(tmp_path)
    (repo / "file.txt").write_text("after\n")
    monkeypatch.setattr(proposal_module, "_maintenance_git_filter_overrides", lambda *a, **kw: None)
    result = build_opencode_proposal(
        repo, scrubber=SensitiveMaterialScrubber(home=tmp_path / "home"),
    )
    assert result == ProposalBuildResult(None, "proposal_unavailable")


def test_proposal_name_stream_overflow_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repository(tmp_path)
    (repo / "file.txt").write_text("changed\n")
    monkeypatch.setattr(proposal_module, "MAX_PROPOSAL_NAME_STREAM_BYTES", 3)
    result = build_opencode_proposal(
        repo, scrubber=SensitiveMaterialScrubber(home=tmp_path / "home")
    )
    assert result == ProposalBuildResult(None, "path_bytes")


def test_proposal_patch_stream_overflow_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repository(tmp_path)
    (repo / "file.txt").write_text("changed enough to exceed the tiny patch cap\n")
    monkeypatch.setattr(proposal_module, "MAX_PROPOSAL_PATCH_BYTES", 16)
    result = build_opencode_proposal(
        repo, scrubber=SensitiveMaterialScrubber(home=tmp_path / "home")
    )
    assert result == ProposalBuildResult(None, "patch_bytes")


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
    assert prompt_contains_sensitive_source_path(f"read {home}-backup", agent_home=home, source_paths=(config,)) is None


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
