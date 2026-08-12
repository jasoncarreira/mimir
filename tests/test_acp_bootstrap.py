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
    assert invoke(tmp_path, ["profile", "set", "default", "--home", "/tmp"], monkeypatch, capfd) == (0, "", "updated\n")
    assert invoke(tmp_path, ["profile", "show", "default"], monkeypatch, capfd) == (0, '{"home":"/tmp","name":"default","remote":null,"version":1}\n', "")
    assert invoke(tmp_path, ["profile", "delete", "default"], monkeypatch, capfd) == (0, "", "deleted\n")
    assert invoke(tmp_path, ["profile", "show", "default"], monkeypatch, capfd) == (1, "", "error: profile-not-found\n")


def test_remote_option_presence_and_default_port(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]) -> None:
    base = ["profile", "set", "remote", "--home", "/remote", "--ssh-host", "example.com", "--ssh-user", "agent", "--identity-file", "/id", "--known-hosts-file", "/known"]
    assert invoke(tmp_path, base, monkeypatch, capfd) == (0, "", "updated\n")
    code, out, err = invoke(tmp_path, ["profile", "show", "remote"], monkeypatch, capfd)
    assert code == 0 and err == "" and json.loads(out)["remote"]["port"] == 22
    before = (tmp_path / "mimir" / "acp" / "profiles.json").read_bytes()
    assert invoke(tmp_path, ["profile", "set", "remote", "--home", "/remote", "--ssh-port", "22"], monkeypatch, capfd) == (1, "", "error: invalid-profile\n")
    assert (tmp_path / "mimir" / "acp" / "profiles.json").read_bytes() == before


@pytest.mark.parametrize("args", [
    ["profile"],
    ["profile", "show"],
    ["profile", "set", "p"],
    ["profile", "set", "p", "--home", "/x", "--ssh-port", "not-int"],
    ["credential"],
    ["credential", "status", "extra"],
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
    assert invoke(tmp_path, ["credential", "status"], monkeypatch, capfd) == (1, "", "error: profile-not-found\n")
    assert consulted == []

    assert invoke(tmp_path, ["profile", "set", "default", "--home", "/tmp"], monkeypatch, capfd)[0] == 0
    class Missing:
        def get(self, name: str) -> None: return None
        def delete(self, name: str) -> None: return None
    monkeypatch.setattr("mimir.acp.credentials.NativeCredentialStore", Missing)
    assert invoke(tmp_path, ["credential", "status"], monkeypatch, capfd) == (0, "", "missing\n")
    assert invoke(tmp_path, ["credential", "delete"], monkeypatch, capfd) == (0, "", "deleted\n")


def test_set_selects_backend_before_tty_and_uncertainty_only_after_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]) -> None:
    assert invoke(tmp_path, ["profile", "set", "default", "--home", "/tmp"], monkeypatch, capfd)[0] == 0
    class Unavailable:
        def require_available(self) -> None: raise CredentialError("secure-store-unavailable")
    monkeypatch.setattr("mimir.acp.credentials.NativeCredentialStore", Unavailable)
    monkeypatch.setattr("mimir.acp.credentials.read_secret_from_tty", lambda: (_ for _ in ()).throw(AssertionError("TTY consulted")))
    assert invoke(tmp_path, ["credential", "set"], monkeypatch, capfd) == (1, "", "error: secure-store-unavailable\n")

    class Uncertain:
        def require_available(self) -> None: return None
        def set(self, name: str, value: str) -> None: raise CredentialMutationUncertain()
    monkeypatch.setattr("mimir.acp.credentials.NativeCredentialStore", Uncertain)
    monkeypatch.setattr("mimir.acp.credentials.read_secret_from_tty", lambda: "SECRET")
    assert invoke(tmp_path, ["credential", "set"], monkeypatch, capfd) == (3, "", "error: credential-mutation-uncertain\n")


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
