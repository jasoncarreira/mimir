from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

import pytest

from mimir.worklink.backends.base import WorkOrder
from mimir.worklink.backends.feature_factory import (
    FACTORY_COMMANDS,
    FACTORY_VERSION,
    FactoryContractError,
    FeatureFactoryBackend,
    epic_run_id,
    parse_factory_status,
    probe_factory_capabilities,
    resolve_factory_entrypoint,
    _DEFAULT_FACTORY_MAX_RETRIES,
    _MAX_FACTORY_MAX_RETRIES,
    _factory_max_retries,
    _run_bounded,
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
        json.dumps({"name": "feature-factory", "version": FACTORY_VERSION}),
        encoding="utf-8",
    )
    (adapter / "package.json").write_text(
        json.dumps({"name": "opencode-feature-factory", "version": FACTORY_VERSION}),
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


def test_status_contract_preserves_nullable_fields_and_opaque_objects() -> None:
    status = parse_factory_status(
        status_payload(
            issue_key=None,
            pr_base=None,
            lock_session=None,
            validator={"status": "pending", "score": 0.5},
            terminal_result={"reason": "opaque"},
        )
    )
    assert status.issue_key is None
    assert status.pr_base is None
    assert status.lock_session is None
    assert status.pr_url is None
    assert status.validator == {"status": "pending", "score": 0.5}
    assert status.terminal_result == {"reason": "opaque"}
    assert parse_factory_status(status.to_json()) == status


@pytest.mark.parametrize("terminal", ["completed", "blocked", "partial"])
def test_top_level_terminal_statuses(terminal: str) -> None:
    assert parse_factory_status(status_payload(status=terminal)).is_terminal


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("valid", "true"),
        ("issue_key", 1551),
        ("pr_base", 1551),
        ("steps", [{"status": "running"}]),
        ("slices", [""]),
        ("lock", {"status": "fresh"}),
        ("lock_session", 4),
        ("pr_url", 4),
        ("next", " "),
    ],
)
def test_status_rejects_wrong_field_types(field: str, value: object) -> None:
    with pytest.raises(FactoryContractError):
        parse_factory_status(status_payload(**{field: value}))


@pytest.mark.parametrize("field", ["validator", "terminal_result"])
@pytest.mark.parametrize("value", ["value", [], True, 1])
def test_status_rejects_non_object_opaque_fields(field: str, value: object) -> None:
    with pytest.raises(FactoryContractError, match=f"{field} must be an object"):
        parse_factory_status(status_payload(**{field: value}))


def test_status_accepts_additive_top_level_fields_after_payload_validation() -> None:
    status = parse_factory_status(
        status_payload(
            workflow="/tmp/factory/1551/WORKFLOW.md",
            future_status_metadata={"generation": 2},
        )
    )
    assert status.run_id == "1551"
    assert status.status == "running"

    with pytest.raises(FactoryContractError, match="finite JSON"):
        parse_factory_status(
            status_payload(future_status_metadata={"value": float("nan")})
        )


def test_status_rejects_missing_known_fields_trailing_json_and_nul() -> None:
    missing = status_payload()
    missing.pop("mode")
    with pytest.raises(FactoryContractError, match="missing field"):
        parse_factory_status(missing)
    with pytest.raises(FactoryContractError, match="one JSON object"):
        parse_factory_status(json.dumps(status_payload()) + "\n{}")
    with pytest.raises(FactoryContractError, match="bounds"):
        parse_factory_status(json.dumps(status_payload()) + "\x00")


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_status_rejects_nonfinite_json_constants(constant: str) -> None:
    payload = json.dumps(status_payload()).replace(
        '"gates": {"brief": "approved"}',
        f'"gates": {{"value": {constant}}}',
    )
    with pytest.raises(FactoryContractError, match="one JSON object"):
        parse_factory_status(payload)


def test_status_rejects_nonfinite_mapping_and_cardinality() -> None:
    with pytest.raises(FactoryContractError, match="finite JSON"):
        parse_factory_status(status_payload(gates={"value": float("nan")}))
    with pytest.raises(FactoryContractError, match="cardinality"):
        parse_factory_status(status_payload(gates={str(index): index for index in range(1001)}))
    with pytest.raises(FactoryContractError, match="cardinality"):
        parse_factory_status(status_payload(steps=["step"] * 1001))


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff",
        b"{}\x00",
        b"{" + b" " * (1024 * 1024) + b"}",
    ],
)
def test_status_rejects_invalid_utf8_nul_and_oversize(payload: bytes) -> None:
    with pytest.raises(FactoryContractError):
        parse_factory_status(payload)


def test_resolve_entrypoint_is_absolute_package_bound_and_lockstep(tmp_path: Path) -> None:
    entrypoint = package_entrypoint(tmp_path)
    assert FACTORY_VERSION == "0.7.2"
    assert resolve_factory_entrypoint(entrypoint) == entrypoint.resolve()
    with pytest.raises(FactoryContractError, match="absolute"):
        resolve_factory_entrypoint(Path("feature-factory/bin/factory.js"))


def test_admit_rejects_nonexistent_entrypoint(tmp_path: Path) -> None:
    with pytest.raises(FactoryContractError, match="does not exist"):
        FeatureFactoryBackend(entrypoint=str(tmp_path / "factory.js")).admit()


@pytest.mark.parametrize(
    ("package", "expected_name"),
    [
        ("feature-factory", "feature-factory"),
        ("opencode-feature-factory", "opencode-feature-factory"),
    ],
)
def test_admit_rejects_either_package_version_mismatch(
    tmp_path: Path,
    package: str,
    expected_name: str,
) -> None:
    entrypoint = package_entrypoint(tmp_path)
    manifest = entrypoint.parents[2] / package / "package.json"
    manifest.write_text(
        json.dumps({"name": expected_name, "version": "0.7.1"}),
        encoding="utf-8",
    )
    calls: list[tuple[str, ...]] = []

    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(tuple(args))
        raise AssertionError("capability probe must not run for a mismatched installation")

    with pytest.raises(
        FactoryContractError,
        match=rf"requires {expected_name}@{re.escape(FACTORY_VERSION)}",
    ):
        FeatureFactoryBackend(entrypoint=str(entrypoint), runner=runner).admit()
    assert calls == []


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

    backend = FeatureFactoryBackend(entrypoint=str(entrypoint), runner=runner)
    assert backend.admit() == entrypoint.resolve()
    assert len(FACTORY_COMMANDS) == 16
    assert [call[2:] for call in calls[1:]] == [argv for _, argv in FACTORY_COMMANDS]
    assert calls[0][2:] == ("__mimir_unknown_command_probe__",)


def test_capability_probe_accepts_recorded_072_diagnostics(tmp_path: Path) -> None:
    entrypoint = package_entrypoint(tmp_path)
    recorded = {
        "status": "a <run-id> is required",
        "heartbeat": "a <run-id> is required",
        "slices-seed": "a <run-id> is required",
        "terminal": "status must be one of completed | blocked | partial | needs-human",
        "slice": "status must be one of pending | running | completed | blocked",
        "gate": "gate must be one of story | brief | pre_pr",
    }

    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        command = args[2]
        if command == "__mimir_unknown_command_probe__":
            diagnostic = f"unknown command {command}"
        else:
            diagnostic = recorded.get(command, f"usage: factory {command}\nmissing argument")
        return subprocess.CompletedProcess(args, 2, stdout=b"", stderr=diagnostic.encode())

    backend = FeatureFactoryBackend(entrypoint=str(entrypoint), runner=runner)
    assert backend.admit() == entrypoint.resolve()


def test_capability_probe_isolated_execution_contract(tmp_path: Path) -> None:
    entrypoint = package_entrypoint(tmp_path)
    observed: list[dict[str, Any]] = []

    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        observed.append({"args": tuple(args), **kwargs})
        command = args[2]
        diagnostic = (
            f"unknown command {command}"
            if command == "__mimir_unknown_command_probe__"
            else f"usage: factory {command}\nmissing required argument"
        )
        return subprocess.CompletedProcess(args, 2, stdout=b"", stderr=diagnostic.encode())

    probe_factory_capabilities(entrypoint, runner=runner)
    forbidden = {"--version", "--help", "help", "cancel", "factory", "feature-factory"}
    assert len(observed) == 17
    for call in observed:
        assert call["args"][:2] == ("node", str(entrypoint))
        assert not forbidden.intersection(call["args"][2:])
        assert call["stdin"] is subprocess.DEVNULL
        assert call["shell"] is False
        assert call["start_new_session"] is True
        assert call["timeout"] == 5
        assert call["cwd"].name == "repo"
        assert set(call["env"]) == {
            "PATH",
            "HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
            "TMPDIR",
            "LANG",
            "LC_ALL",
        }


@pytest.mark.parametrize("mode", ["timeout", "oversize", "invalid_utf8", "mutation"])
def test_capability_probe_rejects_execution_hazards(tmp_path: Path, mode: str) -> None:
    entrypoint = package_entrypoint(tmp_path)

    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if mode == "timeout":
            raise subprocess.TimeoutExpired(args, 5)
        if mode == "mutation":
            (Path(kwargs["cwd"]) / "mutated").write_text("bad", encoding="utf-8")
        command = args[2]
        if command == "__mimir_unknown_command_probe__":
            diagnostic = f"unknown command {command}".encode()
        elif mode == "oversize":
            diagnostic = b"usage: " + command.encode() + b"\n" + b"x" * (64 * 1024)
        elif mode == "invalid_utf8":
            diagnostic = f"usage: {command} missing argument".encode() + b"\xff"
        else:
            diagnostic = f"usage: {command} missing argument".encode()
        return subprocess.CompletedProcess(args, 2, stdout=b"", stderr=diagnostic)

    with pytest.raises(FactoryContractError):
        probe_factory_capabilities(entrypoint, runner=runner)


def test_bounded_runner_stops_oversize_output_during_execution(tmp_path: Path) -> None:
    with pytest.raises(FactoryContractError, match="output exceeds bounds"):
        _run_bounded(
            [sys.executable, "-c", "import sys; sys.stdout.write('x' * 1000000)"],
            cwd=tmp_path,
            env={"PATH": os.environ.get("PATH", "")},
            timeout=5,
            output_limit=1024,
        )


@pytest.mark.parametrize(
    "mode",
    ["zero", "signal", "unknown", "empty", "nul", "unknown_prefix"],
)
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
        elif mode == "empty":
            detail = b""
        elif mode == "nul":
            detail = f"usage: {command}\x00".encode()
        elif mode == "unknown_prefix":
            detail = f"unknown command {command}\nusage: {command} missing argument".encode()
        else:
            raise AssertionError(mode)
        return subprocess.CompletedProcess(args, 2, stdout=b"", stderr=detail)

    with pytest.raises(FactoryContractError):
        probe_factory_capabilities(entrypoint, runner=runner)


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (None, 5),
        ("", 5),
        ("0", 5),
        ("-1", 5),
        ("+1", 5),
        ("1.0", 5),
        ("1e2", 5),
        (" 1", 5),
        ("1 ", 5),
        ("١", 5),
        ("９", 5),
        ("9007199254740992", 5),
        ("9" * 10_000, 5),
        ("1", 1),
        ("42", 42),
        ("00042", 42),
        ("0" * 10_000 + "1", 1),
        ("9007199254740991", 9_007_199_254_740_991),
    ],
)
def test_factory_max_retries_accepts_only_bounded_ascii_decimal(
    monkeypatch: pytest.MonkeyPatch,
    configured: str | None,
    expected: int,
) -> None:
    if configured is None:
        monkeypatch.delenv("MIMIR_FACTORY_MAX_RETRIES", raising=False)
    else:
        monkeypatch.setenv("MIMIR_FACTORY_MAX_RETRIES", configured)

    assert _DEFAULT_FACTORY_MAX_RETRIES == 5
    assert _MAX_FACTORY_MAX_RETRIES == 9_007_199_254_740_991
    assert _factory_max_retries() == expected


@pytest.mark.parametrize(("configured", "expected"), [(None, 5), ("17", 17)])
def test_opencode_launch_argv_has_exact_staged_factory_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured: str | None,
    expected: int,
) -> None:
    if configured is None:
        monkeypatch.delenv("MIMIR_FACTORY_MAX_RETRIES", raising=False)
    else:
        monkeypatch.setenv("MIMIR_FACTORY_MAX_RETRIES", configured)
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
        f" --autonomous --max-retries {expected} 1551",
    )
    payload = (spec.local_argv or ())[-1]
    assert payload.startswith(" ") and not payload.startswith("  ")
    assert "--autonomous" in payload.split()
    assert "--auto" not in payload.split()
    assert "--base" not in payload.split()
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


def test_status_control_rejects_malformed_trailing_and_nonfinite_output(tmp_path: Path) -> None:
    entrypoint = package_entrypoint(tmp_path)
    sandbox = tmp_path / "operator"
    sandbox.mkdir()

    for output in (
        b"not-json",
        json.dumps(status_payload(sandbox_path=str(sandbox))).encode() + b"{}",
        json.dumps(status_payload(sandbox_path=str(sandbox))).replace(
            '"gates": {"brief": "approved"}',
            '"gates": {"value": NaN}',
        ).encode(),
    ):
        backend = FeatureFactoryBackend(
            entrypoint=str(entrypoint),
            runner=lambda args, _output=output, **kwargs: subprocess.CompletedProcess(
                args, 0, stdout=_output, stderr=b""
            ),
        )
        with pytest.raises(FactoryContractError):
            backend.status("1551", sandbox=sandbox, launcher=entrypoint)


def test_migrated_factory_consumers_have_finite_legacy_free_inventory() -> None:
    root = Path(__file__).parent.parent
    consumers = (
        "mimir/worklink/orchestrator.py",
        "mimir/worklink/autonomy.py",
        "mimir/worklink/continuation.py",
        "mimir/worklink/control.py",
        "mimir/worklink/run_state.py",
        "mimir/worklink/factory_state.py",
        "mimir/worklink/checkout.py",
        "mimir/worklink/backends/feature_factory.py",
        "mimir/commands/worklink.py",
        "mimir/server.py",
        "mimir/web_ui.py",
        "mimir/optional-skills/chainlink-orchestrator/scripts/poller.py",
    )
    forbidden = (
        '".opencode" / "factory"',
        "run.json",
        "FactoryRunMetadata",
        "primary_factory",
        "has_concurrent_factory_session",
    )
    assert len(consumers) == 12
    for relative in consumers:
        source = (root / relative).read_text(encoding="utf-8")
        assert all(token not in source for token in forbidden), relative
