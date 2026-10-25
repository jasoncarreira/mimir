"""Offline contract tests for the Cultivate operator installer."""

from __future__ import annotations

import argparse
import base64
from contextlib import redirect_stdout
import io
import json
import logging
import tomllib
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from mimir_cultivate import setup as installer


SHA = "a" * 40


@pytest.fixture(autouse=True)
def isolated_license_record(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


@pytest.mark.parametrize("answer", ["no", "y", "", None])
def test_license_decline_or_eof_writes_nothing_before_other_work(tmp_path, monkeypatch, capsys, answer):
    monkeypatch.setattr(installer.sys.stdin, "isatty", lambda: True)
    license_text = "Business Source License 1.1\nFULL SHIPPED TERMS\n"
    monkeypatch.setattr(installer, "_installed_license", lambda: (license_text.encode(), "1.2.3"))
    def prompt(text):
        assert "FULL SHIPPED TERMS" in capsys.readouterr().out
        if answer is None:
            raise EOFError
        return answer
    monkeypatch.setattr("builtins.input", prompt)
    monkeypatch.setattr(installer, "_field", lambda *a, **kw: pytest.fail("other prompt before license"))
    assert installer.dispatch(argparse.Namespace(cultivate_action="setup")) == 1
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("answer", ["yes", "accept"])
def test_license_acceptance_hash_version_timestamp_and_reprompt(tmp_path, monkeypatch, capsys, answer):
    import hashlib
    monkeypatch.setattr(installer.sys.stdin, "isatty", lambda: True)
    text = [b"Full BSL 1.1 shipped terms\n"]
    monkeypatch.setattr(installer, "_installed_license", lambda: (text[0], "1.2.3"))
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or answer)
    args = argparse.Namespace(accept_license=False)
    installer._license_gate(args)
    record_path = tmp_path / "config/mimir/cultivate-license-accepted.json"
    record = json.loads(record_path.read_text())
    assert record["sha256"] == hashlib.sha256(text[0]).hexdigest()
    assert record["version"] == "1.2.3" and record["accepted_at"]
    original = record_path.read_bytes()
    assert text[0].decode() in capsys.readouterr().out
    installer._license_gate(args)
    assert capsys.readouterr().out == ""
    assert record_path.read_bytes() == original and len(prompts) == 1
    text[0] += b"Changed terms\n"
    installer._license_gate(args)
    assert "Changed terms" in capsys.readouterr().out and len(prompts) == 2
    assert json.loads(record_path.read_text())["sha256"] != record["sha256"]


def test_noninteractive_license_needs_explicit_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(installer.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(installer, "_installed_license", lambda: (b"FULL LICENSE\n", "1.2.3"))
    with pytest.raises(installer.SetupError, match="--accept-license"):
        installer._license_gate(argparse.Namespace(accept_license=False))
    assert not list(tmp_path.iterdir())
    installer._license_gate(argparse.Namespace(accept_license=True))
    assert "FULL LICENSE" in capsys.readouterr().out
    installer._license_gate(argparse.Namespace(accept_license=False))
    assert capsys.readouterr().out == ""


def test_license_uses_installed_resource_not_cwd(tmp_path, monkeypatch):
    from importlib import resources, metadata
    text, version = installer._installed_license()
    resource = resources.files("mimir_cultivate").joinpath("LICENSE")
    expected = resource.read_bytes() if resource.is_file() else Path(installer.__file__).resolve().parent.parent.joinpath("LICENSE").read_bytes()
    assert text == expected and b"Business Source License" in text
    assert version == metadata.version("mimir-cultivate")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "LICENSE").write_text("wrong terms")
    assert installer._installed_license() == (text, version)


def test_validation_and_github_permissions():
    for name, value in (("repo", "../escape"), ("repo", "o/."),
                        ("branch", "../main"), ("bot_email", "bad\nname@example.com"),
                        ("model_spec", "codex-plus:model"), ("saga_model_spec", "voyage:m"),
                        ("embedding", "local;bad"), ("port", "65536")):
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

    with pytest.raises(installer.SetupError, match="invite the bot as a Write collaborator"):
        installer.validate_token("secret", "bot", "org/repo", no_push)

    def no_actions(url, token):
        if "actions/runs" in url:
            return 403, {}
        return request(url, token)

    with pytest.raises(installer.SetupError, match="Actions"):
        installer.validate_token("secret", "bot", "org/repo", no_actions)


def test_pat_help_describes_owner_permissions_and_approval():
    help_text = installer.PAT_HELP
    assert "resource owner = the owner of the target repository" in help_text
    assert "organization for an org repo" in help_text
    assert "Settings, Personal access tokens" in help_text
    assert "repository access = only the target repository" in help_text
    assert "Contents, Pull requests, Issues: Read and write" in help_text
    assert "Actions, Commit statuses, Metadata: Read-only" in help_text
    assert "optional Workflows: Read and write" in help_text
    assert ".github/workflows/" in help_text
    assert "Checks" not in help_text


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
        assert "-C /usr/local/bin cargo-nextest" in layer
    if expected[0] == "node":
        assert "/usr/local/bin/node" in layer
    if expected[0] == "python":
        assert "/usr/local/bin/uv" in layer


@pytest.mark.parametrize("embedding,saga_spec", [
    ("local", None), ("openai:text-embedding-3-small", None),
    ("voyage:voyage-4-lite", "anthropic:claude-haiku-5-5"),
])
def test_setup_is_idempotent_and_keeps_secrets_out_of_output(tmp_path, monkeypatch, capsys, caplog, embedding, saga_spec):
    compile(installer._PROVISION, "provision.py", "exec")
    monkeypatch.setattr(installer.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "")
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
    other_file = tmp_path / "other"
    other_file.write_text("other-secret-test-2008")
    other_file.chmod(0o600)
    extra = ["--embedding", embedding]
    if embedding.startswith("voyage:"):
        extra += ["--embedding-key-file", str(other_file)]
    if saga_spec:
        extra += ["--saga-model-spec", saga_spec, "--saga-model-key-file", str(other_file)]
    args = parser.parse_args([
        "cultivate", "setup", "--accept-license", "--source-repo", "neworg/mimir",
        "--repo", "org/repo", "--branch", "main", "--bot-login", "bot",
        "--bot-email", "bot@example.com", "--owner", "owner", "--display-name", "Owner Name",
        "--owner-email", "owner@example.com", "--model-spec", "openai:gpt-test",
        "--github-token-file", str(token_file), "--model-key-file", str(model_file),
        "--deploy", str(deploy), "--port", "8123",
    ] + extra)

    def request(url, token):
        assert token == github_secret
        if url.endswith("/user"):
            return 200, {"login": "bot"}
        if url.endswith("/repos/org/repo"):
            return 200, {"permissions": {"push": True}}
        if "/git/ref/heads/feature/plugins" in url:
            assert "/repos/neworg/mimir/" in url
            return 200, {"object": {"sha": SHA}}
        if "/git/trees/" in url:
            return 200, {"tree": [{"path": "tools/verify", "mode": "100755", "type": "blob"}]}
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
    if saga_spec is None:
        assert "faster, cheaper" in stdout and "openai:gpt-test" in stdout
    assert len(verified) == 2
    for secret in (github_secret, model_secret, "other-secret-test-2008", "owner-key-test-2008"):
        assert secret not in stdout + stderr + caplog.text
        for argv in calls:
            assert secret not in " ".join(argv)
    assert (deploy / "compose.env").stat().st_mode & 0o777 == 0o600
    assert (deploy / "owner-web-key").stat().st_mode & 0o777 == 0o600
    assert github_secret in (deploy / "compose.env").read_text()
    env_text = (deploy / "compose.env").read_text()
    assert ("VOYAGE_API_KEY=other-secret-test-2008" in env_text) == embedding.startswith("voyage:")
    assert ("ANTHROPIC_API_KEY=other-secret-test-2008" in env_text) == bool(saga_spec)
    saga = json.loads((deploy / "manifest.json").read_text())
    assert saga["saga_model_spec"] == (saga_spec or "openai:" + installer.DEFAULT_SAGA_MODELS["openai"])
    assert saga["embedding"] == embedding
    assert saga["saga_model_spec"] != "openai:gpt-test"
    assert (deploy / "build.sh").stat().st_mode & 0o777 == 0o755
    assert "https://github.com/neworg/mimir.git#" + SHA in (deploy / "build.sh").read_text()
    assert any("https://github.com/neworg/mimir.git#" + SHA in argv for argv in calls)
    # Check both providers and mcp independently, in the live and saved builds.
    expected_extras = {"openai", "mcp", "local-embeddings"}
    if saga_spec:
        expected_extras.add(saga_spec.split(":", 1)[0])
    extras_arg = "MIMIR_EXTRAS=" + ",".join(sorted(expected_extras))
    root_builds = [argv for argv in calls if argv[:2] == ["docker", "build"]
                   and "MIMIR_PLUGIN_SELECTION=cultivate" in argv]
    assert len(root_builds) == 2
    for argv in root_builds:
        index = argv.index("CHAINLINK_ARTIFACT_STAGE=chainlink-build")
        assert argv[index - 1] == "--build-arg"
        assert argv[argv.index(extras_arg) - 1] == "--build-arg"
    import shlex
    build_lines = (deploy / "build.sh").read_text().splitlines()
    saved_build, = [shlex.split(line) for line in build_lines
                   if "MIMIR_PLUGIN_SELECTION=cultivate" in line]
    index = saved_build.index("CHAINLINK_ARTIFACT_STAGE=chainlink-build")
    assert saved_build[index - 1] == "--build-arg"
    assert saved_build[saved_build.index(extras_arg) - 1] == "--build-arg"
    assert json.loads((deploy / "manifest.json").read_text())["test"] == "./tools/verify"
    assert (deploy / ".dockerignore").read_text() == "*\n!Dockerfile.toolchain\n"
    for name in ("compose.yml", "Dockerfile.toolchain", "manifest.json", "provision.py"):
        text = (deploy / name).read_text()
        assert github_secret not in text and model_secret not in text and "other-secret-test-2008" not in text
    compose = yaml.safe_load((deploy / "compose.yml").read_text())
    assert compose["services"]["agent"]["environment"]["MIMIR_PLUGINS"] == "cultivate"
    assert compose["services"]["agent"]["environment"]["MIMIR_ENABLE_CLAUDE_CODE"] == "0"
    assert "WORKLINK_REPO" not in compose["services"]["agent"]["environment"]
    assert compose["services"]["agent"]["environment"]["MIMIR_ACP_ENABLED"] == "1"
    assert compose["services"]["agent"]["environment"]["MIMIR_PR_CHECKOUT_LEASE_ROOT"] == "/workspace/pr-leases"
    assert compose["services"]["agent"]["environment"]["XDG_CACHE_HOME"] == "/home/mimir/.cache"
    assert "cache:/home/mimir/.cache" in compose["services"]["agent"]["volumes"]
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


def test_pinned_auxiliary_tools_and_repo_override():
    files = {".github/workflows/rust.yml": "uses: taiki-e/install-action@v2\nwith:\n  tool: cargo-nextest@0.9.147"}
    pin = installer._tool_pin(files, "nextest", installer.DEFAULT_NEXTEST_VERSION)
    assert pin == "0.9.147"
    layer = installer.render_layer("rust", "1.99.1", "root:sha", nextest_version=pin)
    assert "https://get.nexte.st/0.9.147/$platform" in layer
    assert "platform=linux-arm" in layer and "platform=linux" in layer
    assert "cargo install" not in layer and "sh.rustup.rs" not in layer
    assert "/rustup/archive/1.28.2/" in layer
    assert installer._tool_pin({}, "nextest", installer.DEFAULT_NEXTEST_VERSION) == "0.9.148"
    assert "https://astral.sh/uv/" + installer.DEFAULT_UV_VERSION + "/install.sh" in installer.render_layer("python", "3.11.11", "root:sha")
    files["another"] = "cargo-nextest@0.9.146"
    with pytest.raises(installer.SetupError, match="Conflicting"):
        installer._tool_pin(files, "nextest", installer.DEFAULT_NEXTEST_VERSION)


@pytest.mark.parametrize("path", ["tools/verify", "scripts/verify", "tools/verify.sh", "scripts/verify.sh"])
def test_executable_verify_script_preferred(path):
    for mode, expected in (("100755", "./" + path), ("100644", None)):
        assert installer._verify_script("org/repo", "feature/test", "secret", lambda url, token: (
            200, {"tree": [{"path": path, "mode": mode, "type": "blob"}]})) == expected
    with pytest.raises(installer.SetupError, match="inspect"):
        installer._verify_script("org/repo", "main", "secret", lambda *a: (200, {"truncated": True}))


def test_compose_structural_changes_survive_format_drift_and_refuse_invalid_shape():
    from mimir.scaffold_docker import render_compose_yml
    template = render_compose_yml(service_name="agent", web_port=8123, mode="pypi")
    doc = yaml.safe_load(template[template.index("services:\n"):])
    doc["services"]["agent"]["environment"].pop("MIMIR_PLUGINS")
    doc["services"]["agent"]["environment"].pop("MIMIR_ENABLE_CLAUDE_CODE")
    # Different ordering, indentation and missing old lines cannot silently
    # omit required settings when the installer modifies a parsed mapping.
    drifted = yaml.safe_dump(doc, sort_keys=True, indent=4)
    result = yaml.safe_load(installer.render_compose(drifted, "image:sha", "rust"))
    agent = result["services"]["agent"]
    assert agent["image"] == "image:sha" and "build" not in agent
    assert agent["environment"]["MIMIR_PLUGINS"] == "cultivate"
    assert agent["environment"]["MIMIR_ACP_ENABLED"] == "1"
    assert agent["environment"]["MIMIR_HOME"] == "/home/mimir/agent"
    for bad in ("services: {}", "services: {agent: {build: {}, environment: []}}", "broken: ["):
        with pytest.raises(installer.SetupError, match="Incompatible scaffold"):
            installer.render_compose(bad, "image:sha", "rust")


def test_plain_git_credential_helper_reads_environment_without_persisting_secret(tmp_path):
    # Exercise Git's real helper protocol with no network or gh binary. Run the
    # production helper assignments extracted from the provisioning program.
    import ast
    tree = ast.parse(installer._PROVISION)
    helper_node = next(node for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == "helper" for t in node.targets))
    helper = ast.literal_eval(helper_node.value)
    import os
    env = dict(os.environ, GITHUB_TOKEN="fixture-private-token", GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=str(tmp_path / "empty-config"), GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(["git", "-c", "credential.helper=", "-c", "credential.helper=" + helper,
                             "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                            text=True, capture_output=True, env=env, check=True)
    assert "password=fixture-private-token" in result.stdout
    assert "fixture-private-token" not in helper and not result.stderr


def test_source_repo_validation():
    assert installer.validate("source_repo", "neworg/mimir") == "neworg/mimir"
    for slug in ("../escape", "o/.", "https://github.com/o/r", "o/r;evil"):
        with pytest.raises(installer.SetupError):
            installer.validate("source_repo", slug)


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
    monkeypatch.setenv("HOME", "/home/mimir")
    monkeypatch.setenv("XDG_CACHE_HOME", "/home/mimir/.cache")
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    state = home / "state"
    state.mkdir(parents=True)
    existing = {"people": [{"canonical": "another", "aliases": ["email:another@example.com"]}]}
    (state / "identities.yaml").write_text(yaml.safe_dump(existing))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"slug": "org/repo", "branch": "main", "owner": "owner",
                                     "display": "Owner", "email": "owner@example.com",
                                     "test": "npm run verify", "rotate": False,
                                     "saga_model_spec": "openai:gpt-5.4-nano", "embedding": "local", "reembed": False}))
    script = (installer._PROVISION
              .replace("/opt/cultivate-manifest.json", str(manifest))
              .replace("/home/mimir/agent", str(home))
              .replace("Path('/workspace')", "Path(" + repr(str(tmp_path)) + ")"))
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
    monkeypatch.setattr(installer, "_provision_saga", lambda *a, **kw: False)
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
    assert all(argv[0] == "git" for argv in calls)
    assert not any("setup-git" in argv for argv in calls)
    clone = next(argv for argv in calls if "clone" in argv)
    assert clone[-1] == str(tmp_path / "repo")
    assert "password=$GITHUB_TOKEN" in " ".join(clone)
    pushes = [argv for argv in calls if "push" in argv]
    assert len(pushes) == 2
    assert all(argv[:4] == ["git", "-C", str(repo), "push"] and
               argv[4:6] == ["--dry-run", "origin"] and
               argv[6].startswith("HEAD:refs/heads/cultivate-setup-probe-") for argv in pushes)
    assert pushes[0][6] != pushes[1][6]
    assert all("secret" not in " ".join(argv) for argv in pushes)
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


@pytest.mark.parametrize("spec,embedding,llm,provider,dim,key", [
    ("openai:gpt-5.4-nano", "local", "openai_compat", "local", 1024, None),
    ("anthropic:claude-haiku-5-5", "openai:text-embedding-3-small", "anthropic", "openai", 1536, "OPENAI_API_KEY"),
    ("openai:gpt-5.4-nano", "voyage:voyage-4-lite", "openai_compat", "voyage", 1024, "VOYAGE_API_KEY"),
])
def test_saga_provisioning(tmp_path, spec, embedding, llm, provider, dim, key):
    fetched = []
    assert installer._provision_saga(tmp_path, spec, embedding, prefetch=lambda: fetched.append(True))
    text = (tmp_path / "saga.toml").read_text()
    parsed = tomllib.loads(text)
    assert parsed["llm"]["provider"] == llm
    assert parsed["llm"]["api_key_env"] == spec.split(":")[0].upper() + "_API_KEY"
    assert parsed["embedding"]["provider"] == provider
    if embedding == "local":
        assert parsed["embedding"]["model"] == "voyageai/voyage-4-nano"
    assert parsed["embedding"]["dimensions"] == dim
    assert parsed["embedding"].get("api_key_env") == key
    assert parsed["embedding"].get("send_input_type") == (True if provider == "voyage" else None)
    assert fetched == ([True] if embedding == "local" else [])
    before = (tmp_path / "saga.toml").read_bytes()
    assert not installer._provision_saga(tmp_path, spec, embedding, prefetch=lambda: None)
    assert (tmp_path / "saga.toml").read_bytes() == before
    (tmp_path / "saga.toml").write_text(text.replace(parsed["llm"]["model"], "operator-model"))
    with pytest.raises(RuntimeError, match=r"\[llm\] conflicts"):
        installer._provision_saga(tmp_path, spec, embedding, prefetch=lambda: None)
    assert "operator-model" in (tmp_path / "saga.toml").read_text()


def test_existing_vectors_require_explicit_reembed(tmp_path):
    installer._provision_saga(tmp_path, "openai:gpt-5.4-nano", "local", prefetch=lambda: None)
    with pytest.raises(RuntimeError, match="--reembed"):
        installer._provision_saga(tmp_path, "openai:gpt-5.4-nano", "voyage:voyage-4-lite")
    (tmp_path / ".mimir").mkdir()
    (tmp_path / ".mimir/saga.db").touch()
    with pytest.raises(RuntimeError, match="--reembed"):
        installer._provision_saga(tmp_path, "openai:gpt-5.4-nano", "voyage:voyage-4-lite")
    assert tomllib.loads((tmp_path / "saga.toml").read_text())["embedding"]["provider"] == "local"
    updated = []
    assert installer._provision_saga(tmp_path, "openai:gpt-5.4-nano", "voyage:voyage-4-lite",
                                    reembed=True, repair=lambda path, **kw: updated.append((path, kw)) or {},
                                    prefetch=lambda: None)
    assert updated[0][1]["force"] is True
    assert tomllib.loads((tmp_path / "saga.toml").read_text())["embedding"]["provider"] == "voyage"


def test_operator_embedding_edit_is_never_replaced_even_with_reembed(tmp_path):
    installer._provision_saga(tmp_path, "openai:gpt-5.4-nano", "local", prefetch=lambda: None)
    path = tmp_path / "saga.toml"
    path.write_text(path.read_text().replace("dimensions = 1024", "dimensions = 768"))
    original = path.read_bytes()
    with pytest.raises(RuntimeError, match="conflicts"):
        installer._provision_saga(tmp_path, "openai:gpt-5.4-nano", "voyage:voyage-4-lite",
                                reembed=True)
    assert path.read_bytes() == original


def test_existing_database_without_embedding_config_requires_reembed(tmp_path):
    (tmp_path / ".mimir").mkdir()
    (tmp_path / ".mimir/saga.db").touch()
    with pytest.raises(RuntimeError, match="--reembed"):
        installer._provision_saga(tmp_path, "openai:gpt-5.4-nano", "local", prefetch=lambda: None)
    assert not (tmp_path / "saga.toml").exists()


def test_incomplete_reembed_prevents_provisioning_success(tmp_path):
    installer._provision_saga(tmp_path, "openai:gpt-5.4-nano", "local", prefetch=lambda: None)
    (tmp_path / ".mimir").mkdir()
    (tmp_path / ".mimir/saga.db").touch()
    with pytest.raises(RuntimeError, match="Saga re-embed incomplete"):
        installer._provision_saga(tmp_path, "openai:gpt-5.4-nano", "voyage:voyage-4-lite",
                                reembed=True, repair=lambda *a, **kw: {"sessions_unrepaired": 1})


def test_core_scaffold_is_only_unmarked_config_replaced(tmp_path):
    from mimir.commands.setup import _default_saga_toml
    path = tmp_path / "saga.toml"
    scaffold = _default_saga_toml(tmp_path)
    path.write_text(scaffold + "\n# operator note\n")
    with pytest.raises(RuntimeError, match="conflicts"):
        installer._provision_saga(tmp_path, "openai:gpt-5.4-nano", "local", prefetch=lambda: None)
    assert path.read_text() == scaffold + "\n# operator note\n"
    path.write_text(scaffold)
    assert installer._provision_saga(tmp_path, "openai:gpt-5.4-nano", "local", prefetch=lambda: None)
    result = tomllib.loads(path.read_text())
    assert result["llm"]["provider"] == "openai_compat"
    assert result["embedding"]["provider"] == "local"


def test_provision_prefetches_pinned_nano_before_publishing_config(tmp_path, monkeypatch):
    from mimir.saga import embeddings

    calls = []

    def load(provider):
        assert not (tmp_path / "saga.toml").exists()
        calls.append((provider.model_id, provider.dimensions()))

    monkeypatch.setattr(embeddings.LocalProvider, "_load_model", load)
    installer._provision_saga(tmp_path, "openai:gpt-5.4-nano", "local")
    assert calls == [("voyageai/voyage-4-nano", 1024)]


def test_image_installers_select_locked_cpu_torch_for_local_extra():
    root = Path(__file__).resolve().parents[3]
    lock = tomllib.loads((root / "uv.lock").read_text())
    cpu = next(p for p in lock["package"] if p["name"] == "torch" and p["version"].endswith("+cpu"))
    assert cpu["source"]["registry"] == "https://download.pytorch.org/whl/cpu"
    assert not any(p["name"].startswith("nvidia-") for p in lock["package"])
    for path in ("Dockerfile", "plugins/cultivate/mimir_cultivate/docker/controller.sh",
                 "plugins/cultivate/mimir_cultivate/docker/executor.sh"):
        text = (root / path).read_text()
        assert "*,local-embeddings,*" in text
        assert "torch==" + cpu["version"] in text
        assert "--extra-index-url https://download.pytorch.org/whl/cpu" in text


def test_prompts_recommend_fast_model_and_reuse_embedding_key(monkeypatch, capsys):
    args = argparse.Namespace(model_spec="openai:flagship", saga_model_spec=None, embedding=None)
    monkeypatch.setattr(installer.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert installer._field(args, "saga_model_spec", "Frequent short calls: faster, cheaper; agent openai:flagship is an alternative",
                            default="openai:" + installer.DEFAULT_SAGA_MODELS["openai"]) == "openai:" + installer.DEFAULT_SAGA_MODELS["openai"]
    assert "faster, cheaper" in capsys.readouterr().out


def test_default_saga_anthropic_model():
    assert installer.DEFAULT_SAGA_MODELS["anthropic"] == "claude-haiku-5-5"


def test_embedding_key_distinction():
    assert installer.EMBEDDING_MODELS["openai"][3] == "OPENAI_API_KEY"
    assert installer.EMBEDDING_MODELS["voyage"][3] != "OPENAI_API_KEY"


@pytest.mark.parametrize(("push_ok", "probe_stderr", "expected_marker"), [
    pytest.param(True, b"", None, id="success"),
    pytest.param(False, b"The requested URL returned error: 403", installer._PUSH_PROBE_FAILED, id="403"),
    pytest.param(False, b"The requested URL returned error: 401", installer._PUSH_PROBE_FAILED, id="401"),
    pytest.param(False, b"Authentication failed", installer._PUSH_PROBE_FAILED, id="authentication"),
    pytest.param(False, b"Permission to org/repo.git denied to bot.", installer._PUSH_PROBE_FAILED, id="permission"),
    pytest.param(False, b"could not read Username for 'https://github.com'", installer._PUSH_PROBE_FAILED, id="username"),
    pytest.param(False, b"Could not resolve host: github.com", installer._PUSH_PROBE_UNREACHABLE, id="dns"),
    pytest.param(False, b"Recv failure: Connection reset by peer", installer._PUSH_PROBE_UNREACHABLE, id="reset"),
    pytest.param(False, b"Could not resolve proxy: proxy.example", installer._PUSH_PROBE_UNREACHABLE, id="proxy"),
    pytest.param(False, b"The requested URL returned error: 503", installer._PUSH_PROBE_UNREACHABLE, id="github-503"),
    pytest.param(False, b"unknown failure \\xff", installer._PUSH_PROBE_UNREACHABLE, id="unknown"),
])
@pytest.mark.parametrize("existing_checkout", [False, True], ids=["fresh", "rerun"])
def test_push_probe_controls_agent_start(tmp_path, monkeypatch, capsys, push_ok, probe_stderr, expected_marker, existing_checkout):
    """Exercise setup's run boundary and the actual provision program, offline."""
    from mimir.saga.embeddings import LocalProvider

    monkeypatch.setattr(LocalProvider, "_load_model", lambda self: None)
    monkeypatch.setenv("HOME", "/home/mimir")
    monkeypatch.setenv("XDG_CACHE_HOME", "/home/mimir/.cache")
    monkeypatch.setattr(installer, "_installed_license", lambda: (b"license", "1.0"))
    parser = argparse.ArgumentParser()
    installer.add_argparse(parser.add_subparsers(dest="command"))
    deploy = tmp_path / "deploy"
    monkeypatch.setenv("MIMIR_CULTIVATE_GITHUB_TOKEN", "fixture-token")
    monkeypatch.setenv("MIMIR_CULTIVATE_MODEL_KEY", "fixture-model-key")
    args = parser.parse_args([
        "cultivate", "setup", "--accept-license", "--repo", "org/repo", "--bot-login", "bot",
        "--bot-email", "bot@example.com", "--owner", "owner", "--display-name", "Owner",
        "--owner-email", "owner@example.com", "--model-spec", "openai:gpt-test",
        "--saga-model-spec", "openai:gpt-5.4-nano", "--embedding", "local",
        "--deploy", str(deploy), "--port", "8123",
    ])

    def request(url, token):
        assert token == "fixture-token"
        if url.endswith("/user"):
            return 200, {"login": "bot"}
        if url.endswith("/repos/org/repo"):
            return 200, {"permissions": {"push": True}}  # collaborator role, not PAT write access
        if "/git/ref/" in url:
            return 200, {"object": {"sha": SHA}}
        if "package.json" in url:
            return 200, {"encoding": "base64", "content": base64.b64encode(
                b'{"engines":{"node":"22.16.0"}}').decode()}
        if "/contents/" in url:
            return 404, {}
        if "/git/trees/" in url:
            return 200, {"tree": []}
        return 200, {}

    import mimir.plugins.skills as skills
    monkeypatch.setattr(skills, "resolve_skill", lambda name, core_names: SimpleNamespace(dirname=name.split(":")[1]))
    monkeypatch.setattr(skills, "install_skill", lambda home, skill: ((home / "skills" / skill.dirname).mkdir(parents=True), None))
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    if existing_checkout:
        (repo / ".git").mkdir(parents=True)
    git_calls = []
    docker_calls = []

    def subprocess_run(argv, **kwargs):
        if argv[0] == "git":
            git_calls.append(argv)
            if "clone" in argv:
                (repo / ".git").mkdir(parents=True)
            if "push" in argv:
                return subprocess.CompletedProcess(argv, 0 if push_ok else 128,
                                                   stdout=b"secret remote output fixture-token",
                                                   stderr=probe_stderr + b" remote secret fixture-token")
            return subprocess.CompletedProcess(argv, 0, stdout="https://github.com/org/repo.git\n")
        if argv[:2] == ["docker", "exec"]:
            kwargs["stdout"].write(b"owner-key\n")
            return subprocess.CompletedProcess(argv, 0)
        docker_calls.append(argv)
        if "compose.provision.yml" in argv:
            home.mkdir(exist_ok=True)
            manifest = deploy / "manifest.json"
            script = ((deploy / "provision.py").read_text()
                      .replace("/opt/cultivate-manifest.json", str(manifest))
                      .replace("/home/mimir/agent", str(home))
                      .replace("Path('/workspace')", "Path(" + repr(str(tmp_path)) + ")"))
            output = io.StringIO()
            with redirect_stdout(output):
                try:
                    exec(compile(script, "provision.py", "exec"), {"__name__": "__main__"})
                except SystemExit as exc:
                    assert exc.code == 1
                    assert output.getvalue().splitlines() == [expected_marker]
                    return subprocess.CompletedProcess(argv, exc.code, stdout=output.getvalue(), stderr="remote secret fixture-token")
            return subprocess.CompletedProcess(argv, 0, stdout=output.getvalue(), stderr="")
        if argv[-3:] == ["ps", "-q", "agent"]:
            return subprocess.CompletedProcess(argv, 0, stdout="b" * 64, stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(installer.subprocess, "run", subprocess_run)
    verified = []
    if push_ok:
        installer.setup(args, request=request, verify=lambda *a: verified.append(a))
        assert verified and any(argv[-3:] == ["up", "-d", "agent"] for argv in docker_calls)
    else:
        with pytest.raises(installer.SetupError) as caught:
            installer.setup(args, request=request, verify=lambda *a: verified.append(a))
        message = str(caught.value)
        if expected_marker == installer._PUSH_PROBE_FAILED:
            assert message == installer._PUSH_PROBE_HELP
            assert "resource owner = the repository owner" in message
            assert "organization" in message and "Contents: Read and write" in message
            assert "approval" in message
        else:
            assert message == "Push probe could not reach the remote (network or GitHub error); retry setup."
            assert "Token" not in message and "PAT" not in message
        assert probe_stderr.decode("utf-8", errors="replace") not in message
        assert "fixture-token" not in message and "remote secret" not in message
        assert not verified and not any(argv[-3:] == ["up", "-d", "agent"] for argv in docker_calls)
        assert not (home / "repositories.yaml").exists()
    assert len([argv for argv in git_calls if "push" in argv]) == 1
    assert len([argv for argv in git_calls if "clone" in argv]) == (0 if existing_checkout else 1)
    assert all("fixture-token" not in " ".join(argv) for argv in git_calls + docker_calls)
    captured = capsys.readouterr()
    assert "fixture-token" not in captured.out + captured.err
    assert "secret remote output" not in captured.out + captured.err
