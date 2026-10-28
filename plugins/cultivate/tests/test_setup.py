"""Offline contract tests for the Cultivate operator installer."""

from __future__ import annotations

import argparse
import base64
from contextlib import redirect_stdout
import io
import json
import logging
import os
import re
import shutil
import tomllib
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

from mimir_cultivate import setup as installer


SHA = "a" * 40


@pytest.fixture(autouse=True)
def isolated_license_record(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("GH_USER_NAME", "trellisbot-squashmerge")
    monkeypatch.setenv("GH_USER_EMAIL", "trellisbot@example.com")


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


@pytest.mark.parametrize("adapter,channel,credentials", [
    ("web", None, {}), ("none", None, {}),
    ("discord", "123456789012345678", {"DISCORD_TOKEN": "discord.bot-token"}),
    ("slack", "C12345678", {"SLACK_BOT_TOKEN": "xoxb-bot-token", "SLACK_APP_TOKEN": "xapp-app-token"}),
])
def test_alert_interactive_instructions_precede_hidden_credentials(monkeypatch, capsys, adapter, channel, credentials):
    monkeypatch.setattr(installer.sys.stdin, "isatty", lambda: True)
    for name in credentials:
        monkeypatch.delenv("MIMIR_CULTIVATE_" + name, raising=False)
    prompts = []
    def prompt(text):
        prompts.append(text)
        return adapter if "Where should" in text else channel
    monkeypatch.setattr("builtins.input", prompt)
    displayed = []
    def hidden(text):
        output = capsys.readouterr().out
        displayed.append(output)
        assert (installer.DISCORD_BOT_SETUP if adapter == "discord" else installer.SLACK_BOT_SETUP) in "".join(displayed)
        prompts.append("hidden:" + text)
        return credentials[text.split(":", 1)[0].replace(" ", "_").upper()]
    monkeypatch.setattr(installer.getpass, "getpass", hidden)
    selected, selected_channel, env = installer._alert_settings(argparse.Namespace(), "Owner", {}, {})
    assert selected == adapter and selected_channel == channel
    assert all(env[key] == value for key, value in credentials.items())
    assert sum(text.startswith("hidden:") for text in prompts) == len(credentials)
    assert all(secret not in "".join(displayed) + capsys.readouterr().out for secret in credentials.values())


def test_shipped_bot_instructions_cover_bridge_permissions_and_ids():
    discord = installer.DISCORD_BOT_SETUP
    for step in ("Developer Portal", "discord.com/developers/applications", "Message Content Intent",
                 "bot scope", "scope=bot&permissions=101440", "Developer Mode", "channel ID"):
        assert step in discord
    permissions = (1 << 10) | (1 << 11) | (1 << 16) | (1 << 6) | (1 << 15)
    assert permissions == 101440
    slack = installer.SLACK_BOT_SETUP
    for step in ("Socket Mode", "connections:write", "xapp-", "xoxb-", "install", "channel ID",
                 "chat:write", "channels:history", "groups:history", "im:history", "mpim:history",
                 "im:write", "reactions:write", "users:read", "users:read.email", "files:write",
                 "app_mention", "reaction_added"):
        assert step.lower() in slack.lower()


@pytest.mark.parametrize("adapter,channel,creds,reason", [
    ("discord", None, {}, "--operator-alert-channel-id"),
    ("discord", "abc", {}, "channel ID"),
    ("slack", "C12345678", {}, "--slack-bot-token-file"),
    ("discord", "123456789012345678", {"DISCORD_TOKEN": "bad token"}, "DISCORD_TOKEN"),
    ("slack", "C12345678", {"SLACK_BOT_TOKEN": "wrong", "SLACK_APP_TOKEN": "xapp-valid"}, "SLACK_BOT_TOKEN"),
    ("slack", "C12345678", {"SLACK_BOT_TOKEN": "xoxb-valid", "SLACK_APP_TOKEN": "wrong"}, "SLACK_APP_TOKEN"),
])
def test_alert_noninteractive_errors_before_deployment_write(tmp_path, monkeypatch, adapter, channel, creds, reason):
    monkeypatch.setattr(installer.sys.stdin, "isatty", lambda: False)
    args = argparse.Namespace(operator_alert_adapter=adapter, operator_alert_channel_id=channel)
    with pytest.raises(installer.SetupError, match=reason):
        installer._alert_settings(args, "owner", {}, creds)
    assert not list(tmp_path.iterdir())


def test_alert_flags_manifest_round_trip_and_none(monkeypatch, capsys):
    monkeypatch.setattr(installer.sys.stdin, "isatty", lambda: False)
    parser = argparse.ArgumentParser()
    installer.add_argparse(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["cultivate", "setup", "--operator-alert-adapter", "web"])
    adapter, channel, env = installer._alert_settings(args, "Alice.Name", {}, {})
    assert env == {"MIMIR_OPERATOR_ALERT_CHANNEL": "web-Alice.Name"}
    args.operator_alert_adapter = None
    assert installer._alert_settings(args, "Alice.Name", json.loads(json.dumps({
        "operator_alert_adapter": adapter, "operator_alert_channel_id": channel,
    })), {}) == (adapter, channel, env)
    args.operator_alert_adapter = "none"
    assert installer._alert_settings(args, "Alice.Name", {}, {}) == ("none", None, {})
    assert "rerun setup" in capsys.readouterr().out


def test_alert_adapter_change_removes_old_bot_credentials(tmp_path):
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    assert installer._env(deploy, {"MIMIR_API_KEY": "master",
        "DISCORD_TOKEN": "discord-secret", "MIMIR_OPERATOR_ALERT_CHANNEL": "discord-123456789012345678",
        "MIMIR_DISCORD_ALLOWED_CHANNELS": "123456789012345678",
        "MIMIR_DISCORD_REQUIRE_MENTION": "true"}, manage_alerts=True) == "master"
    installer._env(deploy, {"MIMIR_API_KEY": "rotated-but-preserved",
        "MIMIR_OPERATOR_ALERT_CHANNEL": "web-owner"}, manage_alerts=True)
    text = (deploy / "compose.env").read_text()
    assert "MIMIR_API_KEY=master" in text and "web-owner" in text
    assert "DISCORD_TOKEN" not in text and "MIMIR_DISCORD_" not in text and "discord-secret" not in text
    installer._env(deploy, {"MIMIR_API_KEY": "other"}, manage_alerts=True)
    assert "MIMIR_OPERATOR_ALERT_CHANNEL" not in (deploy / "compose.env").read_text()
    assert (deploy / "compose.env").stat().st_mode & 0o777 == 0o600


def test_alert_verification_checks_owner_history_and_stable_failures(monkeypatch):
    calls = []
    messages = []
    def http(url, *, key, data=None):
        calls.append((url, key, data))
        if url.endswith("history"):
            return {"data": {"messages": list(messages)}}
        assert url.endswith("operator-alert-check") and data == {} and key == "owner-key"
        messages.append({"role": "assistant", "text": "Cultivate setup: operator alerts enabled"})
        return {"ok": True, "data": {"sent": True}}
    monkeypatch.setattr(installer, "_http", http)
    installer._verify_alert("http://127.0.0.1:8123", "owner-key", "web")
    assert [c[0].split("/")[-1] for c in calls] == ["history", "operator-alert-check", "history"]
    calls.clear()
    installer._verify_alert("http://127.0.0.1:8123", "owner-key", "slack")
    assert len(calls) == 1
    def failure(url, *, key, data=None):
        raise installer.urllib.error.HTTPError(url, 409, "token-must-not-leak", {}, None)
    monkeypatch.setattr(installer, "_http", failure)
    with pytest.raises(installer.SetupError, match="alert_bridge_unconnected") as exc:
        installer._verify_alert("http://127.0.0.1:8123", "owner-key", "slack")
    assert "token-must-not-leak" not in str(exc.value)
    monkeypatch.setattr(installer, "_http", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("private token")))
    with pytest.raises(installer.SetupError, match="alert_send_failed") as exc:
        installer._verify_alert("http://127.0.0.1:8123", "owner-key", "discord")
    assert "private token" not in str(exc.value)


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


@pytest.mark.parametrize('kind, version', [('rust', '1.99.1'), ('node', '22.16.0'), ('python', '3.11.11')])
def test_every_generated_run_fails_closed(kind, version):
    layer = installer.render_layer(kind, version, 'root:sha')
    scripts = [line.removeprefix('RUN ') for line in layer.splitlines() if line.startswith('RUN ')]
    assert scripts and all(script.startswith('set -eu; ') for script in scripts)


@pytest.mark.parametrize('kind, packages', [
    ('rust', 'gcc libc6-dev binutils pkg-config'),
    ('node', 'g++ make python3'),
    ('python', 'gcc libc6-dev'),
])
def test_native_build_tools_are_installed_fail_closed(kind, packages):
    layer = installer.render_layer(kind, '1.99.1', 'root:sha')
    install = next(line for line in layer.splitlines() if 'apt-get install' in line)
    assert install.startswith('RUN set -eu; ')
    assert f'apt-get update && apt-get install -y --no-install-recommends {packages} && ' in install
    assert 'rm -rf /var/lib/apt/lists/* && ' in install


@pytest.mark.parametrize('kind', ['rust', 'node', 'python'])
def test_extra_system_packages_run_in_fail_closed_layer(kind):
    layer = installer.render_layer(kind, '1.99.1', 'root:sha',
                                   system_packages=['lsof', 'libfoo-dev=1:2.3-4~deb12'])
    assert ('RUN set -eu; apt-get update && apt-get install -y --no-install-recommends '
            'lsof libfoo-dev=1:2.3-4~deb12 && rm -rf /var/lib/apt/lists/*\n') in layer
    assert layer.index('USER root\nRUN set -eu; apt-get update') < layer.index('libfoo-dev=')


@pytest.mark.parametrize('bad', ['lsof; rm -rf /', '$(id)', 'LSOF', '', 'lsof,',
                                 'lsof=1;id', 'a', 'a,,b', 'lsof;id', 'lsof,  '])
def test_invalid_system_packages_rejected_before_any_file_written(tmp_path, monkeypatch, bad):
    monkeypatch.setattr(installer, '_license_gate', lambda args: pytest.fail('license file written before validation'))
    deploy = tmp_path / 'deploy'
    with pytest.raises(installer.SetupError, match='Invalid system package'):
        installer.setup(argparse.Namespace(system_packages=bad, deploy=str(deploy)))
    assert not list(tmp_path.iterdir())
    with pytest.raises(installer.SetupError, match='Invalid system package'):
        installer.render_layer('node', '22.16.0', 'root:sha', system_packages=[bad])


def test_system_package_whitespace_and_diagnostics():
    assert installer.validate_system_packages('lsof, file'.split(',')) == ['lsof', 'file']
    with pytest.raises(installer.SetupError, match="Invalid system package 'lsof;id'"):
        installer.validate_system_packages(['file', 'lsof;id'])


def test_base_image_installs_common_tools():
    base = Path(installer.__file__).parent / 'docker/base.sh'
    text = base.read_text()
    install = text.split('apt-get install -y --no-install-recommends \\', 1)[1].split('curl -fsSL', 1)[0]
    packages = set(install.replace('\\', ' ').split())
    assert {'lsof', 'file', 'unzip', 'less', 'make', 'netcat-openbsd', 'iproute2', 'psmisc'} <= packages


def test_system_packages_are_checked_as_worklink_in_gate_context(tmp_path, monkeypatch):
    calls = []
    statuses = {'lsof': 'install ok installed', 'file': 'install ok installed'}

    def run(argv, *, cwd):
        assert cwd == tmp_path
        calls.append(argv)
        return statuses[argv[-1]]

    monkeypatch.setattr(installer, '_run', run)
    installer._verify_system_packages(tmp_path, ['lsof', 'file=1:2.3-4'])
    assert calls == [['docker', 'compose', 'exec', '-T', '-u', 'worklink', '-w', '/tmp',
                      'agent', 'dpkg-query', '-W', '-f=${Status}', name] for name in ('lsof', 'file')]
    statuses['file'] = 'deinstall ok config-files'
    with pytest.raises(installer.SetupError, match='system_package_missing: file'):
        installer._verify_system_packages(tmp_path, ['lsof', 'file'])

    def missing(argv, *, cwd):
        raise installer.SetupError('dpkg-query operation failed')

    monkeypatch.setattr(installer, '_run', missing)
    with pytest.raises(installer.SetupError, match='system_package_missing: lsof'):
        installer._verify_system_packages(tmp_path, ['lsof'])


def test_real_image_proof_rejects_layer_without_its_own_compiler():
    import runpy

    script = Path(__file__).resolve().parents[3] / 'scripts/worklink_image_identity.py'
    require = runpy.run_path(str(script))['require_rust_native_toolchain']
    layer = installer.render_layer('rust', '1.90.0', 'root:sha')
    require(layer)
    mutated = layer.replace('apt-get install -y --no-install-recommends gcc libc6-dev binutils pkg-config && ', '')
    assert mutated != layer
    with pytest.raises(RuntimeError, match='omitted its native toolchain'):
        require(mutated)


def test_rust_layer_stops_before_rustup_if_apt_install_fails(tmp_path):
    layer = installer.render_layer('rust', '1.99.1', 'root:sha')
    script = next(line.removeprefix('RUN ') for line in layer.splitlines() if 'rustup-init' in line)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    marker = tmp_path / 'curl-ran'
    for name, body in [('dpkg', 'printf "amd64\\n"\n'),
                       ('apt-get', 'exit 23\n'), ('curl', f'touch {marker}\n')]:
        binary = bin_dir / name
        binary.write_text('#!/bin/sh\n' + body)
        binary.chmod(0o755)
    result = subprocess.run(['/bin/sh', '-c', script],
                            env=dict(os.environ, PATH=str(bin_dir) + ':/usr/bin:/bin'),
                            capture_output=True, text=True)
    assert result.returncode != 0 and not marker.exists()


@pytest.mark.parametrize('architecture', ['amd64', 'unsupported'])
def test_rust_layer_failed_install_cannot_be_masked_by_nextest(tmp_path, architecture):
    layer = installer.render_layer('rust', '1.99.1', 'root:sha')
    script = next(line.removeprefix('RUN ') for line in layer.splitlines() if 'rustup-init' in line)
    for original, replacement in (('/opt/rustup', str(tmp_path / 'rustup')),
                                  ('/opt/cargo', str(tmp_path / 'cargo')),
                                  ('/usr/local/bin', str(tmp_path / 'installed')),
                                  ('/tmp/rustup-init', str(tmp_path / 'rustup-init')),
                                  ('/tmp/nextest.tar.gz', str(tmp_path / 'nextest.tar.gz'))):
        script = script.replace(original, replacement)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    marker = tmp_path / 'nextest-ran'
    (bin_dir / 'dpkg').write_text(f'#!/bin/sh\nprintf "%s\\n" {architecture}\n')
    (bin_dir / 'curl').write_text(
        '#!/bin/sh\n'
        f'case "$*" in *get.nexte.st*) touch {marker}; touch {tmp_path / "nextest.tar.gz"}; exit 0;; esac\n'
        f'printf "#!/bin/sh\\nexit 23\\n" > {tmp_path / "rustup-init"}\n'
    )
    (bin_dir / 'tar').write_text('#!/bin/sh\nexit 0\n')
    for name in ('dpkg', 'curl', 'tar'):
        (bin_dir / name).chmod(0o755)
    result = subprocess.run(['/bin/sh', '-c', script],
                            env=dict(os.environ, PATH=str(bin_dir) + ':/usr/bin:/bin'),
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert not marker.exists(), 'nextest must not run after a failed rustup install or unsupported arch'


def test_rust_layer_links_all_installed_proxies_fail_closed(tmp_path):
    layer = installer.render_layer('rust', '1.99.1', 'root:sha')
    script = next(line.removeprefix('RUN ') for line in layer.splitlines() if 'rustup-init' in line)
    loop = script[script.index('for proxy in /opt/cargo/bin/*;'):].split(' done && ', 1)[0] + ' done'
    cargo_bin = tmp_path / 'cargo/bin'
    cargo_bin.mkdir(parents=True)
    installed = tmp_path / 'installed'
    installed.mkdir()
    names = ('rustup', 'cargo', 'rustc', 'rustfmt', 'cargo-fmt', 'cargo-clippy',
             'rustdoc', 'cargo-miri', 'new-rustup-proxy')
    for name in names:
        (cargo_bin / name).write_text('proxy')
    loop = loop.replace('/opt/cargo/bin', str(cargo_bin)).replace('/usr/local/bin', str(installed))
    result = subprocess.run(['/bin/sh', '-ceu', loop], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert {link.name for link in installed.iterdir()} == set(names)
    assert all((installed / name).is_symlink() and (installed / name).resolve() == cargo_bin / name
               for name in names)
    # A failed link must stop the build, even if another proxy could still link.
    result = subprocess.run(['/bin/sh', '-ceu', loop], capture_output=True, text=True)
    assert result.returncode != 0
    for proxy in cargo_bin.iterdir():
        proxy.unlink()
    result = subprocess.run(['/bin/sh', '-ceu', loop], capture_output=True, text=True)
    assert result.returncode != 0, 'an empty proxy directory must not produce a successful layer'


def test_node_layer_rejects_unsupported_architecture(tmp_path):
    script = next(line.removeprefix('RUN ') for line in installer.render_layer('node', '22.16.0', 'root:sha').splitlines()
                  if 'dpkg --print-architecture' in line)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    marker = tmp_path / 'downloaded'
    for name, body in [('dpkg', 'printf "unsupported\\n"\n'), ('curl', f'touch {marker}\n')]:
        binary = bin_dir / name
        binary.write_text('#!/bin/sh\n' + body)
        binary.chmod(0o755)
    result = subprocess.run(['/bin/sh', '-c', script],
                            env=dict(os.environ, PATH=str(bin_dir) + ':/usr/bin:/bin'), capture_output=True)
    assert result.returncode != 0 and not marker.exists()


@pytest.mark.parametrize("embedding,saga_spec", [
    ("local", None), ("openai:text-embedding-3-small", None),
    ("voyage:voyage-4-lite", "anthropic:claude-haiku-5-5"),
])
@pytest.mark.parametrize("alert_adapter", ["none", "web", "discord", "slack"])
def test_setup_is_idempotent_and_keeps_secrets_out_of_output(tmp_path, monkeypatch, capsys, caplog, embedding, saga_spec, alert_adapter):
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
    extra = ["--embedding", embedding, "--system-packages", "lsof,libfoo-dev=1:2.3-4~deb12"]
    if embedding.startswith("voyage:"):
        extra += ["--embedding-key-file", str(other_file)]
    if saga_spec:
        extra += ["--saga-model-spec", saga_spec, "--saga-model-key-file", str(other_file)]
    alert_secrets = {}
    if alert_adapter in {"discord", "slack"}:
        extra += ["--operator-alert-channel-id", "123456789012345678" if alert_adapter == "discord" else "C12345678"]
        names = ("discord-token",) if alert_adapter == "discord" else ("slack-bot-token", "slack-app-token")
        for name in names:
            value = {"discord-token": "discord.secret.test", "slack-bot-token": "xoxb-bot-secret",
                     "slack-app-token": "xapp-app-secret"}[name]
            path = tmp_path / name
            path.write_text(value)
            path.chmod(0o600)
            extra += ["--" + name + "-file", str(path)]
            alert_secrets[name.upper().replace("-", "_")] = value
    extra += ["--operator-alert-adapter", alert_adapter]
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
        if argv[-3:] == ["mimir", "worklink", "status"]:
            return "No Worklink leaf runs or labeled issues."
        if argv[-1] == installer._PR_LEASE_PROBE:
            return "[]"
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
    for index in range(2):
        if index:
            args.system_packages = None  # Re-run without the flag keeps the manifest's list.
            args.operator_alert_adapter = None
            args.operator_alert_channel_id = None
            args.discord_token_file = None
            args.slack_bot_token_file = None
            args.slack_app_token_file = None
        installer.setup(args, request=request, run=run,
                        verify=lambda *a: verified.append(a))
    stdout, stderr = capsys.readouterr()
    if saga_spec is None:
        assert "faster, cheaper" in stdout and "openai:gpt-test" in stdout
        assert "openai:gpt-6-luna:low" in stdout
    assert len(verified) == 2
    assert all(call[4:] == ('node', '22.16.0', ['lsof', 'libfoo-dev=1:2.3-4~deb12'], alert_adapter) for call in verified)
    assert 'System packages: lsof, libfoo-dev=1:2.3-4~deb12' in stdout
    for secret in (github_secret, model_secret, "other-secret-test-2008", "owner-key-test-2008", *alert_secrets.values()):
        assert secret not in stdout + stderr + caplog.text
        for argv in calls:
            assert secret not in " ".join(argv)
    assert (deploy / "compose.env").stat().st_mode & 0o777 == 0o600
    assert (deploy / "owner-web-key").stat().st_mode & 0o777 == 0o600
    assert github_secret in (deploy / "compose.env").read_text()
    env_text = (deploy / "compose.env").read_text()
    for key, value in alert_secrets.items():
        assert f"{key}={value}" in env_text
    if alert_adapter == "web":
        assert "MIMIR_OPERATOR_ALERT_CHANNEL=web-owner" in env_text
    elif alert_adapter in {"discord", "slack"}:
        prefix = alert_adapter.upper()
        channel = "123456789012345678" if alert_adapter == "discord" else "C12345678"
        assert f"MIMIR_OPERATOR_ALERT_CHANNEL={alert_adapter}-{channel}" in env_text
        assert f"MIMIR_{prefix}_ALLOWED_CHANNELS={channel}" in env_text
        assert f"MIMIR_{prefix}_REQUIRE_MENTION=true" in env_text
    else:
        assert "MIMIR_OPERATOR_ALERT_CHANNEL=" not in env_text
    assert ("VOYAGE_API_KEY=other-secret-test-2008" in env_text) == embedding.startswith("voyage:")
    assert ("ANTHROPIC_API_KEY=other-secret-test-2008" in env_text) == bool(saga_spec)
    saga = json.loads((deploy / "manifest.json").read_text())
    assert saga["saga_model_spec"] == (saga_spec or "openai:" + installer.DEFAULT_SAGA_MODELS["openai"] + ":low")
    assert saga["embedding"] == embedding
    assert saga["operator_alert_adapter"] == alert_adapter
    assert saga["operator_alert_channel_id"] == ("123456789012345678" if alert_adapter == "discord" else "C12345678" if alert_adapter == "slack" else None)
    assert saga["system_packages"] == ['lsof', 'libfoo-dev=1:2.3-4~deb12']
    assert 'lsof libfoo-dev=1:2.3-4~deb12 && rm -rf /var/lib/apt/lists/*' in (deploy / 'Dockerfile.toolchain').read_text()
    assert saga["saga_model_spec"] != "openai:gpt-test"
    assert (deploy / "build.sh").stat().st_mode & 0o777 == 0o755
    assert "https://github.com/neworg/mimir.git#" + SHA in (deploy / "build.sh").read_text()
    assert any("https://github.com/neworg/mimir.git#" + SHA in argv for argv in calls)
    # Check both providers and mcp independently, in the live and saved builds.
    expected_extras = {"openai", "mcp", "local-embeddings"}
    if alert_adapter in {"discord", "slack"}:
        expected_extras.add(alert_adapter)
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
        assert all(secret not in text for secret in (github_secret, model_secret, "other-secret-test-2008", *alert_secrets.values()))
    compose = yaml.safe_load((deploy / "compose.yml").read_text())
    assert compose["services"]["agent"]["environment"]["MIMIR_PLUGINS"] == "cultivate"
    assert compose["services"]["agent"]["environment"]["MIMIR_ENABLE_CLAUDE_CODE"] == "0"
    assert compose["services"]["agent"]["environment"]["MIMIR_WORKLINK_AGENT_ID"] == "cultivate-worklink"
    assert compose["services"]["agent"]["environment"]["WORKLINK_REPO"] == "/workspace/worklink/base/repo"
    assert compose["services"]["agent"]["environment"]["WORKLINK_REPO"] != "/workspace/repo"
    assert compose["services"]["agent"]["environment"]["MIMIR_ACP_ENABLED"] == "1"
    assert compose["services"]["agent"]["environment"]["MIMIR_PR_CHECKOUT_LEASE_ROOT"] == "/workspace/pr-leases"
    assert compose["services"]["agent"]["environment"]["XDG_CACHE_HOME"] == "/home/mimir/.cache"
    assert "cache:/home/mimir/.cache" in compose["services"]["agent"]["volumes"]
    assert compose["services"]["agent"]["depends_on"]["init"]["condition"] == "service_completed_successfully"
    assert set(compose["volumes"]) == {"home", "cache", "workspace", "pr-leases", "worklink"}
    assert any("MIMIR_CONTROLLER_COMMIT=" + SHA in part for argv in calls for part in argv)
    assert sum("docker" == argv[0] and "exec" in argv for argv in calls) == 2  # idle probes on the rerun
    assert sum("-f" in argv and "compose.provision.yml" in argv for argv in calls) == 2
    assert all(argv[argv.index('--user') + 1] == '1001:1001' for argv in calls
               if 'compose.provision.yml' in argv)
    assert sum(argv[-2:] == ["restart", "agent"] for argv in calls) == 0

    def unpushed(url, token):
        if "/git/ref/" in url:
            return 200, {"object": {"sha": "short"}}
        return request(url, token)

    before_calls = len(calls)
    with pytest.raises(installer.SetupError, match="pushed full"):
        installer.setup(args, request=unpushed, run=run, verify=lambda *a: None)
    assert len(calls) == before_calls

    # Exercise real setup writes, not just the renderer: set, change, clear,
    # and omit the flag after clearing. Both generated artifacts must agree
    # after every transition, and the verification callback gets that list.
    for requested, expected in [
        ('lsof, file', ['lsof', 'file']),
        ('unzip=6.0-28', ['unzip=6.0-28']),
        ('none', []),
        (None, []),
    ]:
        args.system_packages = requested
        installer.setup(args, request=request, run=run, verify=lambda *a: verified.append(a))
        assert json.loads((deploy / 'manifest.json').read_text())['system_packages'] == expected
        layer = (deploy / 'Dockerfile.toolchain').read_text()
        assert layer == installer.render_layer(
            'node', '22.16.0', 'mimir-cultivate-root:' + SHA, system_packages=expected)
        if not expected:
            assert 'RUN set -eu; apt-get update' not in layer
        assert verified[-1][-2] == expected

    if alert_adapter == "none":
        # Enabling a bot later must regenerate the saved root build as well as
        # the live build. The old generated build.sh has different extras.
        new_token = tmp_path / "new-discord-token"
        new_token.write_text("new.discord.token")
        new_token.chmod(0o600)
        args.operator_alert_adapter = "discord"
        args.operator_alert_channel_id = "123456789012345678"
        args.discord_token_file = str(new_token)
        installer.setup(args, request=request, run=run, verify=lambda *a: verified.append(a))
        assert "MIMIR_EXTRAS=" + ",".join(sorted(expected_extras | {"discord"})) in (deploy / "build.sh").read_text()
        assert "DISCORD_TOKEN=new.discord.token" in (deploy / "compose.env").read_text()
        assert json.loads((deploy / "manifest.json").read_text())["operator_alert_adapter"] == "discord"
        args.operator_alert_adapter = "none"
        args.discord_token_file = None
        installer.setup(args, request=request, run=run, verify=lambda *a: verified.append(a))
        assert "DISCORD_TOKEN=" not in (deploy / "compose.env").read_text()
        assert "MIMIR_EXTRAS=" + ",".join(sorted(expected_extras)) in (deploy / "build.sh").read_text()


@pytest.fixture
def upgrade_setup(tmp_path, monkeypatch):
    """A saved deployment with a pinned source and fake Docker/Git boundaries."""
    monkeypatch.setattr(installer, '_license_gate', lambda args: None)
    deploy = tmp_path / 'deploy'
    fields = {'repo': 'org/repo', 'branch': 'main', 'bot_login': 'bot',
              'bot_email': 'bot@example.com', 'owner': 'owner', 'display_name': 'Owner',
              'owner_email': 'owner@example.com', 'deploy': str(deploy),
              'model_spec': 'openai:model', 'saga_model_spec': 'openai:model',
              'embedding': 'openai:model', 'source_ref': 'refs/heads/feature/plugins',
              'source_repo': 'org/mimir', 'test_command': 'npm test', 'review_tests': 'targeted',
              'port': '8123'}
    monkeypatch.setattr(installer, '_field', lambda args, name, *a, **kw: fields[name])
    monkeypatch.setattr(installer, '_secret', lambda *a, **kw: 'secret')
    monkeypatch.setattr(installer, 'validate_token', lambda *a: None)
    monkeypatch.setattr(installer, '_alert_settings', lambda *a: ('none', None, {}))
    monkeypatch.setattr(installer, '_content', lambda *a: '{"engines":{"node":"22.16.0"}}' if a[2] == 'package.json' else None)
    monkeypatch.setattr(installer, '_verify_script', lambda *a: None)
    def docker_exec(argv, **kw):
        kw['stdout'].write(b'owner-key\n')
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(installer.subprocess, 'run', docker_exec)
    state = {'sha': SHA, 'running': False, 'status': 'No Worklink leaf runs or labeled issues.', 'leases': '[]'}
    calls = []

    def request(url, token):
        return 200, {'object': {'sha': state['sha']}}

    def run(argv, *, cwd):
        assert cwd == deploy
        calls.append(argv)
        if argv[-3:] == ['ps', '-q', 'agent']:
            return 'b' * 64 if state['running'] else ''
        if argv[-3:] == ['mimir', 'worklink', 'status']:
            return state['status']
        if argv[-1] == installer._PR_LEASE_PROBE:
            return state['leases']
        if argv[-3:] == ['up', '-d', 'agent']:
            state['running'] = True
        return 'SETUP_CHANGED=0' if 'compose.provision.yml' in argv else ''

    args = argparse.Namespace(rotate_web_key=False, reembed=False, force=False, system_packages=None)
    def invoke():
        installer.setup(args, request=request, run=run, verify=lambda *a: None)

    invoke()
    state['running'] = True
    calls.clear()
    return deploy, args, state, calls, invoke


def test_upgrade_rewrites_owned_files_preserves_state_and_prints_rollback(upgrade_setup, capsys):
    deploy, args, state, calls, invoke = upgrade_setup
    old = SHA
    state['sha'] = 'c' * 40
    env = (deploy / 'compose.env').read_bytes()
    key = (deploy / 'owner-web-key').read_bytes()
    (deploy / 'persisted-volume-marker').write_text('untouched')
    invoke()
    output = capsys.readouterr().out
    assert f'Upgrading {old} -> {state["sha"]}' in output
    assert f'Previous agent image retained: mimir-cultivate-agent:{old}-node22-16-0' in output
    assert 'docker compose -f compose.yml -f <(printf' in output
    assert f'image: mimir-cultivate-agent:{old}-node22-16-0' in output
    assert yaml.safe_load((deploy / 'compose.yml').read_text())['services']['agent']['image'] == (
        f'mimir-cultivate-agent:{state["sha"]}-node22-16-0')
    assert (deploy / 'compose.env').read_bytes() == env
    assert (deploy / 'owner-web-key').read_bytes() == key
    assert (deploy / 'persisted-volume-marker').read_text() == 'untouched'
    assert not any('down' in argv or 'volume' in argv or '-v' in argv for argv in calls)
    assert sum(argv[:2] == ['docker', 'build'] for argv in calls) == 2
    assert any(argv[-2:] == ['--rm', 'init'] for argv in calls)
    assert any('compose.provision.yml' in argv for argv in calls)
    assert any(argv[-3:] == ['up', '-d', 'agent'] for argv in calls)
    import hashlib
    hashes = json.loads((deploy / 'manifest.json').read_text())['generated_sha256']
    assert set(hashes) == {'compose.yml', 'provision.py', 'compose.provision.yml', '.gitignore'}
    assert all(hashes[name] == hashlib.sha256((deploy / name).read_bytes()).hexdigest() for name in hashes)


@pytest.mark.parametrize('name', ['compose.yml', 'provision.py', 'compose.provision.yml', '.gitignore'])
def test_upgrade_refuses_operator_edits_until_forced(upgrade_setup, name):
    deploy, args, state, calls, invoke = upgrade_setup
    path = deploy / name
    path.write_text(path.read_text() + '\n# operator edit\n')
    state['sha'] = 'c' * 40
    with pytest.raises(installer.SetupError, match=re.escape(f'{path} already differs')):
        invoke()
    assert not calls and '# operator edit' in path.read_text()
    args.force = True
    invoke()
    assert '# operator edit' not in path.read_text()


def test_upgrade_legacy_unedited_generated_files(upgrade_setup):
    deploy, args, state, calls, invoke = upgrade_setup
    manifest = json.loads((deploy / 'manifest.json').read_text())
    for key in ('generated_sha256', 'source_sha', 'layer_tag'):
        del manifest[key]
    (deploy / 'manifest.json').write_text(json.dumps(manifest))
    state['sha'] = 'c' * 40
    invoke()
    assert state['sha'] in (deploy / 'compose.yml').read_text()


def test_upgrade_legacy_operator_edit_is_refused(upgrade_setup):
    deploy, args, state, calls, invoke = upgrade_setup
    manifest = json.loads((deploy / 'manifest.json').read_text())
    del manifest['generated_sha256']
    (deploy / 'manifest.json').write_text(json.dumps(manifest))
    compose = deploy / 'compose.yml'
    compose.write_text(compose.read_text() + '\n# operator edit\n')
    state['sha'] = 'c' * 40
    with pytest.raises(installer.SetupError, match='compose.yml already differs'):
        invoke()
    assert not calls and '# operator edit' in compose.read_text()


@pytest.mark.parametrize('activity,detail', [('worklink', 'Worklink #42 (running)'),
                                          ('lease', 'PR checkout lease org/repo#17')])
def test_upgrade_refuses_in_flight_before_stop(upgrade_setup, activity, detail):
    deploy, args, state, calls, invoke = upgrade_setup
    state['sha'] = 'c' * 40
    args.reembed = True  # A rerun that would explicitly stop the old agent.
    if activity == 'worklink':
        state['status'] = ('ISSUE  STATE       ATTEMPT  BACKEND           BRANCH                    CHECKOUT  ELAPSED\n'
                           '42     running     1        opencode          main                      -         1m')
    else:
        state['leases'] = '["org/repo#17"]'
    with pytest.raises(installer.SetupError, match=re.escape(detail)):
        invoke()
    assert not any(argv[-2:] == ['stop', 'agent'] for argv in calls)
    assert not any(argv[:2] == ['docker', 'build'] for argv in calls)
    assert SHA in (deploy / 'compose.yml').read_text()
    args.force = True
    invoke()
    assert any(argv[-2:] == ['stop', 'agent'] for argv in calls)


@pytest.mark.parametrize('probe,output', [
    pytest.param('status', 'ISSUE  STATE       ATTEMPT\n42', id='short-status-row'),
    pytest.param('status', 'ISSUE  STATE       ATTEMPT\nunknown clean 1', id='nonnumeric-status-row'),
    pytest.param('status', 'Error: Worklink status unavailable', id='unexpected-status-output'),
    pytest.param('status', '', id='empty-status-output'),
    pytest.param('leases', 'Error: lease registry unavailable', id='non-json-leases'),
    pytest.param('leases', '', id='empty-lease-output'),
])
def test_upgrade_refuses_unreadable_activity_before_mutation_until_forced(upgrade_setup, probe, output):
    deploy, args, state, calls, invoke = upgrade_setup
    state['sha'] = 'c' * 40
    state[probe] = output
    args.reembed = True  # A successful upgrade would stop the running agent.
    before = {path.name: path.read_bytes() for path in deploy.iterdir() if path.is_file()}
    detail = 'Cannot read Worklink status' if probe == 'status' else 'Cannot read PR checkout leases'

    with pytest.raises(installer.SetupError, match=detail):
        invoke()

    # Only read-only probes may run: no stop, up, restart, build or provision.
    expected_calls = [
        ['docker', 'compose', 'ps', '-q', 'agent'],
        ['docker', 'compose', 'exec', '-T', '-u', 'mimir', 'agent', 'mimir', 'worklink', 'status'],
    ]
    if probe == 'leases':
        expected_calls.append([
            'docker', 'compose', 'exec', '-T', '-u', 'mimir', 'agent', 'python', '-c', installer._PR_LEASE_PROBE,
        ])
    assert calls == expected_calls
    assert {path.name: path.read_bytes() for path in deploy.iterdir() if path.is_file()} == before

    # The explicit override must really complete, not merely avoid the refusal.
    calls.clear()
    args.force = True
    invoke()
    assert any(argv == ['docker', 'compose', 'stop', 'agent'] for argv in calls)
    assert sum(argv[:2] == ['docker', 'build'] for argv in calls) == 2
    assert any(argv == ['docker', 'compose', 'up', '-d', 'agent'] for argv in calls)
    assert not any(argv[-3:] == ['mimir', 'worklink', 'status'] for argv in calls)
    assert not any(argv[-1] == installer._PR_LEASE_PROBE for argv in calls)
    assert state['sha'] in (deploy / 'compose.yml').read_text()
    assert json.loads((deploy / 'manifest.json').read_text())['source_sha'] == state['sha']


def test_same_commit_rerun_with_system_packages(upgrade_setup):
    deploy, args, state, calls, invoke = upgrade_setup
    args.system_packages = 'lsof'
    invoke()
    assert 'lsof' in (deploy / 'Dockerfile.toolchain').read_text()
    assert json.loads((deploy / 'manifest.json').read_text())['system_packages'] == ['lsof']


@pytest.mark.parametrize('step', ['build', 'init', 'provision', 'up'])
def test_docker_failure_names_step_without_secrets(tmp_path, monkeypatch, step):
    monkeypatch.setattr(installer, '_license_gate', lambda args: None)
    fields = {'repo': 'org/repo', 'branch': 'main', 'bot_login': 'bot',
              'bot_email': 'bot@example.com', 'owner': 'owner', 'display_name': 'Owner',
              'owner_email': 'owner@example.com', 'deploy': str(tmp_path / 'deploy'),
              'model_spec': 'openai:model', 'saga_model_spec': 'openai:model',
              'embedding': 'openai:model', 'source_ref': 'refs/heads/feature/plugins',
              'source_repo': 'org/mimir', 'test_command': 'npm test', 'review_tests': 'targeted', 'port': '8123'}
    monkeypatch.setattr(installer, '_field', lambda args, name, *a, **kw: fields[name])
    monkeypatch.setattr(installer, '_secret', lambda *a, **kw: 'secret-from-compose-env')
    monkeypatch.setattr(installer, 'validate_token', lambda *a: None)
    monkeypatch.setattr(installer, '_content', lambda *a: '{"engines":{"node":"22.16.0"}}' if a[2] == 'package.json' else None)
    monkeypatch.setattr(installer, '_verify_script', lambda *a: None)

    def request(url, token):
        return 200, {'object': {'sha': SHA}}

    def run(argv, *, cwd):
        if argv[-3:] == ['ps', '-q', 'agent']:
            return ''
        current = ('build' if argv[1] == 'build' else
                   'provision' if 'compose.provision.yml' in argv else
                   'init' if argv[-2:] == ['--rm', 'init'] else 'up')
        if current == step:
            raise installer.SetupError('docker operation failed (exit 1): secret-from-compose-env')
        return ''

    args = argparse.Namespace(rotate_web_key=False, reembed=False, force=False)
    with pytest.raises(installer.SetupError, match=f'Docker {step} failed') as caught:
        installer.setup(args, request=request, run=run)
    assert 'secret-from-compose-env' not in str(caught.value)


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
    result = yaml.safe_load(installer.render_compose(drifted, "image:sha", "rust", "repo"))
    agent = result["services"]["agent"]
    assert agent["image"] == "image:sha" and "build" not in agent
    assert agent["environment"]["MIMIR_PLUGINS"] == "cultivate"
    assert agent["environment"]["MIMIR_ACP_ENABLED"] == "1"
    assert agent["environment"]["MIMIR_HOME"] == "/home/mimir/agent"
    assert "WORKLINK_CHECKOUT_ROOT" not in agent["environment"]
    assert "workspace:/workspace" in agent["volumes"]
    assert "mkdir -p /workspace/pr-leases /workspace/worklink /workspace/.worklink" in result["services"]["init"]["command"][0]
    assert "chmod 0755 /workspace/.worklink" in result["services"]["init"]["command"][0]
    for bad in ("services: {}", "services: {agent: {build: {}, environment: []}}", "broken: ["):
        with pytest.raises(installer.SetupError, match="Incompatible scaffold"):
            installer.render_compose(bad, "image:sha", "rust", "repo")


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
    base = tmp_path / 'base'
    subprocess.run(['git', 'init', str(base)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(base), 'config', '--local', 'user.name', 'trellisbot-squashmerge'], check=True)
    subprocess.run(['git', '-C', str(base), 'config', '--local', 'user.email', 'trellisbot@example.com'], check=True)
    pending = {"alerts": [], "pollers": ["github-activity", "github-ci-watch", "worklink-ready-queue"], "reply": True, "auth": "ok", "root": "ok", "binary": "ok", "model": "ok", "toolchain": "ok"}
    reads = []
    exec_calls = []
    from mimir_cultivate.worklink import worker_client

    async def verify_executor_identity():
        errors = {
            "checkout_root_mismatch": worker_client.CheckoutRootMismatchError("different roots"),
            "executor_stale": worker_client.StaleWorkerExecutorError("old protocol"),
            "executor_peer_rejected": ConnectionResetError("peer rejected"),
            "executor_permission_denied": PermissionError("peer denied"),
            "executor_unreachable": FileNotFoundError("socket unavailable"),
            "executor_refused": ConnectionRefusedError("socket not listening"),
        }
        if pending["root"] != "ok":
            raise errors[pending["root"]]
        return "a" * 40

    monkeypatch.setattr(worker_client, "verify_executor_identity", verify_executor_identity)

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
        exec_calls.append(argv)
        assert cwd == tmp_path
        if installer._TOOLCHAIN_ENV_PROBE in argv:
            return json.dumps(installer.toolchain_probe_environment(SimpleNamespace(backend_settings={})))
        if installer._TOOLCHAIN_WORKER_PROBE in argv:
            assert argv[:10] == ["docker", "compose", "exec", "-T", "-u", "worklink",
                                 "-w", "/tmp", "agent", "/usr/local/bin/python"]
            assert argv[-2:] == ['rust', '1.99.1']
            return pending['toolchain']
        if installer._OPENCODE_WORKER_PROBE in argv:
            assert argv[:10] == ["docker", "compose", "exec", "-T", "-u", "worklink",
                                "-w", "/tmp", "agent", "/usr/local/bin/python"]
            return pending["binary"]
        if installer._OPENCODE_MODEL_PROBE in argv:
            assert argv[4:9] == ["-u", "worklink", "-w", "/tmp", "agent"]
            assert argv[-3:] == ["/opt/mimir-opencode/bin/opencode", "openai", "openai/gpt-6.1-sol"]
            return pending["model"]
        assert argv[:7] == ["docker", "compose", "exec", "-T", "-u", "mimir", "agent"]
        if "plugins" in argv:
            return json.dumps([{"name": "cultivate", "enabled": True}])
        if "print(resolve_opencode_invocation().model)" in argv[-1]:
            return "openai/gpt-6.1-sol"
        if "resolve_opencode_invocation" in argv[-1]:
            return pending["auth"]
        if "verify_executor_identity" in argv[-1]:
            # Execute the actual docker python -c payload: exceptions must be
            # converted to stdout reason codes rather than a nonzero exit.
            output = io.StringIO()
            with redirect_stdout(output):
                exec(compile(argv[-1], "<executor-verification>", "exec"), {})
            return output.getvalue().strip()
        if argv[-1] == installer._OPENCODE_RESOLVE_PROBE:
            assert argv[7:9] == ["/home/mimir/venv/bin/python", "-c"]
            return "/opt/mimir-opencode/bin/opencode"
        if "base_identity_mismatch" in argv[-1]:
            return subprocess.run([sys.executable, '-c', argv[-1]], check=True, capture_output=True, text=True,
                env=dict(os.environ, WORKLINK_REPO=str(base))).stdout.strip()
        return json.dumps(pending["alerts"])

    monkeypatch.setattr(installer, "_http", http)
    monkeypatch.setattr(installer, "_run", run)
    def verify():
        installer._verify(tmp_path, 8123, "org/repo", "owner-key", 'rust', '1.99.1')

    verify()
    assert len(exec_calls) == 11
    assert [argv[7:9] for argv in exec_calls] == [
        ["mimir", "plugins"], ["python", "-c"], ["python", "-c"],
        ["python", "-c"], ["/home/mimir/venv/bin/python", "-c"],
        ["/tmp", "agent"], ["/home/mimir/venv/bin/python", "-c"],
        ["/tmp", "agent"], ["python", "-c"], ["python", "-c"], ["/tmp", "agent"]]
    assert any(url.endswith("/api/v1/chat") and key == "owner-key" for url, key, _ in reads)
    subprocess.run(['git', '-C', str(base), 'config', '--local', 'user.email', 'wrong@example.com'], check=True)
    reads.clear()
    with pytest.raises(installer.SetupError, match="Worklink base identity mismatch.*base_identity_mismatch"):
        verify()
    # A missing key (git rc 1) is a mismatch, not an unreadable repository.
    subprocess.run(['git', '-C', str(base), 'config', '--local', '--unset', 'user.email'], check=True)
    with pytest.raises(installer.SetupError, match="Worklink base identity mismatch.*base_identity_mismatch"):
        verify()
    subprocess.run(['git', '-C', str(base), 'config', '--local', 'user.email', 'trellisbot@example.com'], check=True)
    # Real git returns rc 128 when the configured base cannot be opened.
    moved_base = tmp_path / 'moved-base'
    base.rename(moved_base)
    with pytest.raises(installer.SetupError, match="Worklink base identity unreadable.*base_identity_unreadable") as raised:
        verify()
    assert 'fatal:' not in str(raised.value)
    moved_base.rename(base)
    for reason, message in (
        ("checkout_root_mismatch", "checkout root mismatch; align WORKLINK_CHECKOUT_ROOT"),
        ("executor_stale", "executor is stale; rebuild the image"),
        ("executor_peer_rejected", "executor rejected the agent peer"),
        ("executor_permission_denied", "executor rejected the agent peer"),
        ("executor_unreachable", "executor is unreachable; inspect its service and socket"),
        ("executor_refused", "executor is unreachable; inspect its service and socket"),
    ):
        pending["root"] = reason
        with pytest.raises(installer.SetupError, match=message):
            verify()
    pending["root"] = "ok"
    pending['toolchain'] = 'toolchain_missing: rustc'
    with pytest.raises(installer.SetupError, match='toolchain_missing: rustc'):
        verify()
    pending['toolchain'] = 'ok'
    pending["binary"] = "opencode_missing"
    with pytest.raises(installer.SetupError, match="opencode_missing"):
        verify()
    pending["binary"] = "ok"
    pending["auth"] = "auth_missing"
    with pytest.raises(installer.SetupError, match="OpenCode credential resolution failed.*auth_missing"):
        verify()
    pending["auth"] = "private-key-material"
    with pytest.raises(installer.SetupError, match="unavailable") as raised:
        verify()
    assert "private-key-material" not in str(raised.value)
    pending["auth"] = "ok"
    pending["model"] = "opencode_model_unknown"
    with pytest.raises(installer.SetupError, match="opencode_model_unknown.*openai/gpt-6.1-sol"):
        verify()
    pending["model"] = "ok"
    pending["alerts"] = [{"repository": "org/repo"}]
    with pytest.raises(installer.SetupError, match="binding"):
        verify()
    pending["alerts"] = []
    pending["pollers"] = ["github-activity"]
    with pytest.raises(installer.SetupError, match="github-ci-watch"):
        verify()
    pending["pollers"] = ["github-activity", "github-ci-watch"]
    with pytest.raises(installer.SetupError, match="worklink-ready-queue"):
        verify()
    pending["pollers"] = ["github-activity", "github-ci-watch", "worklink-ready-queue"]
    pending["reply"] = False
    with pytest.raises(installer.SetupError, match="no assistant reply"):
        verify()


@pytest.mark.parametrize("runtime_injected", [False, True])
@pytest.mark.parametrize("outcome", ["ok", "nonzero", "missing", "timeout"])
def test_setup_binary_probe_executes_with_worker_path(
    tmp_path, monkeypatch, outcome, runtime_injected
):
    import subprocess
    import sys

    from mimir_cultivate.contained_execution import (
        base_worker_environment,
        opencode_worker_environment,
    )

    binary = tmp_path / "opencode"
    marker = tmp_path / "probe-environment.json"
    launch_marker = tmp_path / "probe-launch-environment.json"
    binary.write_text(
        f"#!{sys.executable}\nimport json, os, pathlib, sys\n"
        # Model runtime-added state on every platform, without adding it to the
        # environment supplied by the probe itself.
        + ("os.environ['TEST_RUNTIME_ADDED'] = 'runtime-only'\n" if runtime_injected else "")
        + "assert sys.argv[1:] == ['--version']\n"
        "home = pathlib.Path(os.environ['HOME'])\n"
        "assert home.is_dir() and home.stat().st_mode & 0o777 == 0o700\n"
        "assert os.environ['PATH'] == '/usr/local/bin:/usr/bin:/bin'\n"
        "for key, suffix in [('XDG_CONFIG_HOME', '.config'), ('XDG_DATA_HOME', '.local/share'), ('XDG_CACHE_HOME', '.cache')]:\n"
        " assert os.environ[key] == str(home / suffix)\n"
        " (home / suffix).mkdir(parents=True, exist_ok=True)\n"
        f"pathlib.Path({str(marker)!r}).write_text(json.dumps(dict(os.environ)))\n"
        f"sys.exit({1 if outcome == 'nonzero' else 0})\n"
    )
    binary.chmod(0o755)
    if outcome == "missing":
        binary.unlink()
    real_run = subprocess.run
    captured = []

    def run(argv, *, cwd):
        captured.append(argv)
        if argv[-1] == installer._OPENCODE_RESOLVE_PROBE:
            assert argv[:9] == ['docker', 'compose', 'exec', '-T', '-u', 'mimir',
                                'agent', '/home/mimir/venv/bin/python', '-c']
            return str(binary)
        assert argv[4:8] == ['-u', 'worklink', '-w', '/tmp']
        assert argv[9] == '/usr/local/bin/python'
        assert argv[-2:] == [installer._OPENCODE_WORKER_PROBE, str(binary)]
        # Record the exact environment passed at the real subprocess boundary,
        # before the child runtime can inject platform-specific variables.
        # The clean interpreter still executes the unmodified production probe.
        recorder = (
            "import json, pathlib, subprocess\n"
            "real_run = subprocess.run\n"
            "def record_launch(*args, **kwargs):\n"
            f" pathlib.Path({str(launch_marker)!r}).write_text(json.dumps(kwargs['env']))\n"
            + (" raise subprocess.TimeoutExpired(args[0], 15)\n" if outcome == 'timeout'
               else " return real_run(*args, **kwargs)\n")
            + "subprocess.run = record_launch\n"
        )
        code = recorder + argv[-2]
        result = real_run([sys.executable, '-I', '-S', '-c', code, str(binary)],
                          capture_output=True, text=True, check=True)
        return result.stdout.strip()

    monkeypatch.setattr(installer, '_run', run)
    if outcome == 'ok':
        installer._verify_opencode_binary(tmp_path)
    else:
        with pytest.raises(installer.SetupError, match='opencode_missing'):
            installer._verify_opencode_binary(tmp_path)
    assert len(captured) == 2
    assert launch_marker.exists(), 'every outcome must reach the subprocess boundary'
    launch_env = json.loads(launch_marker.read_text())
    # Exact equality rejects every extra launch variable, including names the
    # platform might subsequently inject into the child interpreter.
    base = base_worker_environment('setup-probe-parity')
    runtime_home = str(Path(base['XDG_CONFIG_HOME']).parent)
    expected = opencode_worker_environment(base, {})
    expected = {key: value.replace(runtime_home, launch_env['HOME'])
                for key, value in expected.items()}
    expected['HOME'] = launch_env['HOME']  # assigned by the real executor too
    assert launch_env == expected
    assert not Path(launch_env['HOME']).exists(), 'probe must clean up after every outcome'
    if outcome in {'ok', 'nonzero'}:
        assert marker.exists(), 'positive control must execute the CLI body'
        observed_env = json.loads(marker.read_text())
        for key, value in expected.items():
            assert observed_env[key] == value
        if runtime_injected:
            assert observed_env['TEST_RUNTIME_ADDED'] == 'runtime-only'
            assert 'TEST_RUNTIME_ADDED' not in launch_env
    else:
        assert not marker.exists(), 'failed launches must not execute the CLI body'


@pytest.mark.parametrize('runtime_injected', [False, True])
@pytest.mark.parametrize('outcome', ['listed', 'unknown', 'nonzero', 'missing', 'timeout'])
@pytest.mark.parametrize('provider, model', [
    ('openai', 'openai/gpt-6.1-sol'), ('anthropic', 'anthropic/claude-haiku-5-5'),
])
def test_setup_model_probe_uses_isolated_worker_cli(
    tmp_path, monkeypatch, provider, model, outcome, runtime_injected
):
    from mimir_cultivate.contained_execution import (
        base_worker_environment,
        opencode_worker_environment,
    )

    binary = tmp_path / 'opencode'
    marker = tmp_path / 'model-probe.json'
    launch_marker = tmp_path / 'model-launch-environment.json'
    binary.write_text(
        f'#!{sys.executable}\nimport json, os, pathlib, sys\n'
        + ("os.environ['TEST_RUNTIME_ADDED'] = 'runtime-only'\n" if runtime_injected else '')
        + f"assert sys.argv[1:] == ['models', {provider!r}]\n"
        "home = pathlib.Path(os.environ['HOME'])\n"
        "assert home.is_dir() and home.stat().st_mode & 0o777 == 0o700\n"
        f'pathlib.Path({str(marker)!r}).write_text(json.dumps(dict(os.environ)))\n'
        # A prefix-only match must not satisfy the exact model check.
        f"print({model + '-fast'!r})\n"
        + (f"print({model!r})\n" if outcome != 'unknown' else '')
        + f"sys.exit({1 if outcome == 'nonzero' else 0})\n"
    )
    binary.chmod(0o755)
    if outcome == 'missing':
        binary.unlink()
    real_run = subprocess.run
    captured = []
    monkeypatch.setenv('OPENAI_API_KEY', 'private-openai-key')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'private-anthropic-key')

    def run(argv, *, cwd):
        captured.append(argv)
        assert cwd == tmp_path
        assert argv[:10] == ['docker', 'compose', 'exec', '-T', '-u', 'worklink',
                             '-w', '/tmp', 'agent', '/usr/local/bin/python']
        assert argv[-4:] == [installer._OPENCODE_MODEL_PROBE, str(binary), provider, model]
        # Capture the exact launch mapping before the child runtime can add
        # platform variables. Execute the unmodified stdlib-only setup payload.
        recorder = (
            'import json, pathlib, subprocess\n'
            'real_run = subprocess.run\n'
            'def record_launch(*args, **kwargs):\n'
            f' pathlib.Path({str(launch_marker)!r}).write_text(json.dumps(kwargs["env"]))\n'
            + (' raise subprocess.TimeoutExpired(args[0], 30)\n' if outcome == 'timeout'
               else ' return real_run(*args, **kwargs)\n')
            + 'subprocess.run = record_launch\n'
        )
        result = real_run([sys.executable, '-I', '-S', '-c', recorder + argv[-4], *argv[-3:]],
                          capture_output=True, text=True, check=True)
        return result.stdout.strip()

    monkeypatch.setattr(installer, '_run', run)
    if outcome == 'listed':
        installer._verify_opencode_model(tmp_path, str(binary), model)
    else:
        with pytest.raises(installer.SetupError, match=f'opencode_model_unknown.*{re.escape(model)}'):
            installer._verify_opencode_model(tmp_path, str(binary), model)
    assert len(captured) == 1
    assert launch_marker.exists(), 'every outcome must reach the subprocess boundary'
    launch_env = json.loads(launch_marker.read_text())
    base = base_worker_environment('setup-model-probe-parity')
    runtime_home = str(Path(base['XDG_CONFIG_HOME']).parent)
    expected = opencode_worker_environment(base, {})
    expected = {key: value.replace(runtime_home, launch_env['HOME'])
                for key, value in expected.items()}
    expected['HOME'] = launch_env['HOME']
    # The inert catalog key is the ONLY permitted difference from the worker.
    expected[provider.upper() + '_API_KEY'] = 'model-catalog-probe'
    assert launch_env == expected
    assert not Path(launch_env['HOME']).exists(), 'probe must clean up after every outcome'
    if outcome in {'listed', 'unknown', 'nonzero'}:
        assert marker.exists(), 'positive control must execute the CLI body'
        observed_env = json.loads(marker.read_text())
        for key, value in expected.items():
            assert observed_env[key] == value
        if runtime_injected:
            assert observed_env['TEST_RUNTIME_ADDED'] == 'runtime-only'
            assert 'TEST_RUNTIME_ADDED' not in launch_env
    else:
        assert not marker.exists(), 'failed launches must not execute the CLI body'


def test_real_image_probe_adapter_accepts_actual_setup_argv(monkeypatch):
    import ast
    import subprocess

    # Execute the shipped image-proof payload, not a copy of its adapter. Only
    # the final setuid transport is faked; setup generates both real argv shapes.
    script = Path(__file__).resolve().parents[3] / 'scripts/worklink_image_identity.py'
    payloads = [node.value for node in ast.walk(ast.parse(script.read_text()))
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
                and 'real-image setup OpenCode probe passed' in node.value]
    assert len(payloads) == 1
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        output = '/opt/mimir-opencode/bin/opencode' if argv[1] == 'mimir' else 'ok'
        return SimpleNamespace(returncode=0, stdout=output, stderr='')

    monkeypatch.setattr(subprocess, 'run', run)
    monkeypatch.setattr(installer, '_run', installer._run)
    exec(compile(payloads[0], '<real-image-setup-probe>', 'exec'), {})
    assert [argv for argv, _ in calls] == [
        ['/command/s6-setuidgid', 'mimir', '/home/mimir/venv/bin/python', '-c',
         installer._OPENCODE_RESOLVE_PROBE],
        ['/command/s6-setuidgid', 'worklink', '/usr/local/bin/python', '-c',
         installer._OPENCODE_WORKER_PROBE, '/opt/mimir-opencode/bin/opencode'],
        ['/command/s6-setuidgid', 'worklink', '/usr/local/bin/python', '-c',
         installer._OPENCODE_MODEL_PROBE, '/opt/mimir-opencode/bin/opencode', 'openai', 'openai/gpt-6.1-sol'],
        ['/command/s6-setuidgid', 'worklink', '/usr/local/bin/python', '-c',
         installer._OPENCODE_MODEL_PROBE, '/opt/mimir-opencode/bin/opencode', 'openai', 'openai/gpt-6-luna'],
        ['/command/s6-setuidgid', 'worklink', '/usr/local/bin/python', '-c',
         installer._OPENCODE_MODEL_PROBE, '/opt/mimir-opencode/bin/opencode', 'anthropic', 'anthropic/claude-haiku-5-5'],
    ]
    assert all(kwargs == dict(cwd='/tmp', capture_output=True, text=True, timeout=30)
               for _, kwargs in calls)


def test_setup_binary_resolution_failure_does_not_launch_worker(tmp_path, monkeypatch):
    calls = []
    def run(argv, *, cwd):
        calls.append(argv)
        return 'opencode_missing'
    monkeypatch.setattr(installer, '_run', run)
    with pytest.raises(installer.SetupError, match='opencode_missing'):
        installer._verify_opencode_binary(tmp_path)
    assert len(calls) == 1
    assert calls[0][-1] == installer._OPENCODE_RESOLVE_PROBE


@pytest.mark.parametrize('kind, version, tools', [
    ('rust', '1.99.1', ('rustc', 'cargo', 'cargo-fmt', 'cargo-clippy', 'cargo-nextest')),
    ('node', '22.16.0', ('node', 'npm', 'npx')),
    ('python', '3.11.11', ('uv', 'python')),
])
def test_toolchain_probe_runs_versions_on_worker_path(tmp_path, monkeypatch, kind, version, tools):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    marker = tmp_path / 'launches.jsonl'
    for tool in tools:
        binary = bin_dir / tool
        binary.write_text(
            f'#!{sys.executable}\nimport os, json, pathlib, sys\n'
            f'with pathlib.Path({str(marker)!r}).open("a") as out:\n'
            ' out.write(json.dumps([pathlib.Path(sys.argv[0]).name, sys.argv[1:], dict(os.environ)]) + "\\n")\n'
            + (f'print({str("/opt/uv-python/mock/bin/python")!r})\n' if tool == 'uv' else
               f'print({("rustc " if tool == "rustc" else "v" if tool == "node" else "Python " if tool == "python" else tool + " ") + version!r})\n')
        )
        binary.chmod(0o755)
    # A fake native compiler only links when invoked with source and -o;
    # --version alone cannot satisfy this test.
    compiler = {'rust': 'rustc', 'node': 'c++', 'python': 'cc'}[kind]
    (bin_dir / compiler).write_text(
        f'#!{sys.executable}\nimport os, json, pathlib, sys\n'
        f'with pathlib.Path({str(marker)!r}).open("a") as out:\n'
        ' out.write(json.dumps([pathlib.Path(sys.argv[0]).name, sys.argv[1:], dict(os.environ)]) + "\\n")\n'
        'if "--version" in sys.argv:\n'
        f' print({("rustc " if kind == "rust" else compiler + " ") + version!r})\n'
        'else:\n'
        ' output = pathlib.Path(sys.argv[sys.argv.index("-o") + 1])\n'
        ' output.write_text("#!/bin/sh\\nexit 0\\n")\n'
        ' output.chmod(0o755)\n'
    )
    (bin_dir / compiler).chmod(0o755)
    from mimir_cultivate.worklink.backends.registry import WorklinkConfig
    from mimir_cultivate.worklink.compute import _enabled_child_env

    cache = tmp_path / 'cargo-cache'
    cache.mkdir()
    test_env = installer.toolchain_test_env(kind)
    if kind == 'rust':
        assert test_env == {'RUSTUP_HOME': '/opt/rustup', 'CARGO_HOME': '/var/cache/mimir-cargo'}
        test_env['CARGO_HOME'] = str(cache)
    config_path = tmp_path / 'worklink.yaml'
    config_path.write_text(yaml.safe_dump({'defaults': {'test_command': './tools/verify'},
        'backends': {'opencode': {'test_env': test_env, 'bash_allowlist': ['./tools/verify']}}}))
    config = WorklinkConfig.load(config_path)
    real_run = subprocess.run
    missing = {'rust': 'rustc', 'node': 'node', 'python': 'uv'}[kind]
    launch_marker = tmp_path / 'launch-env.json'

    def run(argv, *, cwd):
        assert cwd == tmp_path
        if installer._TOOLCHAIN_ENV_PROBE in argv:
            return json.dumps(installer.toolchain_probe_environment(config))
        assert argv[:10] == ['docker', 'compose', 'exec', '-T', '-u', 'worklink',
                             '-w', '/tmp', 'agent', '/usr/local/bin/python']
        assert argv[-2:] == [kind, version]
        # Remap only the executable at the child subprocess boundary. Its env,
        # argv and isolated HOME remain the production worker probe's values.
        recorder = (
            'import json, pathlib, subprocess\n'
            'real_run = subprocess.run\n'
            'def launch(argv, **kwargs):\n'
            f' pathlib.Path({str(launch_marker)!r}).write_text(json.dumps(kwargs["env"]))\n'
            f' name = pathlib.Path(argv[0]).name\n'
            ' if name == "cargo" and len(argv) > 1 and argv[1] in ("fmt", "clippy"):\n'
            '  name = "cargo-" + argv[1]\n'
            '  argv = [name, *argv[2:]]\n'
            f' binary = pathlib.Path({str(bin_dir)!r}) / name\n'
            ' if not binary.exists() and pathlib.Path(argv[0]).is_absolute():\n'
            f'  pathlib.Path({str(tmp_path / "executed")!r}).write_text(name)\n'
            '  binary = pathlib.Path(argv[0])\n'
            ' return real_run([str(binary), *argv[1:]], **kwargs)\n'
            'subprocess.run = launch\n'
        )
        output = real_run([sys.executable, '-I', '-S', '-c', recorder + argv[11], *argv[12:]],
                          capture_output=True, text=True, check=True)
        return output.stdout.strip()

    monkeypatch.setattr(installer, '_run', run)
    installer._verify_toolchain(tmp_path, kind, version)
    observed = json.loads(launch_marker.read_text())
    from mimir_cultivate.worklink.backends import opencode as backend_module
    from mimir_cultivate.worklink.backends.base import WorkOrder
    from mimir_cultivate.worklink.backends.registry import BackendRegistry
    invocation = SimpleNamespace(model='proof/model', provider='proof', model_source='test',
                                 config_path=tmp_path / 'absent', pass_env=(), remove_env=())
    monkeypatch.setattr(backend_module, '_coding_enabled', lambda: True)
    monkeypatch.setattr(backend_module, 'resolve_opencode_binary', lambda _: '/usr/local/bin/opencode')
    monkeypatch.setattr(backend_module, 'resolve_worklink_opencode_invocation', lambda _: SimpleNamespace(
        invocation=invocation, env={}, configured_model='proof/model', model_diverged=False))
    monkeypatch.setattr(backend_module, 'opencode_worker_documents', lambda *_: SimpleNamespace(
        config_document=b'{}', auth_document=None))
    spec = BackendRegistry(config).get('opencode').work_spec(
        WorkOrder(1, tmp_path, '', None, 30, {}), attempt=1,
        repo_url='', base_ref='', branch='', test_command='./tools/verify')
    expected = _enabled_child_env(spec, 'parity-control')
    runtime_home = expected['HOME']
    expected = {key: value.replace(runtime_home, observed['HOME']) for key, value in expected.items()}
    assert observed == expected
    if kind == 'rust':
        assert observed['RUSTUP_HOME'] == '/opt/rustup'
        assert not list(cache.iterdir()), 'probe must remove its cache write test'
    launches = [json.loads(row) for row in marker.read_text().splitlines()]
    assert [row[0] for row in launches] == (['rustc', 'cargo', 'cargo-fmt', 'cargo-clippy', 'cargo', 'rustc'] if kind == 'rust' else
                                             ['node', 'npm', 'npx', 'c++'] if kind == 'node' else
                                             ['uv', 'uv', 'python', 'cc'])
    assert (tmp_path / 'executed').read_text() == 'probe'
    assert launches[-1][1][-2].endswith('/probe') and launches[-1][1][-1].endswith(('.rs', '.cc', '.c'))
    assert all(row[2]['PATH'] == '/usr/local/bin:/usr/bin:/bin' for row in launches)
    if kind == 'python':
        assert launches[1][1] == ['python', 'find', '--managed-python', '--no-python-downloads', '--no-project', version]
    homes = {row[2]['HOME'] for row in launches}
    assert len(homes) == 1 and not Path(homes.pop()).exists()
    if kind == 'rust':
        assert launches[2][1] == ['--version'] and launches[3][1] == ['--version']
        assert launches[4][1] == ['nextest', '--version']
        # Cargo subcommands must be available on the fixed worker PATH,
        # independently of the installer's /opt/cargo/bin.
        for name in ('cargo-fmt', 'cargo-clippy'):
            binary = bin_dir / name
            contents = binary.read_text()
            binary.unlink()
            with pytest.raises(installer.SetupError, match='toolchain_missing: ' + name):
                installer._verify_toolchain(tmp_path, kind, version)
            binary.write_text(contents)
            binary.chmod(0o755)
        # A cache that cannot accept a file must fail before version probes.
        cache.rmdir()
        cache.write_text('not a directory')
        with pytest.raises(installer.SetupError, match='toolchain_missing: cargo'):
            installer._verify_toolchain(tmp_path, kind, version)
        cache.unlink()
        cache.mkdir()
        # Rustup proxies must fail with the *configured* gate env, not be
        # rescued by tool-specific settings injected inside the probe.
        rustc = bin_dir / 'rustc'
        rustc.write_text(f'#!{sys.executable}\nimport os, sys\n'
                         'sys.exit(7) if os.environ.get("RUSTUP_HOME") != "/opt/rustup" else None\n'
                         f'print("rustc {version}") if "--version" in sys.argv else None\n'
                         'if "-o" in sys.argv:\n'
                         ' from pathlib import Path\n'
                         ' binary = Path(sys.argv[sys.argv.index("-o") + 1])\n'
                         ' binary.write_text("#!/bin/sh\\nexit 0\\n")\n'
                         ' binary.chmod(0o755)\n')
        installer._verify_toolchain(tmp_path, kind, version)
        del config.backend_settings['opencode']['test_env']['RUSTUP_HOME']
        with pytest.raises(installer.SetupError, match='toolchain_missing: rustc'):
            installer._verify_toolchain(tmp_path, kind, version)
        config.backend_settings['opencode']['test_env']['RUSTUP_HOME'] = '/opt/rustup'
        rustc.write_text(f'#!{sys.executable}\nimport sys\n'
                         f'print("rustc {version}") if "--version" in sys.argv else None\n')
        with pytest.raises(installer.SetupError, match='toolchain_missing: cc'):
            installer._verify_toolchain(tmp_path, kind, version)
    (bin_dir / missing).unlink()
    with pytest.raises(installer.SetupError, match='toolchain_missing: ' + missing):
        installer._verify_toolchain(tmp_path, kind, version)
    if kind in {'rust', 'node'}:
        binary = bin_dir / missing
        binary.write_text(f'#!{sys.executable}\nprint({("rustc " if kind == "rust" else "v") + version + ".bad"!r})\n')
        binary.chmod(0o755)
        with pytest.raises(installer.SetupError, match='toolchain_missing: ' + missing):
            installer._verify_toolchain(tmp_path, kind, version)
    if kind == 'python':
        uv = bin_dir / 'uv'
        uv.write_text(f'#!{sys.executable}\nprint("/usr/bin/python")\n')
        uv.chmod(0o755)
        with pytest.raises(installer.SetupError, match='toolchain_missing: python'):
            installer._verify_toolchain(tmp_path, kind, version)


def test_real_image_toolchain_proof_uses_clean_env_and_negative_control(tmp_path, monkeypatch):
    import runpy

    script = Path(__file__).resolve().parents[3] / 'scripts/worklink_image_identity.py'
    proof = runpy.run_path(str(script))['TOOLCHAIN_PROOF']
    calls = []
    linked = {'cargo-fmt', 'cargo-clippy', 'rustdoc'}

    def launch(argv, **kwargs):
        assert argv[:4] == ['/command/s6-setuidgid', 'worklink', 'env', '-i']
        if argv[4] == '/usr/local/bin/python':
            assert argv[4:7] == ['/usr/local/bin/python', '-c', installer._TOOLCHAIN_WORKER_PROBE]
            env = json.loads(argv[7])
            command = 'probe'
        else:
            assignments = dict(item.split('=', 1) for item in argv[4:] if '=' in item)
            env = assignments
            command = ' '.join(arg for arg in argv[4:] if '=' not in arg)
            assert env['PATH'] == '/usr/local/bin:/usr/bin:/bin'
        assert env['CARGO_HOME'] == '/var/cache/mimir-cargo'
        assert {k: v for k, v in kwargs.items() if k != 'cwd'} == dict(
            capture_output=True, text=True, timeout=120)
        if command == 'cargo doc --offline --no-deps':
            assert (Path(kwargs['cwd']) / 'src/lib.rs').read_text() == 'pub fn proof() {}\n'
        else:
            assert kwargs['cwd'] == '/tmp'
        calls.append((command, env))
        if command == 'probe':
            return SimpleNamespace(returncode=0, stdout='ok' if 'RUSTUP_HOME' in env else 'toolchain_missing: rustc')
        required = {'cargo fmt --version': 'cargo-fmt',
                    'cargo clippy --version': 'cargo-clippy', 'rustdoc --version': 'rustdoc'}
        return SimpleNamespace(returncode=int(required.get(command) not in linked) if command in required else 0,
                               stdout='version')

    monkeypatch.setattr(subprocess, 'run', launch)
    exec(compile(proof, '<image-toolchain-proof>', 'exec'), {})
    assert [command for command, _ in calls] == [
        'probe', 'cargo fmt --version', 'cargo clippy --version', 'rustdoc --version',
        'cargo doc --offline --no-deps', 'probe']
    assert calls[0][1]['RUSTUP_HOME'] == '/opt/rustup'
    assert calls[-1][1] == {k: v for k, v in calls[0][1].items() if k != 'RUSTUP_HOME'}
    linked.remove('cargo-fmt')
    with pytest.raises(AssertionError):
        exec(compile(proof, '<missing-cargo-fmt-image-proof>', 'exec'), {})
    linked.add('cargo-fmt')
    # Mutation: a config producer that forgets Rust's required home must make
    # the live proof's positive control fail, not just its negative pass.
    original = installer.toolchain_test_env
    monkeypatch.setattr(installer, 'toolchain_test_env', lambda kind:
                        {k: v for k, v in original(kind).items() if k != 'RUSTUP_HOME'})
    with pytest.raises(AssertionError):
        exec(compile(proof, '<mutated-image-toolchain-proof>', 'exec'), {})
    assert "'-c', TOOLCHAIN_PROOF" in script.read_text()
    assert 'print(render_layer("rust", "1.90.0"' in script.read_text()


@pytest.mark.parametrize('env', [
    {'RUSTUP_HOME': '/opt/rustup', 'CARGO_HOME': '/var/cache/mimir-cargo'},
    {'UV_PYTHON_INSTALL_DIR': '/opt/uv-python'},
])
def test_toolchain_test_env_validates_paths_and_keeps_reserved_names_closed(env):
    from mimir_cultivate.worklink.backends.opencode import validate_test_env
    validate_test_env(env)
    for key in env:
        for value in ('relative', '/opt/../home/mimir', '/opt/tool\nsecret', ''):
            with pytest.raises(ValueError):
                validate_test_env({key: value})
    for key in ('PATH', 'HOME', 'LD_PRELOAD', 'GITHUB_TOKEN'):
        with pytest.raises(ValueError, match='not allowed'):
            validate_test_env({key: '/tmp/value'})


def test_existing_env_never_overwrites_operator_secrets(tmp_path):
    installer._env(tmp_path, {"MIMIR_API_KEY": "generated", "GITHUB_TOKEN": "original"})
    before = (tmp_path / "compose.env").read_bytes()
    with pytest.raises(installer.SetupError, match="GITHUB_TOKEN"):
        installer._env(tmp_path, {"MIMIR_API_KEY": "new", "GITHUB_TOKEN": "different"})
    assert (tmp_path / "compose.env").read_bytes() == before


@pytest.mark.parametrize('review_tests', ['targeted', 'full', 'none'])
def test_provision_clones_before_declaring_and_preserves_identity(tmp_path, monkeypatch, capsys, caplog, review_tests):
    monkeypatch.setenv("HOME", "/home/mimir")
    monkeypatch.setenv("XDG_CACHE_HOME", "/home/mimir/.cache")
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    state = home / "state"
    state.mkdir(parents=True)
    existing = {"people": [{"canonical": "another", "aliases": ["email:another@example.com"]}]}
    (state / "identities.yaml").write_text(yaml.safe_dump(existing))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"slug": "org/repo", "branch": "main", "toolchain": "rust", "owner": "owner",
                                     "display": "Owner", "email": "owner@example.com",
                                     "test": "npm run verify", "review_tests": review_tests, "rotate": False,
                                     "saga_model_spec": "openai:gpt-5.4-nano", "embedding": "local", "reembed": False}))
    script = (installer._PROVISION
              .replace("/opt/cultivate-manifest.json", str(manifest))
              .replace("/home/mimir/agent", str(home))
              .replace("Path('/workspace')", "Path(" + repr(str(tmp_path)) + ")")
              .replace("Path('/workspace/worklink/base')", "Path(" + repr(str(tmp_path / 'worklink/base')) + ")"))
    import mimir.plugins.skills as skills

    monkeypatch.setattr(skills, "resolve_skill", lambda name, core_names: SimpleNamespace(dirname=name.split(":")[1]))

    def install_skill(home, skill):
        dest = home / "skills" / skill.dirname
        dest.mkdir(parents=True)
        return dest, None

    monkeypatch.setattr(skills, "install_skill", install_skill)
    calls = []
    base_identity = {}

    def git(argv, **kwargs):
        calls.append(argv)
        if argv[0] == 'chainlink':
            if argv[1:] == ['init', '-q']:
                (home / '.chainlink').mkdir()
            else:
                assert argv[1:] == ['agent', 'init', 'cultivate-worklink']
                (home / '.chainlink/agent.json').write_text('{"id":"cultivate-worklink"}')
            return subprocess.CompletedProcess(argv, 0)
        if "clone" in argv:
            (Path(argv[-1]) / ".git").mkdir(parents=True)
        if argv[:3] == ['git', '-C', str(tmp_path / 'worklink/base/repo')] and 'config' in argv:
            if argv[-2:] in (['--get', 'user.name'], ['--get', 'user.email']):
                key = argv[-1]
                return subprocess.CompletedProcess(argv, 0 if key in base_identity else 1,
                                                   stdout=base_identity.get(key, '') + '\n')
            if argv[-2] in ('user.name', 'user.email'):
                base_identity[argv[-2]] = argv[-1]
        if 'symbolic-ref' in argv:
            return subprocess.CompletedProcess(argv, 0, stdout='main\n')
        if 'status' in argv:
            return subprocess.CompletedProcess(argv, 0, stdout='')
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
    assert inventory["repositories"][0]["worklink_base"] == str(tmp_path / 'worklink/base/repo')
    assert inventory["repositories"][0]["origin"] == "https://github.com/org/repo.git"
    assert inventory["repositories"][0]["review_tests"] == review_tests
    assert len(inventory["repositories"]) == 1
    assert base_identity == {'user.name': 'trellisbot-squashmerge', 'user.email': 'trellisbot@example.com'}
    assert {r["root"] for r in inventory["allowed_roots"]} == {"/workspace/pr-leases", "/workspace/worklink"}
    people = yaml.safe_load((state / "identities.yaml").read_text())["people"]
    assert {p["canonical"] for p in people} == {"another", "owner"}
    assert {"user", "admin"}.issubset(people[1]["access"]["roles"])
    assert installer._SHA.fullmatch("a" * 40)
    assert sum("clone" in argv for argv in calls) == 2
    assert [argv for argv in calls if argv[0] == 'chainlink'] == [
        ['chainlink', 'init', '-q'], ['chainlink', 'agent', 'init', 'cultivate-worklink']]
    from mimir_cultivate.worklink.backends.registry import WorklinkConfig
    worklink = WorklinkConfig.load(home / 'worklink.yaml')
    assert worklink.repository == inventory['repositories'][0]['slug']
    assert worklink.defaults.base_branch == inventory['repositories'][0]['base_branch']
    assert worklink.defaults.test_command == inventory['repositories'][0]['test_command']
    assert worklink.defaults.allow_autonomous_local_subprocess
    assert worklink.backend_settings['opencode']['test_env'] == {
        'RUSTUP_HOME': '/opt/rustup', 'CARGO_HOME': '/var/cache/mimir-cargo'}
    assert (home / '.chainlink/agent.json').read_text() == '{"id":"cultivate-worklink"}'
    assert any(argv[-1] == str(tmp_path / 'worklink/base/repo') for argv in calls if 'clone' in argv)
    assert not any("setup-git" in argv for argv in calls)
    clone = next(argv for argv in calls if "clone" in argv and argv[-1] == str(repo))
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
    prior_worklink = (home / 'worklink.yaml').read_bytes()
    prior_key = (home / "owner-web-key").read_bytes()
    legacy = yaml.safe_load(prior_inventory)
    del legacy['repositories'][0]['worklink_base']
    (home / 'repositories.yaml').write_text(yaml.safe_dump(legacy, sort_keys=False))
    exec(compile(script, 'provision.py', 'exec'), {'__name__': '__main__'})
    assert (home / 'repositories.yaml').read_bytes() == prior_inventory
    assert (home / 'worklink.yaml').read_bytes() == prior_worklink
    # Upgrade deployed entries with only test_command (no configured suites),
    # including older entries that also predate worklink_base. Exercise the real
    # provisioning writer and its changed/idempotent output, not a copied merge.
    for missing in [('review_tests',), ('worklink_base', 'review_tests')]:
        legacy = yaml.safe_load(prior_inventory)
        for field in missing:
            del legacy['repositories'][0][field]
        (home / 'repositories.yaml').write_text(yaml.safe_dump(legacy, sort_keys=False))
        capsys.readouterr()
        exec(compile(script, 'provision.py', 'exec'), {'__name__': '__main__'})
        assert capsys.readouterr().out == 'SETUP_CHANGED=1\n'
        assert yaml.safe_load((home / 'repositories.yaml').read_text()) == inventory
        assert (home / 'worklink.yaml').read_bytes() == prior_worklink
        exec(compile(script, 'provision.py', 'exec'), {'__name__': '__main__'})
        assert capsys.readouterr().out == 'SETUP_CHANGED=0\n'
    # Explicit operator policy conflicts remain refusals, even if a legacy
    # declaration is also missing the Worklink base. No inventory is written.
    for missing_base in [False, True]:
        conflict = yaml.safe_load(prior_inventory)
        conflict['repositories'][0]['review_tests'] = 'none' if review_tests != 'none' else 'full'
        if missing_base:
            del conflict['repositories'][0]['worklink_base']
        (home / 'repositories.yaml').write_text(yaml.safe_dump(conflict, sort_keys=False))
        before = (home / 'repositories.yaml').read_bytes()
        with pytest.raises(RuntimeError, match='existing repository declaration differs'):
            exec(compile(script, 'provision.py', 'exec'), {'__name__': '__main__'})
        assert (home / 'repositories.yaml').read_bytes() == before
        assert (home / 'worklink.yaml').read_bytes() == prior_worklink
    explicit = yaml.safe_load(prior_inventory)
    explicit['repositories'][0]['worklink_base'] = str(tmp_path / 'operator-base')
    (home / 'repositories.yaml').write_text(yaml.safe_dump(explicit, sort_keys=False))
    with pytest.raises(RuntimeError, match='existing repository declaration differs'):
        exec(compile(script, 'provision.py', 'exec'), {'__name__': '__main__'})
    assert yaml.safe_load((home / 'repositories.yaml').read_text()) == explicit
    (home / 'repositories.yaml').write_bytes(prior_inventory)
    (repo / ".git").rmdir()
    with pytest.raises(subprocess.CalledProcessError):
        def fail_clone(argv, **kwargs):
            if "clone" in argv:
                raise subprocess.CalledProcessError(1, argv)
            return git(argv, **kwargs)
        monkeypatch.setattr(subprocess, "run", fail_clone)
        exec(compile(script, "provision.py", "exec"), {"__name__": "__main__"})
    assert (home / "repositories.yaml").read_bytes() == prior_inventory
    assert (home / 'worklink.yaml').read_bytes() == prior_worklink
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


@pytest.mark.parametrize('toolchain', ['rust', 'python', 'node'])
def test_worklink_base_and_config_reconcile_with_real_git(tmp_path, monkeypatch, toolchain):
    from functools import partial
    monkeypatch.setattr(installer, '_provision_worklink', partial(installer._provision_worklink, toolchain=toolchain))
    original_run = subprocess.run
    origin = tmp_path / 'remote.git'
    seed = tmp_path / 'seed'
    original_run(['git', 'init', '--bare', str(origin)], check=True, capture_output=True)
    original_run(['git', 'init', '-b', 'integration', str(seed)], check=True, capture_output=True)
    (seed / 'README').write_text('seed\n')
    original_run(['git', '-C', str(seed), 'add', 'README'], check=True, capture_output=True)
    original_run(['git', '-C', str(seed), '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                  'commit', '-m', 'seed'], check=True, capture_output=True)
    original_run(['git', '-C', str(seed), 'push', str(origin), 'HEAD:refs/heads/integration'],
                 check=True, capture_output=True)
    home = tmp_path / 'home'
    home.mkdir()
    base = tmp_path / 'worklink/base/repo'
    checkout = tmp_path / 'repo'
    entry = {'slug': 'org/repo', 'origin': str(origin), 'base_branch': 'integration',
             'test_command': './tools/verify'}

    def run(argv, **kw):
        if argv[0] == 'chainlink':
            assert kw['cwd'] == home
            if argv == ['chainlink', 'init', '-q']:
                (home / '.chainlink').mkdir()
            else:
                assert argv == ['chainlink', 'agent', 'init', 'cultivate-worklink']
                assert (home / '.chainlink').is_dir()
                (home / '.chainlink/agent.json').write_text('{"id": "cultivate-worklink"}')
            return subprocess.CompletedProcess(argv, 0)
        return original_run(argv, **kw)

    monkeypatch.setattr(subprocess, 'run', run)
    auth = ['git', '-c', 'credential.helper=', '-c', 'credential.https://github.com.helper=!f() { :; }; f']
    assert installer._provision_worklink(home, entry, base=base, checkout=checkout, git_auth=auth)
    assert original_run(['git', '-C', str(base), 'config', '--local', '--get', 'user.name'],
                        capture_output=True, text=True, check=True).stdout.strip() == 'trellisbot-squashmerge'
    assert original_run(['git', '-C', str(base), 'config', '--local', '--get', 'user.email'],
                        capture_output=True, text=True, check=True).stdout.strip() == 'trellisbot@example.com'
    from mimir_cultivate.worklink.backends.registry import WorklinkConfig, BackendRegistry
    config = WorklinkConfig.load(home / 'worklink.yaml')
    BackendRegistry(config)
    assert config.repository == entry['slug']
    assert config.defaults.base_branch == entry['base_branch']
    assert config.defaults.test_command == entry['test_command']
    assert config.defaults.backend == 'opencode'
    expected_env = {'rust': {'RUSTUP_HOME': '/opt/rustup', 'CARGO_HOME': '/var/cache/mimir-cargo'},
                    'python': {'UV_PYTHON_INSTALL_DIR': '/opt/uv-python'}, 'node': {}}[toolchain]
    assert config.backend_settings['opencode']['test_env'] == expected_env
    assert config.defaults.timeout_s == 5400
    assert config.defaults.allow_autonomous_local_subprocess
    assert original_run(['git', '-C', str(base), 'status', '--porcelain'], capture_output=True,
                        text=True, check=True).stdout == ''
    assert original_run(['git', '-C', str(base), 'symbolic-ref', '--short', 'HEAD'],
                        capture_output=True, text=True, check=True).stdout.strip() == 'integration'
    assert base != checkout and not base.is_relative_to(checkout)
    from mimir_cultivate.worklink.checkout import create_isolated_checkout
    monkeypatch.setenv('MIMIR_SOURCE_DIR', str(checkout))
    monkeypatch.setenv('WORKLINK_CHECKOUT_ROOT', str(tmp_path / '.worklink'))
    monkeypatch.setattr('mimir_cultivate.worklink.checkout._cultivate_selected', lambda: False)
    events = []
    attempt = create_isolated_checkout(base, issue_id=2016, attempt=1, base='integration',
        event_logger=lambda name, **payload: events.append((name, payload)))
    assert attempt.repo == base and attempt.branch == 'issue/2016-a1'
    assert attempt.path != checkout and attempt.path != base
    assert original_run(['git', '-C', str(attempt.path), 'config', '--local', '--get', 'user.name'],
                        capture_output=True, text=True, check=True).stdout.strip() == 'trellisbot-squashmerge'
    assert original_run(['git', '-C', str(attempt.path), 'config', '--local', '--get', 'user.email'],
                        capture_output=True, text=True, check=True).stdout.strip() == 'trellisbot@example.com'
    [identity] = [p for n, p in events if n == 'worklink_checkout_identity_configured']
    assert (identity['user_name'], identity['user_email'], identity['inherited']) == (
        'trellisbot-squashmerge', 'trellisbot@example.com', True)
    origin_tip = original_run(['git', '-C', str(base), 'rev-parse', 'origin/integration'],
                              capture_output=True, text=True, check=True).stdout.strip()
    assert attempt.local_base == origin_tip
    before = ((home / '.chainlink/agent.json').read_bytes(), (home / 'worklink.yaml').read_bytes(),
               (base / '.git/config').read_bytes())
    assert not installer._provision_worklink(home, entry, base=base, checkout=checkout, git_auth=auth)
    assert before == ((home / '.chainlink/agent.json').read_bytes(), (home / 'worklink.yaml').read_bytes(),
                      (base / '.git/config').read_bytes())
    monkeypatch.delenv('GH_USER_NAME')
    with pytest.raises(RuntimeError, match='identity unconfigured: GH_USER_NAME'):
        installer._provision_worklink(home, entry, base=base, checkout=checkout, git_auth=auth)
    monkeypatch.setenv('GH_USER_NAME', 'trellisbot-squashmerge')
    (base / 'untracked').write_text('dirty')
    with pytest.raises(RuntimeError, match='dirty.*clean'):
        installer._provision_worklink(home, entry, base=base, checkout=checkout, git_auth=auth)
    (base / 'untracked').unlink()
    original_run(['git', '-C', str(base), 'remote', 'set-url', 'origin', str(seed)], check=True)
    with pytest.raises(RuntimeError, match='origin disagrees'):
        installer._provision_worklink(home, entry, base=base, checkout=checkout, git_auth=auth)
    original_run(['git', '-C', str(base), 'remote', 'set-url', 'origin', str(origin)], check=True)
    config_path = home / 'worklink.yaml'
    config_path.write_text(config_path.read_text() + '# operator note\n')
    with pytest.raises(RuntimeError, match='--force'):
        installer._provision_worklink(home, entry, base=base, checkout=checkout, git_auth=auth)
    assert '# operator note' in config_path.read_text()
    assert installer._provision_worklink(home, entry, base=base, checkout=checkout, git_auth=auth, force=True)
    with pytest.raises(RuntimeError, match='overlaps'):
        installer._provision_worklink(home, entry, base=checkout, checkout=checkout, git_auth=auth)
    alias = tmp_path / 'alias'
    alias.symlink_to(base, target_is_directory=True)
    with pytest.raises(RuntimeError, match='symlink'):
        installer._provision_worklink(home, entry, base=alias, checkout=checkout, git_auth=auth)
    original_run(['git', '-C', str(base), 'checkout', '-b', 'wrong-branch'], check=True, capture_output=True)
    with pytest.raises(RuntimeError, match='not on integration'):
        installer._provision_worklink(home, entry, base=base, checkout=checkout, git_auth=auth)
    original_run(['git', '-C', str(base), 'checkout', 'integration'], check=True, capture_output=True)
    config_path.unlink()
    config_path.symlink_to(home / '.chainlink/agent.json')
    with pytest.raises(RuntimeError, match='regular file'):
        installer._provision_worklink(home, entry, base=base, checkout=checkout, git_auth=auth, force=True)
    config_path.unlink()
    installer._provision_worklink(home, entry, base=base, checkout=checkout, git_auth=auth)
    agent = home / '.chainlink/agent.json'
    agent.unlink()
    agent.symlink_to(config_path)
    with pytest.raises(RuntimeError, match='regular file'):
        installer._provision_worklink(home, entry, base=base, checkout=checkout, git_auth=auth)


@pytest.mark.parametrize('store_state', ['fresh', 'existing', 'init_failure', 'init_missing_cli', 'init_no_store',
                                         'symlink', 'dangling_symlink', 'file', 'init_symlink'])
def test_worklink_store_init_precedes_agent_and_refuses_bad_store(tmp_path, monkeypatch, store_state):
    home = tmp_path / 'home'
    home.mkdir()
    store = home / '.chainlink'
    if store_state == 'existing':
        store.mkdir()
    elif store_state in {'symlink', 'init_symlink'}:
        (tmp_path / 'target').mkdir()
        if store_state == 'symlink':
            store.symlink_to(tmp_path / 'target', target_is_directory=True)
    elif store_state == 'dangling_symlink':
        store.symlink_to(tmp_path / 'absent', target_is_directory=True)
    elif store_state == 'file':
        store.write_text('not a store')
    calls = []
    identity = {}

    def run(argv, **kw):
        if argv[0] == 'chainlink':
            calls.append((argv, kw['cwd']))
            if argv == ['chainlink', 'init', '-q']:
                if store_state == 'init_missing_cli':
                    raise FileNotFoundError('chainlink')
                if store_state == 'init_symlink':
                    store.symlink_to(tmp_path / 'target', target_is_directory=True)
                elif store_state != 'init_no_store':
                    store.mkdir()
                return subprocess.CompletedProcess(argv, 1 if store_state == 'init_failure' else 0)
            assert argv == ['chainlink', 'agent', 'init', 'cultivate-worklink']
            (store / 'agent.json').write_text('{"id":"cultivate-worklink"}')
            return subprocess.CompletedProcess(argv, 0)
        if 'clone' in argv:
            (Path(argv[-1]) / '.git').mkdir(parents=True)
        if argv[-2:] in (['--get', 'user.name'], ['--get', 'user.email']):
            key = argv[-1]
            return subprocess.CompletedProcess(argv, 0 if key in identity else 1,
                                               stdout=identity.get(key, '') + '\n')
        if len(argv) > 5 and argv[-2] in ('user.name', 'user.email'):
            identity[argv[-2]] = argv[-1]
        stdout = ('main' if 'symbolic-ref' in argv else '' if 'status' in argv else 'origin') + '\n'
        return subprocess.CompletedProcess(argv, 0, stdout=stdout)

    monkeypatch.setattr(subprocess, 'run', run)
    entry = {'slug': 'org/repo', 'origin': 'origin', 'base_branch': 'main', 'test_command': 'npm test'}
    kwargs = {'base': tmp_path / 'base', 'checkout': tmp_path / 'checkout',
              'git_auth': ['git', '-c', 'credential.helper=fixture']}
    if store_state in {'symlink', 'dangling_symlink', 'file', 'init_symlink', 'init_failure', 'init_missing_cli', 'init_no_store'}:
        message = ('must be a real directory' if store_state in {'symlink', 'dangling_symlink', 'file'} else
                   'could not run' if store_state == 'init_missing_cli' else 'Chainlink init failed')
        with pytest.raises(RuntimeError, match=message) as caught:
            installer._provision_worklink(home, entry, **kwargs)
        assert str(home) in str(caught.value)
        assert calls == ([(['chainlink', 'init', '-q'], home)] if store_state.startswith('init_') else [])
    else:
        assert installer._provision_worklink(home, entry, **kwargs)
        assert calls == ([(['chainlink', 'init', '-q'], home)] if store_state == 'fresh' else []) + [
            (['chainlink', 'agent', 'init', 'cultivate-worklink'], home)]
        before = (store / 'agent.json').read_bytes()
        assert not installer._provision_worklink(home, entry, **kwargs)
        assert (store / 'agent.json').read_bytes() == before
        assert len(calls) == (2 if store_state == 'fresh' else 1)


def test_worklink_provisions_fresh_home_with_real_chainlink(tmp_path):
    if not shutil.which('chainlink'):
        pytest.skip('chainlink CLI is not on PATH')
    origin = tmp_path / 'origin.git'
    seed = tmp_path / 'seed'
    subprocess.run(['git', 'init', '--bare', str(origin)], check=True, capture_output=True)
    subprocess.run(['git', 'init', '-b', 'main', str(seed)], check=True, capture_output=True)
    (seed / 'README').write_text('seed\n')
    subprocess.run(['git', '-C', str(seed), 'add', 'README'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(seed), '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                    'commit', '-m', 'seed'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(seed), 'push', str(origin), 'HEAD:refs/heads/main'],
                   check=True, capture_output=True)
    home = tmp_path / 'home'
    home.mkdir()
    assert installer._provision_worklink(home, {
        'slug': 'org/repo', 'origin': str(origin), 'base_branch': 'main', 'test_command': 'npm test',
    }, base=tmp_path / 'base', checkout=tmp_path / 'checkout',
        git_auth=['git', '-c', 'credential.helper='])
    agent = json.loads((home / '.chainlink/agent.json').read_text())
    assert agent.get('agent_id') == 'cultivate-worklink', agent


@pytest.mark.parametrize('relation', ['equal', 'nested', 'contains', 'lease'])
def test_worklink_overlap_refused_before_side_effects(tmp_path, monkeypatch, relation):
    checkout = tmp_path / 'checkout'
    base = {'equal': checkout, 'nested': checkout / 'base',
            'contains': tmp_path, 'lease': Path('/workspace/pr-leases/base')}[relation]
    home = tmp_path / 'home'
    home.mkdir()
    before = tuple(tmp_path.iterdir())

    def unexpected(*args, **kwargs):
        pytest.fail('overlapping base reached subprocess execution')

    monkeypatch.setattr(subprocess, 'run', unexpected)
    entry = {'origin': 'https://github.com/org/repo.git', 'base_branch': 'main'}
    with pytest.raises(RuntimeError, match='overlaps the live checkout or lease root'):
        installer._provision_worklink(home, entry, base=base, checkout=checkout, git_auth=[])
    assert tuple(tmp_path.iterdir()) == before
    assert not list(home.iterdir())


def test_dedicated_base_selection_requires_declared_persistent_root(tmp_path, monkeypatch):
    from mimir.plugins.manager import load_plugins
    from mimir_cultivate.repository import RepositoryInventory, dedicated_worklink_base
    from mimir_cultivate.worklink.merge_closure import resolve_completion_repository, ClosureReadError

    home = tmp_path / 'home'
    home.mkdir()
    checkout = tmp_path / 'repo'
    # Deliberately not the conventional worklink/base/<name> path: trust
    # follows the explicit field, not a directory-naming convention.
    base = tmp_path / 'worklink/declared-base'
    conventional = tmp_path / 'worklink/base/repo'
    monkeypatch.delenv('MIMIR_WORKLINK_REPO', raising=False)
    (home / 'worklink.yaml').write_text('repository: org/repo\n')
    inventory = home / 'repositories.yaml'
    entry = {'slug': 'org/repo', 'root': str(checkout), 'mode': 'rw',
             'origin': 'https://github.com/org/repo.git', 'base_branch': 'main',
             'worklink_base': str(base)}
    inventory.write_text(yaml.safe_dump({'repositories': [entry],
        'allowed_roots': [{'root': str(tmp_path / 'worklink'), 'mode': 'rw'}]}))
    declaration = RepositoryInventory.load(inventory)
    assert dedicated_worklink_base(declaration, declaration.repository('org/repo')) == base
    selection = load_plugins(enable='cultivate').policy.policy_consumer_hooks.repository_selection
    monkeypatch.setenv('WORKLINK_REPO', str(base))
    selection(home, declaration)
    base.mkdir(parents=True)

    def git(argv):
        assert argv[:3] == ['git', '-C', str(base)]
        return subprocess.CompletedProcess(argv, 0, stdout=(
            str(base) if argv[-1] == '--show-toplevel' else 'https://github.com/org/repo.git') + '\n')

    assert resolve_completion_repository(home, runner=git).root == base.resolve()
    monkeypatch.setenv('MIMIR_WORKLINK_REPO', str(checkout))
    with pytest.raises(ClosureReadError, match='declared root'):
        resolve_completion_repository(home, runner=git)
    monkeypatch.delenv('MIMIR_WORKLINK_REPO')
    monkeypatch.setenv('WORKLINK_REPO', str(checkout))
    selection(home, declaration)
    monkeypatch.setenv('WORKLINK_REPO', str(tmp_path / 'other'))
    with pytest.raises(RuntimeError, match='disagrees'):
        selection(home, declaration)
    monkeypatch.setenv('WORKLINK_REPO', str(conventional))
    with pytest.raises(RuntimeError, match='disagrees'):
        selection(home, declaration)
    for roots in ([], [{'root': str(tmp_path / 'worklink'), 'mode': 'ro'}],
                  [{'root': str(tmp_path / 'elsewhere'), 'mode': 'rw'}]):
        inventory.write_text(yaml.safe_dump({'repositories': [entry], 'allowed_roots': roots}))
        refused = RepositoryInventory.load(inventory)
        monkeypatch.setenv('WORKLINK_REPO', str(base))
        assert dedicated_worklink_base(refused, refused.repository('org/repo')) is None
        with pytest.raises(RuntimeError, match='disagrees'):
            selection(home, refused)
        with pytest.raises(ClosureReadError, match='declared root'):
            resolve_completion_repository(home, runner=git)
    # No field preserves legacy selection, even if the conventional rw root exists.
    entry.pop('worklink_base')
    inventory.write_text(yaml.safe_dump({'repositories': [entry], 'allowed_roots': [
        {'root': str(tmp_path / 'worklink'), 'mode': 'rw'}]}))
    legacy = RepositoryInventory.load(inventory)
    assert legacy.repository('org/repo').worklink_base is None
    assert dedicated_worklink_base(legacy, legacy.repository('org/repo')) is None
    for target in (base, conventional):
        monkeypatch.setenv('WORKLINK_REPO', str(target))
        with pytest.raises(RuntimeError, match='disagrees'):
            selection(home, legacy)
    monkeypatch.setenv('WORKLINK_REPO', str(checkout))
    selection(home, legacy)


@pytest.mark.parametrize('value', [None, 123, '', 'relative/base', '/base/../other', '~/base'])
def test_worklink_base_field_requires_absolute_path(tmp_path, value):
    from mimir_cultivate.repository import RepositoryInventory
    inventory = tmp_path / 'repositories.yaml'
    inventory.write_text(yaml.safe_dump({'repositories': [{
        'slug': 'org/repo', 'root': str(tmp_path / 'repo'), 'mode': 'rw',
        'origin': 'https://github.com/org/repo.git', 'base_branch': 'main',
        'worklink_base': value,
    }]}))
    with pytest.raises(ValueError, match='worklink_base'):
        RepositoryInventory.load(inventory)


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
    assert parsed["llm"]["sampling"] == "default"
    if parsed["llm"]["model"] == installer.DEFAULT_SAGA_MODELS[spec.split(":")[0]]:
        assert parsed["llm"]["reasoning_effort"] == "low"
    else:
        assert "reasoning_effort" not in parsed["llm"]
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


def test_local_prefetch_and_agent_share_persistent_cache(tmp_path, monkeypatch):
    """Execute the provision preamble with real Saga prefetch, without a model download."""
    from mimir.scaffold_docker import render_compose_yml
    from mimir.saga.embeddings import LocalProvider, REMOTE_CODE_REVISIONS

    compose = installer.render_compose(
        render_compose_yml(service_name="agent", web_port=8123, mode="pypi"),
        "image:sha", "python", "repo",
    )
    agent = yaml.safe_load(compose)["services"]["agent"]
    assert "cache:/home/mimir/.cache" in agent["volumes"]
    assert "FASTEMBED_CACHE_PATH" not in agent["environment"]

    container_home = tmp_path / "home-mimir"
    (container_home / "agent").mkdir(parents=True)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"saga_model_spec": "openai:gpt-5.4-nano",
                                    "embedding": "local", "reembed": False}))
    prefetches = []
    resolutions = []

    class LocalEntryNotFoundError(Exception):
        pass

    def snapshot_download(model_name, *, revision, local_files_only):
        cache = Path(os.environ["XDG_CACHE_HOME"]) / "huggingface"
        snapshot = cache / "hub" / ("models--" + model_name.replace("/", "--")) / "snapshots" / revision
        resolutions.append((model_name, revision, local_files_only, cache))
        if not snapshot.is_dir():
            raise LocalEntryNotFoundError("cold cache")
        return str(snapshot)

    def load(model_name, **kwargs):
        cache = Path(os.environ["XDG_CACHE_HOME"]) / "huggingface"
        if model_name == installer.LOCAL_MODEL:
            snapshot = cache / "hub" / ("models--" + model_name.replace("/", "--")) / "snapshots" / kwargs["revision"]
            snapshot.mkdir(parents=True, exist_ok=True)
        else:
            assert Path(model_name).is_dir()
        prefetches.append((model_name, kwargs, cache))
        return object()

    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=snapshot_download))
    monkeypatch.setitem(sys.modules, "huggingface_hub.errors", SimpleNamespace(LocalEntryNotFoundError=LocalEntryNotFoundError))
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=load))
    monkeypatch.delenv("FASTEMBED_CACHE_PATH", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    # Only the prefetch portion of the actual provision program is needed.
    script = (installer._PROVISION.split("repo = Path('/workspace')", 1)[0]
              .replace("/opt/cultivate-manifest.json", str(manifest))
              .replace("/home/mimir", str(container_home)))
    exec(compile(script, "provision.py", "exec"), {"__name__": "__main__"})

    cache = container_home / ".cache" / "huggingface"
    assert cache.is_dir()
    expected = (installer.LOCAL_MODEL,
                {"device": "cpu", "trust_remote_code": True,
                 "revision": REMOTE_CODE_REVISIONS[installer.LOCAL_MODEL]},
                cache)
    assert prefetches == [expected]
    # Match the agent's generated environment as well as the provision preamble.
    monkeypatch.setenv("XDG_CACHE_HOME", agent["environment"]["XDG_CACHE_HOME"].replace(
        "/home/mimir", str(container_home)))
    runtime = LocalProvider(model_name=installer.LOCAL_MODEL, dimensions=1024)
    runtime._load_model()
    revision = REMOTE_CODE_REVISIONS[installer.LOCAL_MODEL]
    snapshot = cache / "hub" / "models--voyageai--voyage-4-nano" / "snapshots" / revision
    assert prefetches == [expected, (str(snapshot), {"device": "cpu", "trust_remote_code": True}, cache)]
    assert resolutions == [(installer.LOCAL_MODEL, revision, True, cache)] * 2


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
    for path in ("Dockerfile", "plugins/cultivate/mimir_cultivate/docker/controller.sh"):
        text = (root / path).read_text()
        assert "*,local-embeddings,*" in text
        assert "torch==" + cpu["version"] in text
        assert "--extra-index-url https://download.pytorch.org/whl/cpu" in text


def test_prompts_recommend_fast_model_and_reuse_embedding_key(monkeypatch, capsys):
    args = argparse.Namespace(model_spec="openai:flagship", saga_model_spec=None, embedding=None)
    monkeypatch.setattr(installer.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert installer._field(args, "saga_model_spec", "Frequent short calls: faster, cheaper; agent openai:flagship is an alternative",
                             default="openai:" + installer.DEFAULT_SAGA_MODELS["openai"] + ":low") == "openai:gpt-6-luna:low"
    assert "faster, cheaper" in capsys.readouterr().out


def test_default_saga_anthropic_model():
    assert installer.DEFAULT_SAGA_MODELS["openai"] == "gpt-6-luna"
    assert installer.DEFAULT_SAGA_MODELS["anthropic"] == "claude-haiku-5-5"


@pytest.mark.parametrize("provider,model", list(installer.DEFAULT_SAGA_MODELS.items()))
def test_recommended_saga_spec_writes_effort_and_sampling(tmp_path, provider, model):
    for spec in (f"{provider}:{model}", f"{provider}:{model}:low"):
        home = tmp_path / ("suffix" if spec.endswith(":low") else "bare")
        home.mkdir()
        installer._provision_saga(home, spec, "local", prefetch=lambda: None)
        llm = tomllib.loads((home / "saga.toml").read_text())["llm"]
        assert llm["model"] == model
        assert llm["reasoning_effort"] == "low"
        assert llm["sampling"] == "default"


@pytest.mark.parametrize("spec,effort", [
    ("openai:other", None), ("openai:other:high", "high"),
    ("anthropic:other", None), ("anthropic:other:xhigh", "xhigh"),
])
def test_saga_spec_optional_effort(spec, effort):
    assert installer.validate("saga_model_spec", spec) == spec
    llm = installer._saga_settings(spec, "local")["llm"]
    assert llm.get("reasoning_effort") == effort
    assert llm["sampling"] == "default"


@pytest.mark.parametrize("spec", ["anthropic:claude-opus-5-5", "openai:non-default-reasoning-model"])
def test_non_default_saga_model_without_effort_uses_default_sampling(tmp_path, spec):
    assert installer._provision_saga(tmp_path, spec, "local", prefetch=lambda: None)
    llm = tomllib.loads((tmp_path / "saga.toml").read_text())["llm"]
    assert llm["model"] == spec.split(":")[1]
    assert llm["sampling"] == "default"
    assert "reasoning_effort" not in llm


@pytest.mark.parametrize("spec", ["openai:gpt-6-luna:none", "anthropic:claude-haiku-5-5:minimal", "openai:m:", "openai:m:low:high"])
def test_invalid_saga_effort_rejected_before_provision(spec, monkeypatch):
    with pytest.raises(installer.SetupError, match="saga model spec"):
        installer.validate("saga_model_spec", spec)


def test_invalid_saga_effort_stops_setup_before_build(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "_license_gate", lambda args: None)
    monkeypatch.setattr(installer, "validate_token", lambda *a, **kw: None)
    monkeypatch.setenv("MIMIR_CULTIVATE_GITHUB_TOKEN", "test-only")
    monkeypatch.setenv("MIMIR_CULTIVATE_MODEL_KEY", "test-only")
    parser = argparse.ArgumentParser()
    installer.add_argparse(parser.add_subparsers(dest="command"))
    deploy = tmp_path / "deploy"
    args = parser.parse_args([
        "cultivate", "setup", "--repo", "org/repo", "--bot-login", "bot",
        "--bot-email", "bot@example.com", "--owner", "owner", "--display-name", "Owner",
        "--owner-email", "owner@example.com", "--model-spec", "openai:agent",
        "--saga-model-spec", "openai:gpt-6-luna:none", "--deploy", str(deploy),
    ])
    with pytest.raises(installer.SetupError, match="not valid for provider 'openai'"):
        installer.setup(args, request=lambda *a: pytest.fail("requested remote before spec validation"),
                        run=lambda *a, **kw: pytest.fail("built before spec validation"))
    assert not deploy.exists()


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
        if argv[0] == 'chainlink':
            if argv[1:] == ['init', '-q']:
                (home / '.chainlink').mkdir()
            else:
                assert argv[1:] == ['agent', 'init', 'cultivate-worklink']
                (home / '.chainlink/agent.json').write_text('{"id":"cultivate-worklink"}')
            return subprocess.CompletedProcess(argv, 0)
        if argv[0] == "git":
            git_calls.append(argv)
            if "clone" in argv:
                (Path(argv[-1]) / ".git").mkdir(parents=True)
            if "push" in argv:
                return subprocess.CompletedProcess(argv, 0 if push_ok else 128,
                                                   stdout=b"secret remote output fixture-token",
                                                   stderr=probe_stderr + b" remote secret fixture-token")
            if 'symbolic-ref' in argv:
                return subprocess.CompletedProcess(argv, 0, stdout='main\n')
            if 'status' in argv:
                return subprocess.CompletedProcess(argv, 0, stdout='')
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
                      .replace("Path('/workspace')", "Path(" + repr(str(tmp_path)) + ")")
                      .replace("Path('/workspace/worklink/base')", "Path(" + repr(str(tmp_path / 'worklink/base')) + ")"))
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
    assert len([argv for argv in git_calls if "clone" in argv]) == (1 if push_ok else 0) + (0 if existing_checkout else 1)
    assert all("fixture-token" not in " ".join(argv) for argv in git_calls + docker_calls)
    captured = capsys.readouterr()
    assert "fixture-token" not in captured.out + captured.err
    assert "secret remote output" not in captured.out + captured.err
