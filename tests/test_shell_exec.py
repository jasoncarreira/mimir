"""Tests for mimir.tools.extra:shell_exec (chainlink #226 + the 2026-06
shell-wrapper fix).

Pins the trust posture of ``shell_exec``: the agent's shell tools
(``shell_exec`` + ``bash_async``) are intentionally unrestricted within
the trusted container. There is no allowlist gate. ``set_shell_allowlist``
was a deepagents-migration PoC affordance that was never wired and has
been removed; these tests defend against re-introducing a half-wired gate.

shell_exec runs via ``bash -lc`` (a real shell, matching ``bash_async``),
so shell syntax — cd-chains, pipes, redirects, env expansion — works; the
prohibited-action guard middleware (not an in-process parse) screens
commands. These tests pin that capability so a future refactor doesn't
silently revert to the shlex+shell=False path that broke it.
"""
from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import mimir.tools.extra as extra
from mimir.tools.extra import shell_exec


@pytest.fixture(autouse=True)
def reset_shell_state(monkeypatch, tmp_path):
    old_cwds = dict(extra._SHELL_STATE["cwd_by_session"])
    old_timeout = extra._SHELL_STATE["timeout_s"]
    extra._SHELL_STATE["cwd_by_session"].clear()
    extra._SHELL_STATE["timeout_s"] = 60.0
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    try:
        yield
    finally:
        extra._SHELL_STATE["cwd_by_session"].clear()
        extra._SHELL_STATE["cwd_by_session"].update(old_cwds)
        extra._SHELL_STATE["timeout_s"] = old_timeout


@contextmanager
def _shell_session(session_id: str):
    from mimir._context import reset_current_turn, set_current_turn
    from mimir.models import TurnContext

    token = set_current_turn(TurnContext(
        turn_id=f"turn-{session_id}",
        session_id=session_id,
        trigger="user_message",
        channel_id=session_id,
        started_at=time.time(),
    ))
    try:
        yield
    finally:
        reset_current_turn(token)


def test_shell_exec_runs_arbitrary_command_in_default_state():
    """Out of the box, shell_exec runs commands without any allowlist gate."""
    result = shell_exec.invoke({"command": "echo chainlink-226"})
    assert "exit=0" in result
    assert "chainlink-226" in result


def test_shell_exec_does_not_emit_rejection_message_for_unfamiliar_command():
    """The previous allowlist gate would return a 'rejected: ... does not
    match any allowlist prefix' string. After chainlink #226, no gate
    exists — assert that surface is gone so future refactors don't
    silently revive a half-wired allowlist.
    """
    result = shell_exec.invoke({"command": "printf foo"})
    assert "rejected" not in result
    assert "allowlist" not in result


def test_shell_exec_description_routes_broad_service_searches_to_typed_grep():
    """The model-facing surface must state the hard recursive-read boundary.

    Service turns see the tool description, not deployment notes. Keep the
    entry/byte caps and the usable fallback there so repo-wide grep commands
    are not repeatedly generated only to fail admission.
    """
    description = shell_exec.description

    assert "256 entries" in description
    assert "8 MiB" in description
    assert "typed ``grep`` tool" in description


def test_set_shell_allowlist_no_longer_exists_on_public_surface():
    """chainlink #226: the dead setter must not be re-exported from
    mimir.tools — the trust model is documented in shell_exec's docstring
    and a future restore should require an explicit decision."""
    import mimir.tools as tools

    assert not hasattr(tools, "set_shell_allowlist"), (
        "set_shell_allowlist was removed in chainlink #226; if you need a "
        "shell gate, gate both shell_exec AND bash_async — not just one."
    )


def test_set_shell_allowlist_not_in_tools_extra_module():
    """Belt-and-braces: the underlying module-level helper is gone too."""
    import mimir.tools.extra as extra

    assert not hasattr(extra, "set_shell_allowlist")
    assert "allowlist" not in extra._SHELL_STATE


def test_shell_exec_still_blocks_empty_command():
    """Argument-shape guard survives — only the allowlist gate was removed."""
    result = shell_exec.invoke({"command": ""})
    assert "command is required" in result


def test_shell_exec_tolerates_non_utf8_stdout():
    """Binary-ish command output must not crash the whole agent turn.

    Heartbeat #470 failures surfaced as bare UnicodeDecodeError records when
    shell commands encountered non-UTF-8 local artifacts (for example grep/find
    probes that crossed binary files). Decode lossy display output with
    replacement instead of letting subprocess text mode raise.
    """
    result = shell_exec.invoke({"command": "printf '\\247'"})

    assert "exit=0" in result
    assert "�" in result


def test_shell_exec_expands_shell_syntax():
    """shell-wrapper fix: shell_exec runs via bash -lc, so shell syntax is
    honored — env vars expand (this test used to pin the OPPOSITE under the
    shlex+shell=False path)."""
    result = shell_exec.invoke({"command": "echo $HOME"})
    assert "exit=0" in result
    # bash -lc expands $HOME — the literal token must NOT survive in stdout.
    assert "$HOME" not in result.split("stdout:")[-1]
    # arithmetic expansion is an env-independent proof of shell parsing.
    assert "42" in shell_exec.invoke({"command": "echo $((6 * 7))"})


def test_shell_exec_supports_cd_chains_and_pipes():
    """cd-chains and pipes work now (the && chain + pipe were swallowed as
    literal args under shell=False)."""
    out = shell_exec.invoke({"command": "cd /tmp && pwd"})
    assert "exit=0" in out
    assert "/tmp" in out  # cd took effect; the && chain ran
    piped = shell_exec.invoke({"command": "echo hello | tr a-z A-Z"})
    assert "exit=0" in piped
    assert "HELLO" in piped


def test_shell_exec_initial_cwd_defaults_to_mimir_home(monkeypatch, tmp_path):
    """Relative commands start in MIMIR_HOME, not the server process cwd."""
    home = tmp_path / "home"
    launch_cwd = tmp_path / "s6-service-dir"
    script = home / "scripts" / "show_home_cwd.py"
    script.parent.mkdir(parents=True)
    launch_cwd.mkdir()
    script.write_text(
        "from pathlib import Path\n"
        "print('script-cwd=' + Path.cwd().name)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.chdir(launch_cwd)

    result = shell_exec.invoke({"command": "python3 scripts/show_home_cwd.py"})

    assert "exit=0" in result
    assert "script-cwd=home" in result


def test_shell_exec_accepts_explicit_cwd(tmp_path):
    target = tmp_path / "explicit"
    target.mkdir()

    result = shell_exec.invoke({"command": "pwd", "cwd": str(target)})

    assert "exit=0" in result
    assert str(target) in result


@pytest.mark.parametrize("kind", ["missing", "file"])
@pytest.mark.parametrize("direct", [False, True])
def test_shell_exec_invalid_cwd_reports_directory_error(tmp_path, kind, direct):
    target = tmp_path / "private-cwd"
    if kind == "file":
        target.write_text("not a directory")
    args = {"command": "pwd", "cwd": str(target)}
    if direct:
        args["mimir_direct_argv"] = [sys.executable, "-c", "import os; print(os.getcwd())"]

    result = shell_exec.invoke(args)

    reason = "not found" if kind == "missing" else "not a directory"
    assert result == f"shell_exec failed: working directory {reason}"
    assert str(target) not in result


def test_shell_exec_missing_direct_executable(tmp_path):
    result = shell_exec.invoke({
        "command": "missing-tool",
        "cwd": str(tmp_path),
        "mimir_direct_argv": [str(tmp_path / "private-missing-tool")],
    })

    assert result == "shell_exec failed: executable could not be started (file not found)"


def test_shell_exec_valid_direct_executable_and_cwd(tmp_path):
    result = shell_exec.invoke({
        "command": "pwd",
        "cwd": str(tmp_path),
        "mimir_direct_argv": [sys.executable, "-c", "import os; print(os.getcwd())"],
    })

    assert result == f"exit=0\n\nstdout:\n{tmp_path}\n"


@pytest.mark.parametrize("filename", [None, "private-unrelated-path", "same-target"])
@pytest.mark.parametrize("error_type", [FileNotFoundError, NotADirectoryError])
def test_shell_exec_ambiguous_launch_error_is_redacted(monkeypatch, filename, error_type):
    def fail(*args, **kwargs):
        code = errno.ENOENT if error_type is FileNotFoundError else errno.ENOTDIR
        raise error_type(code, "private exception detail", filename)

    monkeypatch.setattr(extra.subprocess, "run", fail)
    result = shell_exec.invoke({
        "command": "pwd", "cwd": "same-target",
        "mimir_direct_argv": ["same-target"],
    })

    assert result == "shell_exec failed: process could not be started (check working directory and executable)"


def test_shell_exec_standalone_cd_persists_for_later_calls(tmp_path):
    """A successful standalone cd updates the cwd used by later shell calls."""
    target = tmp_path / "workspace"
    target.mkdir()

    with _shell_session("channel-a"):
        cd_out = shell_exec.invoke({"command": f"cd {target}"})
    # A later turn in the same conversation session keeps the interactive cwd.
    with _shell_session("channel-a"):
        pwd_out = shell_exec.invoke({"command": "pwd"})

    assert "exit=0" in cd_out
    assert "exit=0" in pwd_out
    assert str(target) in pwd_out


def test_shell_exec_sticky_cwd_is_scoped_to_session(tmp_path):
    target = tmp_path / "channel-a-workspace"
    target.mkdir()

    with _shell_session("channel-a"):
        assert "exit=0" in shell_exec.invoke({"command": f"cd {target}"})
        assert str(target) in shell_exec.invoke({"command": "pwd"})
    with _shell_session("channel-b"):
        other = shell_exec.invoke({"command": "pwd"})

    assert "exit=0" in other
    assert str(target) not in other


def test_shell_exec_session_cwd_map_is_bounded_lru(tmp_path, monkeypatch):
    monkeypatch.setattr(extra, "SHELL_CWD_SESSION_MAX", 2)
    targets = []
    for name in ("a", "b", "c"):
        target = tmp_path / name
        target.mkdir()
        targets.append(target)

    extra._remember_shell_cwd("session-a", targets[0])
    extra._remember_shell_cwd("session-b", targets[1])
    assert extra._remembered_shell_cwd("session-a") == targets[0]
    extra._remember_shell_cwd("session-c", targets[2])

    assert list(extra._SHELL_STATE["cwd_by_session"]) == ["session-a", "session-c"]
    assert extra._remembered_shell_cwd("session-b") is None


def test_authorized_direct_argv_never_inherits_session_cwd(tmp_path, monkeypatch):
    target = tmp_path / "interactive-workspace"
    target.mkdir()
    with _shell_session("channel-a"):
        assert "exit=0" in shell_exec.invoke({"command": f"cd {target}"})

    calls: list[dict[str, object]] = []

    def _run(_argv, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(extra.subprocess, "run", _run)
    monkeypatch.setattr(
        extra, "_run_bounded_project_test",
        lambda *args, **kwargs: pytest.fail("unconfigured command used project-test runner"),
    )
    with _shell_session("channel-a"):
        result = shell_exec.invoke({
            "command": "git status --short",
            "mimir_direct_argv": ["/usr/bin/git", "status", "--short"],
        })

    assert "exit=0" in result
    assert calls[-1]["cwd"] is None


def test_shell_exec_supports_redirects(tmp_path):
    """Redirects write files now (``>`` was a literal arg before)."""
    target = tmp_path / "se_redirect.txt"
    out = shell_exec.invoke(
        {"command": f"echo redirected > {target} && cat {target}"}
    )
    assert "exit=0" in out
    assert "redirected" in out
    assert target.read_text().strip() == "redirected"


def test_shell_exec_surfaces_bash_syntax_error():
    """A genuinely malformed command (unterminated quote) now surfaces as a
    non-zero bash exit, not the old shlex 'shell-parse error'."""
    result = shell_exec.invoke({"command": "echo \"unterminated"})
    assert "exit=0" not in result  # bash reports the syntax error
    assert "shell-parse error" not in result  # the shlex path is gone


def test_configured_project_test_timeout_is_named_and_output_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    maintenance_pinned_executables: dict[str, Path],
) -> None:
    from mimir.tools._shell_env import bind_direct_exec_argv, reset_direct_exec_argv

    home = tmp_path / "home"
    repo = tmp_path / "repo"
    home.mkdir()
    repo.mkdir()
    monkeypatch.setenv("MIMIR_HOME", str(home))
    executable = maintenance_pinned_executables["uv"]
    argv = [str(executable), "test"]
    monkeypatch.setenv("MIMIR_FILE_TOOL_ROOTS", f"{repo}:rw")
    monkeypatch.setenv(
        "MIMIR_PROJECT_TEST_COMMAND",
        json.dumps({"argv": argv, "cwd": str(repo)}),
    )
    token = bind_direct_exec_argv(argv)
    try:
        monkeypatch.setattr(
            extra,
            "_run_bounded_project_test",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                subprocess.TimeoutExpired(argv, 60)
            ),
        )
        timeout = shell_exec.invoke({"command": f"{executable} test"})

        monkeypatch.setattr(
            extra,
            "_run_bounded_project_test",
            lambda *_args, **_kwargs: SimpleNamespace(
                returncode=1, stdout=b"o" * 5000, stderr=b"e" * 3000,
            ),
        )
        output = shell_exec.invoke({"command": f"{executable} test"})
    finally:
        reset_direct_exec_argv(token)

    assert "project_test_timeout" in timeout
    assert "shell stdout truncated" in output
    assert "shell stderr truncated" in output
    assert len(output) < 7000


def test_project_test_capture_discards_output_past_hard_byte_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes = []
    popen = subprocess.Popen

    def launch(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(extra.subprocess, "Popen", launch)
    produced = tmp_path / "produced"
    try:
        completed = extra._run_bounded_project_test(
            [sys.executable, "-c",
             "import sys; from pathlib import Path; "
             "count = sys.stdout.write('x' * 1000000); sys.stdout.flush(); "
             "Path(sys.argv[1]).write_text(str(count))", str(produced)],
            cwd=tmp_path,
            # No startup-inclusive stage deadline: pytest bounds the entire test.
            timeout=None,
            env=dict(os.environ),
        )

        assert len(processes) == 1
        assert processes[0].poll() == 0
        assert completed.returncode == 0
        assert produced.read_text() == "1000000"
        assert len(completed.stdout) == extra._PROJECT_TEST_CAPTURE_BYTES
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()
            process.stderr.close()


# ─── trusted system tools + venv-bin fallback on PATH ────────────────


def test_login_shell_command_puts_system_tools_before_venv_bin():
    """The free-form login shell keeps ``mimir`` and venv Python reachable.

    Root-owned system directories come first so an agent-writable virtualenv
    cannot shadow tools such as Git or GitHub CLI.
    """
    import os
    import sys

    from mimir.tools._shell_env import _TRUSTED_PATH, login_shell_command

    cmd = "mimir reflection introspection-report --days 7"
    wrapped = login_shell_command(cmd)
    venv_bin = os.path.dirname(sys.executable)
    expected_path = os.pathsep.join((_TRUSTED_PATH, venv_bin))

    assert wrapped.startswith(f"export PATH={expected_path}")
    assert expected_path.split(os.pathsep)[-1] == venv_bin
    # The original command is preserved verbatim at the tail.
    assert wrapped.endswith("\n" + cmd)


def test_shell_exec_puts_venv_bin_after_system_tools_on_path():
    """End-to-end: login initialization cannot drop the venv fallback."""
    import os
    import sys

    from mimir.tools._shell_env import _TRUSTED_PATH

    venv_bin = os.path.dirname(sys.executable)
    expected_path = os.pathsep.join((_TRUSTED_PATH, venv_bin))
    out = shell_exec.invoke({"command": 'echo "PATHCHECK:$PATH"'})

    assert f"PATHCHECK:{expected_path}" in out


@pytest.mark.asyncio
async def test_service_shell_exec_graph_executes_server_bound_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    maintenance_pinned_executables: dict[str, Path],
) -> None:
    """The real deepagents tool path must deliver only the authorized argv."""
    from deepagents import create_deep_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage, HumanMessage

    from mimir._deepagents_patches import install_deepagents_grep_context_tool
    from mimir.access_control import ToolRegistry, create_auth_context
    from mimir.models import AgentEvent, InformationFlowLabels
    from mimir.tools._shell_env import direct_exec_env
    from mimir.tools.budget_gate import BudgetGateMiddleware

    class _ToolCallingFakeModel(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):  # noqa: ARG002
            return self

    home = tmp_path / "home"
    home.mkdir()
    subprocess.run(["git", "init", "-q", str(home)], check=True)
    monkeypatch.setenv("MIMIR_HOME", str(home))
    monkeypatch.delenv("MIMIR_FILE_TOOL_ROOTS", raising=False)
    command = f"git -C {home} status --short"
    auth = create_auth_context(
        AgentEvent(
            trigger="scheduled_tick",
            channel_id="scheduler:test",
            service_principal="scheduler",
        ),
        enforce=True,
        ifc_labels=InformationFlowLabels(),
    )
    decision = ToolRegistry().authorize_tool(
        "shell_exec", auth, enforce=True, target_channel=command,
    )
    assert decision.allowed is True

    calls: list[tuple[list[str], dict[str, object]]] = []

    def _run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(extra.subprocess, "run", _run)
    monkeypatch.setattr(
        "mimir.tools.budget_gate._emit_event_sync", lambda *_args, **_kwargs: None,
    )
    model = _ToolCallingFakeModel(messages=iter([
        AIMessage(content="", tool_calls=[{
            "name": "shell_exec",
            "args": {
                "command": command,
                "mimir_direct_argv": ["/bin/sh", "-c", "touch /tmp/forged"],
            },
            "id": "tc-service-shell", "type": "tool_call",
        }]),
        AIMessage(content="done"),
    ]))
    install_deepagents_grep_context_tool()
    agent = create_deep_agent(
        model=model,
        tools=[shell_exec],
        system_prompt="test",
        middleware=[BudgetGateMiddleware()],
        context_schema=type(auth),
    )

    await agent.ainvoke(
        {"messages": [HumanMessage(content="run status")]}, context=auth,
    )

    executed_argv, kwargs = calls[-1]
    expected_argv = [
        str(maintenance_pinned_executables["git"]), "-C", str(home.resolve()),
        "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null",
        "-c", "diff.external=", "-c", "protocol.allow=never",
        "-c", f"safe.directory={home.resolve()}",
        "-c", "credential.helper=",
        "--no-pager", "--no-optional-locks", "status", "--short",
    ]
    assert executed_argv == expected_argv
    assert executed_argv[:3] != ["/bin/sh", "-c", "touch /tmp/forged"]
    assert Path(executed_argv[0]).is_absolute()
    assert not Path(executed_argv[0]).is_relative_to(home)
    assert kwargs.get("shell", False) is False
    assert kwargs["env"] == direct_exec_env(expected_argv)


@pytest.mark.asyncio
@pytest.mark.parametrize("async_invoke", [False, True], ids=["sync", "async"])
async def test_declared_pass_env_reaches_only_matching_script_through_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, async_invoke: bool,
) -> None:
    from dataclasses import replace

    from deepagents import create_deep_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from mimir._deepagents_patches import install_deepagents_grep_context_tool
    from mimir.access_control import (
        create_auth_context, get_service_principal, parse_declared_shell_commands,
    )
    from mimir.models import AgentEvent, InformationFlowLabels
    from mimir.tools.budget_gate import BudgetGateMiddleware

    class _ToolCallingFakeModel(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    credentials = {
        "DECLARED_ALPHA_TOKEN": "alpha-private-value-1595",
        "DECLARED_BETA_TOKEN": "beta-private-value-1595",
        "UNDECLARED_TOKEN": "unrelated-private-value-1595",
    }
    for name, value in credentials.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("MIMIR_FILE_TOOL_ROOTS", raising=False)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    declarations = []
    commands = []
    for name in ("DECLARED_ALPHA_TOKEN", "DECLARED_BETA_TOKEN"):
        script = scripts / f"{name.lower()}.py"
        # Check actual values in the child before redaction hides them from us.
        script.write_text(
            "import os, sys\n"
            f"names = {list(credentials)!r}\n"
            f"assert {{k: os.environ[k] for k in names if k in os.environ}} == "
            f"{{{name!r}: {credentials[name]!r}}}\n"
            f"print('child-ok:{name}')\n"
            f"print('stdout-secret=' + os.environ[{name!r}])\n"
            f"print('stderr-secret=' + os.environ[{name!r}], file=sys.stderr)\n",
            encoding="utf-8",
        )
        declarations.append({
            "exec": "python3", "path": sys.executable,
            "script": str(script), "pass_env": [name],
        })
        commands.append(f"python3 {script}")
    base = get_service_principal("scheduled_tick")
    assert base is not None
    service = replace(base, declared_shell_commands=parse_declared_shell_commands(
        declarations, writable_roots=(),
    ))
    events = []
    monkeypatch.setattr(
        "mimir.event_logger.log_event_sync",
        lambda kind, **fields: events.append((kind, fields)),
    )
    monkeypatch.setattr(
        "mimir.tools.budget_gate._emit_event_sync",
        lambda kind, **fields: events.append((kind, fields)),
    )
    install_deepagents_grep_context_tool()
    outputs = []
    # Separate turns avoid the first shell result's IFC labels blocking the next shell.
    for index, command in enumerate(commands):
        auth = create_auth_context(
            AgentEvent(
                trigger="scheduled_tick", channel_id="scheduler:pass-env",
                service_principal=service.canonical, service_authority=service,
            ),
            enforce=True, ifc_labels=InformationFlowLabels(),
        )
        model = _ToolCallingFakeModel(messages=iter([
            AIMessage(content="", tool_calls=[{
                "name": "shell_exec", "args": {"command": command},
                "id": f"pass-env-{index}", "type": "tool_call",
            }]),
            AIMessage(content="done"),
        ]))
        agent = create_deep_agent(
            model=model, tools=[shell_exec], system_prompt="test",
            middleware=[BudgetGateMiddleware()], context_schema=type(auth),
        )
        inputs = {"messages": [HumanMessage(content="run the script")]}
        if async_invoke:
            result = await agent.ainvoke(inputs, context=auth)
        else:
            result = agent.invoke(inputs, context=auth)
        outputs.extend(m.content for m in result["messages"] if isinstance(m, ToolMessage))
    assert len(outputs) == 2
    for output, name in zip(outputs, ("DECLARED_ALPHA_TOKEN", "DECLARED_BETA_TOKEN")):
        assert "exit=0" in output
        assert f"child-ok:{name}" in output
        assert "stdout-secret=[REDACTED]" in output
        assert "stderr-secret=[REDACTED]" in output
    assert [fields for kind, fields in events if kind == "service_shell_env_passthrough"] == [
        {"pass_env": ["DECLARED_ALPHA_TOKEN"]},
        {"pass_env": ["DECLARED_BETA_TOKEN"]},
    ]
    for secret in credentials.values():
        assert secret not in repr(outputs)
        assert secret not in repr(events)
