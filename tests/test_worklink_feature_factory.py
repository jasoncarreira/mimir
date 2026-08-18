from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any

import pytest

from mimir.worklink.backends.base import WorkOrder
from mimir.worklink.backends.feature_factory import (
    FACTORY_COMMANDS,
    FactoryContractError,
    FeatureFactoryBackend,
    epic_run_id,
    parse_factory_status,
    probe_factory_capabilities,
    resolve_factory_entrypoint,
)


def status_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "run_id": "1551",
        "issue_key": "1551",
        "valid": True,
        "sandbox_path": "/tmp/operator",
        "status": "running",
        "mode": "autonomous",
        "branch": "epic/1551",
        "pr_base": "main",
        "pr_draft": False,
        "lock": "fresh",
        "dead_lock": False,
        "lock_session": "session-1",
        "gates": {"brief": "approved"},
        "steps": ["brief", "implementation"],
        "slices": ["factory-070-migration"],
        "validator": {"status": "pending"},
        "pr_url": None,
        "terminal_result": None,
        "next": "implementation",
    }
    payload.update(overrides)
    return payload


def package_entrypoint(tmp_path: Path) -> Path:
    modules = tmp_path / "lib" / "node_modules"
    feature = modules / "feature-factory"
    adapter = modules / "opencode-feature-factory"
    (feature / "bin").mkdir(parents=True)
    adapter.mkdir(parents=True)
    entrypoint = feature / "bin" / "factory.js"
    entrypoint.write_text("", encoding="utf-8")
    (feature / "package.json").write_text(
        json.dumps({"name": "feature-factory", "version": "0.7.0"}),
        encoding="utf-8",
    )
    (adapter / "package.json").write_text(
        json.dumps({"name": "opencode-feature-factory", "version": "0.7.0"}),
        encoding="utf-8",
    )
    return entrypoint


def test_status_contract_preserves_optional_next_and_opaque_terminal_result() -> None:
    without_next = status_payload(status="needs-human", terminal_result={"reason": "do not route"})
    without_next.pop("next")
    status = parse_factory_status(json.dumps(without_next))
    assert status.is_parked
    assert not status.is_terminal
    assert status.next is None
    assert status.next_present is False
    assert status.terminal_result == {"reason": "do not route"}
    with pytest.raises(FactoryContractError, match="next action"):
        status.require_recovery_next()


@pytest.mark.parametrize("terminal", ["completed", "blocked", "partial"])
def test_top_level_terminal_statuses(terminal: str) -> None:
    assert parse_factory_status(status_payload(status=terminal)).is_terminal


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("valid", "true"),
        ("steps", [{"status": "running"}]),
        ("slices", [""]),
        ("lock", {"status": "fresh"}),
        ("lock_session", 4),
        ("pr_url", 4),
        ("validator", []),
        ("terminal_result", "completed"),
        ("next", " "),
    ],
)
def test_status_rejects_wrong_field_types(field: str, value: object) -> None:
    with pytest.raises(FactoryContractError):
        parse_factory_status(status_payload(**{field: value}))


def test_status_rejects_missing_unknown_trailing_and_nul() -> None:
    missing = status_payload()
    missing.pop("mode")
    with pytest.raises(FactoryContractError, match="missing field"):
        parse_factory_status(missing)
    with pytest.raises(FactoryContractError, match="unknown field"):
        parse_factory_status(status_payload(cost={"total": 0}))
    with pytest.raises(FactoryContractError, match="one JSON object"):
        parse_factory_status(json.dumps(status_payload()) + "\n{}")
    with pytest.raises(FactoryContractError, match="bounds"):
        parse_factory_status(json.dumps(status_payload()) + "\x00")


def test_resolve_entrypoint_is_absolute_package_bound_and_lockstep(tmp_path: Path) -> None:
    entrypoint = package_entrypoint(tmp_path)
    assert resolve_factory_entrypoint(entrypoint) == entrypoint.resolve()
    with pytest.raises(FactoryContractError, match="absolute"):
        resolve_factory_entrypoint(Path("feature-factory/bin/factory.js"))
    manifest = entrypoint.parents[1] / "package.json"
    manifest.write_text(
        json.dumps({"name": "feature-factory", "version": "0.6.0"}),
        encoding="utf-8",
    )
    with pytest.raises(FactoryContractError, match="feature-factory@0.7.0"):
        resolve_factory_entrypoint(entrypoint)


def test_capability_probe_matrix_uses_exact_sixteen_nonmutating_commands(
    tmp_path: Path,
) -> None:
    entrypoint = package_entrypoint(tmp_path)
    calls: list[tuple[str, ...]] = []

    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(tuple(args))
        command = args[2]
        if command == "__mimir_unknown_command_probe__":
            diagnostic = f"unknown command {command}"
        else:
            diagnostic = f"usage: factory {command}\nmissing required argument for {command}"
        return subprocess.CompletedProcess(args, 2, stdout=b"", stderr=diagnostic.encode())

    probe_factory_capabilities(entrypoint, runner=runner)
    assert len(FACTORY_COMMANDS) == 16
    assert [call[2:] for call in calls[1:]] == [argv for _, argv in FACTORY_COMMANDS]
    assert calls[0][2:] == ("__mimir_unknown_command_probe__",)


@pytest.mark.parametrize("mode", ["zero", "signal", "unknown", "nonspecific", "nul"])
def test_capability_probe_fails_closed(tmp_path: Path, mode: str) -> None:
    entrypoint = package_entrypoint(tmp_path)

    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        command = args[2]
        if command == "__mimir_unknown_command_probe__":
            return subprocess.CompletedProcess(
                args, 2, stdout=b"", stderr=f"unknown command {command}".encode()
            )
        if mode == "zero":
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
        if mode == "signal":
            return subprocess.CompletedProcess(args, -9, stdout=b"", stderr=b"")
        if mode == "unknown":
            detail = f"unknown command {command}".encode()
        elif mode == "nonspecific":
            detail = f"failed {command}".encode()
        elif mode == "nul":
            detail = f"usage: {command}\x00".encode()
        else:
            raise AssertionError(mode)
        return subprocess.CompletedProcess(args, 2, stdout=b"", stderr=detail)

    with pytest.raises(FactoryContractError):
        probe_factory_capabilities(entrypoint, runner=runner)


def test_opencode_launch_argv_has_one_leading_space_in_final_token(tmp_path: Path) -> None:
    order = WorkOrder(
        issue_id=1551,
        checkout=tmp_path,
        prompt="ignored by the host feature workflow",
        rules=None,
        timeout_s=43200,
        transcript_root=tmp_path,
    )
    spec = FeatureFactoryBackend(entrypoint="/absolute/factory.js").work_spec(
        order,
        attempt=1,
        repo_url="https://github.com/owner/repo.git",
        base_ref="main",
        branch="epic/1551",
        test_command="uv run pytest -q",
    )
    assert tuple(spec.local_argv or ()) == (
        "opencode",
        "run",
        "--log-level",
        "DEBUG",
        "--print-logs",
        "--dir",
        str(tmp_path),
        "--command",
        "feature",
        " --autonomous 1551",
    )
    assert epic_run_id(1551) == "1551"


def test_controls_are_absolute_run_id_first_and_resume_reads_status(tmp_path: Path) -> None:
    entrypoint = package_entrypoint(tmp_path)
    sandbox = tmp_path / "operator"
    sandbox.mkdir()
    calls: list[tuple[str, ...]] = []

    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(tuple(args))
        if args[2] == "status":
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps(
                    status_payload(sandbox_path=str(sandbox), next="implementation")
                ).encode(),
                stderr=b"",
            )
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    backend = FeatureFactoryBackend(entrypoint=str(entrypoint), runner=runner)
    status = backend.resume(
        "1551",
        session="session-1",
        sandbox=sandbox,
        launcher=entrypoint,
    )
    backend.heartbeat(
        "1551", session="session-1", sandbox=sandbox, launcher=entrypoint
    )
    backend.lock(
        "1551",
        "steal",
        session="session-1",
        sandbox=sandbox,
        launcher=entrypoint,
    )
    assert status.status == "running"
    assert [call[2:] for call in calls] == [
        ("resume", "1551", "--session", "session-1", "--repo", str(sandbox)),
        ("status", "1551", "--repo", str(sandbox), "--json"),
        ("heartbeat", "1551", "--session", "session-1", "--repo", str(sandbox)),
        ("lock", "1551", "steal", "--session", "session-1", "--repo", str(sandbox)),
    ]
    assert all(call[:2] == ("node", str(entrypoint.resolve())) for call in calls)
