"""Offline contract tests for the Cultivate operator installer."""

from __future__ import annotations

import argparse
import base64
import json
import logging
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from mimir_cultivate import setup as installer


SHA = "a" * 40


def test_validation_and_github_permissions():
    for name, value in (("repo", "../escape"), ("repo", "o/."),
                        ("branch", "../main"), ("bot_email", "bad\nname@example.com"),
                        ("model_spec", "codex-plus:model"), ("port", "65536")):
        with pytest.raises(installer.SetupError):
            installer.validate(name, value)

    def request(url, token):
        if url.endswith("/user"):
            return 200, {"login": "bot"}
        if url.endswith("/repos/org/repo"):
            return 200, {"permissions": {"push": True}}
        return 200, {}

    installer.validate_token("secret", "bot", "org/repo", request)
    with pytest.raises(installer.SetupError, match="different login"):
        installer.validate_token("secret", "someone-else", "org/repo", request)

    def no_push(url, token):
        status, body = request(url, token)
        if url.endswith("/repos/org/repo"):
            body = {"permissions": {"push": False}}
        return status, body

    with pytest.raises(installer.SetupError, match="push permission"):
        installer.validate_token("secret", "bot", "org/repo", no_push)

    def no_actions(url, token):
        if "actions/runs" in url:
            return 403, {}
        return request(url, token)

    with pytest.raises(installer.SetupError, match="Actions"):
        installer.validate_token("secret", "bot", "org/repo", no_actions)


@pytest.mark.parametrize(("files", "expected"), [
    ({"rust-toolchain.toml": '[toolchain]\nchannel = "1.99.1"', "Cargo.toml": "[package]"},
     ("rust", "1.99.1", "cargo nextest run")),
    ({"package.json": json.dumps({"engines": {"node": "22.16.0"}, "scripts": {"verify": "npm test"}})},
     ("node", "22.16.0", "npm run verify")),
    ({"pyproject.toml": "[project]", "uv.lock": "", ".python-version": "3.11.11\n"},
     ("python", "3.11.11", "uv run pytest -q")),
])
def test_detects_repo_pins_and_pinned_path(files, expected):
    assert installer.detect_toolchain(files) == expected
    layer = installer.render_layer(expected[0], expected[1], "pinned-image:sha")
    assert "FROM pinned-image:sha" in layer
    assert expected[1] in layer
    if expected[0] == "rust":
        assert "RUSTUP_HOME=/opt/rustup" in layer
        assert "/usr/local/bin/cargo-nextest" in layer
    if expected[0] == "node":
        assert "/usr/local/bin/node" in layer
    if expected[0] == "python":
        assert "/usr/local/bin/uv" in layer


def test_setup_is_idempotent_and_keeps_secrets_out_of_output(tmp_path, monkeypatch, capsys, caplog):
    compile(installer._PROVISION, "provision.py", "exec")
    parser = argparse.ArgumentParser()
    installer.add_argparse(parser.add_subparsers(dest="command"))
    deploy = tmp_path / "deploy"
    token_file = tmp_path / "token"
    model_file = tmp_path / "model"
    github_secret = "github-secret-test-2008"
    model_secret = "model-secret-test-2008"
    token_file.write_text(github_secret)
    model_file.write_text(model_secret)
    token_file.chmod(0o600)
    model_file.chmod(0o600)
    args = parser.parse_args([
        "cultivate", "setup", "--repo", "org/repo", "--branch", "main", "--bot-login", "bot",
        "--bot-email", "bot@example.com", "--owner", "owner", "--display-name", "Owner Name",
        "--owner-email", "owner@example.com", "--model-spec", "openai:gpt-test",
        "--github-token-file", str(token_file), "--model-key-file", str(model_file),
        "--deploy", str(deploy), "--port", "8123",
    ])

    def request(url, token):
        assert token == github_secret
        if url.endswith("/user"):
            return 200, {"login": "bot"}
        if url.endswith("/repos/org/repo"):
            return 200, {"permissions": {"push": True}}
        if "/git/ref/heads/feature/plugins" in url:
            return 200, {"object": {"sha": SHA}}
        if "/contents/" in url:
            if "package.json" in url:
                return 200, {"encoding": "base64", "content": base64.b64encode(
                    b'{"engines":{"node":"22.16.0"},"scripts":{"verify":"npm run build"}}'
                ).decode()}
            return 404, {}
        return 200, {}

    calls = []

    def run(argv, *, cwd):
        calls.append(argv)
        if argv[-3:] == ["ps", "-q", "agent"]:
            return "b" * 64
        if "compose.provision.yml" in argv:
            return "SETUP_CHANGED=1" if sum("compose.provision.yml" in c for c in calls) == 1 else "SETUP_CHANGED=0"
        return ""

    def docker_exec(argv, **kwargs):
        assert argv[:2] == ["docker", "exec"]
        kwargs["stdout"].write(b"owner-key-test-2008\n")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(installer.subprocess, "run", docker_exec)
    verified = []
    caplog.set_level(logging.DEBUG)
    for _ in range(2):
        installer.setup(args, request=request, run=run,
                        verify=lambda *a: verified.append(a))
    stdout, stderr = capsys.readouterr()
    assert len(verified) == 2
    for secret in (github_secret, model_secret, "owner-key-test-2008"):
        assert secret not in stdout + stderr + caplog.text
        for argv in calls:
            assert secret not in " ".join(argv)
    assert (deploy / "compose.env").stat().st_mode & 0o777 == 0o600
    assert (deploy / "owner-web-key").stat().st_mode & 0o777 == 0o600
    assert github_secret in (deploy / "compose.env").read_text()
    assert (deploy / "build.sh").stat().st_mode & 0o777 == 0o755
    assert "https://github.com/jasoncarreira/mimir.git#" + SHA in (deploy / "build.sh").read_text()
    assert (deploy / ".dockerignore").read_text() == "*\n!Dockerfile.toolchain\n"
    for name in ("compose.yml", "Dockerfile.toolchain", "manifest.json", "provision.py"):
        text = (deploy / name).read_text()
        assert github_secret not in text and model_secret not in text
    compose = yaml.safe_load((deploy / "compose.yml").read_text())
    assert compose["services"]["agent"]["environment"]["MIMIR_PLUGINS"] == "cultivate"
    assert compose["services"]["agent"]["depends_on"]["init"]["condition"] == "service_completed_successfully"
    assert set(compose["volumes"]) == {"home", "cache", "workspace", "pr-leases", "worklink"}
    assert any("MIMIR_CONTROLLER_COMMIT=" + SHA in part for argv in calls for part in argv)
    assert sum("docker" == argv[0] and "exec" in argv for argv in calls) == 0
    assert sum("-f" in argv and "compose.provision.yml" in argv for argv in calls) == 2
    assert sum(argv[-2:] == ["restart", "agent"] for argv in calls) == 1

    def unpushed(url, token):
        if "/git/ref/" in url:
            return 200, {"object": {"sha": "short"}}
        return request(url, token)

    before_calls = len(calls)
    with pytest.raises(installer.SetupError, match="pushed full"):
        installer.setup(args, request=unpushed, run=run, verify=lambda *a: None)
    assert len(calls) == before_calls


def test_secret_file_requires_private_mode(tmp_path):
    file = tmp_path / "key"
    file.write_text("secret")
    file.chmod(0o644)
    with pytest.raises(installer.SetupError, match="mode 600"):
        installer._secret(argparse.Namespace(github_token_file=str(file), github_token_stdin=False),
                          "github_token", "hidden")


def test_verification_requires_current_binding_pollers_and_new_reply(tmp_path, monkeypatch):
    monkeypatch.setattr(installer.time, "sleep", lambda seconds: None)
    pending = {"alerts": [], "pollers": ["github-activity", "github-ci-watch"], "reply": True}
    reads = []

    def http(url, *, key=None, data=None):
        reads.append((url, key, data))
        if url.endswith("/health"):
            return {"ok": True}
        if url.endswith("/api/v1/scheduler"):
            return {"data": {"pollers": [{"name": p, "next_run_at": "tomorrow"} for p in pending["pollers"]]}}
        if url.endswith("/api/v1/chat/history"):
            return {"data": {"messages": [] if not any(row[2] for row in reads) else
                    [{"role": "assistant", "text": "Hello"}] if pending["reply"] else []}}
        return {"ok": True}

    def run(argv, *, cwd):
        if "plugins" in argv:
            return json.dumps([{"name": "cultivate", "enabled": True}])
        return json.dumps(pending["alerts"])

    monkeypatch.setattr(installer, "_http", http)
    monkeypatch.setattr(installer, "_run", run)
    installer._verify(tmp_path, 8123, "org/repo", "owner-key")
    assert any(url.endswith("/api/v1/chat") and key == "owner-key" for url, key, _ in reads)
    pending["alerts"] = [{"repository": "org/repo"}]
    with pytest.raises(installer.SetupError, match="binding"):
        installer._verify(tmp_path, 8123, "org/repo", "owner-key")
    pending["alerts"] = []
    pending["pollers"] = ["github-activity"]
    with pytest.raises(installer.SetupError, match="github-ci-watch"):
        installer._verify(tmp_path, 8123, "org/repo", "owner-key")
    pending["pollers"] = ["github-activity", "github-ci-watch"]
    pending["reply"] = False
    with pytest.raises(installer.SetupError, match="no assistant reply"):
        installer._verify(tmp_path, 8123, "org/repo", "owner-key")


def test_existing_env_never_overwrites_operator_secrets(tmp_path):
    installer._env(tmp_path, {"MIMIR_API_KEY": "generated", "GITHUB_TOKEN": "original"})
    before = (tmp_path / "compose.env").read_bytes()
    with pytest.raises(installer.SetupError, match="GITHUB_TOKEN"):
        installer._env(tmp_path, {"MIMIR_API_KEY": "new", "GITHUB_TOKEN": "different"})
    assert (tmp_path / "compose.env").read_bytes() == before


def test_provision_clones_before_declaring_and_preserves_identity(tmp_path, monkeypatch, capsys, caplog):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    state = home / "state"
    state.mkdir(parents=True)
    existing = {"people": [{"canonical": "another", "aliases": ["email:another@example.com"]}]}
    (state / "identities.yaml").write_text(yaml.safe_dump(existing))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"slug": "org/repo", "branch": "main", "owner": "owner",
                                    "display": "Owner", "email": "owner@example.com",
                                    "test": "npm run verify", "rotate": False}))
    script = (installer._PROVISION
              .replace("/opt/cultivate-manifest.json", str(manifest))
              .replace("/home/mimir/agent", str(home))
              .replace("/workspace/mimir", str(repo)))
    import mimir.plugins.skills as skills

    monkeypatch.setattr(skills, "resolve_skill", lambda name, core_names: SimpleNamespace(dirname=name.split(":")[1]))

    def install_skill(home, skill):
        dest = home / "skills" / skill.dirname
        dest.mkdir(parents=True)
        return dest, None

    monkeypatch.setattr(skills, "install_skill", install_skill)
    calls = []

    def git(argv, **kwargs):
        calls.append(argv)
        if "clone" in argv:
            (repo / ".git").mkdir(parents=True)
        return subprocess.CompletedProcess(argv, 0, stdout="https://github.com/org/repo.git\n")

    monkeypatch.setattr(subprocess, "run", git)
    caplog.set_level(logging.DEBUG)
    for _ in range(2):
        exec(compile(script, "provision.py", "exec"), {"__name__": "__main__"})
    stdout, stderr = capsys.readouterr()
    assert "SETUP_CHANGED=1" in stdout
    assert "SETUP_CHANGED=0" in stdout
    key = (home / "owner-web-key").read_text().strip()
    assert key not in stdout + stderr + caplog.text
    assert (home / "owner-web-key").stat().st_mode & 0o777 == 0o600
    inventory = yaml.safe_load((home / "repositories.yaml").read_text())
    assert inventory["repositories"][0]["root"] == str(repo)
    assert inventory["repositories"][0]["origin"] == "https://github.com/org/repo.git"
    assert len(inventory["repositories"]) == 1
    assert {r["root"] for r in inventory["allowed_roots"]} == {"/workspace/pr-leases", "/workspace/worklink"}
    people = yaml.safe_load((state / "identities.yaml").read_text())["people"]
    assert {p["canonical"] for p in people} == {"another", "owner"}
    assert {"user", "admin"}.issubset(people[1]["access"]["roles"])
    assert installer._SHA.fullmatch("a" * 40)
    assert sum("clone" in argv for argv in calls) == 1
    assert sum("setup-git" in argv for argv in calls) == 1
    prior_inventory = (home / "repositories.yaml").read_bytes()
    prior_key = (home / "owner-web-key").read_bytes()
    (repo / ".git").rmdir()
    with pytest.raises(subprocess.CalledProcessError):
        def fail_clone(argv, **kwargs):
            if "clone" in argv:
                raise subprocess.CalledProcessError(1, argv)
            return git(argv, **kwargs)
        monkeypatch.setattr(subprocess, "run", fail_clone)
        exec(compile(script, "provision.py", "exec"), {"__name__": "__main__"})
    assert (home / "repositories.yaml").read_bytes() == prior_inventory
    assert (home / "owner-web-key").read_bytes() == prior_key
    (repo / ".git").mkdir()

    def wrong_origin(argv, **kwargs):
        if "remote.origin.url" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="https://github.com/another/repo.git\n")
        return git(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", wrong_origin)
    with pytest.raises(RuntimeError, match="origin disagrees"):
        exec(compile(script, "provision.py", "exec"), {"__name__": "__main__"})
    assert (home / "repositories.yaml").read_bytes() == prior_inventory
    monkeypatch.setattr(subprocess, "run", git)
    identities = yaml.safe_load((state / "identities.yaml").read_text())
    owner_entry = next(person for person in identities["people"] if person["canonical"] == "owner")
    owner_entry["aliases"] = [alias for alias in owner_entry["aliases"] if not alias.startswith("webkey:")]
    (state / "identities.yaml").write_text(yaml.safe_dump(identities))
    with pytest.raises(RuntimeError, match="no longer matches"):
        exec(compile(script, "provision.py", "exec"), {"__name__": "__main__"})
    assert (home / "owner-web-key").read_bytes() == prior_key
    owner_entry["access"]["roles"] = ["user"]
    (state / "identities.yaml").write_text(yaml.safe_dump(identities))
    with pytest.raises(RuntimeError, match=r"admin\+user"):
        exec(compile(script, "provision.py", "exec"), {"__name__": "__main__"})
