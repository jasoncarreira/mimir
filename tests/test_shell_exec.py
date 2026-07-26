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

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import mimir.tools.extra as extra
from mimir.tools.extra import shell_exec


@pytest.fixture(autouse=True)
def reset_shell_state(monkeypatch, tmp_path):
    old_cwd = extra._SHELL_STATE["cwd"]
    old_timeout = extra._SHELL_STATE["timeout_s"]
    extra._SHELL_STATE["cwd"] = None
    extra._SHELL_STATE["timeout_s"] = 60.0
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    try:
        yield
    finally:
        extra._SHELL_STATE["cwd"] = old_cwd
        extra._SHELL_STATE["timeout_s"] = old_timeout


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


def test_shell_exec_standalone_cd_persists_for_later_calls(tmp_path):
    """A successful standalone cd updates the cwd used by later shell calls."""
    target = tmp_path / "workspace"
    target.mkdir()

    cd_out = shell_exec.invoke({"command": f"cd {target}"})
    pwd_out = shell_exec.invoke({"command": "pwd"})

    assert "exit=0" in cd_out
    assert "exit=0" in pwd_out
    assert str(target) in pwd_out


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
        "--no-pager", "--no-optional-locks", "status", "--short",
    ]
    assert executed_argv == expected_argv
    assert executed_argv[:3] != ["/bin/sh", "-c", "touch /tmp/forged"]
    assert Path(executed_argv[0]).is_absolute()
    assert not Path(executed_argv[0]).is_relative_to(home)
    assert kwargs.get("shell", False) is False
    assert kwargs["env"] == direct_exec_env(expected_argv)
