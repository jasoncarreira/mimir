"""Shared confidentiality policy for non-admin filesystem reads."""

from __future__ import annotations

import os
import re
from functools import lru_cache
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


@lru_cache(maxsize=1)
def _framework_large_tool_results_prefix() -> Path:
    # Deepagents does not export the computed prefix. Read it from the same
    # middleware instance that owns eviction so upstream prefix changes follow.
    from deepagents.middleware.filesystem import FilesystemMiddleware

    prefix = Path(FilesystemMiddleware()._large_tool_results_prefix)
    if not prefix.is_absolute() or ".." in prefix.parts:
        raise RuntimeError("deepagents returned an invalid artifact prefix")
    return prefix


def framework_large_tool_results_root(home: Path | None = None) -> Path | None:
    """Map deepagents' own artifact prefix into Mimir's filesystem backend."""
    if home is None:
        home_raw = os.environ.get("MIMIR_HOME", "").strip()
        if not home_raw:
            return None
        home = Path(home_raw)

    prefix = _framework_large_tool_results_prefix()
    return (home.resolve() / Path(*prefix.parts[1:])).resolve()


def is_large_tool_results_path(path: Path) -> bool:
    """Return whether a path is within the framework-owned artifact prefix."""
    root = framework_large_tool_results_root()
    if root is None:
        return False
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return False
    return resolved == root or resolved.is_relative_to(root)


def resolve_large_tool_results_target(raw_path: str) -> Path | None:
    """Resolve a physical or backend-virtual path within the artifact prefix."""
    root = framework_large_tool_results_root()
    home = _resolved_mimir_home()
    if root is None or home is None:
        return None
    candidate = Path(raw_path)
    candidates = (candidate, home / raw_path.lstrip("/"))
    for possible in candidates:
        try:
            resolved = possible.resolve()
        except (OSError, RuntimeError):
            continue
        if resolved == root or resolved.is_relative_to(root):
            return resolved
    return None


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


def resolved_read_target_from_arguments(
    tool_name: str,
    arguments: dict[str, Any] | None,
) -> str | None:
    """Resolve a read selector for diagnostics without applying read policy."""
    args = arguments if isinstance(arguments, dict) else {}
    home = _resolved_mimir_home()
    if home is None:
        return None

    if tool_name == "file_search":
        prefix = args.get("path_prefix")
        scope = str(args.get("scope") or "all").strip().lower()
        raw_path = prefix if isinstance(prefix, str) and prefix.strip() else scope
        if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path:
            return None
        candidate = Path(raw_path.strip())
        if candidate.is_absolute():
            return str(candidate.resolve(strict=False))
        if scope in {"memory", "state"} and candidate.parts[:1] != (scope,):
            candidate = Path(scope) / candidate
        return str((home / candidate).resolve(strict=False))

    raw_path = requested_read_target_from_arguments(tool_name, args)
    if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path:
        return None
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        return str((home / candidate).resolve(strict=False))

    # Composite filesystem routes preserve configured absolute paths. Other
    # absolute spellings are backend-virtual paths rooted at MIMIR_HOME.
    if candidate == home or candidate.is_relative_to(home):
        return str(candidate.resolve(strict=False))
    for root in configured_non_admin_read_roots():
        if candidate == root or candidate.is_relative_to(root):
            return str(candidate.resolve(strict=False))
    return str((home / raw_path.lstrip("/")).resolve(strict=False))


def emit_hard_read_denial(tool: str, target: Any, reason: str) -> None:
    """Record a protected result that the backend actually withheld."""
    from ._context import get_current_turn
    from .tools.budget_gate import _emit_hard_boundary_denied

    turn_context = get_current_turn()
    _emit_hard_boundary_denied(
        tool=tool,
        boundary="protected_read_policy",
        reason=reason,
        target=target,
        auth_context=getattr(turn_context, "auth_context", None),
        turn_context=turn_context,
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
    service_has_read_scope = bool(
        getattr(authority, "filesystem_read_roots", ())
        or getattr(auth_context, "is_service", False)
    )
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


def is_operator_secret_read_path(path: Path) -> bool:
    """Return whether a path is an exact operator-configured secret file."""
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return True
    return any(resolved == secret for secret in _operator_secret_paths())


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
    if is_operator_secret_read_path(resolved):
        return True

    home = _resolved_mimir_home()
    if home is not None and (resolved == home or resolved.is_relative_to(home)):
        state = home / "state"
        memory = home / "memory"
        docs = home / "docs"
        skills = home / "skills"
        builtin_skills = home / ".mimir_builtin_skills"
        artifact_root = framework_large_tool_results_root(home)
        from .access_control import current_turn_scratch_root

        turn_scratch = current_turn_scratch_root()
        if not (
            resolved == state
            or resolved.is_relative_to(state)
            or resolved == memory
            or resolved.is_relative_to(memory)
            or resolved == docs
            or resolved.is_relative_to(docs)
            or resolved == skills
            or resolved.is_relative_to(skills)
            or resolved == builtin_skills
            or resolved.is_relative_to(builtin_skills)
            or artifact_root is not None
            and (resolved == artifact_root or resolved.is_relative_to(artifact_root))
            or turn_scratch is not None
            and (resolved == turn_scratch or resolved.is_relative_to(turn_scratch))
        ):
            return True
    return False


def _has_protected_read_name(path: Path) -> bool:
    """Return whether a path matches the protected-name portion of policy."""
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return False
    name = resolved.name.lower()
    return (
        name in _PROTECTED_BASENAMES
        or name.startswith(".env.")
        or resolved.suffix.lower() in _PROTECTED_SUFFIXES
        or any(part.lower() in _PROTECTED_DIR_NAMES for part in resolved.parts)
        or is_operator_secret_read_path(resolved)
    )


def is_memory_read_path(path: Path) -> bool:
    """Return whether ``path`` lexically or physically targets this home's memory."""
    home = _resolved_mimir_home()
    if home is None:
        return False
    memory = home / "memory"
    if path.is_absolute() and (path == memory or path.is_relative_to(memory)):
        return True
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return False
    return resolved == memory or resolved.is_relative_to(memory)


def is_memory_read_path_allowed(path: Path, auth_context: Any) -> bool:
    """Allow memory reads except beneath a different session's channel directory."""
    home = _resolved_mimir_home()
    channel_id = getattr(auth_context, "channel_id", None)
    authority = getattr(auth_context, "service_authority", None)
    is_job_bound_tick = (
        getattr(authority, "canonical", None) == "scheduler"
        and getattr(authority, "trigger", None) == "scheduled_tick"
        and bool(getattr(authority, "channel_memory_directory", None))
    )
    if is_job_bound_tick:
        channel_id = authority.channel_memory_directory
    if home is None:
        return False
    memory = home / "memory"
    try:
        resolved = path.resolve(strict=True)
        resolved_memory = memory.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    if not (resolved == resolved_memory or resolved.is_relative_to(resolved_memory)):
        return False

    relatives: list[Path] = [resolved.relative_to(resolved_memory)]
    try:
        if path.is_absolute() and (path == memory or path.is_relative_to(memory)):
            relatives.append(path.relative_to(memory))
    except ValueError:
        return False
    for relative in relatives:
        if is_job_bound_tick:
            # Core blocks are already rendered into the system prompt, so denying
            # an explicit re-read removes no exposure.
            if (
                relative.parts[:1] == ("channels",)
                and relative.parts[:2] != ("channels", channel_id)
            ):
                return False
        if relative.parts[:1] == ("channels",) and len(relative.parts) > 1:
            if relative.parts[1] != channel_id:
                return False
    return True


def protected_read_denial_reason(path: Path) -> str | None:
    """Name the path-policy rule that withholds ``path``, if any."""
    service_scoped = is_current_service_scoped_read_path(path)
    from ._context import get_current_turn

    auth_context = getattr(get_current_turn(), "auth_context", None)
    authority = getattr(auth_context, "service_authority", None)
    memory_scope_denied = (
        is_memory_read_path(path)
        and not is_memory_read_path_allowed(path, auth_context)
    )
    general_protected = not service_scoped and is_protected_read_path(path)
    service_name_protected = is_current_service_protected_read_path(path)
    protected_name = _has_protected_read_name(path)
    if not (
        memory_scope_denied
        or general_protected
        or service_name_protected
        or protected_name
    ):
        return None
    if (
        getattr(auth_context, "is_service", False)
        and getattr(authority, "filesystem_read_roots", ())
        and (memory_scope_denied or not service_scoped)
    ):
        return "service_scoped_read_boundary"
    if service_name_protected or protected_name:
        return "protected_name_match"
    return "mimir_home_read_boundary"


def is_current_service_protected_read_path(path: Path) -> bool:
    """Apply the stricter trigger-service protected names during tool execution."""
    from ._context import get_current_turn

    turn = get_current_turn()
    auth_context = getattr(turn, "auth_context", None)
    authority = getattr(auth_context, "service_authority", None)
    from .access_control import service_filesystem_read_roots

    roots = tuple(str(root) for root in service_filesystem_read_roots(authority))
    home = _resolved_mimir_home()
    if home is not None:
        roots += (str(home / "memory"),)
    if not roots:
        return False
    if is_operator_secret_read_path(path):
        return True
    # Keep authorization and read-boundary enforcement on one security list.
    # Import lazily to avoid a module cycle: access_control imports this module
    # from the authorization path that scans individual file contents.
    from .access_control import _is_service_protected_read_path

    for raw_root in roots:
        root = Path(raw_root)
        try:
            if path == root or path.is_relative_to(root):
                lexical = path.relative_to(root)
                if _is_service_protected_read_path(authority, root, lexical):
                    return True
            resolved_root = root.resolve(strict=True)
            resolved = path.resolve(strict=True)
            if resolved == resolved_root or resolved.is_relative_to(resolved_root):
                relative = resolved.relative_to(resolved_root)
                if _is_service_protected_read_path(authority, resolved_root, relative):
                    return True
        except (OSError, RuntimeError, ValueError):
            # Read roots, including the artifact root, may be created lazily.
            # One absent root must not deny a target inside another valid root.
            continue
    return False


def is_current_service_scoped_read_path(path: Path) -> bool:
    """Return whether the resolved path is inside a frozen service read root."""
    from ._context import get_current_turn

    turn = get_current_turn()
    auth_context = getattr(turn, "auth_context", None)
    if is_memory_read_path_allowed(path, auth_context):
        return True
    authority = getattr(auth_context, "service_authority", None)
    from .access_control import service_filesystem_read_roots

    for raw_root in service_filesystem_read_roots(authority):
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


def is_tracked_file_in_current_pr_lease(path: Path) -> bool:
    """Allow published content only in the service turn's exact active PR lease.

    The shared lease resolver and containment check fix the reported refusal of a
    tracked, secret-shaped test file when the turn carries repository-scoped PR
    state. Git index membership remains the publication proof: untracked or
    ignored files, protected path names, symlinks, and paths outside the exact
    active lease remain subject to the content veto.
    """
    from ._context import get_current_turn
    from .access_control import (
        _target_within_active_pr_checkout_lease,
        resolve_repository_review_state,
    )
    from .models import RepoPRScopeRegistry
    from .repo_tools import GitRefusal, RepoGitTools

    auth_context = getattr(get_current_turn(), "auth_context", None)
    if not getattr(auth_context, "is_service", False):
        return False
    state, refusal = resolve_repository_review_state(auth_context, path=str(path))
    if refusal is not None or state is None:
        return False
    if (
        not isinstance(
            getattr(auth_context, "repo_pr_scope_registry", None), RepoPRScopeRegistry,
        )
        and getattr(auth_context, "repo_pr_action_scope", None)
        is not getattr(state, "action_scope", None)
    ):
        return False
    if not _target_within_active_pr_checkout_lease(str(path), state):
        return False
    try:
        return RepoGitTools(state).is_tracked_file(path)
    except (GitRefusal, OSError, RuntimeError, ValueError):
        return False


def protected_read_result_reason(path: Path, *, text: str | None = None) -> str | None:
    """Name the path or content rule that withholds one resolved result."""
    if is_large_tool_results_path(path):
        return None
    path_reason = protected_read_denial_reason(path)
    if path_reason is not None:
        return path_reason
    contains_protected_content = (
        text_contains_secret(text, path=path)
        if text is not None
        else path.is_file() and file_contains_secret(path)
    )
    if contains_protected_content and not is_tracked_file_in_current_pr_lease(path):
        return "protected_read_result"
    return None


def result_is_protected(path: Path, *, text: str | None = None) -> bool:
    """Check a result, exempting only published files in an authorized PR lease."""
    if is_large_tool_results_path(path):
        return False
    return protected_read_result_reason(path, text=text) is not None


def configured_non_admin_read_roots() -> tuple[Path, ...]:
    """Return narrow home roots, configured source roots, and /tmp."""
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
    artifact_root = framework_large_tool_results_root(home)
    from .access_control import current_turn_scratch_root

    turn_scratch = current_turn_scratch_root()
    roots = [
        home / "state",
        home / "memory",
        home / "docs",
        home / "skills",
        home / ".mimir_builtin_skills",
        *((artifact_root,) if artifact_root is not None else ()),
        *((turn_scratch,) if turn_scratch is not None else ()),
        *configured_paths,
    ]

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
        memory = (home / "memory").resolve(strict=False)
        docs = (home / "docs").resolve(strict=False)
        skills = (home / "skills").resolve(strict=False)
        builtin_skills = (home / ".mimir_builtin_skills").resolve(strict=False)
        artifact_root = framework_large_tool_results_root(home)
        from .access_control import current_turn_scratch_root

        turn_scratch = current_turn_scratch_root()
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
        resolved == state
        or resolved.is_relative_to(state)
        or resolved == memory
        or resolved.is_relative_to(memory)
        or resolved == docs
        or resolved.is_relative_to(docs)
        or resolved == skills
        or resolved.is_relative_to(skills)
        or resolved == builtin_skills
        or resolved.is_relative_to(builtin_skills)
        or artifact_root is not None
        and (resolved == artifact_root or resolved.is_relative_to(artifact_root))
        or turn_scratch is not None
        and (resolved == turn_scratch or resolved.is_relative_to(turn_scratch))
    ):
        return None
    # Bind the call to the most specific root named by the caller. A repo path
    # cannot escape into the broader /tmp allowance through ``..`` or a symlink.
    _selected_lexical_root, selected_root = max(
        lexical_roots, key=lambda pair: len(pair[0].parts)
    )
    if not (resolved == selected_root or resolved.is_relative_to(selected_root)):
        return None
    if (
        is_protected_read_path(resolved)
        and not is_large_tool_results_path(resolved)
        and not (allow_home_root and resolved == home)
    ):
        return None
    from ._context import get_current_turn

    auth_context = getattr(get_current_turn(), "auth_context", None)
    if is_memory_read_path(resolved) and not is_memory_read_path_allowed(
        resolved, auth_context,
    ):
        return None
    if scan_file and (
        not resolved.is_file()
        or not is_large_tool_results_path(resolved) and file_contains_secret(resolved)
    ):
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
