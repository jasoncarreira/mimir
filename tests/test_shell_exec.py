"""Tests for mimir.tools.extra:shell_exec (chainlink #226).

Pins the trust posture of ``shell_exec``: the agent's shell tools
(``shell_exec`` + ``bash_async``) are intentionally unrestricted within
the trusted container. There is no allowlist gate. ``set_shell_allowlist``
was a deepagents-migration PoC affordance that was never wired and has
been removed; these tests defend against re-introducing a half-wired
gate (the worst-of-both-worlds state the chainlink flagged).
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


def test_shell_exec_still_uses_shlex_split_no_shell_expansion():
    """Defence: shell=False via shlex.split is the *real* injection guard,
    not the (now-removed) allowlist. Pin it.
    """
    result = shell_exec.invoke({"command": "echo $HOME"})
    assert "exit=0" in result
    # shell=False means $HOME is passed as a literal token, not expanded.
    assert "$HOME" in result


def test_shell_exec_reports_shell_parse_error_for_bad_quoting():
    """shlex.split surfaces parse errors as a typed message rather than
    crashing the tool call."""
    result = shell_exec.invoke({"command": "echo \"unterminated"})
    assert "shell-parse error" in result
