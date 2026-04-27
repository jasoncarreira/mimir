"""Path safety — confine all file-op tool paths to ``<home>`` (SPEC §7.3).

Accepts both relative paths (resolved against ``home``) and absolute paths
(accepted only if they resolve within ``home``). The SDK's CLI subprocess
typically forwards absolute paths to file-op tools; hooks running in mimir
need to recognize these as legitimate without rejecting them.

``..`` segments resolve via ``Path.resolve()`` — symlinks that point outside
home are caught by the same check.
"""

from __future__ import annotations

from pathlib import Path


class PathOutsideHomeError(ValueError):
    """Raised when a tool argument resolves outside ``<home>``."""


def resolve_home_path(home: Path, raw_path: str) -> Path:
    """Resolve ``raw_path`` against ``home``, rejecting any path that escapes.

    Args:
        home: The agent's home root.
        raw_path: A path string from a tool call. May be relative
            (resolved against home) or absolute (must already be inside home).

    Returns:
        An absolute, fully-resolved ``Path`` inside ``home``.

    Raises:
        PathOutsideHomeError: if the resolved path is outside home or empty.
    """
    if not raw_path:
        raise PathOutsideHomeError("path is empty")

    home_resolved = home.resolve()
    candidate = Path(raw_path)
    if candidate.is_absolute():
        full = candidate.resolve()
    else:
        full = (home_resolved / candidate).resolve()

    try:
        full.relative_to(home_resolved)
    except ValueError as exc:
        raise PathOutsideHomeError(
            f"path escapes home: {raw_path!r} → {full}"
        ) from exc

    return full
