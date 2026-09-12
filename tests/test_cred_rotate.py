"""Credential rotation orchestration (SPEC §16 item 14, Phase 3).

Tests exercise the compose.env edit + audit + rollback paths against
a synthetic deployment dir. ``docker compose`` calls are stubbed so
the tests don't depend on a running daemon.
"""

from __future__ import annotations

import json
import os
import stat
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
        DISCORD_TOKEN=old-revoked-token
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


def test_atomic_replace_backups_are_unique_within_same_clock_tick(
    deployment: Path, monkeypatch: pytest.MonkeyPatch,
):
    compose_env = deployment / "compose.env"
    original = compose_env.read_text()
    monkeypatch.setattr(cred_rotate.time, "time_ns", lambda: 123456789)

    _, first = cred_rotate._atomic_replace_env(compose_env, "GITHUB_TOKEN", "FIRST")
    _, second = cred_rotate._atomic_replace_env(compose_env, "GITHUB_TOKEN", "SECOND")

    assert first != second
    assert first.read_text() == original
    assert "GITHUB_TOKEN=FIRST" in second.read_text()


def test_atomic_replace_retains_only_ten_newest_backups(deployment: Path):
    compose_env = deployment / "compose.env"
    for index in range(12):
        (deployment / f"compose.env.bak.{index:02d}").write_text(f"old-{index}")

    _, current = cred_rotate._atomic_replace_env(compose_env, "GITHUB_TOKEN", "NEW")
    backups = list(deployment.glob("compose.env.bak.*"))

    assert len(backups) == cred_rotate._KEEP_ROTATION_BACKUPS
    assert current in backups
    assert not (deployment / "compose.env.bak.00").exists()
    assert not (deployment / "compose.env.bak.01").exists()
    assert not (deployment / "compose.env.bak.02").exists()


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
                      env="GITHUB_TOKEN", rotation_id="fake-rotation-id")
    log = (deployment / "rotations.jsonl").read_text()
    record = json.loads(log.strip())
    assert record["type"] == "credential_rotation_started"
    assert record["env"] == "GITHUB_TOKEN"
    assert "timestamp" in record


# ── full rotation flow (docker mocked) ──────────────────────────────


@pytest.mark.parametrize("existing_mode", [None, 0o644, 0o666, 0o600])
def test_emit_private_before_writing(deployment, monkeypatch, existing_mode):
    path = deployment / "rotations.jsonl"
    previous = '{"type": "previous"}\n'
    if existing_mode is not None:
        path.write_text(previous)
        path.chmod(existing_mode)
    dumps = json.dumps
    os_open = os.open

    def checked_open(file, flags, mode=0o777):
        fd = os_open(file, flags, mode)
        if existing_mode is None:
            try:
                assert stat.S_IMODE(os.fstat(fd).st_mode) == 0o600
            except BaseException:
                os.close(fd)
                raise
        return fd

    def checked_dumps(record):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        return dumps(record)

    monkeypatch.setattr(cred_rotate.json, "dumps", checked_dumps)
    monkeypatch.setattr(cred_rotate.os, "open", checked_open)
    old_umask = os.umask(0)
    try:
        cred_rotate._emit(deployment, "first", detail="Authenticated as alice")
        cred_rotate._emit(deployment, "second")
    finally:
        os.umask(old_umask)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [record["type"] for record in records] == (
        (["previous"] if existing_mode is not None else []) + ["first", "second"]
    )
    assert records[-2]["detail"] == "Authenticated as alice"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_emit_redacts_at_audit_sink(deployment):
    secret = "ghp_" + "a" * 36
    cred_rotate._emit(deployment, "probe", detail=f"failure {secret}", verify="clean result")
    record = json.loads((deployment / "rotations.jsonl").read_text())
    assert record["detail"] == "failure [REDACTED]"
    assert record["verify"] == "clean result"


def test_emit_does_not_write_when_permissions_cannot_be_secured(
    deployment, monkeypatch, capsys,
):
    path = deployment / "rotations.jsonl"
    previous = '{"type": "previous"}\n'
    path.write_text(previous)
    path.chmod(0o644)

    def denied(fd, mode):
        raise PermissionError("cannot secure audit file")

    monkeypatch.setattr(cred_rotate.os, "fchmod", denied)
    cred_rotate._emit(deployment, "new")
    assert path.read_text() == previous
    assert "warn: failed to write rotations.jsonl" in capsys.readouterr().err


@pytest.mark.parametrize("rc", [0, 1])
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("secret", [None, "ghp_" + "a" * 36])
def test_rotate_redacts_probe_output(
    fake_registry, monkeypatch, capsys, rc, stream, secret,
):
    detail = "Authenticated as alice" + (f" using {secret}" if secret else "")
    expected = "Authenticated as alice" + (" using [REDACTED]" if secret else "")

    def docker(compose_file, *args, **kwargs):
        if args[0] == "up":
            return 0, "", ""
        if args[0] == "ps":
            return 0, json.dumps({"Service": "agent", "State": "running"}), ""
        assert args[0] == "exec"
        return rc, detail if stream == "stdout" else "", detail if stream == "stderr" else ""

    monkeypatch.setattr(cred_rotate, "_docker_compose", docker)
    assert cred_rotate.run_rotate(
        "GITHUB_TOKEN", new_value="replacement", deployment_dir=fake_registry,
    ) == rc
    audit = (fake_registry / "rotations.jsonl").read_text()
    record = json.loads(audit.splitlines()[-1])
    assert record["verify" if rc == 0 else "detail"] == expected
    output = capsys.readouterr()
    assert expected in (output.out if rc == 0 else output.err)
    if secret:
        assert secret not in audit + output.out + output.err


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
    # The operator entry shadows only GITHUB_TOKEN; shipped entries remain visible.
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
    records = [json.loads(line) for line in log]
    started = next(row for row in records if row["type"] == "credential_rotation_started")
    completed = next(row for row in records if row["type"] == "credential_rotation_completed")
    assert started["rotation_id"] == completed["rotation_id"]
    assert "old_value_hash" not in started
    assert "new_value_hash" not in started
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
    records = [json.loads(line) for line in log]
    types = [record["type"] for record in records]
    assert "credential_rotation_started" in types
    assert "credential_rotation_failed" in types
    failure = next(
        record for record in records
        if record["type"] == "credential_rotation_failed"
    )
    assert failure["stage"] == "verify"
    assert failure["rolled_back"] is True


def test_rotate_skips_unimplemented_probe_from_shipped_registry(
    deployment: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
):
    """A shipped not_implemented probe is an explicit skip, not failure."""
    monkeypatch.setenv("MIMIR_HOME", str(deployment))
    recreate_calls = 0

    def fake_docker_compose(compose_file, *args, capture=True, timeout=120):
        nonlocal recreate_calls
        if args[0] == "up":
            recreate_calls += 1
            return (0, "", "")
        if args[0] == "ps":
            return (0, json.dumps({"Service": "agent", "State": "running"}), "")
        raise AssertionError(f"unexpected docker compose call: {args}")

    monkeypatch.setattr(cred_rotate, "_docker_compose", fake_docker_compose)

    rc = cred_rotate.run_rotate(
        env_name="DISCORD_TOKEN",
        new_value="new-working-token",
        deployment_dir=deployment,
    )

    assert rc == 0
    assert "DISCORD_TOKEN=new-working-token" in (deployment / "compose.env").read_text()
    assert recreate_calls == 1
    output = capsys.readouterr().out
    assert "verification skipped" in output
    assert "DISCORD_TOKEN" in output

    records = [
        json.loads(line)
        for line in (deployment / "rotations.jsonl").read_text().splitlines()
    ]
    assert [record["type"] for record in records] == [
        "credential_rotation_started",
        "credential_rotation_completed",
    ]
    assert records[-1]["verify"] == (
        "verification skipped (no probe implemented for DISCORD_TOKEN)"
    )


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
