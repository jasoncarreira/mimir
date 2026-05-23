"""Credential verification probes (SPEC §16 item 14, Phase 2).

Tests the registry shape, the per-probe behavior (with subprocess
mocked so we don't depend on which tools are installed in the test
environment), and the CLI entrypoints.
"""

from __future__ import annotations

import io
import os
from contextlib import redirect_stdout
from typing import Any

import pytest

from mimir import cred_verify
from mimir.cred_verify import (
    PROBES,
    Probe,
    ProbeResult,
    run_verify_cred_cmd,
    run_verify_creds_cmd,
    verify,
    verify_all,
)


# ── Registry shape ───────────────────────────────────────────────────


def test_every_probe_is_well_formed():
    """Each registry entry has a name, type, env_vars, description,
    and callable fn. Catches typos / missing fields on new additions."""
    for key, probe in PROBES.items():
        assert key == probe.name, f"registry key mismatch: {key!r} vs {probe.name!r}"
        assert probe.cred_type in ("A", "B", "C", "D"), probe
        assert isinstance(probe.env_vars, tuple), probe
        assert probe.description, probe
        assert callable(probe.fn), probe


def test_registry_has_at_least_one_of_each_type():
    """The classification doc enumerates four types. The registry
    must surface at least one probe per type so the CLI ``--type``
    filter returns non-empty results."""
    types = {p.cred_type for p in PROBES.values()}
    assert types == {"A", "B", "C", "D"}, f"missing types: {set('ABCD') - types}"


# ── Probe behavior ───────────────────────────────────────────────────


def test_static_key_probe_passes_when_format_ok(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-" + "x" * 50)
    result = verify("ANTHROPIC_API_KEY")
    assert result.ok
    assert result.cred_type == "D"
    assert "format ok" in result.detail


def test_static_key_probe_fails_on_wrong_prefix(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "wrong-prefix-" + "x" * 30)
    result = verify("ANTHROPIC_API_KEY")
    assert not result.ok
    assert "prefix" in result.detail.lower()


def test_static_key_probe_fails_when_too_short(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    result = verify("TAVILY_API_KEY")
    assert not result.ok
    assert "too short" in result.detail.lower()


def test_unset_env_reports_unavailable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = verify("ANTHROPIC_API_KEY")
    assert not result.ok
    assert "unavailable" in result.detail.lower()


def test_x_oauth_quartet_needs_all_four(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("X_API_KEY", "k" * 20)
    monkeypatch.setenv("X_API_SECRET", "s" * 40)
    monkeypatch.setenv("X_ACCESS_TOKEN", "t" * 50)
    monkeypatch.delenv("X_ACCESS_TOKEN_SECRET", raising=False)
    result = verify("X_OAUTH")
    assert not result.ok
    assert "X_ACCESS_TOKEN_SECRET" in result.detail


def test_bsky_app_password_format(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ATPROTO_APP_PASSWORD", "abcd-efgh-ijkl-mnop")
    monkeypatch.delenv("BSKY_APP_PASSWORD", raising=False)
    result = verify("BSKY_APP_PASSWORD")
    assert result.ok

    # Wrong shape — should fail.
    monkeypatch.setenv("ATPROTO_APP_PASSWORD", "not-a-valid-shape")
    result = verify("BSKY_APP_PASSWORD")
    assert not result.ok


def test_subprocess_probe_unavailable_without_binary(
    monkeypatch: pytest.MonkeyPatch,
):
    """The Type A probes short-circuit to ``unavailable`` when the
    binary is missing from PATH, rather than running and failing
    with a misleading error."""
    monkeypatch.setattr(cred_verify, "_has_binary", lambda name: False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "x" * 40)
    result = verify("GITHUB_TOKEN")
    assert not result.ok
    assert "unavailable" in result.detail.lower()
    assert "gh" in result.detail.lower()


def test_subprocess_probe_passes_on_zero_exit(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cred_verify, "_has_binary", lambda name: True)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "x" * 40)
    monkeypatch.setattr(
        cred_verify, "_run_quiet",
        lambda cmd, timeout=10: (0, "", "Logged in to github.com as mimir-carreira"),
    )
    result = verify("GITHUB_TOKEN")
    assert result.ok
    assert "mimir-carreira" in result.detail


def test_subprocess_probe_fails_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cred_verify, "_has_binary", lambda name: True)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "x" * 40)
    monkeypatch.setattr(
        cred_verify, "_run_quiet",
        lambda cmd, timeout=10: (1, "", "error: token expired"),
    )
    result = verify("GITHUB_TOKEN")
    assert not result.ok
    assert "expired" in result.detail.lower()


def test_type_b_probes_are_not_implemented():
    """Type B (long-lived bridge) probes are stubbed for Phase 3 —
    they should surface as failing with a distinctive marker rather
    than silently succeeding or being absent from the registry."""
    result = verify("DISCORD_TOKEN")
    assert not result.ok
    assert "not_implemented" in result.detail


def test_type_c_probes_are_not_implemented():
    result = verify("CLAUDE_OAUTH")
    assert not result.ok
    assert "not_implemented" in result.detail


# ── CLI entrypoints ──────────────────────────────────────────────────


def test_verify_cred_unknown_name():
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = run_verify_cred_cmd("NOT_A_REAL_CRED")
    assert rc == 2
    assert "unknown credential" in buf.getvalue()


def test_verify_cred_ok(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-" + "x" * 50)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = run_verify_cred_cmd("ANTHROPIC_API_KEY")
    assert rc == 0
    assert "OK" in buf.getvalue()
    assert "ANTHROPIC_API_KEY" in buf.getvalue()


def test_verify_cred_stale(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = run_verify_cred_cmd("ANTHROPIC_API_KEY")
    assert rc == 1
    assert "FAIL" in buf.getvalue()


def test_verify_creds_filters_by_type(monkeypatch: pytest.MonkeyPatch):
    """``--type D`` runs only Type D probes; the output should not
    contain any Type A/B/C names."""
    # Make every Type D probe pass.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-" + "x" * 50)
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-" + "x" * 40)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-" + "x" * 40)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-" + "x" * 30)
    monkeypatch.setenv("MIMIR_API_KEY", "x" * 20)
    monkeypatch.setenv("X_API_KEY", "x" * 20)
    monkeypatch.setenv("X_API_SECRET", "x" * 40)
    monkeypatch.setenv("X_ACCESS_TOKEN", "x" * 50)
    monkeypatch.setenv("X_ACCESS_TOKEN_SECRET", "x" * 40)
    monkeypatch.setenv("ATPROTO_APP_PASSWORD", "abcd-efgh-ijkl-mnop")

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = run_verify_creds_cmd(only_type="D")
    output = buf.getvalue()
    assert rc == 0
    assert "[D]" in output
    assert "[A]" not in output
    assert "[B]" not in output
    assert "[C]" not in output


def test_verify_creds_reports_partial_failures(monkeypatch: pytest.MonkeyPatch):
    """Filter to Type D and DON'T set the env vars — every probe
    should fail and the rc should be 1, but ALL probes should still
    have been attempted (no short-circuit on first failure)."""
    for env in (
        "ANTHROPIC_API_KEY", "VOYAGE_API_KEY", "OPENAI_API_KEY",
        "TAVILY_API_KEY", "MIMIR_API_KEY", "X_API_KEY",
        "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET",
        "ATPROTO_APP_PASSWORD", "BSKY_APP_PASSWORD",
    ):
        monkeypatch.delenv(env, raising=False)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = run_verify_creds_cmd(only_type="D")
    output = buf.getvalue()
    assert rc == 1
    type_d_count = sum(1 for p in PROBES.values() if p.cred_type == "D")
    assert output.count("[D]") == type_d_count
    assert "0/" + str(type_d_count) + " probes ok" in output


def test_verify_creds_filter_no_matches(monkeypatch: pytest.MonkeyPatch):
    """If a type filter produces no probes (currently impossible
    given the registry, but defensible against future churn), the
    CLI should report that cleanly rather than print '0/0 ok'."""
    # Temporarily clear PROBES of all D entries by monkeypatching.
    filtered = {k: p for k, p in PROBES.items() if p.cred_type != "D"}
    monkeypatch.setattr(cred_verify, "PROBES", filtered)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = run_verify_creds_cmd(only_type="D")
    assert rc == 1
    assert "no probes registered" in buf.getvalue()
