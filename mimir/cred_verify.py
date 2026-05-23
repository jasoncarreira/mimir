"""Credential verification probes (SPEC §16 item 14 — credential
rotation, Phase 2.5).

Skills register their own credentials via a ``credentials.yaml``
manifest shipped alongside ``SKILL.md``. The framework discovers
these at startup and builds the probe registry — no central
hardcoded list to grow as new skills land.

Discovery roots (operator-shadows-bundled, mirrors PR #272's
skills walker):

  1. ``mimir/credentials.yaml`` — mimir-core creds shipped with the
     package (ANTHROPIC_API_KEY, MIMIR_API_KEY, GITHUB_TOKEN, the
     Discord/Slack bridge tokens, Claude OAuth).
  2. ``<home>/.mimir_builtin_skills/<skill>/credentials.yaml`` —
     bundled optional skills.
  3. ``<home>/skills/<skill>/credentials.yaml`` — operator skills.

Within a given probe name, later roots shadow earlier ones — so an
operator can override a bundled probe spec without forking the
framework.

Probe kinds (declarative, no Python needed for the common cases):

  - ``subprocess`` — run a command, exit 0 = live. Short-circuits
    to ``unavailable`` when ``binary`` isn't on PATH.
  - ``format`` — env present + ``prefix`` / ``min_len`` /
    ``disallowed_prefix`` check.
  - ``all_env_set`` — every named env var is set + non-empty.
  - ``not_implemented`` — explicit Phase-3 stub for Type B/C.
  - ``python`` — escape hatch. ``script`` is a path relative to
    the manifest dir; ``function`` (default ``probe``) returns
    ``(ok, detail)``. Loaded via ``importlib.util``.

Probe contract: side-effect-free; runs a cheap auth-status / format
check that confirms reachability without consuming provider quota.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

import yaml

log = logging.getLogger(__name__)


CredType = Literal["A", "B", "C", "D"]


@dataclass
class ProbeResult:
    """Outcome of a single credential probe."""

    name: str
    cred_type: CredType
    ok: bool
    detail: str

    def render(self) -> str:
        status = "OK" if self.ok else "FAIL"
        return f"[{self.cred_type}] {status}  {self.name}: {self.detail}"


@dataclass
class Probe:
    """Registry entry."""

    name: str
    cred_type: CredType
    env_vars: tuple[str, ...]
    description: str
    fn: Callable[[], tuple[bool, str]]
    source: str   # where the manifest lived (for debugging duplicates)


# ── helpers used by factories ────────────────────────────────────────


def _env_set(name: str) -> tuple[bool, str | None]:
    v = os.environ.get(name, "").strip()
    return (bool(v), v if v else None)


def _all_env_set(*names: str) -> tuple[bool, str]:
    missing = [n for n in names if not os.environ.get(n, "").strip()]
    if missing:
        return (False, f"missing env: {', '.join(missing)}")
    return (True, "")


def _has_binary(name: str) -> bool:
    return shutil.which(name) is not None


def _run_quiet(cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
    """``(rc, stdout, stderr)``. ``rc=124`` on timeout, ``rc=127``
    on missing binary (coreutils convention so callers can
    disambiguate)."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except FileNotFoundError as exc:
        return 127, "", str(exc)


def _unavailable(reason: str) -> tuple[bool, str]:
    return (False, f"unavailable: {reason}")


# ── factories — one per probe kind ───────────────────────────────────


def _make_subprocess_probe(
    binary: str, cmd: list[str], env_vars: tuple[str, ...],
    success_detail: str | None = None,
) -> Callable[[], tuple[bool, str]]:
    """``kind: subprocess`` — short-circuit if binary missing or
    declared env vars unset; else run cmd, exit 0 = live."""
    def _probe() -> tuple[bool, str]:
        if not _has_binary(binary):
            return _unavailable(f"`{binary}` not installed")
        if env_vars:
            ok, missing = _all_env_set(*env_vars)
            if not ok:
                return _unavailable(missing)
        rc, out, err = _run_quiet(cmd)
        if rc == 0:
            # Surface the first non-empty stderr/stdout line so the
            # operator sees who the token belongs to (gh writes the
            # login to stderr, acli writes to stdout, etc.).
            first = next(
                (l for l in (err.splitlines() + out.splitlines()) if l.strip()),
                "",
            )
            return (True, success_detail or first or f"{binary} ok")
        return (False, (err.splitlines() + out.splitlines() or [f"{binary} exit {rc}"])[0])
    return _probe


def _make_format_probe(
    env: str, *, prefix: str | None = None, min_len: int = 16,
    disallowed_prefix: str | None = None,
    length: int | None = None, charset: str | None = None,
) -> Callable[[], tuple[bool, str]]:
    """``kind: format`` — env present + (optional) prefix / length /
    charset / disallowed_prefix check. Live API call deferred to
    first real use."""
    def _probe() -> tuple[bool, str]:
        ok, value = _env_set(env)
        if not ok or value is None:
            return _unavailable(f"{env} not set")
        if length is not None and len(value) != length:
            return (False, f"format wrong (expected {length} chars, got {len(value)})")
        if length is None and len(value) < min_len:
            return (False, f"format wrong (too short, got {len(value)} chars)")
        if charset == "hex" and not all(c in "0123456789abcdef" for c in value.lower()):
            return (False, "format wrong (expected hex)")
        if prefix is not None and not value.startswith(prefix):
            return (False, f"format wrong (expected prefix {prefix!r})")
        if disallowed_prefix is not None and value.startswith(disallowed_prefix):
            return (False, f"format wrong (starts with {disallowed_prefix!r} — wrong provider?)")
        return (True, f"format ok ({len(value)} chars)")
    return _probe


def _make_all_env_set_probe(
    env_vars: tuple[str, ...], note: str | None = None,
) -> Callable[[], tuple[bool, str]]:
    """``kind: all_env_set`` — every named env var must be set +
    non-empty. Used for multi-var bundles (X OAuth quartet, ACLI's
    three vars) where partial updates break signing."""
    def _probe() -> tuple[bool, str]:
        ok, missing = _all_env_set(*env_vars)
        if not ok:
            return _unavailable(missing)
        detail = "format ok"
        if note:
            detail = f"{detail} ({note})"
        return (True, detail)
    return _probe


def _make_not_implemented_probe(cred_type: CredType) -> Callable[[], tuple[bool, str]]:
    def _probe() -> tuple[bool, str]:
        return (False, f"not_implemented: Type {cred_type} probe pending Phase 3")
    return _probe


def _make_python_probe(
    manifest_dir: Path, script: str, function: str = "probe",
) -> Callable[[], tuple[bool, str]]:
    """``kind: python`` — escape hatch. The skill ships a Python file
    next to ``credentials.yaml`` (or anywhere relative to it); the
    framework loads it via ``importlib.util`` and calls ``function()``.

    The callable's contract: zero args, returns ``(ok: bool, detail: str)``.
    Module-level side effects (e.g. HTTP imports) happen at the
    FIRST probe call, not at framework startup, so a broken script
    doesn't crash the registry — it surfaces as an unavailable
    probe.
    """
    script_path = (manifest_dir / script).resolve()

    def _probe() -> tuple[bool, str]:
        if not script_path.is_file():
            return _unavailable(f"probe script not found: {script_path}")
        try:
            spec = importlib.util.spec_from_file_location(
                f"_cred_probe_{script_path.stem}_{id(script_path)}", script_path,
            )
            if spec is None or spec.loader is None:
                return _unavailable(f"can't load probe script: {script_path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            fn = getattr(module, function, None)
            if not callable(fn):
                return _unavailable(
                    f"probe script has no callable {function!r}",
                )
            result = fn()
        except Exception as exc:  # noqa: BLE001 — surface any user-script failure
            return (False, f"probe script raised: {type(exc).__name__}: {exc}")
        if (not isinstance(result, tuple) or len(result) != 2
                or not isinstance(result[0], bool)
                or not isinstance(result[1], str)):
            return (False, f"probe script returned bad shape: {result!r}")
        return result
    return _probe


# ── manifest parsing + discovery ─────────────────────────────────────


def _build_probe_from_spec(
    entry: dict[str, Any], manifest_path: Path,
) -> Probe | None:
    """Validate a single ``credentials:`` entry from a manifest and
    build the runtime Probe. Returns ``None`` (with a logged warning)
    on malformed entries — keeps a bad manifest from breaking the
    whole registry."""
    name = entry.get("name")
    cred_type = entry.get("cred_type")
    env_vars = tuple(entry.get("env_vars", []))
    description = entry.get("description", "")
    probe_spec = entry.get("probe", {})
    kind = probe_spec.get("kind") if isinstance(probe_spec, dict) else None

    if not isinstance(name, str) or not name:
        log.warning("credentials_manifest_skipped: %s — missing/bad name", manifest_path)
        return None
    if cred_type not in ("A", "B", "C", "D"):
        log.warning(
            "credentials_manifest_skipped: %s name=%r — bad cred_type=%r",
            manifest_path, name, cred_type,
        )
        return None

    fn: Callable[[], tuple[bool, str]] | None
    if kind == "subprocess":
        fn = _make_subprocess_probe(
            binary=probe_spec["binary"],
            cmd=list(probe_spec["cmd"]),
            env_vars=env_vars,
            success_detail=probe_spec.get("success_detail"),
        )
    elif kind == "format":
        # Pull only the kwargs the factory accepts; ignore extras
        # silently so future-additive spec changes don't crash older
        # frameworks.
        accepted = {"prefix", "min_len", "disallowed_prefix", "length", "charset"}
        kwargs = {k: probe_spec[k] for k in accepted if k in probe_spec}
        env = probe_spec.get("env", env_vars[0] if env_vars else None)
        if not env:
            log.warning(
                "credentials_manifest_skipped: %s name=%r — format probe has no env target",
                manifest_path, name,
            )
            return None
        fn = _make_format_probe(env, **kwargs)
    elif kind == "all_env_set":
        fn = _make_all_env_set_probe(env_vars, note=probe_spec.get("note"))
    elif kind == "not_implemented":
        fn = _make_not_implemented_probe(cred_type)
    elif kind == "python":
        fn = _make_python_probe(
            manifest_path.parent,
            script=probe_spec["script"],
            function=probe_spec.get("function", "probe"),
        )
    else:
        log.warning(
            "credentials_manifest_skipped: %s name=%r — unknown probe kind=%r",
            manifest_path, name, kind,
        )
        return None

    return Probe(
        name=name, cred_type=cred_type, env_vars=env_vars,
        description=description, fn=fn, source=str(manifest_path),
    )


def _load_manifest(manifest_path: Path) -> list[Probe]:
    """Parse one ``credentials.yaml`` file. Returns the probe list
    (possibly empty). Logs and continues on parse/format errors."""
    try:
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError) as exc:
        log.warning("credentials_manifest_unreadable: %s — %s", manifest_path, exc)
        return []
    if not isinstance(data, dict):
        return []
    entries = data.get("credentials")
    if not isinstance(entries, list):
        return []
    out: list[Probe] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        probe = _build_probe_from_spec(entry, manifest_path)
        if probe is not None:
            out.append(probe)
    return out


# Package-shipped manifest (mimir-core credentials). Sibling to this
# file so it's always findable regardless of how mimir is installed.
_PACKAGE_MANIFEST = Path(__file__).parent / "credentials.yaml"


def _resolve_home(home: Path | None) -> Path | None:
    if home is not None:
        return home
    raw = os.environ.get("MIMIR_HOME")
    return Path(raw).expanduser().resolve() if raw else None


def _discover_probes(home: Path | None) -> dict[str, Probe]:
    """Walk the discovery roots, build a Probe per entry, return the
    merged registry. Later-discovered names shadow earlier ones — the
    operator's ``<home>/skills/`` always wins over the bundle."""
    out: dict[str, Probe] = {}

    def _ingest(manifest_path: Path) -> None:
        if not manifest_path.is_file():
            return
        for probe in _load_manifest(manifest_path):
            if probe.name in out:
                log.info(
                    "credentials_manifest_shadow: %s shadows earlier %s",
                    manifest_path, out[probe.name].source,
                )
            out[probe.name] = probe

    _ingest(_PACKAGE_MANIFEST)

    if home is not None:
        for root_name in (".mimir_builtin_skills", "skills"):
            root = home / root_name
            if not root.is_dir():
                continue
            for skill_dir in sorted(root.iterdir()):
                if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                    continue
                _ingest(skill_dir / "credentials.yaml")

    return out


# ── public API ───────────────────────────────────────────────────────


_probes_cache: dict[str, Probe] | None = None
_probes_cache_home: Path | None = None
_probes_cache_marker = object()  # sentinel for "no home queried yet"


def get_probes(home: Path | None = None) -> dict[str, Probe]:
    """Return the merged probe registry. Cached per ``home`` value
    so repeated CLI calls don't re-walk the disk."""
    global _probes_cache, _probes_cache_home
    resolved = _resolve_home(home)
    if _probes_cache is not None and _probes_cache_home == resolved:
        return _probes_cache
    _probes_cache = _discover_probes(resolved)
    _probes_cache_home = resolved
    return _probes_cache


def reset_probes_cache() -> None:
    """For tests / when MIMIR_HOME changes mid-process."""
    global _probes_cache, _probes_cache_home
    _probes_cache = None
    _probes_cache_home = None


def verify(name: str, home: Path | None = None) -> ProbeResult:
    """Run a single probe by registry name. Returns a
    ``ProbeResult(ok=False, detail='unknown credential: ...')`` if
    the name isn't registered — Phase 3 (rotation) calls this inline
    and doesn't want a bare ``KeyError`` on a typo."""
    probes = get_probes(home)
    probe = probes.get(name)
    if probe is None:
        return ProbeResult(
            name=name, cred_type="A", ok=False,
            detail=f"unknown credential: {name!r}",
        )
    ok, detail = probe.fn()
    return ProbeResult(
        name=probe.name, cred_type=probe.cred_type, ok=ok, detail=detail,
    )


def verify_all(home: Path | None = None) -> list[ProbeResult]:
    """Run every discovered probe in registry order."""
    return [verify(name, home=home) for name in get_probes(home)]


# ── CLI entrypoints (wired in cli.py) ────────────────────────────────


def run_verify_cred_cmd(name: str, home: Path | None = None) -> int:
    probes = get_probes(home)
    if name not in probes:
        avail = ", ".join(sorted(probes))
        print(f"unknown credential: {name!r}")
        print(f"  registered: {avail or '(none — no credentials.yaml manifests discovered)'}")
        return 2
    result = verify(name, home=home)
    print(result.render())
    return 0 if result.ok else 1


def run_verify_creds_cmd(only_type: str | None = None, home: Path | None = None) -> int:
    results = verify_all(home=home)
    if only_type:
        results = [r for r in results if r.cred_type == only_type]
    if not results:
        if only_type:
            print(f"no probes registered for type {only_type!r}")
        else:
            print("no probes registered (no credentials.yaml manifests discovered)")
        return 1
    failures = 0
    for r in results:
        print(r.render())
        if not r.ok:
            failures += 1
    print()
    print(f"{len(results) - failures}/{len(results)} probes ok")
    return 0 if failures == 0 else 1
