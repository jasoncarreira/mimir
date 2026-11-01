"""Tests for trusted-service direct-exec environment hardening."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from mimir import github_identity as core_identity

from mimir.tools import _shell_env
from mimir.tools._shell_env import direct_exec_env, direct_exec_env_overlay


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["interactive", "declared", "operator"])
@pytest.mark.parametrize("async_job", [False, True], ids=["sync", "async"])
async def test_otlp_credentials_never_reach_shell_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, async_job: bool,
) -> None:
    from mimir.event_logger import EventLogger
    from mimir.shell_jobs import ShellJobRegistry
    from mimir.tools import shell_async
    from mimir.tools.extra import shell_exec

    sensitive = {
        "OTEL_EXPORTER_OTLP_HEADERS": "plain-header-canary",
        "otel_exporter_otlp_traces_headers": "plain-trace-header-canary",
        "OTEL_EXPORTER_OTLP_METRICS_CLIENT_KEY": "plain-key-canary",
        "OTEL_EXPORTER_OTLP_LOGS_CLIENT_CERTIFICATE": "plain-client-cert-canary",
        "OTEL_EXPORTER_OTLP_CERTIFICATE": "plain-cert-canary",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "https://user:plain-user-canary@collector.test/v1/traces",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "https://collector.test/v1/metrics?token=plain-query-canary",
        "MIMIR_OTLP_ENDPOINT": "not-an-http-url",
    }
    allowed = {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "https://collector.test/base",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "SHELL_EGRESS_CONTROL": "ordinary-child-value",
    }
    for name, value in (sensitive | allowed).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("MIMIR_SHELL_PASS_ENV", ",".join(sensitive | allowed))
    logger = EventLogger(tmp_path / "events.jsonl", "shell-egress")
    monkeypatch.setattr("mimir.event_logger.log_event_sync", logger.log_sync)
    output_path = tmp_path / "child-env.json"
    script = tmp_path / "dump-env.py"
    script.write_text(
        "import json, os, sys\n"
        "with open(sys.argv[1], 'w') as output:\n"
        "    json.dump(dict(os.environ), output)\n"
    )
    argv = [sys.executable, str(script), str(output_path)]
    token = None
    if mode != "interactive":
        token = _shell_env._DIRECT_EXEC_ARGV.set(_shell_env._DirectExecBinding(
            tuple(argv), tuple(sensitive | allowed) if mode == "declared" else (),
            operator_declared=mode == "operator",
        ))
    try:
        if async_job:
            registry = ShellJobRegistry(jobs_dir=tmp_path / "jobs")
            completed = threading.Event()
            jobs = []

            def on_complete(job):
                jobs.append(job)
                completed.set()

            shell_async.set_shell_job_registry(registry, on_complete=on_complete)
            try:
                result = await shell_async.bash_async.coroutine(command="env")
                assert "Spawned job" in result
                assert await asyncio.to_thread(completed.wait, 10)
                assert jobs[0].exit_code == 0
                child_text = jobs[0].stdout_path.read_text()
            finally:
                shell_async.set_shell_job_registry(None)
        else:
            result = shell_exec.invoke({"command": "env"})
            assert "exit=0" in result
            child_text = (result.split("stdout:\n", 1)[1].split("\nstderr:", 1)[0]
                          if mode == "interactive" else result)
    finally:
        if token is not None:
            _shell_env._DIRECT_EXEC_ARGV.reset(token)

    child = (json.loads(output_path.read_text()) if mode != "interactive" else
             dict(line.split("=", 1) for line in child_text.splitlines() if "=" in line))
    assert not sensitive.keys() & child.keys()
    assert {name: child[name] for name in allowed} == allowed
    logger.log_sync("shell_egress_check")
    audit = (tmp_path / "events.jsonl").read_text()
    for value in sensitive.values():
        assert value not in child_text
        assert value not in repr(child)
        assert value not in audit


def test_otlp_filter_applies_again_at_sync_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    from mimir.tools.extra import _run_shell_command

    result = _run_shell_command(
        ["/usr/bin/env"], cwd=None, timeout=5,
        env={"OTEL_EXPORTER_OTLP_HEADERS": "plain-header-canary", "KEEP": "untouched"},
    )
    assert result.returncode == 0
    assert result.stdout.strip() == b"KEEP=untouched"


@pytest.fixture
def selected_shell_policy():
    pytest.importorskip("mimir_cultivate")
    from mimir.plugins.core import _selected_policy
    from mimir.plugins.manager import load_plugins

    selected = load_plugins(enable="cultivate").policy
    assert selected.policy_consumer_hooks is not None
    token = _selected_policy.set(selected)
    try:
        yield selected
    finally:
        _selected_policy.reset(token)


@pytest.mark.parametrize("selected", [False, True])
def test_generic_sync_and_async_shell_execute_through_middleware(
    tmp_path: Path, selected: bool,
) -> None:
    if selected and os.environ.get("MIMIR_PLUGINS") != "cultivate":
        pytest.skip("requires selected Cultivate in this process")
    script = '''
import asyncio, builtins, os, sys, threading
from pathlib import Path
old = builtins.__import__
def no_disabled_provider(name, globals=None, locals=None, fromlist=(), level=0):
    if os.environ.get("MIMIR_PLUGINS") != "cultivate" and name.startswith("mimir_cultivate"):
        raise AssertionError("core-only shell imported disabled provider")
    return old(name, globals, locals, fromlist, level)
builtins.__import__ = no_disabled_provider
from langchain.agents.middleware import ToolCallRequest
from langgraph.runtime import Runtime
from mimir.tools.budget_gate import BudgetGateMiddleware
from mimir.tools.extra import shell_exec
from mimir.tools import shell_async
from mimir.shell_jobs import ShellJobRegistry
from tests.auth_helpers import middleware_auth_context
if os.environ.get("MIMIR_PLUGINS") == "cultivate":
    from mimir.plugins.manager import load_plugins
    selected = load_plugins(enable="cultivate").policy
    assert selected.policy_consumer_hooks is not None
else:
    from mimir.plugins.core import core_policy_registry
    selected = core_policy_registry()
gate = BudgetGateMiddleware(selected)
def request(name, tool):
    return ToolCallRequest(tool_call={"name": name, "args": {"command": "printf direct-ok"},
                                 "id": "generic-shell", "type": "tool_call"},
                           tool=tool, state=None, runtime=Runtime(context=middleware_auth_context()))
sync = gate.wrap_tool_call(request("shell_exec", shell_exec), lambda call: call.tool.invoke(call.tool_call["args"]))
assert "direct-ok" in str(sync) and "exit=0" in str(sync), sync
async def run():
    registry = ShellJobRegistry(jobs_dir=Path(sys.argv[1]))
    done = threading.Event()
    completed = []
    shell_async.set_shell_job_registry(registry, on_complete=lambda job: (completed.append(job), done.set()))
    try:
        async def execute(call):
            return await call.tool.ainvoke(call.tool_call["args"])
        output = await gate.awrap_tool_call(request("bash_async", shell_async.bash_async), execute)
        assert "Spawned job" in str(output), output
        assert await asyncio.to_thread(done.wait, 15)
        assert completed[0].exit_code == 0
        assert completed[0].stdout_path.read_text().strip() == "direct-ok"
    finally:
        shell_async.set_shell_job_registry(None)
asyncio.run(run())
'''
    env = os.environ.copy()
    env["MIMIR_PLUGINS"] = "cultivate" if selected else ""
    env["SHELL_SECRET"] = "hidden-secret"
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "jobs")],
        capture_output=True, text=True, timeout=45, env=env,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_interactive_sync_and_async_children_receive_same_scrubbed_env(
    tmp_path, monkeypatch,
):
    from mimir.shell_jobs import ShellJobRegistry
    from mimir.tools.extra import shell_exec
    from mimir.tools import shell_async

    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setenv("FAKE_API_KEY", "x")
    monkeypatch.setenv("KEEP_ME", "y")
    monkeypatch.setenv("GITHUB_TOKEN", "ungranted")
    monkeypatch.setenv("GH_TOKEN", "ungranted")
    monkeypatch.setenv("MIMIR_SHELL_PASS_ENV", "KEEP_ME")
    monkeypatch.setattr("mimir.event_logger.log_event_sync", lambda *a, **kw: None)
    sync = shell_exec.invoke({"command": "env"})
    registry = ShellJobRegistry(jobs_dir=tmp_path / "jobs")
    done = threading.Event()
    completed = []
    def on_complete(job):
        completed.append(job)
        done.set()

    shell_async.set_shell_job_registry(registry, on_complete=on_complete)
    try:
        result = await shell_async.bash_async.coroutine(command="env", cwd=str(tmp_path))
        assert "Spawned job" in result
        assert await asyncio.to_thread(done.wait, 10)
        [job] = completed
        async_text = job.stdout_path.read_text()
    finally:
        shell_async.set_shell_job_registry(None)

    sync_text = sync.split("stdout:\n", 1)[1]
    sync_env = dict(line.split("=", 1) for line in sync_text.splitlines() if "=" in line)
    async_env = dict(line.split("=", 1) for line in async_text.splitlines() if "=" in line)
    for child in (sync_env, async_env):
        assert child["KEEP_ME"] == "y"
        assert child["MIMIR_HOME"] == str(tmp_path)
        assert not {"FAKE_API_KEY", "GITHUB_TOKEN", "GH_TOKEN"} & child.keys()
    assert sync_env == async_env


def test_server_disables_dumpability_after_update_before_runtime_children():
    import ast

    source = Path(__file__).resolve().parents[1] / "mimir" / "server.py"
    tree = ast.parse(source.read_text())
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
    calls = {
        node.func.id: node.lineno for node in ast.walk(main)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert calls["apply_pending_update"] < calls["disable_process_dumpability"] < calls["build_app"]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux procfs dumpability control")
def test_non_dumpable_parent_environment_unreadable_to_interactive_child():
    if os.geteuid() == 0:
        pytest.skip("root may bypass procfs access controls; exercise as worker uid")
    # Isolate PR_SET_DUMPABLE from pytest's shared process. Probe before and
    # after so a pre-existing procfs restriction cannot make this test vacuous.
    script = '''
import ctypes, subprocess, sys, shlex
from mimir.tools._shell_env import (
    disable_process_dumpability, interactive_shell_env, login_shell_command,
)
probe = "import os; open('/proc/' + str(os.getppid()) + '/environ', 'rb').close()"
argv = ["bash", "-lc", login_shell_command("exec " + shlex.quote(sys.executable) + " -c " + shlex.quote(probe))]
env = interactive_shell_env()
assert subprocess.run(argv, env=env, capture_output=True).returncode == 0
disable_process_dumpability()
assert ctypes.CDLL(None).prctl(3, 0, 0, 0, 0) == 0
result = subprocess.run(argv, env=env, capture_output=True, text=True)
assert result.returncode != 0
assert "PermissionError" in result.stderr
'''
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="Linux procfs dumpability control")
@pytest.mark.parametrize("subcommand", ["watchdog", "worklink"])
def test_real_cli_non_run_sibling_environment_unreadable(subcommand):
    if os.geteuid() == 0:
        pytest.skip("root may bypass procfs access controls; exercise as worker uid")
    if subcommand == "worklink":
        import importlib.util

        if importlib.util.find_spec("mimir_cultivate") is None:
            if {"cultivate", "all"}.intersection(os.environ.get("MIMIR_PLUGINS", "").split(",")):
                pytest.fail("selected Cultivate distribution is not installed")
            pytest.skip("Worklink CLI requires installed Cultivate")
    # Drive the real CLI entry in an isolated sibling process, keeping it alive
    # after --help exits. Prove readability before CLI startup to avoid a
    # vacuous pass on hosts which already restrict same-uid procfs reads.
    script = '''
import contextlib, ctypes, io, sys
from mimir.cli import main
libc = ctypes.CDLL(None)
assert libc.prctl(4, 1, 0, 0, 0) == 0
print("before", flush=True)
sys.stdin.readline()
with contextlib.redirect_stdout(io.StringIO()):
    try:
        main([sys.argv[1], "--help"])
    except SystemExit as exc:
        assert exc.code == 0
print("after", flush=True)
sys.stdin.read()
'''
    child_env = os.environ.copy()
    child_env.pop("MIMIR_PLUGINS", None)
    if subcommand == "worklink":
        child_env["MIMIR_PLUGINS"] = "cultivate"
    sibling = subprocess.Popen(
        [sys.executable, "-c", script, subcommand], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=child_env,
    )
    try:
        assert sibling.stdout.readline().strip() == "before"
        probe = f"open('/proc/{sibling.pid}/environ', 'rb').close()"
        argv = ["bash", "-lc", _shell_env.login_shell_command(
            "exec " + shlex.quote(sys.executable) + " -c " + shlex.quote(probe),
        )]
        env = _shell_env.interactive_shell_env()
        before = subprocess.run(argv, env=env, capture_output=True, timeout=10)
        assert before.returncode == 0, before.stderr
        sibling.stdin.write("start cli\n")
        sibling.stdin.flush()
        assert sibling.stdout.readline().strip() == "after"
        after = subprocess.run(
            argv, env=env, capture_output=True, text=True, timeout=10,
        )
        assert after.returncode != 0
        assert "PermissionError" in after.stderr
        sibling.stdin.close()
        assert sibling.wait(timeout=10) == 0
    finally:
        if sibling.poll() is None:
            sibling.kill()
        sibling.wait(timeout=10)
        sibling.stdout.close()
        sibling.stderr.close()
        if not sibling.stdin.closed:
            sibling.stdin.close()


def test_interactive_passthrough_logs_names_only(monkeypatch):
    events = []
    monkeypatch.setattr("mimir.event_logger.log_event_sync", lambda *a, **kw: events.append((a, kw)))
    monkeypatch.setenv("MIMIR_SHELL_PASS_ENV", "KEEP_ME, ABSENT, KEEP_ME, OTHER*, PATH")
    monkeypatch.setenv("KEEP_ME", "private-value")
    monkeypatch.delenv("ABSENT", raising=False)
    env = _shell_env.interactive_shell_env()
    assert env["KEEP_ME"] == "private-value"
    assert env["PATH"] == _shell_env._TRUSTED_PATH
    assert events == [(("interactive_shell_env_passthrough",), {"pass_env": ["KEEP_ME"]})]
    assert "private-value" not in repr(events)


@pytest.mark.parametrize("executable", ["gh", "git", "echo"])
def test_output_mask_includes_only_granted_child_values(monkeypatch, executable):
    monkeypatch.setattr(_shell_env, "direct_exec_pass_env", lambda argv: (
        "DECLARED", "EMPTY", "ABSENT", "GITHUB_TOKEN",
    ))
    monkeypatch.setenv("GITHUB_TOKEN", "different-parent-secret")
    names = _shell_env.direct_exec_redact_names([f"/usr/bin/{executable}"])
    assert names.count("GITHUB_TOKEN") == 1
    env = {
        "DECLARED": "child-token-suffix", "GITHUB_TOKEN": "child-token",
        "EMPTY": "", "HOME": "/safe/home", "PATH": "/usr/bin",
        "LANG": "C", "GIT_OPTIONAL_LOCKS": "0",
    }
    text = "child-token-suffix child-token /safe/home /usr/bin C 0 different-parent-secret"
    assert _shell_env.redact_direct_exec_output(text, env, names) == (
        "[REDACTED] [REDACTED] /safe/home /usr/bin C 0 different-parent-secret"
    )
    monkeypatch.setattr(_shell_env, "direct_exec_pass_env", lambda argv: ())
    assert _shell_env.direct_exec_redact_names([f"/usr/bin/{executable}"]) == (
        ("GITHUB_TOKEN",) if executable == "gh" else ()
    )


@pytest.mark.parametrize("async_job", [False, True], ids=["sync", "async-job"])
@pytest.mark.parametrize("exit_code", [0, 1])
async def test_implicit_gh_token_is_masked_in_child_output(
    tmp_path, monkeypatch, selected_shell_policy, async_job, exit_code,
):
    from mimir_cultivate import forge as provider_forge
    from mimir.shell_jobs import ShellJobRegistry
    from mimir.tools import shell_async
    from mimir.tools.extra import shell_exec

    secret = "opaque-private-value-1666"
    monkeypatch.setenv("GITHUB_TOKEN", secret)
    monkeypatch.setenv("GH_TOKEN", "ungranted-alternate")
    monkeypatch.setenv("UNDECLARED_TOKEN", "ungranted-private")
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "reviewer")
    monkeypatch.setattr(core_identity, "_verified_identity", (
        "reviewer", hashlib.sha256(secret.encode()).hexdigest(),
    ))
    monkeypatch.setattr(provider_forge, "_github_identity_degraded", False)
    monkeypatch.setattr(provider_forge, "_github_identity_degraded_error", None)
    executable = tmp_path / "gh"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        f"assert os.environ['GITHUB_TOKEN'] == {secret!r}\n"
        "assert 'GH_TOKEN' not in os.environ\n"
        "assert 'UNDECLARED_TOKEN' not in os.environ\n"
        "assert sys.argv[1:] == ['api', 'user']\n"
        "for stream, limit in ((sys.stdout, 4000), (sys.stderr, 2000)):\n"
        "    print('child-ok ' + os.environ['GITHUB_TOKEN'], file=stream)\n"
        "    print('x' * (limit - 38) + os.environ['GITHUB_TOKEN'] + ' trailer' * 20, file=stream)\n"
        f"sys.exit({exit_code})\n"
    )
    executable.chmod(0o755)
    argv = [str(executable), "api", "user"]
    token = _shell_env.bind_direct_exec_argv(argv)
    try:
        assert _shell_env.direct_exec_pass_env(argv) == ()
        if async_job:
            registry = ShellJobRegistry(jobs_dir=tmp_path / "jobs")
            completed = threading.Event()
            captured = []

            def on_complete(job):
                captured.append(job)
                completed.set()

            monkeypatch.setattr(shell_async, "_REGISTRY", registry)
            monkeypatch.setattr(shell_async, "_ON_COMPLETE", on_complete)
            result = await shell_async.bash_async.coroutine(command="gh api user")
            assert "Spawned job" in result
            assert await asyncio.to_thread(completed.wait, 10)
            job = captured[0]
            assert job.exit_code == exit_code
            output = registry.read_job_output(job)
            texts = [job.stdout_path.read_text(), job.stderr_path.read_text(),
                     output["stdout_tail"], output["stderr_tail"]]
        else:
            result = shell_exec.invoke({"command": "gh api user"})
            assert f"exit={exit_code}" in result
            assert result.count("child-ok [REDACTED]") == 2
            assert result.count("[REDACTED]") == 4
            assert "[shell stdout truncated]" in result
            assert "[shell stderr truncated]" in result
            texts = [result]
    finally:
        _shell_env.reset_direct_exec_argv(token)
    for text in texts:
        assert "child-ok [REDACTED]" in text
        assert "opaque" not in text


@pytest.mark.parametrize("overlay", [False, True])
def test_declared_environment_is_bound_to_exact_argv(tmp_path, monkeypatch, overlay):
    from mimir.access_control import parse_declared_shell_commands

    script = tmp_path / "weather.py"
    script.write_text("pass\n")
    monkeypatch.setenv("WEATHER_KEY", "weather-private-value")
    monkeypatch.setenv("UNDECLARED_KEY", "unrelated-private-value")
    monkeypatch.delenv("ABSENT_KEY", raising=False)
    declarations = parse_declared_shell_commands([{
        "exec": "python3", "path": sys.executable, "script": str(script),
        "pass_env": ["WEATHER_KEY", "ABSENT_KEY"],
    }])
    argv = [sys.executable, str(script)]
    events = []
    from mimir.event_logger import EventLogger

    audit_path = tmp_path / "events.jsonl"
    logger = EventLogger(audit_path, "pass-env-test")

    def record(kind, **fields):
        events.append((kind, fields))
        logger.log_sync(kind, **fields)

    monkeypatch.setattr("mimir.event_logger.log_event_sync", record)
    token = _shell_env.bind_direct_exec_argv(
        argv, command=f"python3 {script}", declared=declarations,
    )
    try:
        env = direct_exec_env_overlay(argv) if overlay else direct_exec_env(argv)
        assert env["WEATHER_KEY"] == "weather-private-value"
        assert not env.get("UNDECLARED_KEY")
        assert "ABSENT_KEY" not in env
        assert "WEATHER_KEY" not in direct_exec_env([*argv, "different-argument"])
    finally:
        _shell_env.reset_direct_exec_argv(token)
    assert "WEATHER_KEY" not in direct_exec_env(argv)
    assert events == [("service_shell_env_passthrough", {"pass_env": ["WEATHER_KEY"]})]
    assert "weather-private-value" not in repr(events)
    audit = audit_path.read_text()
    assert "WEATHER_KEY" in audit
    assert "weather-private-value" not in audit


@pytest.mark.parametrize("overlay", [False, True])
@pytest.mark.parametrize("gh_name", ["gh", "GH", "Gh"])
def test_operator_declared_gh_refused_before_identity_or_credentials(
    tmp_path, monkeypatch, overlay, gh_name,
):
    from mimir.access_control import parse_declared_shell_commands
    from mimir.tools.refusals import ToolPolicyRefusal

    # Construct a binding via the job parser to exercise execution even if a
    # malformed/stale operator declaration somehow survives load validation.
    executable = tmp_path / gh_name
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    declarations = parse_declared_shell_commands([{
        "exec": gh_name, "path": str(executable), "subcommands": [["issue", "list"]],
        "pass_env": ["GITHUB_TOKEN"],
    }])
    monkeypatch.setenv("GITHUB_TOKEN", "explicit-test-token")
    monkeypatch.setattr(
        "mimir.plugins.core.selected_policy_registry",
        lambda: pytest.fail("declared gh reached selected owner"),
    )
    monkeypatch.setattr("mimir.event_logger.log_event_sync", lambda *a, **kw: None)
    argv = [str(executable), "issue", "list"]
    token = _shell_env.bind_direct_exec_argv(
        argv, command=f"{gh_name} issue list", declared=declarations, operator_declared=True,
    )
    try:
        assert _shell_env.direct_exec_pass_env(argv) == ("GITHUB_TOKEN",)
        with pytest.raises(ToolPolicyRefusal, match="operator-declared gh is refused"):
            direct_exec_env_overlay(argv) if overlay else direct_exec_env(argv)
    finally:
        _shell_env.reset_direct_exec_argv(token)


@pytest.mark.parametrize("overlay", [False, True])
@pytest.mark.parametrize("wrapper,prefix,args", [
    ("nice", ["gh", "pr", "view"], ["gh", "pr", "view", "5"]),
    ("timeout", ["5", "gh"], ["5", "gh", "pr", "view", "5"]),
    ("nice", ["timeout"], ["timeout", "5", "/usr/bin/gh", "pr", "view"]),
    ("timeout", ["5"], ["5", "/usr/bin/../bin/gh", "pr", "view"]),
    ("chainlink", ["issue", "search"], ["issue", "search", "gh"]),
])
@pytest.mark.parametrize("gh_name", ["gh", "GH", "Gh"])
def test_operator_declared_wrapped_gh_refused_before_env_or_identity(
    tmp_path, monkeypatch, overlay, wrapper, prefix, args, gh_name,
):
    from mimir.access_control import parse_declared_shell_commands
    from mimir.tools.refusals import ToolPolicyRefusal

    prefix = [token.replace("gh", gh_name) for token in prefix]
    args = [token.replace("gh", gh_name) for token in args]
    executable = tmp_path / wrapper
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    declarations = parse_declared_shell_commands([{
        "exec": wrapper, "path": str(executable), "subcommands": [prefix],
        "pass_env": ["GITHUB_TOKEN"],
    }])
    monkeypatch.setenv("HOME", str(tmp_path / "ambient-home"))
    monkeypatch.setenv("GITHUB_TOKEN", "explicit-test-token")
    monkeypatch.setattr(
        "mimir.plugins.core.selected_policy_registry",
        lambda: pytest.fail("wrapped declared gh reached selected owner"),
    )
    env_builds = []
    monkeypatch.setattr(_shell_env, "_minimal_direct_exec_env",
                        lambda: env_builds.append(True) or {})
    argv = [str(executable), *args]
    token = _shell_env.bind_direct_exec_argv(
        argv, command=" ".join([wrapper, *args]), declared=declarations,
        operator_declared=True,
    )
    try:
        assert _shell_env.direct_exec_pass_env(argv) == ("GITHUB_TOKEN",)
        with pytest.raises(ToolPolicyRefusal, match="operator-declared gh is refused") as caught:
            direct_exec_env_overlay(argv) if overlay else direct_exec_env(argv)
        index = next(i for i, value in enumerate(argv) if Path(value).name == gh_name)
        assert f"argv[{index}]={argv[index]!r}" in str(caught.value)
        assert env_builds == []
    finally:
        _shell_env.reset_direct_exec_argv(token)


def test_declared_environment_requires_matching_pinned_execution(tmp_path, monkeypatch):
    from mimir.access_control import parse_declared_shell_commands

    monkeypatch.setattr("mimir.event_logger.log_event_sync", lambda *a, **kw: None)
    monkeypatch.setenv("DECLARED_KEY", "private-value")
    declared = parse_declared_shell_commands([{
        "exec": "probe", "path": "/bin/echo", "subcommands": [["status"]],
        "pass_env": ["DECLARED_KEY"],
    }])
    argv = ["/unrelated/echo", "status"]
    token = _shell_env.bind_direct_exec_argv(argv, command="probe status", declared=declared)
    try:
        assert "DECLARED_KEY" not in direct_exec_env(argv)
    finally:
        _shell_env.reset_direct_exec_argv(token)


@pytest.mark.parametrize("raw", [None, "TOKEN", {}, ["TOKEN*"], ["TOKEN_" + "*"],
                                      ["TOKEN=value-private"], [""], [1], ["A\nB"]])
def test_pass_env_requires_exact_names_without_echoing_bad_values(raw):
    from mimir.access_control import parse_declared_shell_commands

    with pytest.raises(ValueError, match="list of exact environment variable names") as exc:
        parse_declared_shell_commands([{
            "exec": "probe", "path": "/bin/echo", "subcommands": [["status"]],
            "pass_env": raw,
        }])
    assert "value-private" not in str(exc.value)


@pytest.mark.parametrize("name", ["PATH", "MIMIR_MODEL_SPEC", "BASH_ENV", "ENV",
                                    "LD_PRELOAD", "DYLD_LIBRARY_PATH", "PYTHONPATH",
                                    "GIT_CONFIG_GLOBAL", "GH_TOKEN", "GH_CONFIG_DIR"])
def test_pass_env_cannot_override_execution_or_github_identity(name):
    from mimir.access_control import parse_declared_shell_commands

    with pytest.raises(ValueError, match="process-control environment"):
        parse_declared_shell_commands([{
            "exec": "probe", "path": "/bin/echo", "subcommands": [["status"]],
            "pass_env": [name],
        }])


def test_missing_declared_environment_surfaces_child_error(tmp_path, monkeypatch):
    from mimir.access_control import parse_declared_shell_commands
    from mimir.tools.extra import shell_exec

    monkeypatch.setattr("mimir.event_logger.log_event_sync", lambda *a, **kw: None)
    monkeypatch.delenv("ABSENT_KEY", raising=False)
    script = tmp_path / "missing.py"
    script.write_text("import os, sys\nif 'ABSENT_KEY' not in os.environ:\n"
                      "    sys.exit('ABSENT_KEY not set by operator')\n")
    declared = parse_declared_shell_commands([{
        "exec": "python3", "path": sys.executable, "script": str(script),
        "pass_env": ["ABSENT_KEY"],
    }])
    command = f"python3 {script}"
    token = _shell_env.bind_direct_exec_argv(
        [sys.executable, str(script)], command=command, declared=declared,
    )
    try:
        result = shell_exec.invoke({"command": command})
    finally:
        _shell_env.reset_direct_exec_argv(token)
    assert "exit=1" in result
    assert "ABSENT_KEY not set by operator" in result
    assert "refused" not in result


def test_direct_exec_env_defaults_to_minimal_non_secret_environment(monkeypatch) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", "-q")
    monkeypatch.setenv("PYTEST_PLUGINS", "example")
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("HOME", "/safe/home")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("LC_TEST_SENTINEL", "terminal-setting")

    env = direct_exec_env(["/bin/echo", "status"])

    assert env == {
        "HOME": "/safe/home",
        "LANG": "C.UTF-8",
        "PATH": _shell_env._TRUSTED_PATH,
    } | {
        key: value for key, value in os.environ.items() if key.startswith("LC_")
    }


def test_login_shell_command_keeps_venv_console_scripts_after_system_tools() -> None:
    venv_bin = os.path.dirname(sys.executable)
    wrapped = _shell_env.login_shell_command("mimir --help")
    exported_path = wrapped.splitlines()[0].removeprefix("export PATH=")

    assert exported_path.split(os.pathsep) == [
        *_shell_env._TRUSTED_PATH_DIRS,
        venv_bin,
    ]
    assert wrapped.endswith("\nmimir --help")


def test_direct_exec_env_discards_writable_path_and_does_not_select_decoy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo_root = tmp_path / "repo"
    decoy_dir = repo_root / ".venv" / "bin"
    decoy_dir.mkdir(parents=True)
    decoy = decoy_dir / "pwd"
    decoy.write_text("#!/bin/sh\nprintf 'DECOY\\n'\n", encoding="utf-8")
    decoy.chmod(0o755)
    trusted_dir = tmp_path / "image-root" / "bin"
    trusted_dir.mkdir(parents=True)
    trusted_tool = trusted_dir / "pwd"
    trusted_tool.write_text("#!/bin/sh\nprintf 'SYSTEM\\n'\n", encoding="utf-8")
    trusted_tool.chmod(0o755)
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", f"{repo_root}:rw")
    monkeypatch.setenv("PATH", os.pathsep.join((str(decoy_dir), "/usr/bin", "/bin")))
    monkeypatch.setattr(_shell_env, "_TRUSTED_PATH", str(trusted_dir))

    env = direct_exec_env(["pwd"])
    completed = subprocess.run(
        ["pwd"], capture_output=True, check=True, env=env, cwd=repo_root, text=True,
    )

    assert completed.stdout.strip() == "SYSTEM"
    assert all(
        not Path(entry).resolve().is_relative_to(repo_root.resolve())
        for entry in env["PATH"].split(os.pathsep)
    )


def test_direct_exec_env_uv_run_uses_project_virtualenv(
    tmp_path: Path,
    monkeypatch,
) -> None:
    uv = shutil.which("uv")
    if uv is None:
        import pytest

        pytest.skip("uv is not installed")

    project = tmp_path / "project"
    venv_bin = project / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    venv_bin.mkdir(parents=True)
    pyproject = project / "pyproject.toml"
    pyproject.write_text(
        "[project]\nname = 'uv-path-probe'\nversion = '0.0.0'\n"
        "requires-python = '>=3.11'\n",
        encoding="utf-8",
    )
    pyvenv = project / ".venv" / "pyvenv.cfg"
    # A venv's `home` must name the BASE interpreter's directory. `sys.executable`
    # is only that when pytest itself runs on a base interpreter; under `uv run
    # pytest` it is this project's own `.venv/bin/python`, and a venv derived from
    # it has no stdlib -- uv's Python query then dies with "No module named
    # 'encodings'" and the probe fails for a reason unrelated to what it asserts.
    # `sys._base_executable` is what the stdlib `venv` module writes here.
    base_executable = Path(getattr(sys, "_base_executable", None) or sys.executable)
    pyvenv.write_text(
        f"home = {base_executable.parent}\n"
        f"executable = {base_executable}\n"
        f"version = {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}\n",
        encoding="utf-8",
    )
    venv_python = venv_bin / "python"
    venv_python.symlink_to(base_executable)
    monkeypatch.setattr(_shell_env, "_TRUSTED_PATH", str(Path(uv).parent))
    env = direct_exec_env([uv, "run", "python"])
    env["UV_CACHE_DIR"] = str(tmp_path / "uv-cache")

    completed = subprocess.run(
        [uv, "run", "python", "-c", "import sys; print(sys.prefix)"],
        capture_output=True,
        check=True,
        cwd=project,
        env=env,
        text=True,
    )

    assert Path(completed.stdout.strip()).resolve() == (project / ".venv").resolve()


@pytest.fixture(params=["core", "selected"])
def shell_policy_mode(request):
    """Run a floor assertion with no selected hook and with Cultivate's hook."""
    if request.param == "selected":
        pytest.importorskip("mimir_cultivate")
        from mimir_cultivate import forge as provider_forge

        # Cultivate's degraded latch is process state another test may have set.
        monkeypatch = request.getfixturevalue("monkeypatch")
        monkeypatch.setattr(provider_forge, "_github_identity_degraded", False)
        monkeypatch.setattr(provider_forge, "_github_identity_degraded_error", None)
        monkeypatch.setattr(provider_forge, "_github_identity_degraded_callback", None)
        yield request.getfixturevalue("selected_shell_policy")
        return
    from mimir.plugins.core import core_policy_registry, policy_scope

    with policy_scope(core_policy_registry()) as policy:
        assert policy.policy_consumer_hooks is None
        yield policy


def test_direct_exec_env_scrubs_git_repository_and_helper_injection(
    monkeypatch, shell_policy_mode,
) -> None:
    injected = {
        "GIT_DIR": "/outside/.git",
        "GIT_WORK_TREE": "/outside",
        "GIT_CONFIG_GLOBAL": "/outside/config",
        "GIT_CONFIG_SYSTEM": "/outside/system-config",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.fsmonitor",
        "GIT_CONFIG_VALUE_0": "/outside/helper",
        "GIT_EXEC_PATH": "/outside/git-core",
        "GIT_EXTERNAL_DIFF": "/outside/diff",
    }
    for key, value in injected.items():
        monkeypatch.setenv(key, value)

    env = direct_exec_env(["/usr/bin/git", "status"])
    overlay = direct_exec_env_overlay(["/usr/bin/git", "status"])

    assert all(key not in env for key in injected)
    assert all(overlay[key] is None for key in injected)
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_PAGER"] == "cat"
    assert env["GIT_OPTIONAL_LOCKS"] == "0"


def test_git_uses_minimal_env_and_repository_credentials(monkeypatch, selected_shell_policy) -> None:
    """Only review pull/push need auth, supplied by the configured helper.

    Inspection Git has ``credential.helper=`` injected into argv. Authorized
    review pull/push omit that override and use the operator-installed,
    repository-scoped credential helper; neither case needs a token in env.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setenv("GIT_ASKPASS", "/tmp/credential-helper")

    env = direct_exec_env(["/usr/bin/git", "push", "origin", "topic"])

    assert "GITHUB_TOKEN" not in env
    assert "GIT_ASKPASS" not in env
    assert set(env) <= {
        "PATH", "HOME", "LANG", "TZ", "GIT_CONFIG_NOSYSTEM", "GIT_PAGER",
        "GIT_OPTIONAL_LOCKS",
    } | {key for key in env if key.startswith("LC_")}


def test_gh_env_uses_isolated_config_and_scrubs_alternate_credentials(
    monkeypatch, selected_shell_policy,
) -> None:
    from mimir_cultivate import forge as provider_forge
    from mimir_cultivate import review_guard

    monkeypatch.setenv("GITHUB_TOKEN", "declared-token")
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "reviewer")
    monkeypatch.setenv("GH_TOKEN", "stray-token")
    monkeypatch.setenv("GH_HOST", "attacker.invalid")
    monkeypatch.setenv("GH_CONFIG_DIR", "/tmp/stray-gh-config")
    monkeypatch.setattr(core_identity, "_verified_identity", (
        "reviewer", hashlib.sha256(b"declared-token").hexdigest(),
    ))
    monkeypatch.setattr(provider_forge, "_github_identity_degraded", False)
    monkeypatch.setattr(provider_forge, "_github_identity_degraded_error", None)

    env = direct_exec_env(["/usr/bin/gh", "api", "user"])
    overlay = direct_exec_env_overlay(["/usr/bin/gh", "api", "user"])

    assert env["GITHUB_TOKEN"] == "declared-token"
    assert "GH_TOKEN" not in env
    assert "GH_HOST" not in env
    assert env["GH_CONFIG_DIR"] == review_guard._GH_CONFIG_DIR
    assert env["GH_CONFIG_DIR"] != "/tmp/stray-gh-config"
    assert Path(env["GH_CONFIG_DIR"]).stat().st_mode & 0o777 == 0o500
    assert overlay["GH_TOKEN"] is None
    assert overlay["GH_HOST"] is None


def test_non_gh_direct_exec_scrubs_github_cli_selection(monkeypatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "stray-token")
    monkeypatch.setenv("GH_HOST", "attacker.invalid")
    monkeypatch.setenv("GH_CONFIG_DIR", "/tmp/stray-gh-config")
    monkeypatch.setenv("MIMIR_MODEL_SPEC", "codex-plus:agent-model")

    env = direct_exec_env(["/bin/echo", "status"])
    overlay = direct_exec_env_overlay(["/bin/echo", "status"])

    scrubbed = ("GH_TOKEN", "GH_HOST", "GH_CONFIG_DIR", "MIMIR_MODEL_SPEC")
    assert all(key not in env for key in scrubbed)
    assert all(overlay[key] is None for key in scrubbed)


def test_owner_hook_is_not_applied_to_unrelated_direct_exec(selected_shell_policy, monkeypatch) -> None:
    from dataclasses import replace

    def unexpected(*args):
        raise AssertionError("forge environment hook called for unrelated argv")

    monkeypatch.setattr(selected_shell_policy, "_policy_consumer_hooks", replace(
        selected_shell_policy.policy_consumer_hooks, shell_environment=unexpected,
    ))
    assert subprocess.run(
        ["/bin/echo", "generic-ok"], capture_output=True, text=True,
        env=direct_exec_env(["/bin/echo", "generic-ok"]), check=True,
    ).stdout.strip() == "generic-ok"


def _bind_core_identity(monkeypatch, token: str, login: str = "reviewer") -> None:
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", login)
    monkeypatch.setattr(core_identity, "_verified_identity", (
        login, core_identity.credential_fingerprint(token),
    ))


def test_core_only_gh_drops_declared_credential_without_selected_hook(monkeypatch) -> None:
    """A declaration cannot select gh's credential or host; the process token is it."""
    from mimir.plugins.core import core_policy_registry, policy_scope

    monkeypatch.setenv("GITHUB_TOKEN", "process-token")
    monkeypatch.setenv("DECLARED_TOKEN", "declared-token")
    monkeypatch.setenv("GH_HOST", "untrusted.invalid")
    _bind_core_identity(monkeypatch, "process-token")
    argv = ["/usr/bin/gh", "api", "user"]
    binding = _shell_env._DIRECT_EXEC_ARGV.set(
        _shell_env._DirectExecBinding(tuple(argv), ("GITHUB_TOKEN", "GH_HOST")),
    )
    monkeypatch.setattr("mimir.event_logger.log_event_sync", lambda *args, **kwargs: None)
    try:
        with policy_scope(core_policy_registry()):
            env = direct_exec_env(argv)
            overlay = direct_exec_env_overlay(argv)
    finally:
        _shell_env._DIRECT_EXEC_ARGV.reset(binding)
    assert env["GITHUB_TOKEN"] == "process-token" and "GH_HOST" not in env
    assert overlay["GH_HOST"] is None
    assert env["GH_CONFIG_DIR"] == _shell_env._GH_CONFIG_DIR


def test_gh_gets_process_token_isolated_config_and_no_ambient_auth(
    tmp_path: Path, monkeypatch, shell_policy_mode,
) -> None:
    """Core-only and selected gh: process token, empty isolated config, no ambient auth."""
    home = tmp_path / "home"
    (home / ".config" / "gh").mkdir(parents=True)
    (home / ".config" / "gh" / "hosts.yml").write_text(
        "github.com:\n    oauth_token: ambient-token\n", encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GITHUB_TOKEN", "process-token")
    monkeypatch.setenv("GH_TOKEN", "stray-token")
    monkeypatch.setenv("GH_HOST", "attacker.invalid")
    monkeypatch.setenv("GH_CONFIG_DIR", str(home / ".config" / "gh"))
    _bind_core_identity(monkeypatch, "process-token")
    argv = ["/usr/bin/gh", "pr", "view", "1"]
    env = direct_exec_env(argv)
    overlay = direct_exec_env_overlay(argv)

    config_dir = Path(env["GH_CONFIG_DIR"])
    assert env["GITHUB_TOKEN"] == "process-token"
    if shell_policy_mode.policy_consumer_hooks is None:
        assert env["GH_CONFIG_DIR"] == _shell_env._GH_CONFIG_DIR
    assert config_dir.stat().st_mode & 0o777 == 0o500
    assert list(config_dir.iterdir()) == []
    assert not config_dir.resolve().is_relative_to(home.resolve())
    assert {"GH_TOKEN", "GH_HOST"}.isdisjoint(env)
    assert overlay["GITHUB_TOKEN"] == "process-token"
    assert overlay["GH_TOKEN"] is None and overlay["GH_HOST"] is None


@pytest.mark.parametrize("binding", ["other-login", "other-token", "unset-token"])
def test_gh_identity_mismatch_refuses(monkeypatch, shell_policy_mode, binding) -> None:
    from mimir.tools.refusals import ToolPolicyRefusal

    monkeypatch.setenv("GITHUB_TOKEN", "process-token")
    if binding == "other-login":
        _bind_core_identity(monkeypatch, "process-token", login="someone-else")
        monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "reviewer")
    elif binding == "other-token":
        _bind_core_identity(monkeypatch, "rotated-token")
    else:
        _bind_core_identity(monkeypatch, "process-token")
        monkeypatch.delenv("GITHUB_TOKEN")

    def unexpected(token):
        raise AssertionError("a bound identity must not re-query the forge")

    monkeypatch.setattr(core_identity, "fetch_github_login", unexpected)
    with pytest.raises(ToolPolicyRefusal, match="identity verification failed"):
        direct_exec_env(["/usr/bin/gh", "pr", "view", "1"])


@pytest.mark.parametrize("hook_dir", ["ambient", "writable", "missing", "dropped"])
def test_selected_hook_cannot_weaken_core_gh_and_git_floor(
    tmp_path: Path, monkeypatch, hook_dir: str,
) -> None:
    """A hook may substitute a stronger environment, never a weaker one."""
    from types import SimpleNamespace

    from mimir.plugins.core import _selected_policy

    # Non-writable, so only its being the ambient selection disqualifies it.
    ambient = tmp_path / "ambient-gh-config"
    ambient.mkdir()
    ambient.chmod(0o500)
    writable = tmp_path / "writable-gh-config"
    writable.mkdir()
    monkeypatch.setenv("GH_CONFIG_DIR", str(ambient))
    monkeypatch.setenv("GITHUB_TOKEN", "hook-granted")
    _bind_core_identity(monkeypatch, "hook-granted")
    returned = {
        "ambient": str(ambient), "writable": str(writable),
        "missing": str(tmp_path / "missing"), "dropped": None,
    }[hook_dir]

    def weakening(argv, env):
        if Path(argv[0]).name == "git":
            return {key: value for key, value in env.items()
                    if not key.startswith("GIT_")} | {"GIT_CONFIG_NOSYSTEM": "0"}
        env = {key: value for key, value in env.items() if key != "GH_CONFIG_DIR"} | {
            "GH_HOST": "attacker.invalid",
        }
        return env if returned is None else env | {"GH_CONFIG_DIR": returned}

    policy = SimpleNamespace(
        policy_consumer_hooks=SimpleNamespace(shell_environment=weakening),
    )
    token = _selected_policy.set(policy)
    try:
        gh = direct_exec_env(["/usr/bin/gh", "pr", "view", "1"])
        git = direct_exec_env(["/usr/bin/git", "status"])
    finally:
        _selected_policy.reset(token)

    assert gh["GITHUB_TOKEN"] == "hook-granted"
    assert "GH_HOST" not in gh
    assert gh["GH_CONFIG_DIR"] == _shell_env._GH_CONFIG_DIR
    assert {key: git[key] for key in _shell_env.GIT_ENV_PINS} == _shell_env.GIT_ENV_PINS


def test_selected_hook_cannot_swap_in_an_unverified_gh_token(monkeypatch) -> None:
    """The identity check runs on the token the child will actually receive."""
    from types import SimpleNamespace

    from mimir.plugins.core import _selected_policy
    from mimir.tools.refusals import ToolPolicyRefusal

    monkeypatch.setenv("GITHUB_TOKEN", "process-token")
    _bind_core_identity(monkeypatch, "process-token")
    policy = SimpleNamespace(policy_consumer_hooks=SimpleNamespace(
        shell_environment=lambda argv, env: env | {"GITHUB_TOKEN": "swapped-token"},
    ))
    token = _selected_policy.set(policy)
    try:
        with pytest.raises(ToolPolicyRefusal, match="does not match active credential"):
            direct_exec_env(["/usr/bin/gh", "pr", "view", "1"])
    finally:
        _selected_policy.reset(token)


def test_git_probe_env_matches_executed_git_pins() -> None:
    from mimir import access_control

    probe = access_control._maintenance_git_probe_env()
    executed = direct_exec_env(["/usr/bin/git", "status"])
    for key, value in _shell_env.GIT_ENV_PINS.items():
        assert probe[key] == executed[key] == value


def test_jq_direct_exec_env_contains_only_non_secret_process_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JQ_ENV_SENTINEL", "super-secret-value")
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    monkeypatch.setenv("HOME", "/safe/home")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("LC_TIME", "C")
    monkeypatch.setenv("TZ", "UTC")

    argv = ["/usr/bin/jq", "--null-input", "env"]
    env = direct_exec_env(argv)
    overlay = direct_exec_env_overlay(argv)

    assert all(
        key in {"PATH", "HOME", "LANG", "TZ"} or key.startswith("LC_")
        for key in env
    )
    assert env["PATH"] == _shell_env._TRUSTED_PATH
    assert env["HOME"] == "/safe/home"
    assert env["LANG"] == "en_US.UTF-8"
    assert env["LC_TIME"] == "C"
    assert env["TZ"] == "UTC"
    assert "JQ_ENV_SENTINEL" not in env
    assert "GITHUB_TOKEN" not in env
    assert overlay["JQ_ENV_SENTINEL"] is None
    assert overlay["GITHUB_TOKEN"] is None
    assert overlay["PYTHONUNBUFFERED"] is None


def test_every_pinned_service_command_inherits_safe_default(
    monkeypatch: pytest.MonkeyPatch,
    maintenance_pinned_executables: dict[str, Path],
    selected_shell_policy,
) -> None:
    from mimir import access_control

    monkeypatch.setenv("SERVICE_ENV_SENTINEL", "secret")
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")

    for command in access_control._MAINTENANCE_PINNED_EXECUTABLE_DEFAULTS:
        if command == "gh":
            continue
        argv = [str(maintenance_pinned_executables[command])]
        env = direct_exec_env(argv)
        expected = _shell_env._minimal_direct_exec_env()
        if command == "git":
            expected.update({
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_PAGER": "cat",
                "GIT_OPTIONAL_LOCKS": "0",
            })
        assert "SERVICE_ENV_SENTINEL" not in env, command
        assert "GITHUB_TOKEN" not in env, command
        assert env == expected, (command, env)


def _run_admitted_proc_reader(command: str) -> bytes:
    """Fork so ``{pid}`` can name the process that will exec the reader."""
    from mimir.access_control import parse_service_shell_argv

    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(read_fd)
            os.dup2(write_fd, 1)
            os.close(write_fd)
            argv = parse_service_shell_argv(
                command.format(pid=os.getpid()), "maintenance",
            )
            if argv is None:
                os._exit(120)
            os.execve(argv[0], argv, direct_exec_env(argv))
        except BaseException:
            os._exit(121)

    os.close(write_fd)
    chunks = []
    while chunk := os.read(read_fd, 65536):
        chunks.append(chunk)
    os.close(read_fd)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    return b"".join(chunks)


@pytest.mark.skipif(not Path("/proc/self/environ").exists(), reason="Linux /proc required")
@pytest.mark.parametrize("reader", ["cat", "head -c 65536", "tail -c 65536"])
@pytest.mark.parametrize(
    "operand", ["/proc/self/environ", "/proc/self/../self/environ", "/proc/{pid}/environ"],
)
def test_admitted_file_readers_cannot_disclose_child_environment(
    reader: str,
    operand: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir import access_control

    for command in ("cat", "head", "tail"):
        executable = Path(shutil.which(command) or "").resolve(strict=True)
        monkeypatch.setitem(access_control._MAINTENANCE_PINNED_EXECUTABLES, command, executable)
    sentinel = b"SERVICE_PROC_ENV_SENTINEL=super-secret-value"
    monkeypatch.setenv("SERVICE_PROC_ENV_SENTINEL", "super-secret-value")

    output = _run_admitted_proc_reader(f"{reader} {operand}")

    assert sentinel not in output


@pytest.mark.parametrize(
    ("command", "expected"),
    [("cat", "alpha\nbeta\n"), ("head -c 5", "alpha"), ("tail -c 5", "beta\n")],
)
def test_admitted_file_readers_still_read_repository_files(
    command: str,
    expected: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mimir import access_control
    from mimir.access_control import parse_service_shell_argv

    executable_name = command.split()[0]
    executable = Path(shutil.which(executable_name) or "").resolve(strict=True)
    monkeypatch.setitem(
        access_control._MAINTENANCE_PINNED_EXECUTABLES, executable_name, executable,
    )
    sample = tmp_path / "sample.txt"
    sample.write_text("alpha\nbeta\n", encoding="utf-8")
    argv = parse_service_shell_argv(
        f"{command} {shlex.quote(str(sample))}", "maintenance",
    )

    assert argv is not None
    completed = subprocess.run(
        argv, capture_output=True, check=True, env=direct_exec_env(argv), text=True,
    )
    assert completed.stdout == expected


def test_real_jq_cannot_read_parent_credentials_and_still_filters_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jq = shutil.which("jq")
    if jq is None:
        pytest.skip("jq is not installed")

    sample = tmp_path / "sample.json"
    sample.write_text('{"name": "Ada"}\n', encoding="utf-8")
    sentinel = "super-secret-value-12345"
    monkeypatch.setenv("JQ_ENV_SENTINEL", sentinel)
    monkeypatch.setenv("GITHUB_TOKEN", sentinel)
    commands = (
        [jq, "--null-input", "env"],
        [jq, "env", str(sample)],
        [jq, "$ENV.GITHUB_TOKEN", str(sample)],
        [jq, "-r", "$ENV|tostring", str(sample)],
    )

    for argv in commands:
        completed = subprocess.run(
            argv, capture_output=True, check=True, env=direct_exec_env(argv), text=True,
        )
        assert sentinel not in completed.stdout, argv

    legitimate = [jq, "-r", ".name", str(sample)]
    completed = subprocess.run(
        legitimate,
        capture_output=True,
        check=True,
        env=direct_exec_env(legitimate),
        text=True,
    )
    assert completed.stdout == "Ada\n"


@pytest.mark.parametrize("arguments", [
    ["api", "user"],
    ["pr", "view", "17"],
    ["pr", "review", "17", "--approve"],
])
def test_every_gh_command_requires_cached_declared_identity(
    monkeypatch, selected_shell_policy, arguments,
) -> None:
    from mimir_cultivate import forge as provider_forge
    from mimir.tools.refusals import ToolPolicyRefusal

    monkeypatch.setenv("GITHUB_TOKEN", "declared-token")
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "reviewer")
    monkeypatch.setattr(core_identity, "_verified_identity", None)
    monkeypatch.setattr(provider_forge, "_github_identity_degraded", False)
    monkeypatch.setattr(provider_forge, "_github_identity_degraded_error", None)
    argv = ["/usr/bin/gh", *arguments]

    with pytest.raises(ToolPolicyRefusal, match="cache is empty"):
        direct_exec_env(argv)
    assert provider_forge.github_identity_is_degraded() is True

    monkeypatch.setattr(core_identity, "_verified_identity", (
        "reviewer", hashlib.sha256(b"declared-token").hexdigest(),
    ))
    with pytest.raises(ToolPolicyRefusal, match="disabled until restart"):
        direct_exec_env(argv)
