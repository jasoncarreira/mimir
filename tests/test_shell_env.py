"""Tests for trusted-service direct-exec environment hardening."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from mimir.tools import _shell_env
from mimir.tools._shell_env import direct_exec_env, direct_exec_env_overlay


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
    sibling = subprocess.Popen(
        [sys.executable, "-c", script, subcommand], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
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
    tmp_path, monkeypatch, async_job, exit_code,
):
    from mimir.forge import github as github_module
    from mimir.shell_jobs import ShellJobRegistry
    from mimir.tools import forge as forge_tools, shell_async
    from mimir.tools.extra import shell_exec

    secret = "opaque-private-value-1666"
    monkeypatch.setenv("GITHUB_TOKEN", secret)
    monkeypatch.setenv("GH_TOKEN", "ungranted-alternate")
    monkeypatch.setenv("UNDECLARED_TOKEN", "ungranted-private")
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "reviewer")
    monkeypatch.setattr(github_module, "_verified_identity", (
        "reviewer", hashlib.sha256(secret.encode()).hexdigest(),
    ))
    monkeypatch.setattr(forge_tools, "_github_identity_degraded", False)
    monkeypatch.setattr(forge_tools, "_github_identity_degraded_error", None)
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
    from mimir.tools import forge as forge_tools
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
    monkeypatch.setattr(forge_tools, "_github_identity_degraded", False)
    monkeypatch.setattr(forge_tools, "_github_identity_degraded_error", None)
    checks = []
    monkeypatch.setattr(forge_tools, "confirm_github_tool_identity",
                        lambda *args: checks.append(args))
    monkeypatch.setattr("mimir.event_logger.log_event_sync", lambda *a, **kw: None)
    argv = [str(executable), "issue", "list"]
    token = _shell_env.bind_direct_exec_argv(
        argv, command=f"{gh_name} issue list", declared=declarations, operator_declared=True,
    )
    try:
        assert _shell_env.direct_exec_pass_env(argv) == ("GITHUB_TOKEN",)
        with pytest.raises(ToolPolicyRefusal, match="operator-declared gh is refused"):
            direct_exec_env_overlay(argv) if overlay else direct_exec_env(argv)
        assert checks == []
        assert not forge_tools.github_identity_is_degraded()
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
    from mimir.tools import forge as forge_tools
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
    monkeypatch.setattr(forge_tools, "_github_identity_degraded", False)
    monkeypatch.setattr(forge_tools, "_github_identity_degraded_error", None)
    checks = []
    env_builds = []
    monkeypatch.setattr(forge_tools, "confirm_github_tool_identity",
                        lambda *args: checks.append(args))
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
        assert checks == []
        assert not forge_tools.github_identity_is_degraded()
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


def test_direct_exec_env_scrubs_git_repository_and_helper_injection(monkeypatch) -> None:
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


def test_git_uses_minimal_env_and_repository_credentials(monkeypatch) -> None:
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


def test_gh_env_uses_isolated_config_and_scrubs_alternate_credentials(monkeypatch) -> None:
    from mimir.forge import github as github_module

    monkeypatch.setenv("GITHUB_TOKEN", "declared-token")
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "reviewer")
    monkeypatch.setenv("GH_TOKEN", "stray-token")
    monkeypatch.setenv("GH_HOST", "attacker.invalid")
    monkeypatch.setenv("GH_CONFIG_DIR", "/tmp/stray-gh-config")
    monkeypatch.setattr(github_module, "_verified_identity", (
        "reviewer", hashlib.sha256(b"declared-token").hexdigest(),
    ))
    from mimir.tools import forge as forge_tools
    monkeypatch.setattr(forge_tools, "_github_identity_degraded", False)
    monkeypatch.setattr(forge_tools, "_github_identity_degraded_error", None)

    env = direct_exec_env(["/usr/bin/gh", "api", "user"])
    overlay = direct_exec_env_overlay(["/usr/bin/gh", "api", "user"])

    assert env["GITHUB_TOKEN"] == "declared-token"
    assert "GH_TOKEN" not in env
    assert "GH_HOST" not in env
    assert env["GH_CONFIG_DIR"] == _shell_env._GH_CONFIG_DIR
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
) -> None:
    from mimir import access_control

    monkeypatch.setenv("SERVICE_ENV_SENTINEL", "secret")
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")

    assert set(_shell_env._CREDENTIAL_ENV_BY_EXECUTABLE) == {"gh"}
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
def test_every_gh_command_requires_cached_declared_identity(monkeypatch, arguments) -> None:
    from mimir.forge import github as github_module
    from mimir.tools import forge as forge_tools
    from mimir.tools.refusals import ToolPolicyRefusal

    monkeypatch.setenv("GITHUB_TOKEN", "declared-token")
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "reviewer")
    monkeypatch.setattr(github_module, "_verified_identity", None)
    monkeypatch.setattr(forge_tools, "_github_identity_degraded", False)
    monkeypatch.setattr(forge_tools, "_github_identity_degraded_error", None)
    argv = ["/usr/bin/gh", *arguments]

    with pytest.raises(ToolPolicyRefusal, match="cache is empty"):
        direct_exec_env(argv)
    assert forge_tools.github_identity_is_degraded() is True

    monkeypatch.setattr(github_module, "_verified_identity", (
        "reviewer", hashlib.sha256(b"declared-token").hexdigest(),
    ))
    with pytest.raises(ToolPolicyRefusal, match="disabled until restart"):
        direct_exec_env(argv)
