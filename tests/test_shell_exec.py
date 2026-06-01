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

import pytest

from mimir.tools.extra import shell_exec


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
