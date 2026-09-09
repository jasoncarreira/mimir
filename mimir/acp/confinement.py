"""Default execution confinement behind a replaceable platform backend.

Seatbelt denials are not an audit stream. Children may swallow EPERM; this module
makes no claim to identify denied paths. Runtime code/data are explicit read-only
exceptions to the session's writable path scope. Callers must close inherited
file descriptors except their deliberate protocol streams.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import sysconfig
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from mimir.acp.execution_scope import ScopeApproval


class ConfinementUnavailable(RuntimeError):
    """Confinement setup failed; execution must not silently downgrade."""


class BackendUnavailable(ConfinementUnavailable):
    """No backend exists here; only operator-approved fallback may proceed."""


@dataclass(frozen=True)
class PreparedCommand:
    argv: tuple[str, ...]
    env: dict[str, str]
    execution_mode: str = "confined"


class ConfinementBackend(Protocol):
    def prepare(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        approved_paths: Iterable[ScopeApproval],
        scratch_paths: Iterable[Path],
    ) -> PreparedCommand: ...


def _canonical(path: Path) -> Path:
    return path.expanduser().resolve()


def _filter(path: Path, *, tree: bool) -> str:
    # JSON escaping is compatible with Seatbelt string literals; never interpolate
    # raw paths as policy syntax (a path may contain quotes or parentheses).
    return f'({"subpath" if tree else "literal"} {json.dumps(str(path), ensure_ascii=False)})'


def _runtime_reads() -> tuple[set[Path], set[Path]]:
    """Allow the running interpreter and installed code, not their parent trees."""
    trees = {Path(p) for p in ("/usr/lib", "/System/Library", "/bin", "/usr/bin")}
    literals = {Path(p) for p in ("/", "/dev/null", "/dev/random", "/dev/urandom")}
    paths = sysconfig.get_paths()
    for key in ("stdlib", "purelib", "platlib"):
        trees.add(_canonical(Path(paths[key])))
    # Editable installs need their package, not the repository (which may hold
    # credentials). Installed wheels already fall within purelib/platlib.
    trees.add(Path(__file__).resolve().parents[1])
    literals.add(Path(__file__).resolve().parents[2])
    for executable in (sys.executable, getattr(sys, "_base_executable", sys.executable)):
        original = Path(executable).absolute()
        resolved = original.resolve()
        literals.update((original, resolved))
        literals.add((original.parent.parent / "pyvenv.cfg").resolve())
        # CPython may link libpython from its own lib directory. Grant individual
        # shared libraries rather than the prefix, bin directory, or whole home.
        lib = resolved.parent.parent / "lib"
        if lib.is_dir():
            literals.update(p.resolve() for p in lib.glob("*.dylib"))
        for parent in resolved.parents:
            if parent.name.endswith(".framework"):
                trees.add(parent)
                break
    return literals, trees


def _environment() -> dict[str, str]:
    # Filesystem confinement does not protect secrets already inherited in the
    # environment. Keep presentation/search settings, never credentials, agent
    # sockets, dynamic-loader injection, or shell/Python startup configuration.
    # Commands needing application-specific environment must set it explicitly
    # from data within their approved scope.
    allowed = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM")
    env = {name: os.environ[name] for name in allowed if name in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    # Resolve the editable package without granting its parent directory's data.
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    return env


class SeatbeltBackend:
    """Deprecated macOS sandbox-exec backend; failure never falls through."""

    executable = Path("/usr/bin/sandbox-exec")

    def prepare(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        approved_paths: Iterable[ScopeApproval] = (),
        scratch_paths: Iterable[Path] = (),
    ) -> PreparedCommand:
        if not self.executable.is_file() or not os.access(self.executable, os.X_OK):
            raise BackendUnavailable("Hands confinement unavailable: sandbox-exec is missing")
        if not argv:
            raise ValueError("a confined command requires argv")
        # Session binding freezes the canonical cwd. Following a replacement
        # symlink here would silently widen the original operator-approved tree.
        cwd = Path(os.path.abspath(cwd))
        if cwd.resolve() != cwd:
            raise ConfinementUnavailable("Hands confinement session cwd changed identity")
        if not cwd.is_dir():
            raise ConfinementUnavailable("Hands confinement requires an existing session cwd")
        literals, trees = _runtime_reads()
        read_filters = [_filter(p, tree=False) for p in sorted(literals)]
        read_filters += [_filter(p, tree=True) for p in sorted(trees)]
        writable = {_filter(cwd, tree=True)}
        for value in approved_paths:
            # The scope authority freezes canonical paths at approval time. Do
            # not follow a replacement symlink and silently grant its new target.
            path = Path(os.path.abspath(value.path))
            writable.add(_filter(path, tree=value.recursive))
        scratch_roots: list[str] = []
        for value in scratch_paths:
            path = Path(os.path.abspath(value))
            if path.resolve() != path:
                raise ConfinementUnavailable("Hands confinement scratch directory changed identity")
            if not path.is_dir():
                raise ConfinementUnavailable("Hands confinement requires existing scratch directories")
            writable.add(_filter(path, tree=True))
            scratch_roots.append(_filter(path, tree=False))
        profile = "\n".join((
            "(version 1)",
            "(deny default)",
            "(allow process-exec process-fork)",
            "(allow sysctl-read)",
            # Metadata supports path traversal/import discovery, not file content.
            "(allow file-read-metadata)",
            "(allow file-read* " + " ".join(read_filters) + ")",
            "(allow file-read* file-write* " + " ".join(sorted(writable)) + ")",
            '(allow file-write* (literal "/dev/null"))',
            # Workers may create/remove capture entries, but never replace the
            # host-owned scratch root itself. Parent cleanup is not sandboxed.
            "(deny file-write-unlink " + " ".join(scratch_roots) + ")" if scratch_roots else "",
        ))
        env = _environment()
        env["TMPDIR"] = str(cwd)
        return PreparedCommand((str(self.executable), "-p", profile, *argv), env)


def apparmor_profile(
    cwd: Path, approved_paths: Iterable[ScopeApproval] = (), scratch_paths: Iterable[Path] = (),
) -> str:
    """Synthesize only: inputs are frozen absolute paths, never filesystem lookups.

    A suffix below an exact path grants descendants, not similarly named siblings.
    AppArmor's path language is deliberately restricted rather than escaped.
    """
    scratch_paths = tuple(scratch_paths)
    roots = set()
    grants = (ScopeApproval(cwd, recursive=True), *approved_paths,
              *(ScopeApproval(path, recursive=True) for path in scratch_paths))
    for grant in grants:
        value = str(grant.path)
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ConfinementUnavailable("AppArmor cannot represent non-UTF-8 paths") from exc
        if (not value.startswith("/") or value == "/"
                or any(part in (".", "..") for part in value.split("/"))
                or not re.fullmatch(r"/[\w./+-]+", value, flags=re.ASCII)):
            raise ConfinementUnavailable("AppArmor cannot represent scope path unambiguously")
        roots.add((value, grant.recursive))
    rules = [
        # No ux/px or change_profile permission: every exec inherits this profile.
        "  /** ix,",
        "  /usr/bin/** mr,", "  /bin/** mr,",
        "  /usr/lib/** mr,", "  /usr/lib64/** mr,",
        "  /lib/** mr,", "  /lib64/** mr,", "  /usr/local/lib/** mr,",
        "  /etc/ld.so.cache r,",
        "  /dev/null rw,", "  /dev/urandom r,", "  /dev/random r,",
        "  /proc/*/attr/current r,",
    ]
    for root, recursive in sorted(roots):
        rules.append(f"  {root} rwk,")
        if recursive:
            rules.extend((f"  {root}/ rw,", f"  {root}/** rwk,"))
    # Deny overrides an overlapping cwd grant without denying scratch contents.
    for path in sorted(set(scratch_paths)):
        rules.extend((f"  deny {path} w,", f"  deny {path}/ w,"))
    body = "\n".join(rules)
    name = "mimir-hands-" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    return f"profile {name} {{\n{body}\n}}\n"


# Use an isolated system interpreter so scope-writable startup modules cannot
# forge the label check. Both the probe and the actual launcher use this code.
# Failure exits the confined child; there is no unconfined runtime retry.
_APPARMOR_LAUNCH = """import os, sys
with open('/proc/self/attr/current', encoding='ascii') as stream:
    label = stream.read().strip()
if label != sys.argv[1] + ' (enforce)':
    raise SystemExit('AppArmor enforcing transition not verified')
if len(sys.argv) == 2:
    print(label)
else:
    os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
"""


class AppArmorBackend:
    """Unverified on real hardware; never infer enforcement from tool presence."""

    parser = Path("/usr/sbin/apparmor_parser")
    executable = Path("/usr/bin/aa-exec")
    interpreter = Path("/usr/bin/python3")
    enabled = Path("/sys/module/apparmor/parameters/enabled")

    def _available(self) -> None:
        try:
            if self.enabled.read_text(encoding="ascii").strip() != "Y":
                raise BackendUnavailable("Hands confinement unavailable: AppArmor LSM is not enabled")
            for tool in (self.parser, self.executable, self.interpreter):
                if not tool.is_file() or not os.access(tool, os.X_OK):
                    raise BackendUnavailable(f"Hands confinement unavailable: missing executable {tool}")
        except (OSError, UnicodeError) as exc:
            raise BackendUnavailable("Hands confinement unavailable: cannot verify AppArmor LSM") from exc

    def prepare(
        self, argv: Sequence[str], *, cwd: Path,
        approved_paths: Iterable[ScopeApproval] = (), scratch_paths: Iterable[Path] = (),
    ) -> PreparedCommand:
        if not argv:
            raise ValueError("a confined command requires argv")
        self._available()
        scratch_paths = tuple(scratch_paths)
        for path in (cwd, *scratch_paths):
            if path.resolve() != path or not path.is_dir():
                raise ConfinementUnavailable("AppArmor cwd/scratch changed identity")
        profile = apparmor_profile(cwd, approved_paths, scratch_paths)
        name = profile.split()[1]
        env = _environment()
        env["TMPDIR"] = str(cwd)
        # Fixed executable/options, sanitized environment, stdin policy, same uid.
        # Ignore parser.conf (which can enable complain mode or skip loading).
        parser = (str(self.parser), "--config-file=/dev/null", "--skip-cache", "--Werror")
        try:
            checked = subprocess.run(
                (*parser, "--skip-kernel-load"), input=profile, text=True,
                capture_output=True, env=env, cwd="/", timeout=15, close_fds=True,
            )
            if checked.returncode != 0:
                raise ConfinementUnavailable("AppArmor profile compilation failed")
            loaded = subprocess.run(
                (*parser, "--replace"), input=profile, text=True,
                capture_output=True, env=env, cwd="/", timeout=15, close_fds=True,
            )
            if loaded.returncode != 0:
                raise BackendUnavailable("Hands confinement unavailable: AppArmor profile load failed for this uid")
            launcher = (
                str(self.executable), "-p", name, "--", str(self.interpreter),
                "-I", "-S", "-c", _APPARMOR_LAUNCH, name,
            )
            probe = subprocess.run(
                launcher, text=True, capture_output=True, env=env, cwd=cwd,
                timeout=15, close_fds=True,
            )
            if not self._transition_verified(probe, name):
                raise BackendUnavailable("Hands confinement unavailable: AppArmor enforcing transition failed")
        except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
            raise BackendUnavailable("Hands confinement unavailable: AppArmor verification failed") from exc
        return PreparedCommand((*launcher, *argv), env)

    @staticmethod
    def _transition_verified(probe: subprocess.CompletedProcess, name: str) -> bool:
        return probe.returncode == 0 and probe.stdout.strip() == name + " (enforce)"


def _backend() -> ConfinementBackend:
    if sys.platform == "linux":
        return AppArmorBackend()
    if sys.platform != "darwin":
        raise BackendUnavailable(
            "Hands confinement unavailable on this platform; macOS Seatbelt is required"
        )
    return SeatbeltBackend()


def validate_scope(*, cwd: Path, candidate_paths: Iterable[ScopeApproval]) -> None:
    """Validate canonical candidate syntax without loading or running its policy."""
    if sys.platform == "linux":
        apparmor_profile(cwd, candidate_paths)
        AppArmorBackend()._available()
    else:
        prepare_command(("/bin/true",), cwd=cwd, approved_paths=candidate_paths)


def prepare_command(
    argv: Sequence[str],
    *,
    cwd: Path,
    approved_paths: Iterable[ScopeApproval] = (),
    scratch_paths: Iterable[Path] = (),
    allow_unconfined: bool = False,
) -> PreparedCommand:
    try:
        return _backend().prepare(
            argv, cwd=cwd, approved_paths=approved_paths, scratch_paths=scratch_paths
        )
    except BackendUnavailable:
        if allow_unconfined is not True:
            raise
        # This flag is host-owned risk authority, never a tool argument. Preserve
        # non-OS hardening and still reject replaced cwd/scratch identities.
        directory = Path(os.path.abspath(cwd))
        if directory.resolve() != directory or not directory.is_dir():
            raise ConfinementUnavailable("Hands execution cwd changed identity") from None
        for value in scratch_paths:
            path = Path(os.path.abspath(value))
            if path.resolve() != path or not path.is_dir():
                raise ConfinementUnavailable("Hands execution scratch changed identity") from None
        if not argv:
            raise ValueError("an execution command requires argv")
        env = _environment()
        env["TMPDIR"] = str(directory)
        return PreparedCommand(tuple(argv), env, "unconfined")
