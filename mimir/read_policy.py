"""Shared confidentiality policy for non-admin filesystem reads."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .secret_scan import contains_secret

_PEM_PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----", re.IGNORECASE,
)
_BASIC_AUTH_URL_PATTERN = re.compile(
    r"\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@", re.IGNORECASE,
)


READ_RESOURCE_OPERATIONS = frozenset({
    "read_file", "aread", "ls", "als", "glob", "aglob", "grep", "agrep",
    "file_search", "get_turn", "mimir_get_turn",
})


def requested_read_target_from_arguments(
    tool_name: str,
    arguments: dict[str, Any] | None,
) -> Any:
    """Return the caller-supplied read selector without validating it."""
    args = arguments if isinstance(arguments, dict) else {}
    if tool_name in {"read_file", "aread"}:
        return args.get("file_path") or args.get("path")
    if tool_name in {"ls", "als", "glob", "aglob", "grep", "agrep"}:
        return args.get("path")
    if tool_name == "file_search":
        return args.get("path_prefix") if "path_prefix" in args else args.get("scope")
    if tool_name in {"get_turn", "mimir_get_turn"}:
        return args.get("turn_id")
    return None


def emit_hard_read_denial(tool: str, target: Any, reason: str) -> None:
    """Record a protected result that the backend actually withheld."""
    from .tools.budget_gate import _emit_hard_boundary_denied

    _emit_hard_boundary_denied(
        tool=tool,
        boundary="protected_read_policy",
        reason=reason,
        target=target,
    )

_PROTECTED_BASENAMES = frozenset({
    ".env",
    "compose.env",
    "credentials.json",
    "credentials.yaml",
    "credentials.yml",
    "identities.json",
    "identities.yaml",
    "identities.yml",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
    ".netrc",
    ".pypirc",
    ".npmrc",
})
_PROTECTED_DIR_NAMES = frozenset({"credentials", "identities"})
_PROTECTED_SUFFIXES = frozenset({".key", ".pem", ".p12", ".pfx"})


def non_admin_read_filter_enabled() -> bool:
    """Return whether the current tool caller needs protected-read filtering."""
    from ._context import get_current_turn

    turn = get_current_turn()
    auth_context = getattr(turn, "auth_context", None)
    if auth_context is None:
        return False
    roles = getattr(auth_context, "roles", ()) or ()
    authority = getattr(auth_context, "service_authority", None)
    service_has_read_scope = bool(getattr(authority, "filesystem_read_roots", ()))
    return "admin" not in roles and (
        not getattr(auth_context, "is_service", False) or service_has_read_scope
    )


def _operator_secret_paths() -> tuple[Path, ...]:
    """Return exact secret/config files whose locations are operator supplied."""
    paths: list[Path] = []
    for variable in ("MIMIR_MCP_SERVERS_PATH",):
        raw = os.environ.get(variable, "").strip()
        if raw:
            try:
                paths.append(Path(raw).expanduser().resolve())
            except (OSError, RuntimeError):
                continue
    return tuple(paths)


def _resolved_mimir_home() -> Path | None:
    home_raw = os.environ.get("MIMIR_HOME", "").strip()
    if not home_raw:
        return None
    try:
        return Path(home_raw).resolve()
    except (OSError, RuntimeError):
        return None


def is_mimir_home_root(path: Path) -> bool:
    """Return whether ``path`` is the home node traversed to reach ``state``."""
    home = _resolved_mimir_home()
    if home is None:
        return False
    try:
        return path.resolve() == home
    except (OSError, RuntimeError):
        return False


def is_protected_read_path(path: Path) -> bool:
    """Cheap path-only check shared by authz and collection backends."""
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return True

    name = resolved.name.lower()
    if (
        name in _PROTECTED_BASENAMES
        or name.startswith(".env.")
        or resolved.suffix.lower() in _PROTECTED_SUFFIXES
        or any(part.lower() in _PROTECTED_DIR_NAMES for part in resolved.parts)
    ):
        return True
    if any(resolved == secret for secret in _operator_secret_paths()):
        return True

    home = _resolved_mimir_home()
    if home is not None and (resolved == home or resolved.is_relative_to(home)):
        state = home / "state"
        if not (resolved == state or resolved.is_relative_to(state)):
            return True
    return False


def is_current_service_protected_read_path(path: Path) -> bool:
    """Apply the stricter trigger-service protected names during tool execution."""
    from ._context import get_current_turn

    turn = get_current_turn()
    auth_context = getattr(turn, "auth_context", None)
    authority = getattr(auth_context, "service_authority", None)
    if not getattr(authority, "filesystem_read_roots", ()):
        return False
    # Keep authorization and read-boundary enforcement on one security list.
    # Import lazily to avoid a module cycle: access_control imports this module
    # from the authorization path that scans individual file contents.
    from .access_control import _TRIGGER_SERVICE_PROTECTED_READ_NAMES

    for raw_root in authority.filesystem_read_roots:
        root = Path(raw_root)
        try:
            if path == root or path.is_relative_to(root):
                lexical = path.relative_to(root)
                if any(
                    part.lower() in _TRIGGER_SERVICE_PROTECTED_READ_NAMES
                    for part in lexical.parts
                ):
                    return True
            resolved_root = root.resolve(strict=True)
            resolved = path.resolve(strict=True)
            if resolved == resolved_root or resolved.is_relative_to(resolved_root):
                relative = resolved.relative_to(resolved_root)
                if any(
                    part.lower() in _TRIGGER_SERVICE_PROTECTED_READ_NAMES
                    for part in relative.parts
                ):
                    return True
        except (OSError, RuntimeError, ValueError):
            return True
    return False


def is_current_service_scoped_read_path(path: Path) -> bool:
    """Return whether the resolved path is inside a frozen service read root."""
    from ._context import get_current_turn

    turn = get_current_turn()
    auth_context = getattr(turn, "auth_context", None)
    authority = getattr(auth_context, "service_authority", None)
    for raw_root in getattr(authority, "filesystem_read_roots", ()):
        try:
            root = Path(raw_root).resolve(strict=True)
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if resolved == root or resolved.is_relative_to(root):
            return True
    return False


def text_contains_secret(text: str, *, path: Path | None = None) -> bool:
    """Detect protected credential bodies, including path-specific config forms."""
    if contains_secret(text) or _PEM_PRIVATE_KEY_PATTERN.search(text):
        return True
    return (
        path is not None
        and path.name.lower() == "config"
        and path.parent.name.lower() == ".git"
        and _BASIC_AUTH_URL_PATTERN.search(text) is not None
    )


def file_contains_secret(path: Path) -> bool:
    """Scan one file, failing closed when it cannot be inspected."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, RuntimeError):
        return True
    return text_contains_secret(text, path=path)


def result_is_protected(path: Path, *, text: str | None = None) -> bool:
    """Check a result at its read boundary without re-reading supplied text."""
    service_scoped = is_current_service_scoped_read_path(path)
    if (
        (not service_scoped and is_protected_read_path(path))
        or is_current_service_protected_read_path(path)
    ):
        return True
    if text is not None:
        return text_contains_secret(text, path=path)
    return path.is_file() and file_contains_secret(path)


def configured_non_admin_read_roots() -> tuple[Path, ...]:
    """Return state, configured source roots, and /tmp, never the home root."""
    home_raw = os.environ.get("MIMIR_HOME", "").strip()
    if not home_raw:
        return ()
    try:
        home = Path(home_raw).resolve()
    except (OSError, RuntimeError):
        return ()

    from .config import _parse_file_tool_roots

    raw_configured = os.environ.get("MIMIR_FILE_TOOL_ROOTS", "")
    configured = _parse_file_tool_roots(raw_configured, home)
    configured_paths = [Path(path) for path, _mode in configured]
    configured_path_set = set(configured_paths)
    roots = [home / "state", *configured_paths]

    # The shared parser intentionally returns canonical paths. Retain any
    # validated spelling supplied by the operator for lexical root selection.
    for entry in raw_configured.split(","):
        path_part, separator, mode_part = entry.strip().rpartition(":")
        has_mode = separator and mode_part.strip().lower() in {"ro", "rw"}
        raw_path = path_part if has_mode else entry
        root = Path(raw_path.strip())
        if not root.is_absolute():
            continue
        try:
            accepted = root.resolve() in configured_path_set
        except (OSError, RuntimeError):
            accepted = False
        if accepted and root not in roots:
            roots.append(root)
    tmp = Path("/tmp")
    if tmp.is_dir() and tmp not in roots:
        roots.append(tmp)
    return tuple(roots)


def resolve_non_admin_read_target(
    raw_path: Any,
    *,
    scan_file: bool = False,
    allow_home_root: bool = False,
) -> Path | None:
    """Resolve one absolute target within the non-admin read roots.

    ``allow_home_root`` is only for collection operations.  The home directory
    may be traversed as a routing node so the backend can expose ``state/``, but
    every non-state descendant remains protected.  Single-file reads never set
    it and therefore cannot read the home directory itself.
    """
    if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path:
        return None
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        return None
    root_pairs: list[tuple[Path, Path]] = []
    for root in configured_non_admin_read_roots():
        try:
            resolved_root = root.resolve(strict=True)
        except (OSError, RuntimeError):
            resolved_root = root
        root_pairs.append((root, resolved_root))
    home_raw = os.environ.get("MIMIR_HOME", "").strip()
    try:
        home = Path(home_raw).resolve(strict=True)
        state = (home / "state").resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if allow_home_root and all(
        resolved_root != home for _root, resolved_root in root_pairs
    ):
        root_pairs.append((home, home))
    lexical_roots = [
        (root, resolved_root) for root, resolved_root in root_pairs
        if candidate == root or candidate.is_relative_to(root)
    ]
    if not lexical_roots:
        try:
            from .readonly_backend import _RootAwareFilesystemBackend

            candidate = _RootAwareFilesystemBackend(
                root_dir=home, virtual_mode=True,
            )._resolve_path(raw_path)
            lexical_roots = [
                (root, resolved_root) for root, resolved_root in root_pairs
                if candidate == root or candidate.is_relative_to(root)
            ]
        except (OSError, RuntimeError, ValueError):
            return None
    if not lexical_roots:
        return None
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    # /tmp commonly contains MIMIR_HOME in tests and local deployments. The
    # narrower home carve-out wins so /tmp never accidentally exposes all home.
    if resolved == home:
        if not allow_home_root:
            return None
    elif resolved.is_relative_to(home) and not (
        resolved == state or resolved.is_relative_to(state)
    ):
        return None
    # Bind the call to the most specific root named by the caller. A repo path
    # cannot escape into the broader /tmp allowance through ``..`` or a symlink.
    _selected_lexical_root, selected_root = max(
        lexical_roots, key=lambda pair: len(pair[0].parts)
    )
    if not (resolved == selected_root or resolved.is_relative_to(selected_root)):
        return None
    if is_protected_read_path(resolved) and not (
        allow_home_root and resolved == home
    ):
        return None
    if scan_file and (not resolved.is_file() or file_contains_secret(resolved)):
        return None
    return resolved


def read_target_from_arguments(tool_name: str, arguments: dict[str, Any] | None) -> Path | None:
    """Resolve only a call's root; never enumerate collection descendants."""
    args = arguments if isinstance(arguments, dict) else {}
    if tool_name in {"read_file", "aread"}:
        return resolve_non_admin_read_target(
            args.get("file_path") or args.get("path"), scan_file=True
        )
    if tool_name in {"ls", "als", "glob", "aglob", "grep", "agrep"}:
        return resolve_non_admin_read_target(
            args.get("path"), allow_home_root=True,
        )
    if tool_name == "file_search":
        if str(args.get("scope") or "all").strip().lower() != "state":
            return None
        prefix = args.get("path_prefix")
        if prefix is not None and (not isinstance(prefix, str) or Path(prefix).is_absolute()):
            return None
        parts = Path(prefix or ".").parts
        if parts[:1] == ("state",):
            parts = parts[1:]
        if ".." in parts:
            return None
        home = os.environ.get("MIMIR_HOME", "").strip()
        if not home:
            return None
        return resolve_non_admin_read_target(str(Path(home) / "state" / Path(*parts)))
    if tool_name in {"get_turn", "mimir_get_turn"}:
        if not isinstance(args.get("turn_id"), str) or not args["turn_id"].strip():
            return None
        from .tools.extra import _TURN_STATE

        path = _TURN_STATE.get("turns_log_path")
        return resolve_non_admin_read_target(str(path)) if path is not None else None
    return None
