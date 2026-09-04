from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from mimir.acp import bootstrap
from mimir.acp.credentials import CredentialError, CredentialMutationUncertain


def invoke(tmp_path: Path, args: list[str], monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    code = bootstrap.main(args)
    out, err = capfd.readouterr()
    return code, out, err


def test_profile_command_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]) -> None:
    assert invoke(tmp_path, ["profile", "list"], monkeypatch, capfd) == (0, '{"profiles":[],"version":1}\n', "")
    assert invoke(tmp_path, ["profile", "add-local", "default", "--home", "/tmp"], monkeypatch, capfd) == (0, "", "added\n")
    assert invoke(tmp_path, ["profile", "list"], monkeypatch, capfd) == (0, '{"profiles":["default"],"version":1}\n', "")
    assert invoke(tmp_path, ["profile", "add-local", "default", "--home", "/other"], monkeypatch, capfd) == (1, "", "error: profile-already-exists\n")
    assert invoke(tmp_path, ["profile", "remove", "default"], monkeypatch, capfd) == (0, "", "removed\n")
    assert invoke(tmp_path, ["profile", "remove", "default"], monkeypatch, capfd) == (1, "", "error: profile-not-found\n")


def test_remote_option_presence_and_default_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]) -> None:
    base = ["profile", "add-ssh", "remote", "--home", "/remote", "--ssh-host", "example.com", "--ssh-user", "agent", "--identity-file", "/id", "--known-hosts-file", "/known"]
    assert invoke(tmp_path, base, monkeypatch, capfd) == (0, "", "added\n")
    assert invoke(tmp_path, ["profile", "list"], monkeypatch, capfd) == (0, '{"profiles":["remote"],"version":1}\n', "")
    assert json.loads((tmp_path / "mimir" / "acp" / "profiles.json").read_text())["profiles"]["remote"]["remote"]["port"] == 22
    before = (tmp_path / "mimir" / "acp" / "profiles.json").read_bytes()
    code, out, err = invoke(tmp_path, ["profile", "add-ssh", "remote2", "--home", "/remote", "--ssh-port", "22"], monkeypatch, capfd)
    assert code == 2 and out == "" and "required" in err
    assert (tmp_path / "mimir" / "acp" / "profiles.json").read_bytes() == before


def test_profile_set_timeout_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    assert invoke(
        tmp_path,
        ["profile", "add-local", "default", "--home", "/tmp"],
        monkeypatch,
        capfd,
    ) == (0, "", "added\n")
    path = tmp_path / "mimir" / "acp" / "profiles.json"
    before = path.read_bytes()
    invalid = ("", "0", "601", "+1", "1.0", " 1", "１", "²", "١")
    for value in invalid:
        args = ["profile", "set-timeout", "default"]
        if value:
            args.append(value)
        code, out, err = invoke(tmp_path, args, monkeypatch, capfd)
        assert code == 2 and out == ""
        if value:
            assert err == "argument seconds: must be an ASCII integer from 1 through 600\n"
        else:
            assert "required" in err
        assert path.read_bytes() == before

    assert invoke(
        tmp_path,
        ["profile", "set-timeout", "missing", "60"],
        monkeypatch,
        capfd,
    ) == (2, "", "profile 'missing' does not exist\n")
    assert path.read_bytes() == before

    for seconds in ("1", "060", "600"):
        assert invoke(
            tmp_path,
            ["profile", "set-timeout", "default", seconds],
            monkeypatch,
            capfd,
        ) == (
            0,
            "",
            f"Set timeout for profile 'default' to {int(seconds)} seconds.\n",
        )
    assert json.loads(path.read_text())["profiles"]["default"]["timeoutSeconds"] == 600

    code, out, err = invoke(
        tmp_path,
        ["profile", "add-local", "other", "--home", "/tmp", "--timeout", "2"],
        monkeypatch,
        capfd,
    )
    assert code == 2 and out == "" and "unrecognized arguments" in err


@pytest.mark.parametrize(("port", "valid"), [(0, False), (1, True), (65535, True), (65536, False)])
def test_remote_port_boundaries(tmp_path: Path, port: int, valid: bool, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]) -> None:
    args = ["profile", "add-ssh", "remote", "--home", "/remote", "--ssh-host", "example.com", "--ssh-user", "agent", "--ssh-port", str(port), "--identity-file", "/id", "--known-hosts-file", "/known"]
    expected = (0, "", "added\n") if valid else (1, "", "error: invalid-profile\n")
    assert invoke(tmp_path, args, monkeypatch, capfd) == expected
    path = tmp_path / "mimir" / "acp" / "profiles.json"
    if valid:
        assert json.loads(path.read_text())["profiles"]["remote"]["remote"]["port"] == port
    else:
        assert not path.exists()


@pytest.mark.parametrize("args", [
    ["profile"],
    ["profile", "add-local", "p"],
    ["profile", "add-ssh", "p", "--home", "/x"],
    ["profile", "add-ssh", "p", "--home", "/x", "--ssh-host", "h", "--ssh-user", "u", "--identity-file", "/i", "--known-hosts-file", "/k", "--ssh-port", "not-int"],
    ["credential"],
    ["credential", "add"],
    ["unknown"],
    ["profile", "list", "--unknown"],
])
def test_complete_usage_error_matrix(tmp_path: Path, args: list[str], monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]) -> None:
    code, out, err = invoke(tmp_path, args, monkeypatch, capfd)
    assert code == 2
    assert out == ""
    assert "usage:" in err and "error:" in err


def test_help_uses_reserved_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]) -> None:
    code, out, err = invoke(tmp_path, ["--help"], monkeypatch, capfd)
    assert code == 0 and "usage:" in out and err == ""


def test_credential_precedence_and_exact_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]) -> None:
    consulted: list[str] = []
    class Store:
        def __init__(self) -> None: consulted.append("backend")
    monkeypatch.setattr("mimir.acp.credentials.NativeCredentialStore", Store)
    assert invoke(tmp_path, ["credential", "add", "default"], monkeypatch, capfd) == (1, "", "error: profile-not-found\n")
    assert consulted == []

    assert invoke(tmp_path, ["profile", "add-local", "default", "--home", "/tmp"], monkeypatch, capfd)[0] == 0
    class Missing:
        def get(self, name: str) -> None: return None
        def delete(self, name: str) -> None: return None
    monkeypatch.setattr("mimir.acp.credentials.NativeCredentialStore", Missing)
    assert invoke(tmp_path, ["credential", "list"], monkeypatch, capfd) == (0, '{"credentials":[{"profile":"default","stored":false}],"version":1}\n', "")
    assert invoke(tmp_path, ["credential", "remove", "default"], monkeypatch, capfd) == (0, "", "removed\n")


def test_add_replace_presence_and_uncertainty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]) -> None:
    assert invoke(tmp_path, ["profile", "add-local", "default", "--home", "/tmp"], monkeypatch, capfd)[0] == 0
    class Available:
        value: str | None = "OLD"
        def require_available(self) -> None: return None
        def get(self, name: str) -> str | None: return self.value
        def set(self, name: str, value: str) -> None: self.value = value
    store = Available()
    monkeypatch.setattr("mimir.acp.credentials.NativeCredentialStore", lambda: store)
    monkeypatch.setattr("mimir.acp.credentials.read_secret_from_tty", lambda: (_ for _ in ()).throw(AssertionError("TTY consulted")))
    assert invoke(tmp_path, ["credential", "add", "default"], monkeypatch, capfd) == (1, "", "error: credential-already-exists\n")
    store.value = None
    assert invoke(tmp_path, ["credential", "replace", "default"], monkeypatch, capfd) == (1, "", "error: credential-not-found\n")

    class Unavailable:
        def require_available(self) -> None: raise CredentialError("secure-store-unavailable")
    monkeypatch.setattr("mimir.acp.credentials.NativeCredentialStore", Unavailable)
    assert invoke(tmp_path, ["credential", "add", "default"], monkeypatch, capfd) == (1, "", "error: secure-store-unavailable\n")

    class Uncertain:
        def require_available(self) -> None: return None
        def get(self, name: str) -> None: return None
        def set(self, name: str, value: str) -> None: raise CredentialMutationUncertain()
    monkeypatch.setattr("mimir.acp.credentials.NativeCredentialStore", Uncertain)
    monkeypatch.setattr("mimir.acp.credentials.read_secret_from_tty", lambda: "SECRET")
    assert invoke(tmp_path, ["credential", "add", "default"], monkeypatch, capfd) == (3, "", "error: credential-mutation-uncertain\n")


def test_credential_mutation_verbs_dispatch_exactly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]) -> None:
    assert invoke(tmp_path, ["profile", "add-local", "p", "--home", "/tmp"], monkeypatch, capfd)[0] == 0
    class Store:
        value: str | None = None
        writes: list[str] = []
        deletes = 0
        def require_available(self) -> None: return None
        def get(self, name: str) -> str | None: return self.value
        def set(self, name: str, value: str) -> None:
            self.writes.append(value); self.value = value
        def delete(self, name: str) -> None:
            self.deletes += 1; self.value = None
    store = Store()
    monkeypatch.setattr("mimir.acp.credentials.NativeCredentialStore", lambda: store)
    secrets = iter(("FIRST", "SECOND"))
    monkeypatch.setattr("mimir.acp.credentials.read_secret_from_tty", lambda: next(secrets))
    assert invoke(tmp_path, ["credential", "add", "p"], monkeypatch, capfd) == (0, "", "added\n")
    assert invoke(tmp_path, ["credential", "replace", "p"], monkeypatch, capfd) == (0, "", "replaced\n")
    assert invoke(tmp_path, ["credential", "remove", "p"], monkeypatch, capfd) == (0, "", "removed\n")
    assert store.writes == ["FIRST", "SECOND"] and store.deletes == 1


@pytest.mark.parametrize(("group", "verb"), [
    ("profile", "set"), ("profile", "show"), ("profile", "delete"), ("profile", "status"),
    ("credential", "set"), ("credential", "show"), ("credential", "delete"), ("credential", "status"),
])
def test_incorrect_public_verbs_are_not_retained(tmp_path: Path, group: str, verb: str, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]) -> None:
    code, out, err = invoke(tmp_path, [group, verb], monkeypatch, capfd)
    assert code == 2 and out == "" and "invalid choice" in err


def test_mimir_acp_argv_and_exit_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    import mimir.entrypoint
    observed: list[list[str]] = []
    monkeypatch.setattr(bootstrap, "main", lambda argv: observed.append(list(argv)) or 17)
    monkeypatch.setattr(sys, "argv", ["mimir", "acp", "profile", "list"])
    assert mimir.entrypoint.main() == 17
    assert observed == [["profile", "list"]]


def test_mimir_agent_acp_argv_and_relay_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    import mimir.entrypoint
    observed: list[list[str]] = []
    monkeypatch.setattr(bootstrap, "main", lambda argv: observed.append(list(argv)) or 0)
    monkeypatch.setattr(sys, "argv", ["mimir-agent", "acp", "relay", "--home", "/remote"])
    assert mimir.entrypoint.main() == 0
    assert observed == [["relay", "--home", "/remote"]]


def test_module_main_forwards_exact_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[list[str]] = []
    monkeypatch.setattr(bootstrap, "main", lambda argv: observed.append(list(argv)) or 0)
    monkeypatch.setattr(sys, "argv", ["python", "profile", "list"])
    with pytest.raises(SystemExit, match="0"):
        runpy.run_module("mimir.acp.__main__", run_name="__main__")
    assert observed == [["profile", "list"]]


def test_module_extra_acp_is_usage_error(tmp_path: Path) -> None:
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path)}
    completed = subprocess.run([sys.executable, "-m", "mimir.acp", "acp"], capture_output=True, text=True, env=env)
    assert completed.returncode == 2 and completed.stdout == "" and "usage:" in completed.stderr
