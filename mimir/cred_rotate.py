"""Credential rotation (SPEC §16 item 14 — Phase 3).

Builds on the verification probes from Phase 2 / 2.5. The rotation
flow:

  1. Validate inputs — credential is registered, env var name is
     listed for that credential.
  2. Snapshot ``compose.env`` to a timestamped backup so a failed
     rotation can be rolled back atomically.
  3. Atomic edit — write to a sibling tmp file, fsync, rename over
     ``compose.env``. Single line replaced; surrounding lines + comments
     preserved verbatim.
  4. Emit ``credential_rotation_started`` to ``./rotations.jsonl`` with
     SHA-256 prefixes of old + new values (12 chars — enough to
     distinguish across rotations without exposing the secret).
  5. ``docker compose up -d --force-recreate`` (per the §14
     reload-semantics gotcha — ``restart`` doesn't reload env_file).
  6. Wait for the container to come back up (poll ``docker compose ps
     --format json`` until state==running).
  7. ``docker exec <service> mimir verify-cred <cred>`` — the
     verification probe runs INSIDE the new container with the new
     env value. Exit 0 = live; anything else = stale.
  8. On verify success → emit ``credential_rotation_completed`` and
     return 0.
  9. On any failure (write, recreate, verify): restore ``compose.env``
     from the backup, recreate again to revert, emit
     ``credential_rotation_failed``, return 1.

Scope: single-deployment, host-side. Operator runs from the deployment
directory (``cd ~/projects/odin/muninn-mimir && mimir rotate --env
GITHUB_TOKEN``). Multi-var bundle rotation (``--cred X_OAUTH`` →
rotate all 4 vars atomically) is a follow-up; for now this is
single-env-var.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cred_verify import get_probes


_COMPOSE_FILES = ("compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml")


@dataclass
class RotationContext:
    """All the state a single rotation needs. Built once at the top of
    ``run_rotate`` and threaded through the steps."""

    env_name: str
    new_value: str
    deployment_dir: Path
    compose_env_path: Path
    compose_file_path: Path
    service_name: str
    cred_name: str | None = None  # name of the credential the env var belongs to
    cred_type: str | None = None
    backup_path: Path | None = None
    started_at: float = field(default_factory=time.monotonic)


# ── audit ────────────────────────────────────────────────────────────


def _value_hash(value: str) -> str:
    """SHA-256 prefix of a value. Enough to disambiguate rotations
    without exposing the secret in audit logs."""
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _emit(deployment_dir: Path, kind: str, **fields: Any) -> None:
    """Append a JSON line to ``<deployment_dir>/rotations.jsonl``. This
    is the audit trail Phase 4 (drain mode) will cross-reference into
    the container's events.jsonl. Fire-and-forget — never raises;
    rotation correctness must not depend on the audit succeeding."""
    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "type": kind,
        **fields,
    }
    log_path = deployment_dir / "rotations.jsonl"
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as exc:
        print(f"warn: failed to write rotations.jsonl: {exc}", file=sys.stderr)


# ── deployment-dir discovery ─────────────────────────────────────────


def _resolve_compose_file(deployment_dir: Path) -> Path:
    for name in _COMPOSE_FILES:
        candidate = deployment_dir / name
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"No compose file ({'/'.join(_COMPOSE_FILES)}) found in {deployment_dir}",
    )


def _resolve_service_name(compose_file: Path, requested: str | None) -> str:
    """Pick the service to recreate + verify against. If ``requested``
    is provided, use it; else parse the compose file and require
    exactly one service."""
    if requested:
        return requested
    try:
        import yaml
        data = yaml.safe_load(compose_file.read_text(encoding="utf-8")) or {}
    except (OSError, ImportError) as exc:
        raise RuntimeError(f"Can't parse compose file {compose_file}: {exc}") from exc
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict) or not services:
        raise RuntimeError(f"No services found in {compose_file}")
    if len(services) == 1:
        return next(iter(services))
    raise RuntimeError(
        f"Multiple services in {compose_file} ({sorted(services)}); "
        f"specify --service <name>",
    )


# ── compose.env edit ─────────────────────────────────────────────────


_ENV_LINE_RE = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$")


def _read_env_value(compose_env: Path, env_name: str) -> str | None:
    """Return the current value of ``env_name`` in ``compose.env``, or
    None if not set. Preserves no surrounding state — pure read."""
    if not compose_env.is_file():
        return None
    for raw in compose_env.read_text(encoding="utf-8").splitlines():
        line = raw.lstrip()
        if not line or line.startswith("#"):
            continue
        m = _ENV_LINE_RE.match(line)
        if m and m.group("name") == env_name:
            return m.group("value")
    return None


def _atomic_replace_env(
    compose_env: Path, env_name: str, new_value: str,
) -> tuple[str | None, Path]:
    """Replace the value of ``env_name`` in ``compose.env`` atomically.
    Returns ``(old_value, backup_path)``. Creates a sibling
    ``compose.env.bak.<ts>`` BEFORE writing so rollback always has a
    target — see PR #283 review note about the prior wording's
    rollback gap.

    The replacement preserves the file verbatim except for one line:
    only the matching ``<name>=...`` line is changed. Comments,
    blank lines, ordering, and trailing whitespace stay as-is.
    """
    if not compose_env.is_file():
        raise FileNotFoundError(f"compose.env not found: {compose_env}")

    backup_path = compose_env.with_suffix(
        compose_env.suffix + f".bak.{int(time.time())}",
    )
    shutil.copy2(compose_env, backup_path)

    old_value: str | None = None
    lines = compose_env.read_text(encoding="utf-8").splitlines(keepends=True)
    new_lines: list[str] = []
    replaced = False
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("#") or not stripped:
            new_lines.append(line)
            continue
        m = _ENV_LINE_RE.match(stripped)
        if m and m.group("name") == env_name and not replaced:
            old_value = m.group("value")
            # Preserve any leading whitespace from the original line.
            leading = line[: len(line) - len(stripped)]
            trailing = "\n" if line.endswith("\n") else ""
            new_lines.append(f"{leading}{env_name}={new_value}{trailing}")
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        # Env var wasn't set yet — append to the end. Operator may
        # be adding a new credential mid-rotation; not common but
        # legal.
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] = new_lines[-1] + "\n"
        new_lines.append(f"{env_name}={new_value}\n")

    # Atomic write: tmp file in the same directory, fsync, rename
    # over the target. The rename is the commit point — a crash
    # before rename leaves compose.env unchanged.
    tmp = compose_env.with_suffix(compose_env.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.writelines(new_lines)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(compose_env)

    return old_value, backup_path


def _rollback(backup_path: Path, compose_env: Path) -> None:
    """Restore ``compose.env`` from ``backup_path``. Used when any
    later step (recreate, verify) fails. The backup is left in place
    after restore — operator inspects manually whether to delete."""
    if not backup_path.is_file():
        print(
            f"warn: backup {backup_path} missing — cannot rollback automatically",
            file=sys.stderr,
        )
        return
    shutil.copy2(backup_path, compose_env)


# ── docker compose operations ────────────────────────────────────────


def _docker_compose(compose_file: Path, *args: str,
                    capture: bool = True, timeout: int = 120,
                    ) -> tuple[int, str, str]:
    cmd = ["docker", "compose", "-f", str(compose_file), *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=capture, text=True, check=False, timeout=timeout,
        )
        return (
            proc.returncode,
            (proc.stdout or "").strip(),
            (proc.stderr or "").strip(),
        )
    except subprocess.TimeoutExpired:
        return (124, "", f"timeout after {timeout}s")
    except FileNotFoundError as exc:
        return (127, "", f"docker not found: {exc}")


def _recreate(compose_file: Path, service: str) -> tuple[bool, str]:
    rc, out, err = _docker_compose(
        compose_file, "up", "-d", "--force-recreate", service, timeout=120,
    )
    if rc != 0:
        return (False, (err or out or f"recreate exited {rc}").splitlines()[0])
    return (True, "")


def _wait_for_running(compose_file: Path, service: str, *,
                      timeout: int = 60, poll_interval: float = 1.0,
                      ) -> tuple[bool, str]:
    """Poll ``docker compose ps`` until the named service reports
    state ``running``. Returns ``(ok, detail)``.

    ``docker compose ps --format json`` outputs one JSON object per
    line (NDJSON); a single object when only one service exists.
    """
    deadline = time.monotonic() + timeout
    last_state = "unknown"
    while time.monotonic() < deadline:
        rc, out, err = _docker_compose(compose_file, "ps", "--format", "json")
        if rc == 0 and out:
            for raw in out.splitlines():
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if obj.get("Service") != service:
                    continue
                state = obj.get("State", "unknown")
                last_state = state
                if state == "running":
                    return (True, "running")
                break
        time.sleep(poll_interval)
    return (False, f"timed out waiting for state=running (last seen: {last_state})")


def _verify_in_container(compose_file: Path, service: str,
                         cred_name: str) -> tuple[bool, str]:
    """Run ``mimir verify-cred <cred_name>`` inside the container so
    the probe sees the freshly-rotated env value. Returns ``(ok,
    detail)``."""
    rc, out, err = _docker_compose(
        compose_file, "exec", "-T", service,
        "mimir", "verify-cred", cred_name, timeout=30,
    )
    detail = (out or err or "").splitlines()[0] if (out or err) else f"exit {rc}"
    return (rc == 0, detail)


# ── orchestration ────────────────────────────────────────────────────


def _find_cred_for_env(env_name: str) -> tuple[str, str] | None:
    """Look up which credential owns ``env_name`` and what its type
    is. Returns ``(cred_name, cred_type)`` or None if no credential
    in the registry lists this env var.

    Walks the registry built from credentials.yaml manifests — so a
    new skill's credential is discoverable without code changes here.
    """
    probes = get_probes()
    for name, probe in probes.items():
        if env_name in probe.env_vars:
            return (name, probe.cred_type)
    return None


def _prompt_new_value(env_name: str) -> str:
    """Read the new value from stdin. Uses ``getpass`` so the value
    isn't echoed to the terminal — important when rotating a token
    from a shared screen / pair-coding context."""
    if sys.stdin.isatty():
        return getpass.getpass(f"New value for {env_name}: ").strip()
    # Non-interactive (piped stdin): read one line, strip the
    # trailing newline. Operator-friendly for scripted rotations.
    return sys.stdin.readline().rstrip("\n")


def run_rotate(
    env_name: str,
    *,
    new_value: str | None = None,
    deployment_dir: Path | None = None,
    service: str | None = None,
    skip_recreate: bool = False,
) -> int:
    """The CLI entrypoint. Returns the exit code (0 = success, 1 =
    rotation failed and was rolled back, 2 = invalid input)."""
    dep_dir = (deployment_dir or Path.cwd()).resolve()
    compose_env = dep_dir / "compose.env"
    if not compose_env.is_file():
        print(f"compose.env not found in {dep_dir}", file=sys.stderr)
        return 2

    try:
        compose_file = _resolve_compose_file(dep_dir)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        service_name = _resolve_service_name(compose_file, service)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    cred_info = _find_cred_for_env(env_name)
    cred_name: str | None = None
    cred_type: str | None = None
    if cred_info is not None:
        cred_name, cred_type = cred_info
    else:
        print(
            f"warn: env var {env_name!r} is not listed by any credentials.yaml manifest. "
            "Rotation will proceed but post-rotation verify is skipped.",
            file=sys.stderr,
        )

    if new_value is None:
        new_value = _prompt_new_value(env_name)
    if not new_value:
        print("empty value — aborting", file=sys.stderr)
        return 2

    ctx = RotationContext(
        env_name=env_name,
        new_value=new_value,
        deployment_dir=dep_dir,
        compose_env_path=compose_env,
        compose_file_path=compose_file,
        service_name=service_name,
        cred_name=cred_name,
        cred_type=cred_type,
    )

    return _execute(ctx, skip_recreate=skip_recreate)


def _execute(ctx: RotationContext, *, skip_recreate: bool) -> int:
    old_value, backup_path = _atomic_replace_env(
        ctx.compose_env_path, ctx.env_name, ctx.new_value,
    )
    ctx.backup_path = backup_path

    _emit(
        ctx.deployment_dir, "credential_rotation_started",
        env=ctx.env_name,
        cred=ctx.cred_name,
        cred_type=ctx.cred_type,
        old_value_hash=_value_hash(old_value) if old_value is not None else None,
        new_value_hash=_value_hash(ctx.new_value),
        backup=str(backup_path.name),
        service=ctx.service_name,
    )

    if skip_recreate:
        print(f"compose.env updated; backup={backup_path.name}")
        print("--no-recreate set; skipping docker compose + verify steps")
        _emit(
            ctx.deployment_dir, "credential_rotation_completed",
            env=ctx.env_name, duration_s=round(time.monotonic() - ctx.started_at, 2),
            verify="skipped (--no-recreate)",
        )
        return 0

    recreate_ok, recreate_detail = _recreate(ctx.compose_file_path, ctx.service_name)
    if not recreate_ok:
        _rollback(backup_path, ctx.compose_env_path)
        # Recreate from the restored backup so the previous good
        # state is re-running before we surface the failure.
        _recreate(ctx.compose_file_path, ctx.service_name)
        _emit(
            ctx.deployment_dir, "credential_rotation_failed",
            env=ctx.env_name, stage="recreate", detail=recreate_detail,
            rolled_back=True,
        )
        print(f"recreate failed: {recreate_detail}", file=sys.stderr)
        print(f"rolled back to {backup_path.name}", file=sys.stderr)
        return 1

    ready_ok, ready_detail = _wait_for_running(
        ctx.compose_file_path, ctx.service_name,
    )
    if not ready_ok:
        _rollback(backup_path, ctx.compose_env_path)
        _recreate(ctx.compose_file_path, ctx.service_name)
        _emit(
            ctx.deployment_dir, "credential_rotation_failed",
            env=ctx.env_name, stage="wait_for_running", detail=ready_detail,
            rolled_back=True,
        )
        print(f"container didn't reach running: {ready_detail}", file=sys.stderr)
        return 1

    if ctx.cred_name is not None:
        verify_ok, verify_detail = _verify_in_container(
            ctx.compose_file_path, ctx.service_name, ctx.cred_name,
        )
        if not verify_ok:
            _rollback(backup_path, ctx.compose_env_path)
            _recreate(ctx.compose_file_path, ctx.service_name)
            _emit(
                ctx.deployment_dir, "credential_rotation_failed",
                env=ctx.env_name, stage="verify", detail=verify_detail,
                rolled_back=True,
            )
            print(f"post-rotation verify failed: {verify_detail}", file=sys.stderr)
            print(f"rolled back to {backup_path.name}", file=sys.stderr)
            return 1
        verify_summary = verify_detail
    else:
        verify_summary = "skipped (env not in registry)"

    _emit(
        ctx.deployment_dir, "credential_rotation_completed",
        env=ctx.env_name,
        duration_s=round(time.monotonic() - ctx.started_at, 2),
        verify=verify_summary,
    )
    print(f"rotation ok: {ctx.env_name} ({verify_summary})")
    return 0
