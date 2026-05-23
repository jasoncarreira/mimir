"""Credential rotation orchestration (SPEC §16 item 14, Phase 3).

Tests exercise the compose.env edit + audit + rollback paths against
a synthetic deployment dir. ``docker compose`` calls are stubbed so
the tests don't depend on a running daemon.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from mimir import cred_rotate, cred_verify


@pytest.fixture(autouse=True)
def _reset_probe_cache():
    cred_verify.reset_probes_cache()
    yield
    cred_verify.reset_probes_cache()


@pytest.fixture
def deployment(tmp_path: Path) -> Path:
    """Synthetic deployment directory with a minimal compose.env +
    compose.yml. The compose file has exactly one service so
    ``_resolve_service_name`` doesn't need ``--service``."""
    compose_env = tmp_path / "compose.env"
    compose_env.write_text(textwrap.dedent("""
        # Header comment
        GITHUB_TOKEN=ghp_OLD_VALUE_XXXXXXXXXXXXXX
        # Blank line below

        OTHER_VAR=stable
        ATPROTO_HANDLE=alice.bsky.social
    """).lstrip())
    compose_yml = tmp_path / "compose.yml"
    compose_yml.write_text(textwrap.dedent("""
        services:
          agent:
            image: ./
            env_file: compose.env
    """).lstrip())
    return tmp_path


# ── compose.env atomic edit ──────────────────────────────────────────


def test_atomic_replace_preserves_surrounding_lines(deployment: Path):
    compose_env = deployment / "compose.env"
    original = compose_env.read_text()

    old, backup = cred_rotate._atomic_replace_env(
        compose_env, "GITHUB_TOKEN", "ghp_NEW_VALUE",
    )

    assert old == "ghp_OLD_VALUE_XXXXXXXXXXXXXX"
    assert backup.is_file()
    assert backup.read_text() == original

    new_content = compose_env.read_text()
    # The matching line is replaced; everything else preserved.
    assert "GITHUB_TOKEN=ghp_NEW_VALUE" in new_content
    assert "ghp_OLD_VALUE" not in new_content
    assert "# Header comment" in new_content  # comment preserved
    assert "OTHER_VAR=stable" in new_content
    assert "ATPROTO_HANDLE=alice.bsky.social" in new_content


def test_atomic_replace_appends_when_var_absent(deployment: Path):
    """If the env var isn't in the file yet, append it. Operator
    adding a new credential mid-rotation is legal."""
    compose_env = deployment / "compose.env"
    cred_rotate._atomic_replace_env(compose_env, "NEWLY_ADDED_KEY", "value-1")
    content = compose_env.read_text()
    assert "NEWLY_ADDED_KEY=value-1\n" in content
    # Original lines still intact.
    assert "GITHUB_TOKEN=ghp_OLD_VALUE_XXXXXXXXXXXXXX" in content


def test_atomic_replace_creates_timestamped_backup(deployment: Path):
    compose_env = deployment / "compose.env"
    original = compose_env.read_text()
    _, backup = cred_rotate._atomic_replace_env(compose_env, "GITHUB_TOKEN", "NEW")
    assert backup.name.startswith("compose.env.bak.")
    assert backup.read_text() == original


def test_atomic_replace_only_changes_first_match(deployment: Path):
    """Duplicate ``GITHUB_TOKEN=...`` lines in compose.env (rare but
    legal) — only the first is replaced so we don't accidentally
    mutate a commented-out duplicate or a placeholder later in the
    file. Subsequent duplicates can be handled by a separate operator
    cleanup pass."""
    compose_env = deployment / "compose.env"
    compose_env.write_text(
        "GITHUB_TOKEN=first\n"
        "GITHUB_TOKEN=second\n"
    )
    cred_rotate._atomic_replace_env(compose_env, "GITHUB_TOKEN", "NEW")
    lines = compose_env.read_text().splitlines()
    assert lines[0] == "GITHUB_TOKEN=NEW"
    assert lines[1] == "GITHUB_TOKEN=second"


def test_read_env_value(deployment: Path):
    compose_env = deployment / "compose.env"
    assert cred_rotate._read_env_value(compose_env, "GITHUB_TOKEN") == "ghp_OLD_VALUE_XXXXXXXXXXXXXX"
    assert cred_rotate._read_env_value(compose_env, "NOPE") is None


# ── deployment dir resolution ────────────────────────────────────────


def test_resolve_compose_file_finds_compose_yml(deployment: Path):
    assert cred_rotate._resolve_compose_file(deployment).name == "compose.yml"


def test_resolve_compose_file_missing_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        cred_rotate._resolve_compose_file(tmp_path)


def test_resolve_service_uses_explicit_request_unchanged(deployment: Path):
    """Operator-provided --service wins, even if the compose file
    would otherwise auto-detect a different one."""
    compose_file = deployment / "compose.yml"
    assert cred_rotate._resolve_service_name(compose_file, "foo") == "foo"


def test_resolve_service_single_service_auto_detected(deployment: Path):
    compose_file = deployment / "compose.yml"
    assert cred_rotate._resolve_service_name(compose_file, None) == "agent"


def test_resolve_service_multi_service_requires_explicit(deployment: Path):
    compose_file = deployment / "compose.yml"
    compose_file.write_text(textwrap.dedent("""
        services:
          one:
            image: a
          two:
            image: b
    """).lstrip())
    with pytest.raises(RuntimeError, match="Multiple services"):
        cred_rotate._resolve_service_name(compose_file, None)


# ── audit log ────────────────────────────────────────────────────────


def test_emit_writes_jsonl(deployment: Path):
    cred_rotate._emit(deployment, "credential_rotation_started",
                      env="GITHUB_TOKEN", new_value_hash="sha256:abc123")
    log = (deployment / "rotations.jsonl").read_text()
    record = json.loads(log.strip())
    assert record["type"] == "credential_rotation_started"
    assert record["env"] == "GITHUB_TOKEN"
    assert "timestamp" in record


def test_value_hash_is_sha256_prefix():
    h = cred_rotate._value_hash("secret-value")
    assert h.startswith("sha256:")
    # 12-char hex prefix.
    assert len(h.split(":", 1)[1]) == 12


# ── full rotation flow (docker mocked) ──────────────────────────────


@pytest.fixture
def fake_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deployment: Path):
    """Discover a credentials.yaml that lists GITHUB_TOKEN so the
    rotate flow's cred-lookup succeeds + verify is exercised."""
    monkeypatch.setenv("MIMIR_HOME", str(deployment))
    skill_dir = deployment / "skills" / "core"
    skill_dir.mkdir(parents=True)
    (skill_dir / "credentials.yaml").write_text(textwrap.dedent("""
        credentials:
          - name: GITHUB_TOKEN
            cred_type: A
            env_vars: [GITHUB_TOKEN]
            description: ""
            probe:
              kind: format
              env: GITHUB_TOKEN
              prefix: ghp_
              min_len: 20
    """))
    # Replace the package manifest so the test only sees our fixture.
    monkeypatch.setattr(
        cred_verify, "_PACKAGE_MANIFEST", deployment / "no-such-file.yaml",
    )
    cred_verify.reset_probes_cache()
    return deployment


def test_rotate_happy_path(
    fake_registry: Path, monkeypatch: pytest.MonkeyPatch,
):
    """End-to-end: docker calls all succeed, verify reports ok, the
    rotation completes and the new value is in compose.env."""
    calls: list[list[str]] = []

    def fake_docker_compose(compose_file, *args, capture=True, timeout=120):
        calls.append(list(args))
        if args[0] == "up":
            return (0, "", "")
        if args[0] == "ps":
            return (0, json.dumps({"Service": "agent", "State": "running"}), "")
        if args[0] == "exec":
            return (0, "[A] OK  GITHUB_TOKEN: gh ok", "")
        return (0, "", "")

    monkeypatch.setattr(cred_rotate, "_docker_compose", fake_docker_compose)

    rc = cred_rotate.run_rotate(
        env_name="GITHUB_TOKEN",
        new_value="ghp_NEW_VALUE_XXXXXXXX",
        deployment_dir=fake_registry,
    )
    assert rc == 0
    # compose.env actually changed.
    assert "ghp_NEW_VALUE_XXXXXXXX" in (fake_registry / "compose.env").read_text()
    # Audit trail recorded the right events.
    log = (fake_registry / "rotations.jsonl").read_text().splitlines()
    types = [json.loads(line)["type"] for line in log]
    assert "credential_rotation_started" in types
    assert "credential_rotation_completed" in types
    # docker compose was invoked: up + ps + exec at minimum.
    invoked_verbs = {c[0] for c in calls}
    assert {"up", "ps", "exec"}.issubset(invoked_verbs)


def test_rotate_rollback_on_verify_failure(
    fake_registry: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Verify fails post-rotation → compose.env is restored from
    the backup, container is recreated again from the old value."""
    original_env = (fake_registry / "compose.env").read_text()

    recreate_calls = 0

    def fake_docker_compose(compose_file, *args, capture=True, timeout=120):
        nonlocal recreate_calls
        if args[0] == "up":
            recreate_calls += 1
            return (0, "", "")
        if args[0] == "ps":
            return (0, json.dumps({"Service": "agent", "State": "running"}), "")
        if args[0] == "exec":
            # Verify-cred inside the container returns nonzero =
            # the new value is bad (or the probe says so).
            return (1, "", "[A] FAIL  GITHUB_TOKEN: bad value")
        return (0, "", "")

    monkeypatch.setattr(cred_rotate, "_docker_compose", fake_docker_compose)

    rc = cred_rotate.run_rotate(
        env_name="GITHUB_TOKEN",
        new_value="ghp_PROBABLY_BAD_VALUE",
        deployment_dir=fake_registry,
    )
    assert rc == 1
    # compose.env was rolled back to the original content.
    assert (fake_registry / "compose.env").read_text() == original_env
    # Recreate was called twice — once for the (failed) rotation,
    # once to revert to the old value.
    assert recreate_calls == 2
    # Audit trail recorded the failure.
    log = (fake_registry / "rotations.jsonl").read_text().splitlines()
    types = [json.loads(line)["type"] for line in log]
    assert "credential_rotation_started" in types
    assert "credential_rotation_failed" in types


def test_rotate_recreate_failure_rolls_back_immediately(
    fake_registry: Path, monkeypatch: pytest.MonkeyPatch,
):
    """If the first ``docker compose up`` fails, we don't get to the
    verify step — rollback happens immediately."""
    original_env = (fake_registry / "compose.env").read_text()

    def fake_docker_compose(compose_file, *args, capture=True, timeout=120):
        if args[0] == "up":
            # Fail the first attempt (the rotation); subsequent
            # rollback recreate succeeds.
            if "ghp_NEW" in (fake_registry / "compose.env").read_text():
                return (1, "", "image build failed")
            return (0, "", "")
        if args[0] == "ps":
            return (0, json.dumps({"Service": "agent", "State": "running"}), "")
        return (0, "", "")

    monkeypatch.setattr(cred_rotate, "_docker_compose", fake_docker_compose)

    rc = cred_rotate.run_rotate(
        env_name="GITHUB_TOKEN",
        new_value="ghp_NEW_VALUE_XX",
        deployment_dir=fake_registry,
    )
    assert rc == 1
    assert (fake_registry / "compose.env").read_text() == original_env
    log = (fake_registry / "rotations.jsonl").read_text().splitlines()
    types = [json.loads(line)["type"] for line in log]
    assert "credential_rotation_failed" in types


def test_rotate_no_recreate_skips_docker(
    fake_registry: Path, monkeypatch: pytest.MonkeyPatch,
):
    """``--no-recreate`` only edits compose.env and emits the audit
    events; no docker compose calls."""
    called = False

    def fake_docker_compose(*args, **kwargs):
        nonlocal called
        called = True
        return (0, "", "")

    monkeypatch.setattr(cred_rotate, "_docker_compose", fake_docker_compose)
    rc = cred_rotate.run_rotate(
        env_name="GITHUB_TOKEN",
        new_value="ghp_NEW_VALUE_XXXX",
        deployment_dir=fake_registry,
        skip_recreate=True,
    )
    assert rc == 0
    assert not called
    assert "ghp_NEW_VALUE_XXXX" in (fake_registry / "compose.env").read_text()
    log = (fake_registry / "rotations.jsonl").read_text().splitlines()
    types = [json.loads(line)["type"] for line in log]
    assert "credential_rotation_started" in types
    assert "credential_rotation_completed" in types


def test_rotate_warns_on_unregistered_env(
    deployment: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
):
    """If the env var isn't in any credentials.yaml manifest, the
    rotation still proceeds (with a warning) but skips post-rotation
    verify."""
    monkeypatch.setenv("MIMIR_HOME", str(deployment))
    monkeypatch.setattr(
        cred_verify, "_PACKAGE_MANIFEST", deployment / "no-such-file.yaml",
    )
    cred_verify.reset_probes_cache()

    def fake_docker_compose(compose_file, *args, capture=True, timeout=120):
        if args[0] == "up":
            return (0, "", "")
        if args[0] == "ps":
            return (0, json.dumps({"Service": "agent", "State": "running"}), "")
        # ``exec`` should NOT be called when no cred is registered.
        raise AssertionError(f"unexpected docker compose call: {args}")

    monkeypatch.setattr(cred_rotate, "_docker_compose", fake_docker_compose)

    rc = cred_rotate.run_rotate(
        env_name="OTHER_VAR",
        new_value="new-value",
        deployment_dir=deployment,
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "not listed by any credentials.yaml" in err


def test_rotate_empty_value_aborts(
    fake_registry: Path,
):
    rc = cred_rotate.run_rotate(
        env_name="GITHUB_TOKEN", new_value="",
        deployment_dir=fake_registry,
    )
    assert rc == 2


def test_rotate_missing_compose_env_exits_2(tmp_path: Path):
    """No compose.env in the deployment dir → invalid input, exit 2."""
    rc = cred_rotate.run_rotate(
        env_name="GITHUB_TOKEN", new_value="x",
        deployment_dir=tmp_path,
    )
    assert rc == 2
