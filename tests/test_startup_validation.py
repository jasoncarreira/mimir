from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from mimir.startup_validation import (
    ProbeObservation,
    STARTUP_REQUIREMENTS,
    StartupEnvironment,
    StartupStatus,
    build_requirement_registry,
    startup_requirement_registry,
    validate_startup,
)


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
