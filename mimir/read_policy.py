"""Shared confidentiality policy for non-admin filesystem reads."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .secret_scan import contains_secret


READ_RESOURCE_OPERATIONS = frozenset({
    "read_file", "aread", "ls", "als", "glob", "aglob", "grep", "agrep",
    "file_search", "get_turn", "mimir_get_turn",
})

_PROTECTED_BASENAMES = frozenset({
    ".env",
    "compose.env",
    "credentials.json",
    "credentials.yaml",
    "credentials.yml",
    "identities.json",
    "identities.yaml",
    "identities.yml",
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
    return "admin" not in roles and not getattr(auth_context, "is_service", False)


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

    home_raw = os.environ.get("MIMIR_HOME", "").strip()
    if home_raw:
        try:
            home = Path(home_raw).resolve()
        except (OSError, RuntimeError):
            return True
        protected_roots = (home / ".mimir", home / "prompts", home / "memory" / "core")
        if any(resolved == root or resolved.is_relative_to(root) for root in protected_roots):
            return True
    return False


def text_contains_secret(text: str) -> bool:
    return contains_secret(text)


def file_contains_secret(path: Path) -> bool:
    """Scan one file, failing closed when it cannot be inspected."""
    try:
        return contains_secret(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, RuntimeError):
        return True


def result_is_protected(path: Path, *, text: str | None = None) -> bool:
    """Check a result at its read boundary without re-reading supplied text."""
    if is_protected_read_path(path):
        return True
    if text is not None:
        return text_contains_secret(text)
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

    configured = _parse_file_tool_roots(
        os.environ.get("MIMIR_FILE_TOOL_ROOTS", ""), home
    )
    roots = [home / "state", *(Path(path) for path, _mode in configured)]
    tmp = Path("/tmp")
    if tmp.is_dir() and tmp not in roots:
        roots.append(tmp)
    return tuple(roots)


def resolve_non_admin_read_target(raw_path: Any, *, scan_file: bool = False) -> Path | None:
    """Resolve one absolute target within the non-admin read roots."""
    if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path:
        return None
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        return None
    try:
        resolved = candidate.resolve(strict=True)
        roots = tuple(root.resolve(strict=True) for root in configured_non_admin_read_roots())
    except (OSError, RuntimeError):
        return None
    home_raw = os.environ.get("MIMIR_HOME", "").strip()
    try:
        home = Path(home_raw).resolve(strict=True)
        state = (home / "state").resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    # /tmp commonly contains MIMIR_HOME in tests and local deployments. The
    # narrower home carve-out wins so /tmp never accidentally exposes all home.
    if (resolved == home or resolved.is_relative_to(home)) and not (
        resolved == state or resolved.is_relative_to(state)
    ):
        return None
    lexical_roots = [
        root for root in roots
        if candidate == root or candidate.is_relative_to(root)
    ]
    if not lexical_roots:
        return None
    # Bind the call to the most specific root named by the caller. A repo path
    # cannot escape into the broader /tmp allowance through ``..`` or a symlink.
    selected_root = max(lexical_roots, key=lambda root: len(root.parts))
    if not (resolved == selected_root or resolved.is_relative_to(selected_root)):
        return None
    if is_protected_read_path(resolved):
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
        return resolve_non_admin_read_target(args.get("path"))
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
