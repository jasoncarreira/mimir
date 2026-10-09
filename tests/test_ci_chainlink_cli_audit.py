"""Protect the pinned Chainlink audit and its early-failure evidence handling."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import yaml


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SHA = "0e830bf230fdade13b148a99a1a3c19bfbd33b98"


def _workflow() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())


def _install_step() -> dict:
    steps = _workflow()["jobs"]["chainlink-cli-audit"]["steps"]
    return next(step for step in steps if step.get("name") == "Install pinned Chainlink CLI")


def test_chainlink_cli_audit_prepares_evidence_before_install() -> None:
    steps = _workflow()["jobs"]["chainlink-cli-audit"]["steps"]
    names = [step.get("name") for step in steps]
    assert names.index("Prepare pytest evidence directory") < names.index("Install pinned Chainlink CLI")
    assert names.index("Install pinned Chainlink CLI") < names.index("Audit live Chainlink command surface")
    assert _workflow()["permissions"] == {"contents": "read"}
    assert "permissions" not in _workflow()["jobs"]["chainlink-cli-audit"]


def test_chainlink_cli_install_uses_scoped_auth_git_and_bounded_retry() -> None:
    step = _install_step()
    script, env = step["run"], step["env"]
    assert env["GITHUB_TOKEN"] == "${{ github.token }}"
    assert env["CARGO_NET_GIT_FETCH_WITH_CLI"] == "true"
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
    assert "--git https://x-access-token@github.com/dollspace-gay/chainlink.git" in script
    assert "git ls-remote --exit-code https://x-access-token@github.com/dollspace-gay/chainlink.git" in script
    assert "--tag chainlink-1.6.0" in script
    assert f"expected_sha={EXPECTED_SHA}" in script
    assert "refs/tags/chainlink-1.6.0" in script
    assert "for attempt in 1 2 3; do" in script
    assert "sleep $((30 * attempt))" in script


def test_chainlink_cli_install_rejects_moved_tag_before_cargo(tmp_path: Path) -> None:
    """Run the actual install shell with a moved tag and reject before cargo runs."""
    commands = tmp_path / "commands"
    commands.mkdir()
    git = commands / "git"
    git.write_text("#!/bin/sh\nprintf 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\\trefs/tags/chainlink-1.6.0\\n'\n")
    git.chmod(0o755)
    cargo = commands / "cargo"
    cargo.write_text("#!/bin/sh\nprintf 'cargo called\\n' >> \"$CALLS\"\n")
    cargo.chmod(0o755)
    calls = tmp_path / "calls"
    env = {**os.environ, "PATH": f"{commands}:{os.environ['PATH']}",
           "CALLS": str(calls), "RUNNER_TEMP": str(tmp_path),
           "GITHUB_PATH": str(tmp_path / "github-path")}
    result = subprocess.run(["bash", "-c", _install_step()["run"]], env=env,
                            capture_output=True, text=True)
    assert result.returncode == 1
    assert "no longer resolves to the pinned commit" in result.stdout
    assert not calls.exists()


def test_chainlink_cli_install_retries_transient_fetch_and_install(tmp_path: Path) -> None:
    commands = tmp_path / "commands"
    commands.mkdir()
    git = commands / "git"
    git.write_text(
        "#!/bin/sh\n"
        "printf 'git %s\\n' \"$*\" >> \"$CALLS\"\n"
        "test \"$(grep -c '^git ' \"$CALLS\")\" -gt 1 || exit 1\n"
        f"printf '{EXPECTED_SHA}\\trefs/tags/chainlink-1.6.0\\n'\n"
    )
    git.chmod(0o755)
    cargo = commands / "cargo"
    cargo.write_text(
        "#!/bin/sh\n"
        "printf 'cargo %s\\n' \"$*\" >> \"$CALLS\"\n"
        "test \"$(grep -c '^cargo ' \"$CALLS\")\" -gt 1\n"
    )
    cargo.chmod(0o755)
    sleep = commands / "sleep"
    sleep.write_text("#!/bin/sh\nprintf 'sleep %s\\n' \"$*\" >> \"$CALLS\"\n")
    sleep.chmod(0o755)
    calls = tmp_path / "calls"
    github_path = tmp_path / "github-path"
    step = _install_step()
    env = {**os.environ, "PATH": f"{commands}:{os.environ['PATH']}",
           "CALLS": str(calls), "RUNNER_TEMP": str(tmp_path),
           "GITHUB_PATH": str(github_path), "GITHUB_TOKEN": "secret-test-token",
           **{key: value for key, value in step["env"].items() if key != "GITHUB_TOKEN"}}
    result = subprocess.run(["bash", "-c", step["run"]], env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == [
        "git ls-remote --exit-code https://x-access-token@github.com/dollspace-gay/chainlink.git refs/tags/chainlink-1.6.0",
        "sleep 30",
        "git ls-remote --exit-code https://x-access-token@github.com/dollspace-gay/chainlink.git refs/tags/chainlink-1.6.0",
        f"cargo install --git https://x-access-token@github.com/dollspace-gay/chainlink.git --tag chainlink-1.6.0 --root {tmp_path}/chainlink chainlink-tracker",
        "sleep 60",
        "git ls-remote --exit-code https://x-access-token@github.com/dollspace-gay/chainlink.git refs/tags/chainlink-1.6.0",
        f"cargo install --git https://x-access-token@github.com/dollspace-gay/chainlink.git --tag chainlink-1.6.0 --root {tmp_path}/chainlink chainlink-tracker",
    ]
    assert github_path.read_text() == f"{tmp_path}/chainlink/bin\n"
    assert "secret-test-token" not in result.stdout + result.stderr + calls.read_text()
