"""Credential verification — discovery + factories + CLI (Phase 2.5).

Skills register their credentials via ``credentials.yaml`` next to
``SKILL.md``. The framework discovers these at startup, builds a
probe per entry via factory functions, and merges them with the
mimir-core manifest shipped at ``mimir/credentials.yaml``.

Tests here cover:
- Each probe factory in isolation (subprocess, format, all_env_set,
  not_implemented, python escape hatch).
- The discovery walker (which roots, what shadows what, malformed
  manifests don't break the registry).
- The CLI entrypoints (``mimir verify-cred`` / ``verify-creds``).
- End-to-end: a synthetic home with multiple manifests yields the
  expected combined registry.
"""

from __future__ import annotations

import io
import os
import textwrap
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from mimir import cred_verify
from mimir.cred_verify import (
    ProbeResult,
    get_probes,
    reset_probes_cache,
    run_verify_cred_cmd,
    run_verify_creds_cmd,
    verify,
    verify_all,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    """Each test gets a fresh registry — no leakage from prior runs."""
    reset_probes_cache()
    yield
    reset_probes_cache()


def _write_manifest(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body))


# ── Factories — exercised end-to-end via a tmp manifest ──────────────


def test_format_probe_passes_with_correct_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setenv("FAKE_KEY", "sk-ant-" + "x" * 50)
    _write_manifest(tmp_path / "skills" / "fake" / "credentials.yaml", """
        credentials:
          - name: FAKE_KEY
            cred_type: D
            env_vars: [FAKE_KEY]
            description: "fake"
            probe:
              kind: format
              env: FAKE_KEY
              prefix: "sk-ant-"
              min_len: 20
    """)
    # Replace the package manifest with an empty one so this test
    # only exercises the operator-side discovery.
    monkeypatch.setattr(
        cred_verify, "_PACKAGE_MANIFEST", tmp_path / "no-such-file.yaml",
    )
    result = verify("FAKE_KEY")
    assert result.ok
    assert "format ok" in result.detail


def test_format_probe_rejects_disallowed_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_KEY", "sk-ant-" + "x" * 50)
    _write_manifest(tmp_path / "skills" / "fake" / "credentials.yaml", """
        credentials:
          - name: OPENAI_KEY
            cred_type: D
            env_vars: [OPENAI_KEY]
            description: "openai-shape"
            probe:
              kind: format
              env: OPENAI_KEY
              prefix: "sk-"
              disallowed_prefix: "sk-ant-"
              min_len: 20
    """)
    monkeypatch.setattr(cred_verify, "_PACKAGE_MANIFEST", tmp_path / "no-such-file.yaml")
    result = verify("OPENAI_KEY")
    assert not result.ok
    assert "sk-ant-" in result.detail


def test_format_probe_unavailable_when_env_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.delenv("MISSING_KEY", raising=False)
    _write_manifest(tmp_path / "skills" / "fake" / "credentials.yaml", """
        credentials:
          - name: MISSING_KEY
            cred_type: D
            env_vars: [MISSING_KEY]
            description: ""
            probe:
              kind: format
              env: MISSING_KEY
    """)
    monkeypatch.setattr(cred_verify, "_PACKAGE_MANIFEST", tmp_path / "no-such-file.yaml")
    result = verify("MISSING_KEY")
    assert not result.ok
    assert "unavailable" in result.detail


def test_subprocess_probe_unavailable_without_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setenv("FAKE_TOKEN", "x" * 40)
    _write_manifest(tmp_path / "skills" / "fake" / "credentials.yaml", """
        credentials:
          - name: FAKE_TOKEN
            cred_type: A
            env_vars: [FAKE_TOKEN]
            description: ""
            probe:
              kind: subprocess
              binary: definitely-not-installed
              cmd: [definitely-not-installed, status]
    """)
    monkeypatch.setattr(cred_verify, "_PACKAGE_MANIFEST", tmp_path / "no-such-file.yaml")
    monkeypatch.setattr(cred_verify, "_has_binary", lambda name: False)
    result = verify("FAKE_TOKEN")
    assert not result.ok
    assert "unavailable" in result.detail
    assert "definitely-not-installed" in result.detail


def test_subprocess_probe_passes_on_zero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setenv("FAKE_TOKEN", "x" * 40)
    _write_manifest(tmp_path / "skills" / "fake" / "credentials.yaml", """
        credentials:
          - name: FAKE_TOKEN
            cred_type: A
            env_vars: [FAKE_TOKEN]
            description: ""
            probe:
              kind: subprocess
              binary: faketool
              cmd: [faketool, status]
    """)
    monkeypatch.setattr(cred_verify, "_PACKAGE_MANIFEST", tmp_path / "no-such-file.yaml")
    monkeypatch.setattr(cred_verify, "_has_binary", lambda name: True)
    monkeypatch.setattr(
        cred_verify, "_run_quiet",
        lambda cmd, timeout=10: (0, "", "Authenticated as alice"),
    )
    result = verify("FAKE_TOKEN")
    assert result.ok
    assert "alice" in result.detail


def test_all_env_set_probe_needs_every_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setenv("FAKE_A", "1")
    monkeypatch.setenv("FAKE_B", "2")
    monkeypatch.delenv("FAKE_C", raising=False)
    _write_manifest(tmp_path / "skills" / "fake" / "credentials.yaml", """
        credentials:
          - name: FAKE_QUARTET
            cred_type: D
            env_vars: [FAKE_A, FAKE_B, FAKE_C]
            description: ""
            probe:
              kind: all_env_set
              note: "rotation must be atomic"
    """)
    monkeypatch.setattr(cred_verify, "_PACKAGE_MANIFEST", tmp_path / "no-such-file.yaml")
    result = verify("FAKE_QUARTET")
    assert not result.ok
    assert "FAKE_C" in result.detail
    # Add FAKE_C and re-run.
    monkeypatch.setenv("FAKE_C", "3")
    reset_probes_cache()
    result = verify("FAKE_QUARTET")
    assert result.ok
    assert "rotation must be atomic" in result.detail


def test_not_implemented_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    _write_manifest(tmp_path / "skills" / "fake" / "credentials.yaml", """
        credentials:
          - name: FUTURE_BRIDGE_TOKEN
            cred_type: B
            env_vars: [FUTURE_BRIDGE_TOKEN]
            description: ""
            probe:
              kind: not_implemented
    """)
    monkeypatch.setattr(cred_verify, "_PACKAGE_MANIFEST", tmp_path / "no-such-file.yaml")
    result = verify("FUTURE_BRIDGE_TOKEN")
    assert not result.ok
    assert "not_implemented" in result.detail
    assert "Type B" in result.detail


def test_python_probe_loads_skill_local_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setenv("FAKE_KEY", "the-expected-value")
    skill_dir = tmp_path / "skills" / "fake"
    _write_manifest(skill_dir / "credentials.yaml", """
        credentials:
          - name: FAKE_KEY
            cred_type: D
            env_vars: [FAKE_KEY]
            description: ""
            probe:
              kind: python
              script: my_probe.py
    """)
    (skill_dir / "my_probe.py").write_text(textwrap.dedent("""
        import os
        def probe() -> tuple[bool, str]:
            v = os.environ.get("FAKE_KEY", "")
            if v == "the-expected-value":
                return (True, "custom probe says ok")
            return (False, f"got: {v!r}")
    """))
    monkeypatch.setattr(cred_verify, "_PACKAGE_MANIFEST", tmp_path / "no-such-file.yaml")
    result = verify("FAKE_KEY")
    assert result.ok
    assert "custom probe says ok" in result.detail


def test_python_probe_missing_script_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    skill_dir = tmp_path / "skills" / "fake"
    _write_manifest(skill_dir / "credentials.yaml", """
        credentials:
          - name: FAKE_KEY
            cred_type: D
            env_vars: [FAKE_KEY]
            description: ""
            probe:
              kind: python
              script: not_actually_there.py
    """)
    monkeypatch.setattr(cred_verify, "_PACKAGE_MANIFEST", tmp_path / "no-such-file.yaml")
    result = verify("FAKE_KEY")
    assert not result.ok
    assert "probe script not found" in result.detail


def test_python_probe_handles_script_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """A broken probe script must not crash the registry; surface
    the exception as a probe failure."""
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    skill_dir = tmp_path / "skills" / "fake"
    _write_manifest(skill_dir / "credentials.yaml", """
        credentials:
          - name: BROKEN_PROBE
            cred_type: D
            env_vars: []
            description: ""
            probe:
              kind: python
              script: bad.py
    """)
    (skill_dir / "bad.py").write_text("def probe():\n    raise ValueError('nope')\n")
    monkeypatch.setattr(cred_verify, "_PACKAGE_MANIFEST", tmp_path / "no-such-file.yaml")
    result = verify("BROKEN_PROBE")
    assert not result.ok
    assert "raised" in result.detail
    assert "nope" in result.detail


# ── Discovery walker ─────────────────────────────────────────────────


def test_discovery_walks_both_skill_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """A bundled + operator manifest are both included; the registry
    contains entries from both roots."""
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    _write_manifest(tmp_path / ".mimir_builtin_skills" / "bundled" / "credentials.yaml", """
        credentials:
          - name: BUNDLED_KEY
            cred_type: D
            env_vars: [BUNDLED_KEY]
            description: ""
            probe:
              kind: format
              env: BUNDLED_KEY
              min_len: 4
    """)
    _write_manifest(tmp_path / "skills" / "operator" / "credentials.yaml", """
        credentials:
          - name: OPERATOR_KEY
            cred_type: D
            env_vars: [OPERATOR_KEY]
            description: ""
            probe:
              kind: format
              env: OPERATOR_KEY
              min_len: 4
    """)
    monkeypatch.setattr(cred_verify, "_PACKAGE_MANIFEST", tmp_path / "no-such-file.yaml")
    probes = get_probes()
    assert "BUNDLED_KEY" in probes
    assert "OPERATOR_KEY" in probes


def test_operator_manifest_shadows_bundled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """When a name appears in both roots, the operator copy wins."""
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    _write_manifest(tmp_path / ".mimir_builtin_skills" / "common" / "credentials.yaml", """
        credentials:
          - name: SHARED_KEY
            cred_type: D
            env_vars: [SHARED_KEY]
            description: "bundled version"
            probe:
              kind: format
              env: SHARED_KEY
              min_len: 4
    """)
    _write_manifest(tmp_path / "skills" / "common" / "credentials.yaml", """
        credentials:
          - name: SHARED_KEY
            cred_type: D
            env_vars: [SHARED_KEY]
            description: "operator version"
            probe:
              kind: format
              env: SHARED_KEY
              min_len: 4
    """)
    monkeypatch.setattr(cred_verify, "_PACKAGE_MANIFEST", tmp_path / "no-such-file.yaml")
    probes = get_probes()
    assert probes["SHARED_KEY"].description == "operator version"
    assert "skills/common" in probes["SHARED_KEY"].source


def test_malformed_manifest_doesnt_kill_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """A bad YAML file logs a warning but the rest of the registry
    still loads."""
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    (tmp_path / "skills" / "broken").mkdir(parents=True)
    (tmp_path / "skills" / "broken" / "credentials.yaml").write_text("not: [valid")  # syntax error
    _write_manifest(tmp_path / "skills" / "ok" / "credentials.yaml", """
        credentials:
          - name: GOOD_KEY
            cred_type: D
            env_vars: [GOOD_KEY]
            description: ""
            probe:
              kind: format
              env: GOOD_KEY
              min_len: 4
    """)
    monkeypatch.setattr(cred_verify, "_PACKAGE_MANIFEST", tmp_path / "no-such-file.yaml")
    probes = get_probes()
    assert "GOOD_KEY" in probes
    # Broken manifest contributed no entries.
    assert all("broken" not in p.source for p in probes.values())


def test_unknown_probe_kind_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Future probe kinds shouldn't crash an older framework."""
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    _write_manifest(tmp_path / "skills" / "future" / "credentials.yaml", """
        credentials:
          - name: FUTURE_KEY
            cred_type: D
            env_vars: [FUTURE_KEY]
            description: ""
            probe:
              kind: hypothetical_future_kind
              foo: bar
          - name: OK_KEY
            cred_type: D
            env_vars: [OK_KEY]
            description: ""
            probe:
              kind: format
              env: OK_KEY
              min_len: 4
    """)
    monkeypatch.setattr(cred_verify, "_PACKAGE_MANIFEST", tmp_path / "no-such-file.yaml")
    probes = get_probes()
    assert "FUTURE_KEY" not in probes
    assert "OK_KEY" in probes


def test_package_manifest_loaded_by_default(monkeypatch: pytest.MonkeyPatch):
    """The mimir-core ``credentials.yaml`` shipped with the package
    must be discovered even when MIMIR_HOME is unset."""
    monkeypatch.delenv("MIMIR_HOME", raising=False)
    probes = get_probes()
    # The core manifest must include at least ANTHROPIC_API_KEY +
    # MIMIR_API_KEY + GITHUB_TOKEN — the mimir-process foundations.
    assert "ANTHROPIC_API_KEY" in probes
    assert "MIMIR_API_KEY" in probes
    assert "GITHUB_TOKEN" in probes


# ── CLI entrypoints ──────────────────────────────────────────────────


def test_verify_cred_unknown_name_reports_registered_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    _write_manifest(tmp_path / "skills" / "fake" / "credentials.yaml", """
        credentials:
          - name: SOMEKEY
            cred_type: D
            env_vars: [SOMEKEY]
            description: ""
            probe: { kind: format, env: SOMEKEY, min_len: 4 }
    """)
    monkeypatch.setattr(cred_verify, "_PACKAGE_MANIFEST", tmp_path / "no-such-file.yaml")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = run_verify_cred_cmd("NOT_REAL")
    assert rc == 2
    out = buf.getvalue()
    assert "unknown credential" in out
    assert "SOMEKEY" in out  # the registered name listed for the operator


def test_verify_creds_summary_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setenv("OK_KEY", "x" * 20)
    monkeypatch.delenv("BAD_KEY", raising=False)
    _write_manifest(tmp_path / "skills" / "fake" / "credentials.yaml", """
        credentials:
          - name: OK_KEY
            cred_type: D
            env_vars: [OK_KEY]
            description: ""
            probe: { kind: format, env: OK_KEY, min_len: 4 }
          - name: BAD_KEY
            cred_type: D
            env_vars: [BAD_KEY]
            description: ""
            probe: { kind: format, env: BAD_KEY, min_len: 4 }
    """)
    monkeypatch.setattr(cred_verify, "_PACKAGE_MANIFEST", tmp_path / "no-such-file.yaml")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = run_verify_creds_cmd()
    assert rc == 1  # partial failure
    out = buf.getvalue()
    assert "1/2 probes ok" in out


def test_verify_creds_filter_by_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setenv("D_KEY", "x" * 20)
    monkeypatch.setenv("A_KEY", "y" * 20)
    _write_manifest(tmp_path / "skills" / "fake" / "credentials.yaml", """
        credentials:
          - name: D_KEY
            cred_type: D
            env_vars: [D_KEY]
            description: ""
            probe: { kind: format, env: D_KEY, min_len: 4 }
          - name: A_KEY
            cred_type: A
            env_vars: [A_KEY]
            description: ""
            probe: { kind: format, env: A_KEY, min_len: 4 }
    """)
    monkeypatch.setattr(cred_verify, "_PACKAGE_MANIFEST", tmp_path / "no-such-file.yaml")
    buf = io.StringIO()
    with redirect_stdout(buf):
        run_verify_creds_cmd(only_type="D")
    out = buf.getvalue()
    assert "[D]" in out
    assert "[A]" not in out


def test_verify_returns_unknown_result_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Phase 3 (rotation) calls ``verify(name)`` inline; a typo
    shouldn't propagate a bare ``KeyError``. Return a ProbeResult."""
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr(cred_verify, "_PACKAGE_MANIFEST", tmp_path / "no-such-file.yaml")
    result = verify("DEFINITELY_NOT_REGISTERED")
    assert not result.ok
    assert "unknown credential" in result.detail
