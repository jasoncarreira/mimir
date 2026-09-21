from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import socket
import subprocess
import threading

import pytest
import yaml

from mimir import providers
from mimir.startup_validation import (
    ProbeObservation,
    STARTUP_REQUIREMENTS,
    StartupEnvironment,
    StartupStatus,
    build_requirement_registry,
    startup_requirement_registry,
    validate_startup,
)
from mimir.worklink.worker_client import EXECUTOR_PROTOCOL_IDENTITY


EXPECTED_TABLE = (
    ("coding.feature_state", "Existing switch", "enabled/disabled", "Boolean", "Configure as intended", "Always", "Pass; disabled skips below"),
    ("coding.opencode.executable", "PATH/realpath/file/mode", "Executable", "Path/missing", "Install pinned OpenCode", "Coding", "Fatal"),
    ("coding.opencode.version", "fixed --version, 5s", "1.18.21", "Version/failure", "Install 1.18.21", "Coding", "Fatal"),
    ("coding.git.executable", "/usr/bin/git lstat", "Regular executable", "Type/mode", "Install Git", "Coding", "Fatal"),
    ("coding.pr_checkout_lease_root", "Existing path/write probe", "Valid root", "Path/result", "Repair config", "Coding", "Fatal; not containment"),
    ("coding.github.identity", "Existing identity probe", "Resolved", "Login/reason", "Repair credentials", "Coding", "Warning"),
    ("coding.repositories.inventory", "Inventory load", "Strict canonical schema", "Named result", "Correct inventory", "Coding+repos", "Fatal"),
    ("coding.repositories.root_mode_agreement", "Canonical path→mode maps", "Exact equality", "Both maps", "Align roots/modes", "Coding+repos", "Fatal"),
    ("coding.worklink.target_rw_unique", "Resolve target", "One canonical rw", "Target/path/count/mode", "Correct target", "Ready queue", "Fatal"),
    ("coding.worklink.git_binding", "Top-level/origin", "Exact root/origin", "Expected/actual", "Repair binding", "Ready queue", "Fatal"),
    ("coding.factory.package_versions", "Entrypoint/manifests", "Package-bound, both 0.9.2", "Paths/versions", "Install 0.9.2", "Factory recovery", "Fatal"),
    ("coding.factory.command_contract", "Isolated probe", "Exact 16 commands", "Commands/failure", "Install compatible factory", "Factory recovery", "Fatal"),
    ("coding.worklink.worker_protocol", "Handshake", "Matching protocols", "Both versions", "Rebuild worker", "Retained recovery", "Fatal"),
)


def test_published_startup_requirement_registry_is_exact_and_stable() -> None:
    assert tuple(
        (
            row.name,
            row.probe,
            row.expected,
            row.observed,
            row.remediation,
            row.applicability,
            row.failure_behavior,
        )
        for row in startup_requirement_registry()
    ) == EXPECTED_TABLE
    assert startup_requirement_registry() is STARTUP_REQUIREMENTS


def test_requirement_registry_rejects_duplicate_stable_names() -> None:
    duplicate = replace(STARTUP_REQUIREMENTS[1], probe="different")
    with pytest.raises(ValueError, match="duplicate startup requirement name"):
        build_requirement_registry((STARTUP_REQUIREMENTS[1], duplicate))


def _all_applicable_environment(tmp_path: Path) -> StartupEnvironment:
    return StartupEnvironment(
        home=tmp_path,
        coding_enabled=True,
        repositories_configured=True,
        ready_queue_enabled=True,
        factory_recovery_enabled=True,
        retained_recovery_enabled=True,
        worklink_repository="owner/repo",
    )


def _passing_overrides() -> dict[str, ProbeObservation]:
    return {
        requirement.name: ProbeObservation(True, f"observed:{requirement.name}")
        for requirement in STARTUP_REQUIREMENTS
    }


@pytest.mark.parametrize(
    "requirement_name",
    [pytest.param(row.name, id=row.name) for row in STARTUP_REQUIREMENTS[1:]],
)
def test_every_named_startup_requirement_reports_its_missing_case(
    tmp_path: Path, requirement_name: str,
) -> None:
    overrides = _passing_overrides()
    overrides[requirement_name] = ProbeObservation(False, "missing", "fixture missing")

    report = validate_startup(
        _all_applicable_environment(tmp_path), probe_overrides=overrides
    )

    check = report.check(requirement_name)
    expected = (
        StartupStatus.WARNING
        if requirement_name == "coding.github.identity"
        else StartupStatus.FATAL
    )
    assert check.status is expected
    assert check.observed == "missing"
    assert check.detail == "fixture missing"
    assert [item.name for item in report.checks] == [row.name for row in STARTUP_REQUIREMENTS]


def test_startup_report_is_deterministic_and_fatal_names_are_observable(tmp_path: Path) -> None:
    overrides = _passing_overrides()
    overrides["coding.opencode.version"] = ProbeObservation(False, "1.18.9")
    environment = _all_applicable_environment(tmp_path)

    first = validate_startup(environment, probe_overrides=overrides)
    second = validate_startup(environment, probe_overrides=overrides)

    assert first == second
    assert [check.name for check in first.fatal] == ["coding.opencode.version"]
    with pytest.raises(RuntimeError, match="coding.opencode.version.*1.18.9"):
        first.require_success()


def test_coding_disabled_probes_only_feature_state_and_adds_no_requirement(
    tmp_path: Path,
) -> None:
    called: list[str] = []

    def forbidden(name: str):
        def probe() -> ProbeObservation:
            called.append(name)
            raise AssertionError("disabled probe ran")
        return probe

    overrides = {
        row.name: forbidden(row.name)
        for row in STARTUP_REQUIREMENTS[1:]
    }
    report = validate_startup(
        StartupEnvironment(
            home=tmp_path,
            coding_enabled=False,
            repositories_configured=True,
            ready_queue_enabled=True,
            factory_recovery_enabled=True,
            retained_recovery_enabled=True,
        ),
        probe_overrides=overrides,
    )

    assert called == []
    assert report.checks[0].status is StartupStatus.PASS
    assert report.checks[0].observed == "disabled"
    assert all(check.status is StartupStatus.SKIPPED for check in report.checks[1:])
    assert all(check.observed == "unprobed" for check in report.checks[1:])


def test_optional_scopes_are_skipped_without_running_their_probes(tmp_path: Path) -> None:
    overrides = _passing_overrides()
    report = validate_startup(
        StartupEnvironment(home=tmp_path, coding_enabled=True),
        probe_overrides=overrides,
    )

    statuses = {check.name: check.status for check in report.checks}
    assert statuses["coding.opencode.version"] is StartupStatus.PASS
    assert statuses["coding.repositories.inventory"] is StartupStatus.SKIPPED
    assert statuses["coding.worklink.target_rw_unique"] is StartupStatus.SKIPPED
    assert statuses["coding.factory.package_versions"] is StartupStatus.SKIPPED
    assert statuses["coding.worklink.worker_protocol"] is StartupStatus.SKIPPED


def test_documented_registry_names_every_closed_requirement() -> None:
    documentation = (
        Path(__file__).resolve().parents[1] / "docs/coding-startup-requirements.md"
    ).read_text(encoding="utf-8")
    for requirement in STARTUP_REQUIREMENTS:
        assert documentation.count(f"`{requirement.name}`") == 1


@pytest.fixture(autouse=True)
def _forbid_external_startup_access(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("startup validation attempted external access")

    monkeypatch.setattr("requests.sessions.Session.request", forbidden)


def _basic_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, version: str = "1.18.21",
) -> StartupEnvironment:
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir(exist_ok=True)
    opencode = binary_dir / "opencode"
    opencode.write_text(f"#!/bin/sh\nprintf '%s\\n' '{version}'\n", encoding="utf-8")
    opencode.chmod(0o755)
    lease = tmp_path / "leases"
    lease.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", f"{binary_dir}:/usr/bin:/bin")
    monkeypatch.setenv("MIMIR_PR_CHECKOUT_LEASE_ROOT", str(lease))
    monkeypatch.setenv("MIMIR_GITHUB_SELF_LOGIN", "local-stub")
    monkeypatch.setattr("mimir.tools.forge.github_identity_is_degraded", lambda: False)
    return StartupEnvironment(home=tmp_path, coding_enabled=True)


def _write_repository_inventory(
    home: Path, checkout: Path, *, mode: str = "rw", origin: str = "https://github.com/owner/repo.git",
) -> None:
    (home / "repositories.yaml").write_text(
        yaml.safe_dump({"repositories": [{
            "slug": "owner/repo",
            "root": str(checkout),
            "mode": mode,
            "origin": origin,
            "base_branch": "main",
        }]}),
        encoding="utf-8",
    )
    (home / "worklink.yaml").write_text("repository: owner/repo\n", encoding="utf-8")


def _init_repository(path: Path, origin: str = "https://github.com/owner/repo.git") -> None:
    path.mkdir()
    subprocess.run(["/usr/bin/git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["/usr/bin/git", "-C", str(path), "remote", "add", "origin", origin],
        check=True,
    )


def test_production_feature_state_disables_every_dependent_probe(tmp_path: Path) -> None:
    report = validate_startup(StartupEnvironment(home=tmp_path, coding_enabled=False))
    assert report.check("coding.feature_state").observed == "disabled"
    assert all(check.status is StartupStatus.SKIPPED for check in report.checks[1:])


def test_production_opencode_executable_missing_is_named_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _basic_environment(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", "")
    assert validate_startup(environment).check("coding.opencode.executable").status is StartupStatus.FATAL


def test_historical_opencode_mismatch_is_startup_fatal_before_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _basic_environment(tmp_path, monkeypatch, version="1.18.9")
    monkeypatch.setenv("MIMIR_HOME", str(tmp_path))
    monkeypatch.setattr(
        "mimir.startup_validation.current_startup_environment", lambda: environment
    )
    monkeypatch.setattr(
        "mimir.tools.forge.initialize_github_forge_identity", lambda: True
    )
    registered = False

    with pytest.raises(RuntimeError, match="coding.opencode.version.*1.18.9"):
        providers.opencode_available()
        registered = True

    assert registered is False


def test_production_git_executable_missing_is_named_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = replace(
        _basic_environment(tmp_path, monkeypatch),
        git_executable=tmp_path / "missing-git",
    )
    assert validate_startup(environment).check("coding.git.executable").status is StartupStatus.FATAL


def test_production_lease_root_missing_is_named_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _basic_environment(tmp_path, monkeypatch)
    monkeypatch.delenv("MIMIR_PR_CHECKOUT_LEASE_ROOT")
    assert validate_startup(environment).check("coding.pr_checkout_lease_root").status is StartupStatus.FATAL


def test_production_github_identity_missing_is_named_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _basic_environment(tmp_path, monkeypatch)
    monkeypatch.delenv("MIMIR_GITHUB_SELF_LOGIN")
    assert validate_startup(environment).check("coding.github.identity").status is StartupStatus.WARNING


@pytest.mark.parametrize("contents", [None, "{}\n", "repositories: [\n"])
def test_applicable_missing_or_malformed_inventory_is_named_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contents: str | None,
) -> None:
    environment = replace(
        _basic_environment(tmp_path, monkeypatch), repositories_configured=True
    )
    if contents is not None:
        (tmp_path / "repositories.yaml").write_text(contents, encoding="utf-8")
    check = validate_startup(environment).check("coding.repositories.inventory")
    assert check.status is StartupStatus.FATAL
    assert check.observed in {"missing", "malformed", "ParserError"}


def test_production_root_mode_disagreement_is_named_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _basic_environment(tmp_path, monkeypatch)
    checkout = tmp_path / "repo"
    _write_repository_inventory(tmp_path, checkout)
    environment = replace(
        environment,
        repositories_configured=True,
        authorized_root_modes=((str(checkout), "ro"),),
    )
    assert validate_startup(environment).check(
        "coding.repositories.root_mode_agreement"
    ).status is StartupStatus.FATAL


def test_production_ready_queue_target_ro_is_named_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _basic_environment(tmp_path, monkeypatch)
    checkout = tmp_path / "repo"
    _write_repository_inventory(tmp_path, checkout, mode="ro")
    environment = replace(
        environment,
        repositories_configured=True,
        ready_queue_enabled=True,
        worklink_repository="owner/repo",
        authorized_root_modes=((str(checkout), "ro"),),
    )
    assert validate_startup(environment).check(
        "coding.worklink.target_rw_unique"
    ).status is StartupStatus.FATAL


def test_production_git_binding_mismatch_is_named_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _basic_environment(tmp_path, monkeypatch)
    checkout = tmp_path / "repo"
    _init_repository(checkout, "https://github.com/owner/other.git")
    _write_repository_inventory(tmp_path, checkout)
    environment = replace(
        environment,
        repositories_configured=True,
        ready_queue_enabled=True,
        worklink_repository="owner/repo",
        authorized_root_modes=((str(checkout), "rw"),),
    )
    assert validate_startup(environment).check(
        "coding.worklink.git_binding"
    ).status is StartupStatus.FATAL


def _factory_fixture(root: Path, version: str, script: str = "") -> Path:
    modules = root / "node_modules"
    factory = modules / "feature-factory"
    plugin = modules / "opencode-feature-factory"
    (factory / "bin").mkdir(parents=True)
    plugin.mkdir(parents=True)
    entrypoint = factory / "bin" / "factory.js"
    entrypoint.write_text(script, encoding="utf-8")
    for path, name in (
        (factory, "feature-factory"),
        (plugin, "opencode-feature-factory"),
    ):
        (path / "package.json").write_text(
            json.dumps({"name": name, "version": version}), encoding="utf-8"
        )
    return entrypoint


def test_historical_factory_mismatch_is_startup_fatal_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = replace(
        _basic_environment(tmp_path, monkeypatch),
        factory_recovery_enabled=True,
        factory_entrypoint=_factory_fixture(tmp_path, "0.9.1"),
    )
    report = validate_startup(environment)
    dispatched = False

    with pytest.raises(RuntimeError, match="coding.factory.package_versions"):
        report.require_success()
        dispatched = True

    assert dispatched is False
    assert report.check("coding.factory.package_versions").observed[
        "feature_factory_version"
    ] == "0.9.1"


def test_production_factory_command_contract_missing_is_named_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = "console.error('unknown command ' + process.argv[2]); process.exit(1);\n"
    environment = replace(
        _basic_environment(tmp_path, monkeypatch),
        factory_recovery_enabled=True,
        factory_entrypoint=_factory_fixture(tmp_path, "0.9.2", script),
    )
    assert validate_startup(environment).check(
        "coding.factory.command_contract"
    ).status is StartupStatus.FATAL


def _worker_server(path: Path, identity: str) -> threading.Thread:
    ready = threading.Event()

    def serve() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as server:
            server.bind(str(path))
            server.listen(1)
            ready.set()
            connection, _ = server.accept()
            with connection:
                connection.recv(4096)
                connection.send(json.dumps({
                    "status": "identity",
                    "executor_identity": identity,
                    "source_commit": "a" * 40,
                }).encode())

    thread = threading.Thread(target=serve)
    thread.start()
    assert ready.wait(timeout=2)
    return thread


@pytest.mark.parametrize(
    ("identity", "expected_status"),
    [
        (EXECUTOR_PROTOCOL_IDENTITY, StartupStatus.PASS),
        ("worklink-executor-v10-historical", StartupStatus.FATAL),
    ],
)
def test_production_worker_handshake_reports_actual_matching_or_mismatched_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity: str,
    expected_status: StartupStatus,
) -> None:
    environment = replace(
        _basic_environment(tmp_path, monkeypatch),
        retained_recovery_enabled=True,
        executor_socket=tmp_path / "worker.sock",
    )
    thread = _worker_server(environment.executor_socket, identity)

    check = validate_startup(environment).check("coding.worklink.worker_protocol")
    thread.join(timeout=2)

    assert check.status is expected_status
    assert check.observed == {
        "controller": EXECUTOR_PROTOCOL_IDENTITY,
        "worker": identity,
        "source_commit": "a" * 40,
    }
    assert not thread.is_alive()
