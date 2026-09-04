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

from ...opencode_config import OpenCodeConfigError
from ..compute import ComputeResult, WorkSpec
from .base import Caps, CheckoutShape, RawResult, WorkOrder
from .opencode import resolve_worklink_opencode_invocation


FACTORY_VERSION = "0.8.2"
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
_DEFAULT_FACTORY_MAX_RETRIES = 5
_MAX_FACTORY_MAX_RETRIES = 9_007_199_254_740_991
_FACTORY_MAX_RETRIES_ENV = "MIMIR_FACTORY_MAX_RETRIES"
_ASCII_DECIMAL = re.compile(r"[0-9]+")
_RUN_ID = re.compile(r"[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?")
_UNKNOWN_STRUCTURE = re.compile(r"(?im)\b(?:unknown|unrecognized)\b[^\n]*\bcommand\b")
_VALIDATOR_VERDICTS = frozenset({"GO", "GO-WITH-NITS", "NO-GO"})

Runner = Callable[..., subprocess.CompletedProcess[Any]]


def _factory_max_retries(environ: Mapping[str, str] | None = None) -> int:
    source = os.environ if environ is None else environ
    raw = source.get(_FACTORY_MAX_RETRIES_ENV)
    if raw is None or _ASCII_DECIMAL.fullmatch(raw) is None:
        return _DEFAULT_FACTORY_MAX_RETRIES
    normalized = raw.lstrip("0")
    if not normalized:
        return _DEFAULT_FACTORY_MAX_RETRIES
    maximum = str(_MAX_FACTORY_MAX_RETRIES)
    if len(normalized) > len(maximum) or (
        len(normalized) == len(maximum) and normalized > maximum
    ):
        return _DEFAULT_FACTORY_MAX_RETRIES
    return int(normalized)


class FactoryContractError(RuntimeError):
    pass


@dataclass(frozen=True)
class FactoryStatus:
    run_id: str
    valid: bool
    sandbox_path: str
    issue_key: str | None = None
    status: str | None = None
    mode: str | None = None
    branch: str | None = None
    pr_base: str | None = None
    pr_draft: bool | None = None
    lock: str | None = None
    dead_lock: bool | None = None
    lock_session: str | None = None
    gates: dict[str, Any] | None = None
    steps: tuple[str, ...] | None = None
    slices: tuple[str, ...] | None = None
    validator: str | None = None
    pr_url: str | None = None
    terminal_result: dict[str, Any] | None = None
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
            "steps": list(self.steps) if self.steps is not None else None,
            "slices": list(self.slices) if self.slices is not None else None,
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
        "valid",
        "sandbox_path",
    }
)


def _bounded_text(value: object, name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip():
        raise FactoryContractError(f"factory status {name} must be a nonblank string")
    text = value.strip()
    if len(text.encode("utf-8")) > _MAX_TEXT_BYTES or "\x00" in text:
        raise FactoryContractError(f"factory status {name} is invalid")
    return text


def _bool(value: object, name: str, *, nullable: bool = False) -> bool | None:
    if value is None and nullable:
        return None
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


def _validator_verdict(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise FactoryContractError("factory status validator must be a documented verdict")
    if value not in _VALIDATOR_VERDICTS:
        raise FactoryContractError("factory status validator has an unknown verdict")
    return value


def _compact_strings(
    value: object, name: str, *, nullable: bool = False
) -> tuple[str, ...] | None:
    if value is None and nullable:
        return None
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
    if missing:
        raise FactoryContractError(f"factory status missing field: {sorted(missing)[0]}")
    lock = _bounded_text(decoded.get("lock"), "lock", nullable=True)
    if lock is not None and lock not in {"fresh", "stale", "absent"}:
        raise FactoryContractError("factory status lock must be fresh, stale, or absent")
    next_present = "next" in decoded
    next_value = _bounded_text(decoded.get("next"), "next", nullable=True) if next_present else None
    return FactoryStatus(
        run_id=_bounded_text(decoded["run_id"], "run_id") or "",
        valid=_bool(decoded["valid"], "valid"),
        sandbox_path=_bounded_text(decoded["sandbox_path"], "sandbox_path") or "",
        issue_key=_bounded_text(decoded.get("issue_key"), "issue_key", nullable=True),
        status=_bounded_text(decoded.get("status"), "status", nullable=True),
        mode=_bounded_text(decoded.get("mode"), "mode", nullable=True),
        branch=_bounded_text(decoded.get("branch"), "branch", nullable=True),
        pr_base=_bounded_text(decoded.get("pr_base"), "pr_base", nullable=True),
        pr_draft=_bool(decoded.get("pr_draft"), "pr_draft", nullable=True),
        lock=lock,
        dead_lock=_bool(decoded.get("dead_lock"), "dead_lock", nullable=True),
        lock_session=_bounded_text(decoded.get("lock_session"), "lock_session", nullable=True),
        gates=_opaque_dict(decoded.get("gates"), "gates", nullable=True),
        steps=_compact_strings(decoded.get("steps"), "steps", nullable=True),
        slices=_compact_strings(decoded.get("slices"), "slices", nullable=True),
        validator=_validator_verdict(decoded.get("validator")),
        pr_url=_bounded_text(decoded.get("pr_url"), "pr_url", nullable=True),
        terminal_result=_opaque_dict(
            decoded.get("terminal_result"), "terminal_result", nullable=True
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
    cwd: Path | None,
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
    cwd: Path | None = None,
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
    kwargs: dict[str, Any] = {
        "env": dict(env),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "timeout": timeout,
        "check": False,
        "shell": False,
        "start_new_session": True,
    }
    if cwd is not None:
        kwargs["cwd"] = cwd
    return runner(
        list(args),
        **kwargs,
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
        for command, argv in FACTORY_COMMANDS:
            _, diagnostic = invoke(argv)
            if not diagnostic:
                raise FactoryContractError(f"factory capability probe failed for {command}")
            if _UNKNOWN_STRUCTURE.search(diagnostic) is not None:
                raise FactoryContractError(f"factory capability probe treated {command} as unknown")
            unknown_form = unknown.replace(_UNKNOWN_PROBE, command)
            if diagnostic == unknown_form or diagnostic.startswith(unknown_form):
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
        session: str | None = None,
        run_id: str | None = None,
    ) -> WorkSpec:
        try:
            resolution = resolve_worklink_opencode_invocation(order.env)
        except OpenCodeConfigError as exc:
            raise FactoryContractError(
                f"feature_factory_opencode_resolution_failed:{exc.reason_code}"
            ) from exc
        invocation = resolution.invocation
        run_id = run_id or epic_run_id(order.issue_id)
        if _RUN_ID.fullmatch(run_id) is None:
            raise FactoryContractError("factory launch run_id has an invalid shape")
        work_item_json = order.env.get("MIMIR_WORK_ITEM_JSON")
        if work_item_json is not None:
            try:
                work_item = json.loads(work_item_json)
            except json.JSONDecodeError as exc:
                raise FactoryContractError("factory work item JSON is malformed") from exc
            if not isinstance(work_item, dict) or work_item.get("run_id") != run_id:
                raise FactoryContractError("factory work item run_id does not match the launch run_id")
        command = self.opencode_argv(
            order.checkout,
            run_id,
            invocation.model,
            session=session,
        )
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
            backend_config={
                "entrypoint": str(self.entrypoint),
                "run_id": run_id,
                "model": invocation.model,
                "configured_model": resolution.configured_model,
                "model_diverged": resolution.model_diverged,
                "model_source": invocation.model_source,
            },
            local_checkout=order.checkout,
            local_argv=command,
            output_root=order.transcript_root,
        )

    @staticmethod
    def opencode_argv(
        operator_checkout: Path,
        run_id: str,
        model: str,
        *,
        session: str | None = None,
    ) -> tuple[str, ...]:
        if _RUN_ID.fullmatch(run_id) is None:
            raise FactoryContractError("factory launch run_id has an invalid shape")
        if session is not None and (not session.strip() or "\x00" in session):
            raise FactoryContractError("factory launch session is invalid")
        retries = _factory_max_retries()
        # feature-factory 0.7.5 stages the workflow inside the run directory, so
        # OpenCode --auto must not bypass it.
        session_args = ("--session", session) if session is not None else ()
        return (
            "opencode",
            "run",
            "--log-level",
            "DEBUG",
            "--print-logs",
            *session_args,
            "-m",
            model,
            "--dir",
            str(operator_checkout),
            "--command",
            "feature",
            f" --autonomous --max-retries {retries} {run_id}",
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


# feature-factory 0.8.0+'s own variable, read by the factory CHILD. Distinct from
# mimir's operator-facing ``MIMIR_FACTORY_PUBLISHING_IDENTITY``, which selects the
# identity on the controller side; the resolved value is forwarded under this name.
FACTORY_PUBLISHING_IDENTITY_ENV = "FACTORY_PUBLISHING_IDENTITY"


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
        # feature-factory 0.8.0+ reads the declared publishing identity from this
        # inherited variable and compares it against ``gh api /user``. The mimir
        # repository is published from two accounts -- ``jasoncarreira`` from a
        # maintainer's checkout and ``mimir-carreira`` from mimirbot -- so the
        # deployment must select the identity.
        #
        # It MUST be allow-listed here or the deployment can export it and the driver
        # never sees it. The symptom of the omission is a Gate 1 park that reads as a
        # deployment misconfiguration rather than a stripped variable.
        #
        # The value must never be derived from ``gh``, the token, or any command
        # result: an expectation read from the credential being checked always
        # matches, and the guard stops guarding.
        FACTORY_PUBLISHING_IDENTITY_ENV,
    }
    return {
        key: value
        for key, value in os.environ.items()
        if key in allowed or key.startswith(("LC_", "XDG_"))
    }


def epic_run_id(issue_id: int) -> str:
    if isinstance(issue_id, bool) or issue_id <= 0:
        raise ValueError("factory issue id must be positive")
    run_id = f"chainlink-{issue_id}"
    if _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("factory issue id does not produce a valid run id")
    return run_id
