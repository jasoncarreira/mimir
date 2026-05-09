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

import os
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


def claude_code_persisted_output_root() -> Path:
    """Return Claude Code's persisted-output parent dir.

    Claude Code's CLI (the bundled ``claude`` binary that the SDK
    spawns) persists any tool result exceeding ~32KB to a file under
    ``~/.claude/projects/<encoded-cwd>/<session-uuid>/tool-results/<id>.txt``
    and embeds that path in the tool result as a "<persisted-output>"
    block, expecting the agent to ``Read`` it for the rest of the
    output. Without this dir included in the file-op roots, the agent
    sees a ``path escapes configured roots`` denial on every overflow
    Read — which silently breaks any workflow involving large bash
    output (build logs, pytest -v, large grep, etc.).

    Returned eagerly (no on-disk presence check) so the root is
    included in file-op roots even when the dir hasn't been created
    yet — Claude Code creates it lazily on first overflow. Without
    eager inclusion, fresh containers (or any environment where the
    dir hasn't yet appeared at ``Agent.__init__`` time) hit the same
    denial this function exists to prevent on the very first
    overflow Read. ``resolve_within_roots`` is a string-prefix check
    on resolved paths, so an absent root is harmless until something
    inside it is referenced — at which point the file's parents do
    exist and the check passes.

    Honors ``CLAUDE_CONFIG_DIR`` if set (the SDK uses this to relocate
    the project state); otherwise falls back to ``~/.claude``.
    """
    base_env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    base = Path(base_env) if base_env else Path.home() / ".claude"
    return base / "projects"
