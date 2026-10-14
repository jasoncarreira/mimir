"""Agent-turn gog wrapper and its shipped command declaration."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from mimir.access_control import (
    parse_declared_shell_commands,
    parse_service_shell_argv_with_diagnostics,
)


SKILL = Path(__file__).resolve().parents[1]
WRAPPER = SKILL / "scripts" / "run-gog.sh"


def test_declared_agent_command_parses_with_required_environment(tmp_path: Path) -> None:
    manifest = json.loads((SKILL / "pollers.json").read_text(encoding="utf-8"))
    declaration = manifest["pollers"][0]["authority"]["shell_commands"][0].copy()
    installed = tmp_path / "skills" / "gmail-poller" / "scripts"
    installed.mkdir(parents=True)
    shutil.copy2(WRAPPER, installed / WRAPPER.name)
    declaration["script"] = str(installed / WRAPPER.name)
    declaration["path"] = shutil.which("bash")
    assert declaration["path"] is not None
    parsed, = parse_declared_shell_commands([declaration], writable_roots=(tmp_path / "state",))
    assert parsed.pass_env == ("GOG_ACCOUNT", "GOG_KEYRING_PASSWORD", "MIMIR_HOME", "STATE_DIR")
    assert parsed.options == ("--account", "--max", "--json", "--no-input", "--full")
    assert manifest["pollers"][0]["pass_env"] == [
        "GOG_ACCOUNT", "MIMIR_GMAIL_QUERY", "MIMIR_GMAIL_MAX_FETCH", "MIMIR_HOME", "JEV_KEY",
    ]


def test_documented_commands_are_admitted_by_service_gate(tmp_path: Path) -> None:
    manifest = json.loads((SKILL / "pollers.json").read_text(encoding="utf-8"))
    declaration = manifest["pollers"][0]["authority"]["shell_commands"][0].copy()
    original_script = declaration["script"]
    installed = tmp_path / "skills" / "gmail-poller" / "scripts"
    installed.mkdir(parents=True)
    shutil.copy2(WRAPPER, installed / WRAPPER.name)
    declaration["script"] = str(installed / WRAPPER.name)
    declaration["path"] = shutil.which("bash")
    assert declaration["path"] is not None
    declared = parse_declared_shell_commands([declaration], writable_roots=(tmp_path / "state",))
    documentation = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    examples = documentation.split("```sh\n", 1)[1].split("```", 1)[0].splitlines()
    assert len(examples) == 3
    for example in examples:
        assert "--account you@gmail.com" in example
        argv, reason, _ = parse_service_shell_argv_with_diagnostics(
            example.replace(original_script, declaration["script"]),
            "scheduler_read_only", declared=declared,
        )
        assert argv is not None, reason
        assert "GOG_ACCOUNT" not in " ".join(argv)


@pytest.fixture
def gog_stub(tmp_path: Path):
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    binary = binary_dir / "gog"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['GOG_STUB_OUTPUT'], 'w') as out:\n"
        "    json.dump({'args': sys.argv[1:], 'account': os.environ.get('GOG_ACCOUNT'), "
        "'home': os.environ.get('GOG_HOME')}, out)\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    source = WRAPPER.read_text(encoding="utf-8")
    assert "exec /usr/local/bin/gog " in source
    wrapper = tmp_path / WRAPPER.name
    # Substitute only in this private copy; production has no binary override.
    wrapper.write_text(source.replace("/usr/local/bin/gog", str(binary)), encoding="utf-8")
    hijack_dir = tmp_path / ".local" / "bin"
    hijack_dir.mkdir(parents=True)
    hijacked = tmp_path / "hijacked"
    hijack = hijack_dir / "gog"
    hijack.write_text(f"#!/bin/sh\ntouch '{hijacked}'\nexit 99\n", encoding="utf-8")
    hijack.chmod(0o755)
    output = tmp_path / "invocation.json"
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path),
        "MIMIR_HOME": str(tmp_path),
        "PATH": f"{hijack_dir}:{env['PATH']}",
        "GOG_ACCOUNT": "agent@example.test",
        "GOG_KEYRING_PASSWORD": "stub-password",
        "GOG_STUB_OUTPUT": str(output),
    })

    def run(*args: str, account: str | None = "agent@example.test",
            state_dir: Path | None = None):
        output.unlink(missing_ok=True)
        command_env = env.copy()
        if account is None:
            command_env.pop("GOG_ACCOUNT", None)
        else:
            command_env["GOG_ACCOUNT"] = account
        command_env.pop("STATE_DIR", None)
        if state_dir is not None:
            command_env["STATE_DIR"] = str(state_dir)
        result = subprocess.run(
            ["bash", str(wrapper), *args], env=command_env,
            capture_output=True, text=True, check=False,
        )
        assert not hijacked.exists(), "agent-writable PATH gog was executed"
        invocation = json.loads(output.read_text()) if output.exists() else None
        return result, invocation

    return run


def _configured_accounts(state_dir: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "config.json").write_text(json.dumps({"accounts": [
        {"name": "home", "email": "home@example.test"},
        {"name": "work", "email": "work@example.test"},
    ]}), encoding="utf-8")


@pytest.mark.parametrize("arguments", [
    ("gmail", "messages", "search", "in:inbox is:unread", "--max", "5", "--json"),
    ("gmail", "get", "msg_123-A", "--full", "--json", "--no-input"),
    ("gmail", "messages", "get", "msg_123-A", "--full", "--json", "--no-input"),
    ("gmail", "thread", "get", "thread_123-A", "--full", "--json", "--no-input"),
    ("auth", "list"),
])
@pytest.mark.parametrize("explicit_account", [False, True])
def test_read_commands_force_runtime_guards(
    gog_stub, arguments: tuple[str, ...], explicit_account: bool,
) -> None:
    account_args = ["--account", "agent@example.test"] if explicit_account and arguments[0] == "gmail" else []
    result, invocation = gog_stub(*arguments, *account_args)
    assert result.returncode == 0, result.stderr
    assert invocation is not None
    normalized = ("gmail", "get", *arguments[3:]) if arguments[:3] == (
        "gmail", "messages", "get",
    ) else arguments
    assert invocation["args"] == ["--readonly", "--gmail-no-send", *normalized, *(
        ["--account", "agent@example.test"] if arguments[0] == "gmail" else []
    )]
    assert invocation["account"] == "agent@example.test"
    assert invocation["home"].endswith("/.local/share/gog")


@pytest.mark.parametrize("arguments", [
    ("gmail", "get", "msg1", "--download"),
    ("gmail", "messages", "get", "msg1", "--download"),
    ("gmail", "messages", "get", "bad/id"),
    ("gmail", "get", "msg1", "--out-dir", "x"),
    ("gmail", "get", "msg1", "msg2"),
    ("gmail", "get", "not.an.id"),
    ("gmail", "get"),
    ("gmail", "thread", "get", "thread1", "thread2"),
    ("gmail", "thread", "get", "bad/id"),
    ("gmail", "send", "--to", "x@y"),
    ("gmail", "thread", "modify", "thread1"),
    ("gmail", "get", "msg1", "--account", "other@x"),
    ("gmail", "get", "msg1", "--account", "agent@example.test", "--account", "other@x"),
    ("gmail", "get", "msg1", "--max", "5"),
    ("gmail", "messages", "search", "in:inbox", "--full"),
    ("gmail", "get", "msg1", "--access-token", "token"),
    ("gmail", "get", "msg1", "--home", "/tmp"),
    ("gmail", "get", "msg1", "--client", "other"),
    ("gmail", "get", "msg1", "--enable-commands=send"),
    ("gmail", "get", "msg1", "--disable-commands", "get"),
    ("auth", "list", "extra"),
    ("auth", "list", "--json"),
    ("auth", "list", "--account", "agent@example.test"),
    ("auth", "list", "--full"),
    ("gmail", "messages", "search"),
    ("gmail", "messages", "search", "in:inbox", "is:unread"),
    ("gmail", "messages", "search", "in:inbox", "--max", "unlimited"),
    ("gmail", "get", "msg1", "--account"),
    ("gmail", "get", "msg1", "--account", "--download"),
], ids=lambda arguments: " ".join(arguments))
def test_refuses_unsafe_or_malformed_invocations(gog_stub, arguments: tuple[str, ...]) -> None:
    result, invocation = gog_stub(*arguments)
    assert result.returncode == 2, (arguments, result.stderr)
    assert invocation is None


def test_refuses_missing_config_and_account(gog_stub) -> None:
    result, invocation = gog_stub("gmail", "get", "msg1", account=None)
    assert result.returncode == 2
    assert "configured account name: (none)" in result.stderr
    assert invocation is None


@pytest.mark.parametrize("email", ["home@example.test", "work@example.test"])
def test_multi_account_get_uses_configured_email(gog_stub, tmp_path: Path, email: str) -> None:
    state_dir = tmp_path / "poller-state"
    _configured_accounts(state_dir)
    result, invocation = gog_stub(
        "gmail", "get", "msg1", "--account", email, "--full", "--json",
        account=None, state_dir=state_dir,
    )
    assert result.returncode == 0, result.stderr
    assert invocation["args"] == [
        "--readonly", "--gmail-no-send", "gmail", "get", "msg1",
        "--account", email, "--full", "--json",
    ]
    assert invocation["account"] is None


@pytest.mark.parametrize("args", [
    ("gmail", "get", "msg1"),
    ("gmail", "get", "msg1", "--account", "outsider@example.test"),
    ("gmail", "get", "msg1", "--account", "home@example.test", "--account", "outsider@example.test"),
])
def test_multi_account_refusals_list_names_only(gog_stub, tmp_path: Path, args) -> None:
    state_dir = tmp_path / "poller-state"
    _configured_accounts(state_dir)
    result, invocation = gog_stub(*args, account=None, state_dir=state_dir)
    assert result.returncode == 2
    assert invocation is None
    assert "--account" in result.stderr
    assert "home, work" in result.stderr
    assert "@" not in result.stderr


def test_multi_account_default_state_dir(gog_stub, tmp_path: Path) -> None:
    _configured_accounts(tmp_path / "state" / "pollers" / "gmail-inbox")
    result, invocation = gog_stub(
        "gmail", "messages", "get", "msg1", "--account", "work@example.test",
        account=None,
    )
    assert result.returncode == 0, result.stderr
    assert invocation["args"] == [
        "--readonly", "--gmail-no-send", "gmail", "get", "msg1",
        "--account", "work@example.test",
    ]


def test_legacy_mismatched_account_is_refused(gog_stub) -> None:
    result, invocation = gog_stub(
        "gmail", "get", "msg1", "--account", "other@example.test",
    )
    assert result.returncode == 2
    assert "does not match the declared account" in result.stderr
    assert "@" not in result.stderr
    assert invocation is None


def test_multi_account_invalid_config_fails_closed(gog_stub, tmp_path: Path) -> None:
    state_dir = tmp_path / "poller-state"
    state_dir.mkdir()
    (state_dir / "config.json").write_text("{invalid json", encoding="utf-8")
    result, invocation = gog_stub(
        "gmail", "get", "msg1", "--account", "home@example.test",
        account=None, state_dir=state_dir,
    )
    assert result.returncode == 2
    assert invocation is None
    assert "configured account name: (none)" in result.stderr
    assert "@" not in result.stderr
