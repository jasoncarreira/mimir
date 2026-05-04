"""Path safety — confine all file-op tool paths to a configured set of
roots (SPEC §7.3).

The default root is ``<home>`` — the agent's runtime state directory.
Additional roots can be configured via ``MIMIR_FILE_OP_ROOTS`` (colon-
separated paths) for deployments where the agent needs to operate on
sibling directories — e.g., mimirbot dev-iteration mode where the
agent reads/edits ``/workspace/mimir`` (its own source) and
``/benchmark`` (the bench harness).

Accepts both relative paths (resolved against the *first* root —
typically ``home``) and absolute paths (accepted only if they resolve
within any configured root). The SDK's CLI subprocess typically
forwards absolute paths to file-op tools; hooks running in mimir need
to recognize these as legitimate without rejecting them.

``..`` segments resolve via ``Path.resolve()`` — symlinks that point
outside every root are caught by the same check.
"""

from __future__ import annotations

from pathlib import Path


class PathOutsideHomeError(ValueError):
    """Raised when a tool argument resolves outside every configured
    root. Name kept for back-compat with callers that catch it."""


def resolve_home_path(home: Path, raw_path: str) -> Path:
    """Resolve ``raw_path`` against ``home``, rejecting any path that
    escapes. Single-root convenience — equivalent to
    ``resolve_within_roots([home], raw_path)``."""
    return resolve_within_roots([home], raw_path)


def resolve_within_roots(roots: list[Path], raw_path: str) -> Path:
    """Resolve ``raw_path`` against the configured roots, returning
    the absolute resolved path if it lies inside any of them.

    Args:
        roots: Allowed roots, in priority order. The first root is
            the home / primary root — relative paths resolve against
            it. Subsequent roots are checked for absolute-path
            containment.
        raw_path: A path string from a tool call. Relative paths
            resolve against ``roots[0]``. Absolute paths must already
            be inside one of the roots.

    Returns:
        An absolute, fully-resolved ``Path`` inside one of ``roots``.

    Raises:
        PathOutsideHomeError: if the resolved path is outside every
            root, the path is empty, or the roots list is empty.
    """
    if not raw_path:
        raise PathOutsideHomeError("path is empty")
    if not roots:
        raise PathOutsideHomeError("no roots configured")

    resolved_roots = [r.resolve() for r in roots]
    candidate = Path(raw_path)
    if candidate.is_absolute():
        full = candidate.resolve()
    else:
        full = (resolved_roots[0] / candidate).resolve()

    for root in resolved_roots:
        try:
            full.relative_to(root)
            return full
        except ValueError:
            continue

    roots_str = ", ".join(str(r) for r in resolved_roots)
    raise PathOutsideHomeError(
        f"path escapes configured roots ({roots_str}): {raw_path!r} → {full}"
    )
