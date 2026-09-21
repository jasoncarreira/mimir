"""Closed, observable startup requirements for coding and retained recovery."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import threading
from typing import Any

from .providers import probe_opencode_executable, probe_opencode_version
from .repository_config import RepositoryInventory
from .worklink.backends.feature_factory import (
    DEFAULT_FACTORY_ENTRYPOINT,
    FACTORY_COMMANDS,
    probe_factory_capabilities,
    resolve_factory_entrypoint,
)
from .worklink.tool_pins import FACTORY_VERSION, OPENCODE_VERSION, probe_factory_packages
from .worklink.worker_client import (
    DEFAULT_EXECUTOR_SOCKET,
    EXECUTOR_PROTOCOL_IDENTITY,
    verify_executor_identity,
)


class StartupStatus(str, Enum):
    PASS = "pass"
    FATAL = "fatal"
    WARNING = "warning"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class StartupRequirement:
    name: str
    probe: str
    expected: str
    observed: str
    remediation: str
    applicability: str
    failure_behavior: str


def build_requirement_registry(
    requirements: tuple[StartupRequirement, ...],
) -> tuple[StartupRequirement, ...]:
    names: set[str] = set()
    for requirement in requirements:
        if not requirement.name or requirement.name in names:
            raise ValueError(f"duplicate startup requirement name: {requirement.name!r}")
        names.add(requirement.name)
    return requirements


STARTUP_REQUIREMENTS = build_requirement_registry((
    StartupRequirement(
        "coding.feature_state", "Existing switch", "enabled/disabled",
        "Boolean", "Configure as intended", "Always", "Pass; disabled skips below",
    ),
    StartupRequirement(
        "coding.opencode.executable", "PATH/realpath/file/mode", "Executable",
        "Path/missing", "Install pinned OpenCode", "Coding", "Fatal",
    ),
    StartupRequirement(
        "coding.opencode.version", "fixed --version, 5s", OPENCODE_VERSION,
        "Version/failure", f"Install {OPENCODE_VERSION}", "Coding", "Fatal",
    ),
    StartupRequirement(
        "coding.git.executable", "/usr/bin/git lstat", "Regular executable",
        "Type/mode", "Install Git", "Coding", "Fatal",
    ),
    StartupRequirement(
        "coding.pr_checkout_lease_root", "Existing path/write probe", "Valid root",
        "Path/result", "Repair config", "Coding", "Fatal; not containment",
    ),
    StartupRequirement(
        "coding.github.identity", "Existing identity probe", "Resolved",
        "Login/reason", "Repair credentials", "Coding", "Warning",
    ),
    StartupRequirement(
        "coding.repositories.inventory", "Inventory load", "Strict canonical schema",
        "Named result", "Correct inventory", "Coding+repos", "Fatal",
    ),
    StartupRequirement(
        "coding.repositories.root_mode_agreement", "Canonical path→mode maps",
        "Exact equality", "Both maps", "Align roots/modes", "Coding+repos", "Fatal",
    ),
    StartupRequirement(
        "coding.worklink.target_rw_unique", "Resolve target", "One canonical rw",
        "Target/path/count/mode", "Correct target", "Ready queue", "Fatal",
    ),
    StartupRequirement(
        "coding.worklink.git_binding", "Top-level/origin", "Exact root/origin",
        "Expected/actual", "Repair binding", "Ready queue", "Fatal",
    ),
    StartupRequirement(
        "coding.factory.package_versions", "Entrypoint/manifests",
        f"Package-bound, both {FACTORY_VERSION}", "Paths/versions", f"Install {FACTORY_VERSION}",
        "Factory recovery", "Fatal",
    ),
    StartupRequirement(
        "coding.factory.command_contract", "Isolated probe", "Exact 16 commands",
        "Commands/failure", "Install compatible factory", "Factory recovery", "Fatal",
    ),
    StartupRequirement(
        "coding.worklink.worker_protocol", "Handshake", "Matching protocols",
        "Both versions", "Rebuild worker", "Retained recovery", "Fatal",
    ),
))


def startup_requirement_registry() -> tuple[StartupRequirement, ...]:
    return STARTUP_REQUIREMENTS


@dataclass(frozen=True)
class ProbeObservation:
    ok: bool
    observed: Any
    detail: str = ""


@dataclass(frozen=True)
class StartupCheck:
    requirement: StartupRequirement
    status: StartupStatus
    observed: Any
    detail: str = ""

    @property
    def name(self) -> str:
        return self.requirement.name


@dataclass(frozen=True)
class StartupReport:
    checks: tuple[StartupCheck, ...]

    @property
    def fatal(self) -> tuple[StartupCheck, ...]:
        return tuple(check for check in self.checks if check.status is StartupStatus.FATAL)

    @property
    def warnings(self) -> tuple[StartupCheck, ...]:
        return tuple(check for check in self.checks if check.status is StartupStatus.WARNING)

    @property
    def successful(self) -> bool:
        return not self.fatal

    def check(self, name: str) -> StartupCheck:
        for check in self.checks:
            if check.name == name:
                return check
        raise KeyError(name)

    def require_success(self) -> None:
        if self.fatal:
            rendered = "\n- ".join(
                f"{check.name}: {check.detail or check.observed}"
                for check in self.fatal
            )
            raise RuntimeError(f"coding startup validation failed:\n- {rendered}")


@dataclass(frozen=True)
class StartupEnvironment:
    home: Path
    coding_enabled: bool
    repositories_configured: bool | None = None
    ready_queue_enabled: bool = False
    factory_recovery_enabled: bool = False
    retained_recovery_enabled: bool = False
    worklink_repository: str | None = None
    authorized_root_modes: tuple[tuple[str, str], ...] | None = None
    factory_entrypoint: Path = Path(DEFAULT_FACTORY_ENTRYPOINT)
    executor_socket: Path = DEFAULT_EXECUTOR_SOCKET


Probe = Callable[[], ProbeObservation]


def validate_startup(
    environment: StartupEnvironment,
    *,
    probe_overrides: Mapping[str, ProbeObservation | Probe] | None = None,
) -> StartupReport:
    """Evaluate the registry in stable order, without probing skipped rows."""
    overrides = probe_overrides or {}
    defaults = _default_probes(environment)
    repositories_configured = (
        environment.repositories_configured
        if environment.repositories_configured is not None
        else (environment.home / "repositories.yaml").exists()
    )
    checks: list[StartupCheck] = []
    for requirement in STARTUP_REQUIREMENTS:
        if not _applies(requirement, environment, repositories_configured):
            checks.append(StartupCheck(requirement, StartupStatus.SKIPPED, "unprobed"))
            continue
        selected = overrides.get(requirement.name, defaults[requirement.name])
        try:
            observation = selected() if callable(selected) else selected
            if not isinstance(observation, ProbeObservation):
                raise TypeError("probe did not return ProbeObservation")
        except Exception as exc:  # startup reports probe failure under its stable row
            observation = ProbeObservation(False, f"probe failed: {type(exc).__name__}", str(exc))
        if observation.ok:
            status = StartupStatus.PASS
        elif requirement.failure_behavior.startswith("Warning"):
            status = StartupStatus.WARNING
        else:
            status = StartupStatus.FATAL
        checks.append(
            StartupCheck(requirement, status, observation.observed, observation.detail)
        )
    return StartupReport(tuple(checks))


def _applies(
    requirement: StartupRequirement,
    environment: StartupEnvironment,
    repositories_configured: bool,
) -> bool:
    if requirement.name == "coding.feature_state":
        return True
    if not environment.coding_enabled:
        return False
    if requirement.applicability == "Coding":
        return True
    if requirement.applicability == "Coding+repos":
        return repositories_configured or environment.ready_queue_enabled
    if requirement.applicability == "Ready queue":
        return environment.ready_queue_enabled
    if requirement.applicability == "Factory recovery":
        return environment.factory_recovery_enabled
    if requirement.applicability == "Retained recovery":
        return environment.retained_recovery_enabled
    return True


def _default_probes(environment: StartupEnvironment) -> dict[str, Probe]:
    inventory_path = environment.home / "repositories.yaml"

    def inventory() -> RepositoryInventory:
        return RepositoryInventory.load(inventory_path)

    def opencode_executable() -> ProbeObservation:
        result = probe_opencode_executable()
        return ProbeObservation(result.executable, result.observed)

    def opencode_version() -> ProbeObservation:
        executable = probe_opencode_executable()
        if not executable.executable or executable.path is None:
            return ProbeObservation(False, "executable unavailable")
        result = probe_opencode_version(executable.path)
        return ProbeObservation(result.version == OPENCODE_VERSION, result.observed)

    def git_executable() -> ProbeObservation:
        path = Path("/usr/bin/git")
        try:
            mode = path.lstat().st_mode
            ok = stat.S_ISREG(mode) and os.access(path, os.X_OK)
            observed = {"path": str(path), "type": "regular" if stat.S_ISREG(mode) else "other", "executable": os.access(path, os.X_OK)}
        except OSError as exc:
            return ProbeObservation(False, {"path": str(path), "result": type(exc).__name__})
        return ProbeObservation(ok, observed)

    def lease_root() -> ProbeObservation:
        raw = os.environ.get("MIMIR_PR_CHECKOUT_LEASE_ROOT", "").strip()
        path = Path(raw) if raw else None
        if path is None or not path.is_absolute():
            return ProbeObservation(False, {"path": raw or None, "result": "not absolute"})
        try:
            if not path.is_dir() or path.is_symlink():
                raise OSError("not an existing non-symlink directory")
            with tempfile.TemporaryFile(dir=path) as probe:
                probe.write(b"coding startup probe")
                probe.flush()
        except OSError as exc:
            return ProbeObservation(False, {"path": str(path), "result": str(exc)})
        return ProbeObservation(True, {"path": str(path.resolve()), "result": "writable"})

    def github_identity() -> ProbeObservation:
        from .tools.forge import github_identity_is_degraded

        login = os.environ.get("MIMIR_GITHUB_SELF_LOGIN", "").strip()
        degraded = github_identity_is_degraded()
        return ProbeObservation(
            bool(login) and not degraded,
            login if login and not degraded else "identity unavailable",
        )

    def inventory_probe() -> ProbeObservation:
        try:
            loaded = inventory()
        except (OSError, ValueError) as exc:
            return ProbeObservation(False, type(exc).__name__, str(exc))
        return ProbeObservation(True, {
            "repositories": len(loaded.repositories),
            "allowed_roots": len(loaded.allowed_roots),
        })

    def authorized_roots(loaded: RepositoryInventory) -> tuple[tuple[str, str], ...]:
        if environment.authorized_root_modes is not None:
            return tuple(
                (str(Path(path).resolve()), mode)
                for path, mode in environment.authorized_root_modes
            )
        raw = os.environ.get("MIMIR_FILE_TOOL_ROOTS", "")
        parsed: list[tuple[str, str]] = []
        for entry in raw.split(",") if raw else ():
            path, separator, mode = entry.strip().rpartition(":")
            if not separator or mode not in {"ro", "rw"} or not Path(path).is_absolute():
                raise ValueError(f"invalid authorized repository root: {entry!r}")
            parsed.append((str(Path(path).resolve()), mode))
        return tuple(parsed)

    def root_mode_agreement() -> ProbeObservation:
        try:
            loaded = inventory()
            actual_rows = authorized_roots(loaded)
            actual = dict(actual_rows)
        except (OSError, ValueError) as exc:
            return ProbeObservation(False, type(exc).__name__, str(exc))
        expected = loaded.root_mode_map()
        return ProbeObservation(expected == actual and len(actual) == len(actual_rows), {
            "declared": expected,
            "authorized": actual,
        })

    def target_rw_unique() -> ProbeObservation:
        target_name = environment.worklink_repository or _worklink_repository(environment.home)
        try:
            loaded = inventory()
            roots = authorized_roots(loaded)
            target = loaded.coding_target(target_name or "", authorized_roots=roots)
        except (OSError, ValueError) as exc:
            return ProbeObservation(False, {"target": target_name, "result": str(exc)})
        count = sum(str(Path(path).resolve()) == str(target.root) for path, _ in roots)
        return ProbeObservation(True, {
            "target": target.slug,
            "path": str(target.root),
            "count": count,
            "mode": target.mode,
        })

    def git_binding() -> ProbeObservation:
        target_name = environment.worklink_repository or _worklink_repository(environment.home)
        target = inventory().repository(target_name or "")
        if target is None:
            return ProbeObservation(False, {"target": target_name, "result": "not declared"})
        expected = {"root": str(target.root), "origin": target.origin}
        try:
            top = subprocess.run(
                ["/usr/bin/git", "-C", str(target.root), "rev-parse", "--show-toplevel"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5, check=False,
            )
            origin = subprocess.run(
                ["/usr/bin/git", "-C", str(target.root), "config", "--local", "--get", "remote.origin.url"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5, check=False,
            )
            actual_root = str(Path(top.stdout.strip()).resolve()) if top.returncode == 0 else None
            actual_origin = origin.stdout.strip() if origin.returncode == 0 else None
        except (OSError, subprocess.SubprocessError) as exc:
            return ProbeObservation(False, {"expected": expected, "actual": type(exc).__name__})
        actual = {"root": actual_root, "origin": actual_origin}
        return ProbeObservation(actual == expected, {"expected": expected, "actual": actual})

    def factory_packages() -> ProbeObservation:
        result = probe_factory_packages(environment.factory_entrypoint)
        return ProbeObservation(result.package_bound, {
            "entrypoint": result.entrypoint,
            "feature_factory_manifest": result.feature_factory_manifest,
            "plugin_manifest": result.plugin_manifest,
            "feature_factory_version": result.feature_factory_version,
            "plugin_version": result.plugin_version,
        }, result.detail)

    def factory_commands() -> ProbeObservation:
        expected = tuple(name for name, _ in FACTORY_COMMANDS)
        try:
            entrypoint = resolve_factory_entrypoint(environment.factory_entrypoint)
            probe_factory_capabilities(entrypoint)
        except Exception as exc:
            return ProbeObservation(False, {"commands": None, "failure": str(exc)})
        return ProbeObservation(len(expected) == 16, {"commands": expected})

    def worker_protocol() -> ProbeObservation:
        try:
            source_commit = _run_coroutine(verify_executor_identity(environment.executor_socket))
        except Exception as exc:
            return ProbeObservation(False, {
                "controller": EXECUTOR_PROTOCOL_IDENTITY,
                "worker": "unavailable",
                "failure": str(exc),
            })
        return ProbeObservation(True, {
            "controller": EXECUTOR_PROTOCOL_IDENTITY,
            "worker": EXECUTOR_PROTOCOL_IDENTITY,
            "source_commit": source_commit,
        })

    return {
        "coding.feature_state": lambda: ProbeObservation(
            True, "enabled" if environment.coding_enabled else "disabled"
        ),
        "coding.opencode.executable": opencode_executable,
        "coding.opencode.version": opencode_version,
        "coding.git.executable": git_executable,
        "coding.pr_checkout_lease_root": lease_root,
        "coding.github.identity": github_identity,
        "coding.repositories.inventory": inventory_probe,
        "coding.repositories.root_mode_agreement": root_mode_agreement,
        "coding.worklink.target_rw_unique": target_rw_unique,
        "coding.worklink.git_binding": git_binding,
        "coding.factory.package_versions": factory_packages,
        "coding.factory.command_contract": factory_commands,
        "coding.worklink.worker_protocol": worker_protocol,
    }


def _worklink_repository(home: Path) -> str | None:
    from .worklink.backends.registry import WorklinkConfig

    return WorklinkConfig.load(home / "worklink.yaml").repository


def _run_coroutine(coroutine: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    result: list[Any] = []
    failure: list[BaseException] = []

    def run() -> None:
        try:
            result.append(asyncio.run(coroutine))
        except BaseException as exc:
            failure.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    if failure:
        raise failure[0]
    return result[0]


validate_coding_startup = validate_startup
