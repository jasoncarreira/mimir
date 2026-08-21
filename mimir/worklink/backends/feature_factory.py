from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import threading
from typing import Any, Callable, ClassVar, Mapping, Sequence

from ..compute import ComputeResult, WorkSpec
from .base import Caps, CheckoutShape, RawResult, WorkOrder


FACTORY_VERSION = "0.7.2"
DEFAULT_FACTORY_ENTRYPOINT = "/opt/mimir-opencode/lib/node_modules/feature-factory/bin/factory.js"
FACTORY_COMMANDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("init", ("init",)),
    ("status", ("status",)),
    ("amend-paths", ("amend-paths",)),
    ("resume", ("resume",)),
    ("lock", ("lock", "probe-run")),
    ("heartbeat", ("heartbeat",)),
    ("gate", ("gate",)),
    ("step", ("step",)),
    ("terminal", ("terminal",)),
    ("slices-seed", ("slices-seed",)),
    ("slice", ("slice",)),
    ("observe", ("observe",)),
    ("validator", ("validator",)),
    ("pr", ("pr",)),
    ("reverify-repair", ("reverify-repair",)),
    ("effective-push", ("effective-push",)),
)
_UNKNOWN_PROBE = "__mimir_unknown_command_probe__"
_MAX_DIAGNOSTIC_BYTES = 64 * 1024
_MAX_STATUS_BYTES = 1024 * 1024
_MAX_LIST_ITEMS = 1000
_MAX_TEXT_BYTES = 16 * 1024
_MAX_JSON_DEPTH = 32
_ARGUMENT_STRUCTURE = re.compile(
    r"(?im)(?:^\s*usage\s*:|\b(?:argument|option|operand|required|requires|missing|expected)\b)"
)
_UNKNOWN_STRUCTURE = re.compile(r"(?im)\b(?:unknown|unrecognized)\b[^\n]*\bcommand\b")

Runner = Callable[..., subprocess.CompletedProcess[Any]]


class FactoryContractError(RuntimeError):
    pass


@dataclass(frozen=True)
class FactoryStatus:
    run_id: str
    issue_key: str
    valid: bool
    sandbox_path: str
    status: str
    mode: str
    branch: str
    pr_base: str
    pr_draft: bool
    lock: str
    dead_lock: bool
    lock_session: str | None
    gates: dict[str, Any]
    steps: tuple[str, ...]
    slices: tuple[str, ...]
    validator: dict[str, Any] | None
    pr_url: str | None
    terminal_result: dict[str, Any] | None
    next: str | None = None
    next_present: bool = False

    TERMINAL_STATUSES: ClassVar[frozenset[str]] = frozenset(
        {"completed", "blocked", "partial"}
    )

    @property
    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL_STATUSES

    @property
    def is_parked(self) -> bool:
        return self.status == "needs-human"

    def require_recovery_next(self) -> str:
        if not self.next_present or self.next is None:
            raise FactoryContractError("factory status recovery requires a nonblank next action")
        return self.next

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "run_id": self.run_id,
            "issue_key": self.issue_key,
            "valid": self.valid,
            "sandbox_path": self.sandbox_path,
            "status": self.status,
            "mode": self.mode,
            "branch": self.branch,
            "pr_base": self.pr_base,
            "pr_draft": self.pr_draft,
            "lock": self.lock,
            "dead_lock": self.dead_lock,
            "lock_session": self.lock_session,
            "gates": self.gates,
            "steps": list(self.steps),
            "slices": list(self.slices),
            "validator": self.validator,
            "pr_url": self.pr_url,
            "terminal_result": self.terminal_result,
        }
        if self.next_present:
            payload["next"] = self.next
        return payload


_REQUIRED_STATUS_FIELDS = frozenset(
    {
        "run_id",
        "issue_key",
        "valid",
        "sandbox_path",
        "status",
        "mode",
        "branch",
        "pr_base",
        "pr_draft",
        "lock",
        "dead_lock",
        "lock_session",
        "gates",
        "steps",
        "slices",
        "validator",
        "pr_url",
        "terminal_result",
    }
)
_STATUS_FIELDS = _REQUIRED_STATUS_FIELDS | {"next"}


def _bounded_text(value: object, name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip():
        raise FactoryContractError(f"factory status {name} must be a nonblank string")
    text = value.strip()
    if len(text.encode("utf-8")) > _MAX_TEXT_BYTES or "\x00" in text:
        raise FactoryContractError(f"factory status {name} is invalid")
    return text


def _bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise FactoryContractError(f"factory status {name} must be a boolean")
    return value


def _opaque_dict(value: object, name: str, *, nullable: bool = False) -> dict[str, Any] | None:
    if value is None and nullable:
        return None
    if not isinstance(value, dict):
        raise FactoryContractError(f"factory status {name} must be an object")
    _validate_json_value(value, name=name)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise FactoryContractError(f"factory status {name} is not valid JSON") from exc
    if len(encoded) > _MAX_STATUS_BYTES:
        raise FactoryContractError(f"factory status {name} exceeds size limit")
    return dict(value)


def _compact_strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > _MAX_LIST_ITEMS:
        raise FactoryContractError(f"factory status {name} must be a bounded string list")
    result: list[str] = []
    for item in value:
        text = _bounded_text(item, name)
        assert text is not None
        result.append(text)
    return tuple(result)


def _validate_json_value(value: object, *, name: str, depth: int = 0) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise FactoryContractError(f"factory status {name} exceeds nesting limit")
    if isinstance(value, float) and not math.isfinite(value):
        raise FactoryContractError(f"factory status {name} is not finite JSON")
    if isinstance(value, dict):
        if len(value) > _MAX_LIST_ITEMS or not all(isinstance(key, str) for key in value):
            raise FactoryContractError(f"factory status {name} exceeds cardinality limit")
        for item in value.values():
            _validate_json_value(item, name=name, depth=depth + 1)
    elif isinstance(value, list):
        if len(value) > _MAX_LIST_ITEMS:
            raise FactoryContractError(f"factory status {name} exceeds cardinality limit")
        for item in value:
            _validate_json_value(item, name=name, depth=depth + 1)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON value: {value}")


def parse_factory_status(payload: bytes | str | Mapping[str, Any]) -> FactoryStatus:
    if isinstance(payload, bytes):
        if len(payload) > _MAX_STATUS_BYTES or b"\x00" in payload:
            raise FactoryContractError("factory status output exceeds bounds")
        try:
            payload = payload.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise FactoryContractError("factory status output is not UTF-8") from exc
    if isinstance(payload, str):
        if len(payload.encode("utf-8")) > _MAX_STATUS_BYTES or "\x00" in payload:
            raise FactoryContractError("factory status output exceeds bounds")
        try:
            decoded = json.loads(payload, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError) as exc:
            raise FactoryContractError("factory status output is not one JSON object") from exc
    else:
        decoded = payload
    if not isinstance(decoded, Mapping):
        raise FactoryContractError("factory status must be a JSON object")
    _validate_json_value(decoded, name="payload")
    try:
        encoded = json.dumps(
            decoded,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise FactoryContractError("factory status output is not one JSON object") from exc
    if len(encoded) > _MAX_STATUS_BYTES:
        raise FactoryContractError("factory status output exceeds bounds")
    keys = set(decoded)
    missing = _REQUIRED_STATUS_FIELDS - keys
    unknown = keys - _STATUS_FIELDS
    if missing:
        raise FactoryContractError(f"factory status missing field: {sorted(missing)[0]}")
    if unknown:
        raise FactoryContractError(f"factory status contains unknown field: {sorted(unknown)[0]}")
    lock = _bounded_text(decoded["lock"], "lock")
    if lock not in {"fresh", "stale", "absent"}:
        raise FactoryContractError("factory status lock must be fresh, stale, or absent")
    next_present = "next" in decoded
    next_value = _bounded_text(decoded.get("next"), "next", nullable=True) if next_present else None
    return FactoryStatus(
        run_id=_bounded_text(decoded["run_id"], "run_id") or "",
        issue_key=_bounded_text(decoded["issue_key"], "issue_key") or "",
        valid=_bool(decoded["valid"], "valid"),
        sandbox_path=_bounded_text(decoded["sandbox_path"], "sandbox_path") or "",
        status=_bounded_text(decoded["status"], "status") or "",
        mode=_bounded_text(decoded["mode"], "mode") or "",
        branch=_bounded_text(decoded["branch"], "branch") or "",
        pr_base=_bounded_text(decoded["pr_base"], "pr_base") or "",
        pr_draft=_bool(decoded["pr_draft"], "pr_draft"),
        lock=lock,
        dead_lock=_bool(decoded["dead_lock"], "dead_lock"),
        lock_session=_bounded_text(decoded["lock_session"], "lock_session", nullable=True),
        gates=_opaque_dict(decoded["gates"], "gates") or {},
        steps=_compact_strings(decoded["steps"], "steps"),
        slices=_compact_strings(decoded["slices"], "slices"),
        validator=_opaque_dict(decoded["validator"], "validator", nullable=True),
        pr_url=_bounded_text(decoded["pr_url"], "pr_url", nullable=True),
        terminal_result=_opaque_dict(
            decoded["terminal_result"], "terminal_result", nullable=True
        ),
        next=next_value,
        next_present=next_present,
    )


def _read_manifest(path: Path, expected_name: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise FactoryContractError(f"cannot read {expected_name} package manifest") from exc
    if len(raw) > _MAX_STATUS_BYTES or b"\x00" in raw:
        raise FactoryContractError(f"invalid {expected_name} package manifest")
    try:
        data = json.loads(
            raw.decode("utf-8", "strict"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise FactoryContractError(f"invalid {expected_name} package manifest") from exc
    if not isinstance(data, dict):
        raise FactoryContractError(f"invalid {expected_name} package manifest")
    if data.get("name") != expected_name or data.get("version") != FACTORY_VERSION:
        raise FactoryContractError(
            f"requires {expected_name}@{FACTORY_VERSION}"
        )
    return data


def resolve_factory_entrypoint(configured: str | Path) -> Path:
    raw = Path(configured)
    if not raw.is_absolute():
        raise FactoryContractError("feature_factory.entrypoint must be an absolute path")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise FactoryContractError("feature_factory entrypoint does not exist") from exc
    if not resolved.is_file() or resolved.name != "factory.js" or resolved.parent.name != "bin":
        raise FactoryContractError("feature_factory entrypoint must name bin/factory.js")
    package_root = resolved.parent.parent
    if package_root.name != "feature-factory":
        raise FactoryContractError("feature_factory entrypoint is not package-bound")
    _read_manifest(package_root / "package.json", "feature-factory")
    _read_manifest(package_root.parent / "opencode-feature-factory" / "package.json", "opencode-feature-factory")
    return resolved


def _tree_manifest(root: Path) -> tuple[tuple[str, str, str], ...]:
    rows: list[tuple[str, str, str]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            rows.append((relative, "symlink", os.readlink(path)))
        elif path.is_dir():
            rows.append((relative, "dir", ""))
        elif path.is_file():
            rows.append((relative, "file", hashlib.sha256(path.read_bytes()).hexdigest()))
        else:
            rows.append((relative, "other", ""))
    return tuple(rows)


def _strict_diagnostic(result: subprocess.CompletedProcess[Any]) -> str:
    chunks: list[bytes] = []
    for value in (result.stdout, result.stderr):
        if value is None:
            continue
        if isinstance(value, str):
            value = value.encode("utf-8", "strict")
        if not isinstance(value, bytes):
            raise FactoryContractError("factory capability probe returned invalid output")
        chunks.append(value)
    raw = b"\n".join(chunks)
    if len(raw) > _MAX_DIAGNOSTIC_BYTES or b"\x00" in raw:
        raise FactoryContractError("factory capability probe output exceeds bounds")
    try:
        return raw.decode("utf-8", "strict").strip()
    except UnicodeDecodeError as exc:
        raise FactoryContractError("factory capability probe output is not UTF-8") from exc


def _terminate_bounded_process(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except (ProcessLookupError, OSError):
            pass


def _run_bounded(
    args: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
    output_limit: int,
) -> subprocess.CompletedProcess[bytes]:
    process = subprocess.Popen(
        list(args),
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        start_new_session=True,
    )
    retained = {"stdout": bytearray(), "stderr": bytearray()}
    total = 0
    lock = threading.Lock()
    overflow = threading.Event()

    def drain(name: str, stream: Any) -> None:
        nonlocal total
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            with lock:
                remaining = output_limit - total
                if remaining > 0:
                    kept = chunk[:remaining]
                    retained[name].extend(kept)
                    total += len(kept)
                if len(chunk) > max(remaining, 0) and not overflow.is_set():
                    overflow.set()
                    _terminate_bounded_process(process)

    threads = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_bounded_process(process)
        process.wait()
        for thread in threads:
            thread.join(timeout=1)
        raise
    for thread in threads:
        thread.join(timeout=1)
    if any(thread.is_alive() for thread in threads):
        _terminate_bounded_process(process)
        for thread in threads:
            thread.join(timeout=1)
    if any(thread.is_alive() for thread in threads):
        raise FactoryContractError("factory command output could not be drained")
    if overflow.is_set():
        raise FactoryContractError("factory command output exceeds bounds")
    return subprocess.CompletedProcess(
        list(args),
        process.returncode,
        stdout=bytes(retained["stdout"]),
        stderr=bytes(retained["stderr"]),
    )


def _invoke_bounded_or_injected(
    runner: Runner,
    args: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
    output_limit: int,
) -> subprocess.CompletedProcess[Any]:
    if runner is subprocess.run:
        return _run_bounded(
            args,
            cwd=cwd,
            env=env,
            timeout=timeout,
            output_limit=output_limit,
        )
    return runner(
        list(args),
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
        shell=False,
        start_new_session=True,
    )


def _probe_environment(root: Path) -> dict[str, str]:
    path = os.environ.get("PATH", "")
    return {
        "PATH": path,
        "HOME": str(root / "home"),
        "XDG_CONFIG_HOME": str(root / "xdg-config"),
        "XDG_DATA_HOME": str(root / "xdg-data"),
        "XDG_CACHE_HOME": str(root / "xdg-cache"),
        "TMPDIR": str(root / "tmp"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


def probe_factory_capabilities(entrypoint: Path, *, runner: Runner = subprocess.run) -> None:
    with tempfile.TemporaryDirectory(prefix="mimir-factory-probe-") as temporary:
        root = Path(temporary)
        repo = root / "repo"
        repo.mkdir()
        for name in ("home", "xdg-config", "xdg-data", "xdg-cache", "tmp"):
            (root / name).mkdir()
        baseline = _tree_manifest(repo)

        def invoke(args: Sequence[str]) -> tuple[subprocess.CompletedProcess[Any], str]:
            try:
                result = _invoke_bounded_or_injected(
                    runner,
                    ["node", str(entrypoint), *args],
                    cwd=repo,
                    env=_probe_environment(root),
                    timeout=5,
                    output_limit=_MAX_DIAGNOSTIC_BYTES,
                )
            except subprocess.TimeoutExpired as exc:
                raise FactoryContractError("factory capability probe timed out") from exc
            if _tree_manifest(repo) != baseline:
                raise FactoryContractError("factory capability probe mutated its scratch repository")
            if result.returncode <= 0:
                raise FactoryContractError("factory capability probe did not fail normally")
            return result, _strict_diagnostic(result)

        _, unknown = invoke((_UNKNOWN_PROBE,))
        if _UNKNOWN_PROBE not in unknown or _UNKNOWN_STRUCTURE.search(unknown) is None:
            raise FactoryContractError("factory unknown-command control was not structural")
        unknown_form = unknown.replace(_UNKNOWN_PROBE, "{command}")
        for command, argv in FACTORY_COMMANDS:
            _, diagnostic = invoke(argv)
            token = re.compile(rf"(?<![A-Za-z0-9_-]){re.escape(command)}(?![A-Za-z0-9_-])")
            if token.search(diagnostic) is None or _ARGUMENT_STRUCTURE.search(diagnostic) is None:
                raise FactoryContractError(f"factory capability probe failed for {command}")
            if _UNKNOWN_STRUCTURE.search(diagnostic) is not None:
                raise FactoryContractError(f"factory capability probe treated {command} as unknown")
            normalized = token.sub("{command}", diagnostic)
            if normalized == unknown_form or normalized.startswith(unknown_form):
                raise FactoryContractError(f"factory capability probe matched unknown form for {command}")


@dataclass(frozen=True)
class FeatureFactoryBackend:
    entrypoint: str = field(
        default_factory=lambda: os.environ.get(
            "MIMIR_FACTORY_ENTRYPOINT", DEFAULT_FACTORY_ENTRYPOINT
        )
    )
    name: str = "feature_factory"
    checkout_shape: CheckoutShape = CheckoutShape.ISOLATED_CLONE
    poll_interval_s: int = 10
    runner: Runner = field(default=subprocess.run, compare=False, repr=False)

    def capabilities(self) -> Caps:
        return Caps(
            tool_category="feature-factory",
            persistent_sessions=True,
            json_output=True,
            native_pr_creation=True,
            quota_pool=None,
        )

    def admit(self) -> Path:
        resolved = resolve_factory_entrypoint(self.entrypoint)
        probe_factory_capabilities(resolved, runner=self.runner)
        return resolved

    def work_spec(
        self,
        order: WorkOrder,
        *,
        attempt: int,
        repo_url: str,
        base_ref: str,
        branch: str,
        test_command: str,
    ) -> WorkSpec:
        command = self.opencode_argv(order.checkout, order.issue_id)
        return WorkSpec(
            issue_id=order.issue_id,
            attempt=attempt,
            repo_url=repo_url,
            base_ref=base_ref,
            branch=branch,
            prompt=order.prompt,
            rules=order.rules,
            test_command=test_command,
            backend=self.name,
            timeout_s=order.timeout_s,
            env=order.env,
            backend_config={"entrypoint": str(self.entrypoint)},
            local_checkout=order.checkout,
            local_argv=command,
        )

    @staticmethod
    def opencode_argv(operator_checkout: Path, issue_number: int) -> tuple[str, ...]:
        return (
            "opencode",
            "run",
            "--log-level",
            "DEBUG",
            "--print-logs",
            "--dir",
            str(operator_checkout),
            "--command",
            "feature",
            f" --autonomous {issue_number}",
        )

    def _control(
        self,
        launcher: str | Path,
        args: Sequence[str],
        *,
        sandbox: Path,
    ) -> subprocess.CompletedProcess[Any]:
        entrypoint = resolve_factory_entrypoint(launcher)
        try:
            result = _invoke_bounded_or_injected(
                self.runner,
                ["node", str(entrypoint), *args],
                cwd=sandbox,
                env=_control_environment(),
                timeout=30,
                output_limit=_MAX_STATUS_BYTES,
            )
        except subprocess.TimeoutExpired as exc:
            raise FactoryContractError("factory control command timed out") from exc
        _strict_diagnostic(result)
        if result.returncode != 0:
            detail = _strict_diagnostic(result)
            raise FactoryContractError(detail or f"factory control exited {result.returncode}")
        return result

    def status(self, run_id: str, *, sandbox: Path, launcher: str | Path) -> FactoryStatus:
        result = self._control(
            launcher,
            ("status", run_id, "--repo", str(sandbox), "--json"),
            sandbox=sandbox,
        )
        stdout = result.stdout if result.stdout is not None else b""
        return parse_factory_status(stdout)

    def resume(
        self,
        run_id: str,
        *,
        session: str,
        sandbox: Path,
        launcher: str | Path,
    ) -> FactoryStatus:
        self._control(
            launcher,
            ("resume", run_id, "--session", session, "--repo", str(sandbox)),
            sandbox=sandbox,
        )
        return self.status(run_id, sandbox=sandbox, launcher=launcher)

    def heartbeat(
        self,
        run_id: str,
        *,
        session: str,
        sandbox: Path,
        launcher: str | Path,
    ) -> None:
        self._control(
            launcher,
            ("heartbeat", run_id, "--session", session, "--repo", str(sandbox)),
            sandbox=sandbox,
        )

    def lock(
        self,
        run_id: str,
        action: str,
        *,
        session: str,
        sandbox: Path,
        launcher: str | Path,
    ) -> None:
        if action not in {"claim", "steal", "release"}:
            raise ValueError("factory lock action must be claim, steal, or release")
        self._control(
            launcher,
            ("lock", run_id, action, "--session", session, "--repo", str(sandbox)),
            sandbox=sandbox,
        )

    async def interpret(self, order: WorkOrder, result: object) -> RawResult:
        if not isinstance(result, ComputeResult):
            raise TypeError("FeatureFactoryBackend.interpret expects ComputeResult")
        if result.launch_error:
            return RawResult(-1, None, "backend_error", result.launch_error)
        if result.timed_out:
            return RawResult(-1, None, "failed", "OpenCode process timed out")
        if result.exit_code != 0:
            return RawResult(result.exit_code, None, "failed", result.stderr or None)
        return RawResult(0, None, "interrupted", None)


def _control_environment() -> dict[str, str]:
    allowed = {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "TERM",
        "TMPDIR",
        "TMP",
        "TEMP",
        "TZ",
        "NODE_EXTRA_CA_CERTS",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
    return {
        key: value
        for key, value in os.environ.items()
        if key in allowed or key.startswith(("LC_", "XDG_"))
    }


def epic_run_id(issue_id: int) -> str:
    if isinstance(issue_id, bool) or issue_id <= 0:
        raise ValueError("factory issue id must be positive")
    return str(issue_id)
