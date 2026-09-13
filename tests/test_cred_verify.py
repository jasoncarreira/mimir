"""Credential verification — discovery + factories + CLI (Phase 2.5).

Skills register their credentials via ``credentials.yaml`` next to
``SKILL.md``. The framework discovers these at startup, builds a
probe per entry via factory functions, and merges them with the
mimir-core manifest shipped at ``mimir/credentials.yaml``.

Tests here cover:
- Each probe factory in isolation (subprocess, format, all_env_set,
  not_implemented, python escape hatch).
- The discovery walker (package trust, installed optional skills, malformed
  manifests don't break the registry).
- The CLI entrypoints (``mimir verify-cred`` / ``verify-creds``).
- End-to-end: synthetic packaged manifests and home installation markers yield the
  expected combined registry.
"""

from __future__ import annotations

import io
import json
import os
import sys
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


@pytest.fixture
def package_skills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "package" / "optional-skills"
    root.mkdir(parents=True)
    monkeypatch.setattr(cred_verify, "_PACKAGE_SKILLS_ROOT", root)
    monkeypatch.setattr(cred_verify, "_PACKAGE_MANIFEST", root.parent / "credentials.yaml")
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    return root


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
    monkeypatch.setattr(
        cred_verify, "_PACKAGE_MANIFEST", tmp_path / "skills" / "fake" / "credentials.yaml",
    )
    result = verify("FAKE_KEY")
    assert result.ok
    assert "format ok" in result.detail


@pytest.mark.parametrize("value", ["sk-ant-" + "x" * 20 + "\r", " sk-ant-" + "x" * 20])
def test_format_probe_rejects_surrounding_whitespace_used_by_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str,
):
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setenv("FAKE_KEY", value)
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
    monkeypatch.setattr(cred_verify, "_PACKAGE_MANIFEST", tmp_path / "skills" / "fake" / "credentials.yaml")

    result = verify("FAKE_KEY")

    assert not result.ok
    assert "surrounding whitespace" in result.detail
    assert f"got {len(value)} chars" in result.detail
    assert os.environ["FAKE_KEY"] == value


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
    monkeypatch.setattr(cred_verify, "_PACKAGE_MANIFEST", tmp_path / "skills" / "fake" / "credentials.yaml")
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
    monkeypatch.setattr(cred_verify, "_PACKAGE_MANIFEST", tmp_path / "skills" / "fake" / "credentials.yaml")
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
    monkeypatch.setattr(cred_verify, "_PACKAGE_MANIFEST", tmp_path / "skills" / "fake" / "credentials.yaml")
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
    monkeypatch.setattr(cred_verify, "_PACKAGE_MANIFEST", tmp_path / "skills" / "fake" / "credentials.yaml")
    monkeypatch.setattr(cred_verify, "_has_binary", lambda name: True)
    monkeypatch.setattr(
        cred_verify, "_run_quiet",
        lambda cmd, timeout=10: (0, "", "Authenticated as alice"),
    )
    result = verify("FAKE_TOKEN")
    assert result.ok
    assert "alice" in result.detail


def test_subprocess_probe_uses_exact_restricted_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    from mimir.contained_execution import base_worker_environment

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    monkeypatch.setenv("GITHUB_TOKEN", "test-github-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-secret")
    monkeypatch.setenv("UNRELATED_SECRET", "also-private")
    cmd = [sys.executable, "-c", "import json, os; print(json.dumps(dict(os.environ)))"]
    probe = cred_verify._make_subprocess_probe(
        binary=sys.executable, cmd=cmd,
        env_vars=("GITHUB_TOKEN", "ANTHROPIC_API_KEY"),
    )

    ok, detail = probe()

    assert ok, detail
    child_env = json.loads(detail)
    assert "GITHUB_TOKEN" not in child_env
    assert "ANTHROPIC_API_KEY" not in child_env
    assert "UNRELATED_SECRET" not in child_env
    assert child_env == base_worker_environment("cred-verify")


def test_binary_lookup_uses_restricted_path(monkeypatch):
    from mimir.contained_execution import base_worker_environment

    calls = []
    monkeypatch.setenv("PATH", "/agent-writable/bin")
    monkeypatch.setattr(cred_verify.shutil, "which", lambda name, **kw: calls.append((name, kw)))
    assert not cred_verify._has_binary("tool")
    assert calls == [("tool", {"path": base_worker_environment("cred-verify")["PATH"]})]


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
    monkeypatch.setattr(cred_verify, "_PACKAGE_MANIFEST", tmp_path / "skills" / "fake" / "credentials.yaml")
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
    monkeypatch.setattr(cred_verify, "_PACKAGE_MANIFEST", tmp_path / "skills" / "fake" / "credentials.yaml")
    result = verify("FUTURE_BRIDGE_TOKEN")
    assert not result.ok
    assert result.skipped
    assert "not_implemented" in result.detail
    assert "Type B" in result.detail
    assert "SKIP" in result.render()
    assert run_verify_cred_cmd("FUTURE_BRIDGE_TOKEN") == 0


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
    monkeypatch.setattr(cred_verify, "_PACKAGE_MANIFEST", skill_dir / "credentials.yaml")
    result = verify("FAKE_KEY")
    assert result.ok
    assert "custom probe says ok" in result.detail


@pytest.mark.parametrize("path_kind", ["parent", "absolute", "symlink"])
def test_python_probe_rejects_script_outside_manifest_directory(
    tmp_path: Path, path_kind: str,
):
    manifest_dir = tmp_path / "package"
    manifest_dir.mkdir()
    marker = tmp_path / "outside-executed"
    outside = tmp_path / "outside.py"
    outside.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
        "def probe():\n    return True, 'outside script executed'\n"
    )
    if path_kind == "parent":
        script = "../outside.py"
    elif path_kind == "absolute":
        script = str(outside)
    else:
        (manifest_dir / "linked.py").symlink_to(outside)
        script = "linked.py"

    manifest = manifest_dir / "credentials.yaml"
    _write_manifest(manifest, f"""
        credentials:
          - name: ESCAPE
            cred_type: D
            probe: {{kind: python, script: {script!r}}}
    """)
    probe = cred_verify._load_manifest(manifest)[0].fn
    assert not marker.exists()
    ok, detail = probe()

    assert not marker.exists(), "out-of-directory script executed its top-level marker"
    assert not ok
    assert "outside manifest directory" in detail


def test_python_probe_rejects_script_retargeted_after_construction(tmp_path: Path):
    manifest_dir = tmp_path / "package"
    manifest_dir.mkdir()
    inside = manifest_dir / "inside.py"
    inside.write_text("def probe():\n    return True, 'inside'\n")
    marker = tmp_path / "outside-executed"
    outside = tmp_path / "outside.py"
    outside.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
        "def probe():\n    return True, 'outside'\n"
    )
    link = manifest_dir / "linked.py"
    link.symlink_to(inside)
    probe = cred_verify._make_python_probe(manifest_dir, "linked.py")
    link.unlink()
    link.symlink_to(outside)

    ok, detail = probe()

    assert not marker.exists()
    assert not ok
    assert "outside manifest directory" in detail


def test_python_probe_loads_nested_script_created_after_construction(tmp_path: Path):
    probe = cred_verify._make_python_probe(tmp_path, "nested/probe.py")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "probe.py").write_text(
        "def probe():\n    return True, 'nested script'\n"
    )
    assert probe() == (True, "nested script")


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
    monkeypatch.setattr(cred_verify, "_PACKAGE_MANIFEST", skill_dir / "credentials.yaml")
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
    monkeypatch.setattr(cred_verify, "_PACKAGE_MANIFEST", skill_dir / "credentials.yaml")
    result = verify("BROKEN_PROBE")
    assert not result.ok
    assert "raised" in result.detail
    assert "nope" in result.detail


# ── Discovery walker ─────────────────────────────────────────────────


@pytest.mark.parametrize("ok", [True, False])
@pytest.mark.parametrize("kind", ["subprocess", "python"])
@pytest.mark.parametrize("secret", [None, "ghp_" + "a" * 36])
def test_verify_redacts_probe_details(monkeypatch, capsys, ok, kind, secret):
    detail = "Authenticated as alice" + (f" using {secret}" if secret else "")
    expected = "Authenticated as alice" + (" using [REDACTED]" if secret else "")
    probe = cred_verify.Probe(
        name="TEST", cred_type="A", env_vars=(), description="",
        kind=kind, fn=lambda: (ok, detail), source="test",
    )
    monkeypatch.setattr(cred_verify, "get_probes", lambda home=None: {"TEST": probe})
    result = verify("TEST")
    assert result.ok is ok
    assert result.detail == expected
    assert run_verify_cred_cmd("TEST") == (0 if ok else 1)
    assert run_verify_creds_cmd() == (0 if ok else 1)
    output = capsys.readouterr().out
    assert output.count(expected) == 2
    if secret:
        assert secret not in output


def test_discovery_walks_both_skill_roots(
    tmp_path: Path, package_skills: Path,
):
    """Both home roots activate package manifests, even without home content."""
    (tmp_path / ".mimir_builtin_skills" / "bundled").mkdir(parents=True)
    (tmp_path / "skills" / "operator").mkdir(parents=True)
    _write_manifest(package_skills / "bundled" / "credentials.yaml", """
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
    _write_manifest(package_skills / "operator" / "credentials.yaml", """
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
    probes = get_probes()
    assert list(probes) == ["BUNDLED_KEY", "OPERATOR_KEY"]
    assert probes["BUNDLED_KEY"].source == str(package_skills / "bundled" / "credentials.yaml")
    assert probes["OPERATOR_KEY"].source == str(package_skills / "operator" / "credentials.yaml")


@pytest.mark.parametrize("root_name", ["skills", ".mimir_builtin_skills"])
def test_home_manifests_cannot_inject_or_shadow_packaged_probes(
    tmp_path: Path, package_skills: Path, root_name: str,
):
    _write_manifest(cred_verify._PACKAGE_MANIFEST, """
        credentials:
          - name: GITHUB_TOKEN
            cred_type: D
            description: packaged core
            probe: {kind: not_implemented}
    """)
    _write_manifest(package_skills / "common" / "credentials.yaml", """
        credentials:
          - name: OPTIONAL_KEY
            cred_type: D
            probe: {kind: not_implemented}
    """)
    marker = tmp_path / "home-script-executed"
    for skill in ("common", "unpackaged"):
        home_dir = tmp_path / root_name / skill
        _write_manifest(home_dir / "credentials.yaml", """
            credentials:
              - name: GITHUB_TOKEN
                cred_type: D
                description: malicious override
                probe: {kind: python, script: evil.py}
              - name: INJECTED_KEY
                cred_type: D
                probe: {kind: python, script: evil.py}
        """)
        (home_dir / "evil.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
            "def probe():\n    return True, 'malicious probe'\n"
        )
    probes = get_probes()
    results = verify_all()
    assert not marker.exists()
    assert list(probes) == ["GITHUB_TOKEN", "OPTIONAL_KEY"]
    assert probes["GITHUB_TOKEN"].description == "packaged core"
    assert probes["GITHUB_TOKEN"].source == str(cred_verify._PACKAGE_MANIFEST)
    assert all(result.skipped for result in results)


@pytest.mark.parametrize("installed", ["absent", "file", "operator", "bundled", "both"])
def test_optional_package_manifest_requires_installed_directory(
    tmp_path: Path, package_skills: Path, installed: str,
):
    _write_manifest(package_skills / "optional" / "credentials.yaml", """
        credentials:
          - name: OPTIONAL_KEY
            cred_type: D
            probe: {kind: not_implemented}
    """)
    if installed == "file":
        (tmp_path / "skills").mkdir()
        (tmp_path / "skills" / "optional").write_text("not a directory")
    if installed in ("operator", "both"):
        (tmp_path / "skills" / "optional").mkdir(parents=True)
    if installed in ("bundled", "both"):
        (tmp_path / ".mimir_builtin_skills" / "optional").mkdir(parents=True)

    assert list(get_probes()) == (
        ["OPTIONAL_KEY"] if installed in ("operator", "bundled", "both") else []
    )


def test_discovery_loads_core_then_sorted_package_skills(
    tmp_path: Path, package_skills: Path,
):
    _write_manifest(cred_verify._PACKAGE_MANIFEST, """
        credentials:
          - name: CORE_KEY
            cred_type: D
            probe: {kind: not_implemented}
    """)
    for skill in ("zebra", "alpha"):
        (tmp_path / "skills" / skill).mkdir(parents=True)
        _write_manifest(package_skills / skill / "credentials.yaml", f"""
            credentials:
              - name: {skill.upper()}_KEY
                cred_type: D
                probe: {{kind: not_implemented}}
              - name: SHARED_KEY
                cred_type: D
                description: {skill}
                probe: {{kind: not_implemented}}
        """)
    probes = get_probes()
    assert list(probes) == ["CORE_KEY", "ALPHA_KEY", "SHARED_KEY", "ZEBRA_KEY"]
    assert probes["SHARED_KEY"].description == "zebra"


def test_optional_package_manifest_not_loaded_without_home(
    package_skills: Path, monkeypatch: pytest.MonkeyPatch,
):
    _write_manifest(package_skills / "optional" / "credentials.yaml", """
        credentials:
          - name: OPTIONAL_KEY
            cred_type: D
            probe: {kind: not_implemented}
    """)
    monkeypatch.delenv("MIMIR_HOME", raising=False)
    assert get_probes() == {}


def test_malformed_manifest_doesnt_kill_registry(
    tmp_path: Path, package_skills: Path,
):
    """A bad YAML file logs a warning but the rest of the registry
    still loads."""
    (tmp_path / "skills" / "broken").mkdir(parents=True)
    (tmp_path / "skills" / "ok").mkdir(parents=True)
    _write_manifest(package_skills / "broken" / "credentials.yaml", "not: [valid")
    _write_manifest(package_skills / "ok" / "credentials.yaml", """
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
    probes = get_probes()
    assert "GOOD_KEY" in probes
    # Broken manifest contributed no entries.
    assert all("broken" not in p.source for p in probes.values())


def test_unknown_probe_kind_skipped(
    tmp_path: Path, package_skills: Path,
):
    """Future probe kinds shouldn't crash an older framework."""
    (tmp_path / "skills" / "future").mkdir(parents=True)
    _write_manifest(package_skills / "future" / "credentials.yaml", """
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
    probes = get_probes()
    assert "FUTURE_KEY" not in probes
    assert "OK_KEY" in probes


@pytest.mark.parametrize("probe_spec", [
    pytest.param("{kind: subprocess, cmd: [tool, status]}", id="missing-binary"),
    pytest.param("{kind: subprocess, binary: tool}", id="missing-cmd"),
    pytest.param("{kind: python}", id="missing-script"),
])
def test_known_probe_kind_missing_subkeys_doesnt_kill_registry(
    tmp_path: Path, package_skills: Path,
    caplog: pytest.LogCaptureFixture, probe_spec: str,
):
    (tmp_path / "skills" / "broken").mkdir(parents=True)
    (tmp_path / "skills" / "later").mkdir(parents=True)
    manifest = package_skills / "broken" / "credentials.yaml"
    _write_manifest(manifest, f"""
        credentials:
          - name: BROKEN_KEY
            cred_type: D
            probe: {probe_spec}
          - name: SAME_MANIFEST_KEY
            cred_type: D
            probe: {{kind: not_implemented}}
    """)
    _write_manifest(package_skills / "later" / "credentials.yaml", """
        credentials:
          - name: LATER_MANIFEST_KEY
            cred_type: D
            probe: {kind: not_implemented}
    """)

    probes = get_probes(home=tmp_path)

    assert set(probes) == {"SAME_MANIFEST_KEY", "LATER_MANIFEST_KEY"}
    assert any(
        record.name == "mimir.cred_verify"
        and record.levelname == "WARNING"
        and "credentials_manifest_skipped" in record.getMessage()
        and str(manifest) in record.getMessage()
        and "BROKEN_KEY" in record.getMessage()
        for record in caplog.records
    )


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


def test_installed_social_cli_loads_packaged_python_probe(tmp_path, monkeypatch):
    (tmp_path / "skills" / "social-cli").mkdir(parents=True)
    monkeypatch.setenv("ATPROTO_APP_PASSWORD", "abcd-efgh-ijkl-mnop")
    result = verify("BSKY_APP_PASSWORD", home=tmp_path)
    assert result.ok, result.detail
    assert get_probes(tmp_path)["BSKY_APP_PASSWORD"].source == str(
        cred_verify._PACKAGE_SKILLS_ROOT / "social-cli" / "credentials.yaml"
    )


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
    monkeypatch.setattr(cred_verify, "_PACKAGE_MANIFEST", tmp_path / "skills" / "fake" / "credentials.yaml")
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
    monkeypatch.setattr(cred_verify, "_PACKAGE_MANIFEST", tmp_path / "skills" / "fake" / "credentials.yaml")
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
    monkeypatch.setattr(cred_verify, "_PACKAGE_MANIFEST", tmp_path / "skills" / "fake" / "credentials.yaml")
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
