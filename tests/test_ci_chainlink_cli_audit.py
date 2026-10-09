"""Protect the pinned Chainlink audit and its credential-free build boundary."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SHA = "0e830bf230fdade13b148a99a1a3c19bfbd33b98"
AUTH_KEYS = ("GITHUB_TOKEN", "GH_TOKEN", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0")


def _workflow() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())


def _step(name: str) -> dict:
    steps = _workflow()["jobs"]["chainlink-cli-audit"]["steps"]
    return next(step for step in steps if step.get("name") == name)


def test_chainlink_cli_audit_prepares_evidence_before_install() -> None:
    steps = _workflow()["jobs"]["chainlink-cli-audit"]["steps"]
    names = [step.get("name") for step in steps]
    assert names.index("Prepare pytest evidence directory") < names.index("Fetch pinned Chainlink source")
    assert names.index("Fetch pinned Chainlink source") < names.index("Install pinned Chainlink CLI")
    assert names.index("Install pinned Chainlink CLI") < names.index("Audit live Chainlink command surface")
    assert _workflow()["permissions"] == {"contents": "read"}
    assert "permissions" not in _workflow()["jobs"]["chainlink-cli-audit"]


def test_chainlink_cli_fetch_uses_scoped_auth_git_and_bounded_retry() -> None:
    step = _step("Fetch pinned Chainlink source")
    script, env = step["run"], step["env"]
    assert env["GITHUB_TOKEN"] == "${{ github.token }}"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "credential.https://github.com.helper"
    assert '"$GITHUB_TOKEN"' in env["GIT_CONFIG_VALUE_0"]
    assert "password=%s" in env["GIT_CONFIG_VALUE_0"]
    assert "echo" not in env["GIT_CONFIG_VALUE_0"]
    assert "set -x" not in script
    assert "echo $GITHUB_TOKEN" not in script
    assert "echo \"$GITHUB_TOKEN\"" not in script
    assert "${{ github.token }}" not in script
    assert "cargo " not in script
    assert "git ls-remote --exit-code https://x-access-token@github.com/dollspace-gay/chainlink.git" in script
    assert f"expected_sha={EXPECTED_SHA}" in script
    assert "refs/tags/chainlink-1.6.0" in script
    assert 'rev-parse FETCH_HEAD)" != "$expected_sha"' in script
    assert 'checkout --detach "$expected_sha"' in script
    assert "for attempt in 1 2 3; do" in script
    assert "sleep $((30 * attempt))" in script


def test_chainlink_cli_cargo_invocations_have_no_token_environment() -> None:
    """Lint both metadata and install, not just the fetch/build step names."""
    workflow = _workflow()
    job = workflow["jobs"]["chainlink-cli-audit"]
    build = _step("Install pinned Chainlink CLI")
    for layer in (workflow, job, build):
        assert not set(AUTH_KEYS).intersection(layer.get("env", {}))
    script = build["run"]
    assert "unset " + " ".join(AUTH_KEYS) in script
    assert script.index("unset ") < script.index("cargo metadata") < script.index("cargo install")
    assert 'cargo install --path "$crate_path" --locked --root "$RUNNER_TEMP/chainlink"' in script
    assert "--git" not in script
    assert "github.token" not in script
    assert "for attempt in 1 2 3; do" in script
    assert "sleep $((30 * attempt))" in script


def _command(commands: Path, name: str, script: str) -> None:
    path = commands / name
    path.write_text("#!/bin/sh\nset -eu\n" + script)
    path.chmod(0o755)


def _environment(tmp_path: Path) -> dict[str, str]:
    commands = tmp_path / "commands"
    commands.mkdir()
    _command(commands, "sleep", 'printf "sleep %s\\n" "$*" >> "$CALLS"\n')
    return {
        "PATH": f"{commands}:{Path(sys.executable).parent}:{os.defpath}",
        "CALLS": str(tmp_path / "calls"),
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_PATH": str(tmp_path / "github-path"),
        "EXPECTED_SHA": EXPECTED_SHA,
    }


def _run(step: dict, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", "-c", step["run"]], env=env,
                          capture_output=True, text=True, timeout=10)


def _fake_git(commands: Path) -> None:
    _command(commands, "git", '''
# The authenticated fetch really receives the step-scoped helper and token.
test "$GITHUB_TOKEN" = secret-test-token
test "$GIT_CONFIG_KEY_0" = credential.https://github.com.helper
printf 'git %s\\n' "$*" >> "$CALLS"
if [ "$1" = init ]; then exit 0; fi
if [ "$1" = ls-remote ]; then
  if [ "$MODE" = exhausted ]; then exit 1; fi
  if [ "$MODE" = retry ] && [ "$(grep -c '^git ls-remote' "$CALLS")" -eq 1 ]; then exit 1; fi
  sha="$EXPECTED_SHA"
  if [ "$MODE" = moved ]; then sha=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa; fi
  printf '%s\\trefs/tags/chainlink-1.6.0\\n' "$sha"
elif [ "$3" = fetch ]; then
  if [ "$MODE" = retry ] && [ "$(grep -c ' fetch ' "$CALLS")" -eq 1 ]; then exit 1; fi
elif [ "$3" = rev-parse ]; then
  sha="$EXPECTED_SHA"
  if [ "$MODE" = raced ]; then sha=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa; fi
  printf '%s\\n' "$sha"
fi
''')


@pytest.mark.parametrize("mode, message", [
    ("moved", "no longer resolves to the pinned commit"),
    ("raced", "Fetched Chainlink source does not match the pinned commit"),
    ("exhausted", "source fetch failed after 3 attempts"),
])
def test_chainlink_cli_fetch_rejects_bad_pin_or_exhausted_retries(tmp_path: Path, mode: str, message: str) -> None:
    env = _environment(tmp_path)
    _fake_git(tmp_path / "commands")
    step = _step("Fetch pinned Chainlink source")
    env.update(step["env"], GITHUB_TOKEN="secret-test-token", MODE=mode)
    result = _run(step, env)
    assert result.returncode == 1, result.stderr
    assert message in result.stdout
    calls = (tmp_path / "calls").read_text()
    assert "checkout" not in calls
    assert "cargo" not in calls
    if mode == "exhausted":
        assert calls.count("git ls-remote") == 3
        assert [line for line in calls.splitlines() if line.startswith("sleep ")] == ["sleep 30", "sleep 60"]


def test_chainlink_cli_fetch_retries_then_checks_actual_checkout_sha(tmp_path: Path) -> None:
    env = _environment(tmp_path)
    _fake_git(tmp_path / "commands")
    step = _step("Fetch pinned Chainlink source")
    env.update(step["env"], GITHUB_TOKEN="secret-test-token", MODE="retry")
    result = _run(step, env)
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text()
    assert calls.count("git ls-remote") == 3
    assert calls.count(" fetch ") == 2
    assert [line for line in calls.splitlines() if line.startswith("sleep ")] == ["sleep 30", "sleep 60"]
    assert calls.index("rev-parse FETCH_HEAD") < calls.index(f"checkout --detach {EXPECTED_SHA}")
    assert "secret-test-token" not in result.stdout + result.stderr + calls


@pytest.mark.parametrize("mode", ["success", "retry", "exhausted"])
def test_chainlink_cli_build_unsets_auth_before_any_cargo_subprocess(tmp_path: Path, mode: str) -> None:
    env = _environment(tmp_path)
    commands = tmp_path / "commands"
    crate = tmp_path / "chainlink-source" / "crates" / "chainlink-tracker"
    _command(commands, "cargo", '''
# Simulate dependency build scripts: all inherited credentials must be absent.
for key in GITHUB_TOKEN GH_TOKEN GIT_CONFIG_COUNT GIT_CONFIG_KEY_0 GIT_CONFIG_VALUE_0; do
  if printenv "$key" >/dev/null; then echo "credential reached cargo" >&2; exit 97; fi
done
printf 'cargo %s\\n' "$*" >> "$CALLS"
if [ "$1" = metadata ]; then
  printf '%s\\n' "$CRATE_JSON"
  exit 0
fi
if [ "$MODE" = exhausted ]; then exit 1; fi
if [ "$MODE" = retry ] && [ "$(grep -c '^cargo install' "$CALLS")" -lt 3 ]; then exit 1; fi
''')
    step = _step("Install pinned Chainlink CLI")
    env.update(step.get("env", {}))
    env.update({key: "secret-test-token" for key in AUTH_KEYS})
    env.update(MODE=mode, CRATE_JSON=json.dumps({"packages": [
        {"name": "chainlink-tracker", "manifest_path": str(crate / "Cargo.toml")},
    ]}))
    result = _run(step, env)
    assert result.returncode == (1 if mode == "exhausted" else 0), result.stderr
    calls = (tmp_path / "calls").read_text()
    expected_install = f"cargo install --path {crate} --locked --root {tmp_path}/chainlink"
    assert calls.count(expected_install) == (1 if mode == "success" else 3)
    sleeps = [line for line in calls.splitlines() if line.startswith("sleep ")]
    assert sleeps == ([] if mode == "success" else ["sleep 30", "sleep 60"])
    assert "secret-test-token" not in result.stdout + result.stderr + calls
    github_path = tmp_path / "github-path"
    if mode == "exhausted":
        assert "install failed after 3 attempts" in result.stdout
        assert not github_path.exists()
    else:
        assert github_path.read_text() == f"{tmp_path}/chainlink/bin\n"

